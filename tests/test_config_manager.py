"""Unit tests for ConfigManager remote retrieval and local caching."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from core.config_manager import ConfigError, ConfigManager


class TestConfigManager(unittest.TestCase):
    """Test suite for ConfigManager rules and target hotel loading."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.cache_dir = Path(self.test_dir) / ".cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.fake_url = "https://fake-project.supabase.co"
        self.fake_key = "fake-api-key"

        self.sample_booking_rules = {
            "price_selector": ".bui-price-display__value",
            "room_selector": ".hprt-roomtype-link",
            "regex_currency": r"([A-Z]{3}|€|\$)",
            "timeout_ms": 15000,
        }

        self.sample_hotels = [
            {
                "id": "hotel_grand",
                "name": "Grand Hotel",
                "booking_url": "https://booking.com/hotel/grand",
                "direct_url": "https://grandhotel.com/book",
                "direct_booking_engine": "hoteladmin",
            },
            {
                "id": "hotel_boutique",
                "name": "Boutique Palace",
                "booking_url": "https://booking.com/hotel/boutique",
                "direct_url": "https://boutiquepalace.com",
                "direct_booking_engine": "profitroom",
            },
        ]

    def tearDown(self) -> None:
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scraping Rules Tests
    # -------------------------------------------------------------------------

    @patch("requests.get")
    def test_get_scraping_rules_from_remote_and_cache(self, mock_get: MagicMock) -> None:
        """Remote retrieval should return rules and update local config_cache.json."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"engine": "booking", "rules": self.sample_booking_rules}
        ]
        mock_get.return_value = mock_response

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        rules = manager.get_scraping_rules("booking")
        self.assertEqual(rules, self.sample_booking_rules)

        # Verify endpoint and parameter
        mock_get.assert_called_once()
        endpoint = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs_scraping_configs")
        self.assertEqual(params.get("engine"), "eq.booking")

        # Check local cache was persisted
        cache_file = self.cache_dir / "config_cache.json"
        self.assertTrue(cache_file.exists())
        with open(cache_file, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        self.assertEqual(saved_data.get("booking"), self.sample_booking_rules)

    def test_get_scraping_rules_from_supabase_client(self) -> None:
        """Client object query should target hs_scraping_configs filtering on engine."""
        mock_client = MagicMock()
        mock_query = mock_client.table.return_value.select.return_value.eq.return_value
        mock_query.execute.return_value.data = [
            {"engine": "booking", "rules": self.sample_booking_rules}
        ]

        manager = ConfigManager(
            supabase_client=mock_client,
            cache_dir=self.cache_dir,
        )

        rules = manager.get_scraping_rules("booking")
        self.assertEqual(rules, self.sample_booking_rules)
        mock_client.table.assert_called_with("hs_scraping_configs")
        mock_client.table.return_value.select.assert_called_with("*")
        mock_client.table.return_value.select.return_value.eq.assert_called_with("engine", "booking")

    @patch("requests.get", side_effect=requests.exceptions.ConnectionError("Offline"))
    def test_get_scraping_rules_fallback_to_cache(self, mock_get: MagicMock) -> None:
        """When remote fails, rules should be loaded from existing local cache."""
        cache_file = self.cache_dir / "config_cache.json"
        cached_data = {"booking": self.sample_booking_rules}
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cached_data, f)

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        rules = manager.get_scraping_rules("booking")
        self.assertEqual(rules, self.sample_booking_rules)

    @patch("requests.get", side_effect=requests.exceptions.Timeout("Timeout"))
    def test_get_scraping_rules_raises_config_error_when_no_cache(
        self, mock_get: MagicMock
    ) -> None:
        """When remote fails and no local cache exists, ConfigError must be raised."""
        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        with self.assertRaises(ConfigError) as ctx:
            manager.get_scraping_rules("booking")
        self.assertIn("no valid local cache exists", str(ctx.exception))

    def test_invalid_engine_name_raises_value_error(self) -> None:
        """Empty or non-string engine_name must raise ValueError."""
        manager = ConfigManager(cache_dir=self.cache_dir)
        with self.assertRaises(ValueError):
            manager.get_scraping_rules("")

        with self.assertRaises(ValueError):
            manager.get_scraping_rules(None)  # type: ignore[arg-type]

    # -------------------------------------------------------------------------
    # Target Hotels Tests
    # -------------------------------------------------------------------------

    @patch("requests.get")
    def test_get_target_hotels_from_remote_and_cache(self, mock_get: MagicMock) -> None:
        """Remote retrieval should return hotels and update hotels_cache.json."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self.sample_hotels
        mock_get.return_value = mock_response

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        hotels = manager.get_target_hotels()
        self.assertEqual(len(hotels), 2)
        self.assertEqual(hotels[0]["name"], "Grand Hotel")
        self.assertIsNone(hotels[0]["rooms_catalog_url"])
        self.assertIsNone(hotels[0]["booking_rooms_url"])

        # Verify endpoint and filter
        mock_get.assert_called_once()
        endpoint = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs_target_hotels")
        self.assertEqual(params.get("is_active"), "eq.true")
        self.assertIn("is_active", params.get("select", ""))
        self.assertIn("booking_rooms_url", params.get("select", ""))
        self.assertIn("rooms_catalog_url", params.get("select", ""))

        # Verify cache file
        cache_file = self.cache_dir / "hotels_cache.json"
        self.assertTrue(cache_file.exists())
        with open(cache_file, "r", encoding="utf-8") as f:
            saved_hotels = json.load(f)
        self.assertEqual(saved_hotels, self.sample_hotels)

    def test_get_target_hotels_preserves_booking_rooms_url(self) -> None:
        """Remote hotel payload with booking_rooms_url should preserve it without overwriting."""
        payload = [
            {
                "id": "hotel_rooms",
                "name": "Rooms Hotel",
                "booking_url": "https://booking.com/hotel/rooms",
                "booking_rooms_url": "https://booking.com/hotel/rooms-catalog",
                "direct_url": "https://roomshotel.sk/book",
                "rooms_catalog_url": "https://roomshotel.sk/izby",
                "direct_booking_engine": "generic",
                "is_active": True,
            }
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = payload

        with patch("requests.get", return_value=mock_response):
            manager = ConfigManager(
                supabase_url=self.fake_url,
                supabase_key=self.fake_key,
                cache_dir=self.cache_dir,
            )
            hotels = manager.get_target_hotels()

        self.assertEqual(len(hotels), 1)
        self.assertEqual(
            hotels[0]["booking_rooms_url"], "https://booking.com/hotel/rooms-catalog"
        )
        self.assertEqual(hotels[0]["rooms_catalog_url"], "https://roomshotel.sk/izby")

    def test_get_target_hotels_from_supabase_client(self) -> None:
        """Client object query should target hs_target_hotels with is_active True."""
        mock_client = MagicMock()
        mock_query = mock_client.table.return_value.select.return_value.eq.return_value
        mock_query.execute.return_value.data = self.sample_hotels

        manager = ConfigManager(
            supabase_client=mock_client,
            cache_dir=self.cache_dir,
        )

        hotels = manager.get_target_hotels()
        self.assertEqual(len(hotels), 2)
        mock_client.table.assert_called_with("hs_target_hotels")
        mock_client.table.return_value.select.assert_called_with(
            "id, name, booking_url, direct_url, direct_booking_engine, is_active"
        )
        mock_client.table.return_value.select.return_value.eq.assert_called_with("is_active", True)

    @patch("requests.get", side_effect=requests.exceptions.ConnectionError("Offline"))
    def test_get_target_hotels_fallback_to_cache(self, mock_get: MagicMock) -> None:
        """When remote fails, target hotels should load from existing local cache."""
        cache_file = self.cache_dir / "hotels_cache.json"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(self.sample_hotels, f)

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        hotels = manager.get_target_hotels()
        self.assertEqual(len(hotels), 2)
        self.assertEqual(hotels[1]["id"], "hotel_boutique")

    @patch("requests.get", side_effect=requests.exceptions.RequestException("Error"))
    def test_get_target_hotels_raises_config_error_when_no_cache(
        self, mock_get: MagicMock
    ) -> None:
        """When remote fails and no hotels cache exists, ConfigError must be raised."""
        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        with self.assertRaises(ConfigError) as ctx:
            manager.get_target_hotels()
        self.assertIn("no valid local cache exists", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Room Mappings Tests (hs-room_mappings)
    # -------------------------------------------------------------------------

    @patch("requests.get")
    def test_get_room_mappings_from_remote_with_filter_and_cache(
        self, mock_get: MagicMock
    ) -> None:
        """Remote retrieval should query hs-room_mappings, sort by priority, and cache."""
        sample_mappings = [
            {
                "hotel_id": "hotel_grand",
                "capacity_group": "suite",
                "direct_room_pattern": "Apartmán Deluxe",
                "booking_room_name": "Deluxe Suite",
                "priority": 2,
                "is_active": True,
            },
            {
                "hotel_id": "hotel_grand",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Standard",
                "booking_room_name": "Double Standard Room",
                "priority": 1,
                "is_active": True,
            },
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_mappings
        mock_get.return_value = mock_response

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        mappings = manager.get_room_mappings(hotel_id="hotel_grand")
        self.assertEqual(len(mappings), 2)
        self.assertEqual(mappings[0]["priority"], 1)
        self.assertEqual(mappings[0]["direct_room_pattern"], "Izba Standard")

        endpoint = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs-room_mappings")
        self.assertEqual(params.get("is_active"), "eq.true")
        self.assertEqual(params.get("hotel_id"), "eq.hotel_grand")
        self.assertEqual(params.get("order"), "priority.asc")

        cache_file = self.cache_dir / "room_mappings_cache.json"
        self.assertTrue(cache_file.exists())

    def test_get_room_mappings_from_supabase_client(self) -> None:
        """Supabase client query should target hs-room_mappings filtered by is_active and hotel_id."""
        sample = [
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Double",
                "booking_room_name": "Double Room",
                "priority": 1,
                "is_active": True,
            }
        ]
        mock_client = MagicMock()
        (
            mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value.data
        ) = sample

        manager = ConfigManager(supabase_client=mock_client, cache_dir=self.cache_dir)
        mappings = manager.get_room_mappings(hotel_id="h1")

        self.assertEqual(len(mappings), 1)
        mock_client.table.assert_called_with("hs-room_mappings")

    @patch("requests.get", side_effect=requests.exceptions.ConnectionError("Offline"))
    def test_get_room_mappings_fallback_to_cache(self, mock_get: MagicMock) -> None:
        """Offline fallback should filter cached mappings by hotel_id and sort by priority."""
        cached_mappings = [
            {
                "hotel_id": "h1",
                "capacity_group": "suite",
                "direct_room_pattern": "Suite",
                "priority": 5,
                "is_active": True,
            },
            {
                "hotel_id": "h2",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Standard",
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Standard",
                "priority": 1,
                "is_active": True,
            },
        ]
        cache_file = self.cache_dir / "room_mappings_cache.json"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cached_mappings, f)

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )
        mappings = manager.get_room_mappings(hotel_id="h1")
        self.assertEqual(len(mappings), 2)
        self.assertEqual(mappings[0]["direct_room_pattern"], "Standard")
        self.assertEqual(mappings[1]["direct_room_pattern"], "Suite")

    @patch("requests.post")
    @patch("requests.get")
    def test_upsert_room_mappings_protects_manual_override(
        self, mock_get: MagicMock, mock_post: MagicMock
    ) -> None:
        """Upsert must preserve booking_room_name and capacity_group when is_manual_override=True."""
        existing_db = [
            {
                "hotel_id": "h1",
                "capacity_group": "suite",
                "direct_room_pattern": "Izba Komfort",
                "booking_room_name": "Manual Executive Suite",
                "is_exclusive_direct": False,
                "is_manual_override": True,
                "match_score": 100,
                "priority": 1,
                "is_active": True,
            }
        ]
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = existing_db
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_post.return_value = mock_post_resp

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        new_mappings = [
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Komfort",
                "booking_room_name": "Auto Double Room",
                "is_exclusive_direct": False,
                "is_manual_override": False,
                "match_score": 78,
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "h1",
                "capacity_group": "family",
                "direct_room_pattern": "Rodinný apartmán",
                "booking_room_name": None,
                "is_exclusive_direct": True,
                "is_manual_override": False,
                "match_score": 0,
                "priority": 2,
                "is_active": True,
            },
        ]

        ok = manager.upsert_room_mappings(new_mappings)
        self.assertTrue(ok)
        upsert_calls = [
            c for c in mock_post.call_args_list if "hs-room_mappings" in str(c[0][0])
        ]
        self.assertEqual(len(upsert_calls), 1)

        endpoint = upsert_calls[0][0][0]
        kwargs = upsert_calls[0][1]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs-room_mappings")
        self.assertEqual(
            kwargs["params"], {"on_conflict": "hotel_id,capacity_group,direct_room_pattern"}
        )
        self.assertEqual(
            kwargs["headers"]["Prefer"],
            "resolution=merge-duplicates,return=representation",
        )

        sent_payload = kwargs["json"]
        self.assertEqual(len(sent_payload), 2)

        # Manual override record must retain manual booking_room_name & capacity_group
        overridden = sent_payload[0]
        self.assertEqual(overridden["booking_room_name"], "Manual Executive Suite")
        self.assertEqual(overridden["capacity_group"], "suite")
        self.assertTrue(overridden["is_manual_override"])
        self.assertIn("last_synced_at", overridden)

        # Non-overridden record goes through normally
        normal = sent_payload[1]
        self.assertEqual(normal["capacity_group"], "family")
        self.assertIsNone(normal["booking_room_name"])

        # Cache file updated
        cache_file = self.cache_dir / "room_mappings_cache.json"
        self.assertTrue(cache_file.exists())

    # -------------------------------------------------------------------------
    # Price Comparisons Tests (hs-price_comparisons)
    # -------------------------------------------------------------------------

    @patch("requests.post")
    def test_upsert_price_comparisons_posts_with_on_conflict_and_normalized_rows(
        self, mock_post: MagicMock
    ) -> None:
        """Batch upsert must POST to hs-price_comparisons with per-channel on_conflict key."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_post_resp.json.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
        mock_post.return_value = mock_post_resp

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        records = [
            {
                "scraped_date": "2026-11-17",
                "hotel_id": "h1",
                "check_in": "2026-12-01",
                "check_out": "2026-12-03",
                "adults": 2,
                "children": 0,
                "children_ages": None,
                "channel": "Booking",
                "capacity_group": "2_pax",
                "matched_room_key": "Double Room",
                "room_name": "Double Room",
                "price": 220.0,
                "bonuses": "Raňajky",
            },
            {
                "scraped_date": "2026-11-17",
                "hotel_id": "h1",
                "check_in": "2026-12-01",
                "check_out": "2026-12-03",
                "adults": 2,
                "children": 0,
                "children_ages": None,
                "channel": "Direct",
                "capacity_group": "2_pax",
                "matched_room_key": "Double Room",
                "room_name": "Izba Štandard",
                "price": 205.0,
                "bonuses": "Polpenzia",
            },
            {
                "scraped_date": "2026-11-17",
                "hotel_id": "h1",
                "check_in": "2026-12-01",
                "check_out": "2026-12-03",
                "adults": 2,
                "children": 1,
                "children_ages": [6],
                "channel": "Direct",
                "capacity_group": "family",
                "matched_room_key": "Direct Only: Rodinná izba",
                "room_name": "Rodinná izba",
                "price": 320.0,
                "bonuses": None,
            },
        ]

        count = manager.upsert_price_comparisons(records)
        self.assertEqual(count, 3)
        mock_post.assert_called_once()

        endpoint = mock_post.call_args[0][0]
        kwargs = mock_post.call_args[1]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs-price_comparisons")
        self.assertEqual(
            kwargs["params"]["on_conflict"],
            "scraped_date,hotel_id,check_in,check_out,adults,children,children_ages,channel,room_name,price",
        )
        self.assertEqual(
            kwargs["headers"]["Prefer"],
            "resolution=merge-duplicates,return=representation",
        )

        sent = kwargs["json"]
        self.assertEqual(sent[0]["channel"], "Booking")
        self.assertEqual(sent[0]["room_name"], "Double Room")
        self.assertEqual(sent[0]["matched_room_key"], "Double Room")
        self.assertEqual(sent[0]["price"], 220.0)
        self.assertEqual(sent[0]["children_ages"], "")

        self.assertEqual(sent[1]["channel"], "Direct")
        self.assertEqual(sent[1]["room_name"], "Izba Štandard")
        self.assertEqual(sent[1]["matched_room_key"], "Double Room")
        self.assertEqual(sent[1]["price"], 205.0)

        self.assertEqual(sent[2]["channel"], "Direct")
        self.assertEqual(sent[2]["room_name"], "Rodinná izba")
        self.assertEqual(sent[2]["children_ages"], "6")

    def test_upsert_price_comparisons_via_supabase_client(
        self,
    ) -> None:
        """Supabase client upsert uses normalized per-channel on_conflict key."""
        mock_client = MagicMock()
        mock_client.table.return_value.upsert.return_value.execute.return_value.data = [
            {"id": 1}
        ]
        manager = ConfigManager(
            supabase_client=mock_client,
            cache_dir=self.cache_dir,
        )
        records = [
            {
                "scraped_date": "2026-11-18",
                "hotel_id": "h1",
                "check_in": "2026-12-01",
                "check_out": "2026-12-03",
                "adults": 2,
                "children": 0,
                "children_ages": "",
                "channel": "Direct",
                "capacity_group": "2_pax",
                "matched_room_key": "Double Room",
                "room_name": "Studio Komfort",
                "price": 234.0,
                "bonuses": "Raňajky",
            }
        ]
        count = manager.upsert_price_comparisons(records)
        self.assertEqual(count, 1)
        upsert_kwargs = mock_client.table.return_value.upsert.call_args[1]
        self.assertEqual(
            upsert_kwargs["on_conflict"],
            "scraped_date,hotel_id,check_in,check_out,adults,children,children_ages,channel,room_name,price",
        )

    @patch("requests.get")
    def test_fetch_all_price_comparisons_queries_ordered_rows(
        self, mock_get: MagicMock
    ) -> None:
        """fetch_all_price_comparisons must GET all columns ordered by scraped_date DESC, hotel_name ASC, ..."""
        expected_rows = [
            {
                "hotel_name": "Hotel A",
                "channel": "Booking",
                "capacity_group": "2_pax",
                "matched_room_key": "Double",
                "room_name": "Double",
                "price": 210.0,
            },
            {
                "hotel_name": "Hotel A",
                "channel": "Direct",
                "capacity_group": "2_pax",
                "matched_room_key": "Double",
                "room_name": "Izba Double",
                "price": 190.0,
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = expected_rows
        mock_get.return_value = mock_resp

        manager = ConfigManager(
            supabase_url=self.fake_url,
            supabase_key=self.fake_key,
            cache_dir=self.cache_dir,
        )

        rows = manager.fetch_all_price_comparisons()
        self.assertEqual(rows, expected_rows)
        mock_get.assert_called_once()

        endpoint = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertEqual(endpoint, f"{self.fake_url}/rest/v1/hs-price_comparisons")
        self.assertEqual(params["select"], "*")
        self.assertEqual(
            params["order"],
            "scraped_date.desc,hotel_name.asc,check_in.asc,capacity_group.asc,matched_room_key.asc,channel.asc,price.asc",
        )


if __name__ == "__main__":
    unittest.main()

