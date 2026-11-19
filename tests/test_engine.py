"""Unit and integration tests for ScrapingEngine orchestrator."""

from datetime import date, timedelta
import queue
import unittest
from unittest.mock import MagicMock, patch

from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from scraper.engine import ScrapingEngine


class TestScrapingEngine(unittest.TestCase):
    """Test suite for ScrapingEngine execution, progress dispatch, and defensive recovery."""

    def setUp(self) -> None:
        self.today = date.today()
        self.check_in = self.today + timedelta(days=2)
        self.check_out = self.today + timedelta(days=5)

        self.job = ScrapingJob(
            check_in=self.check_in,
            check_out=self.check_out,
            adults=2,
            children=0,
        )

        self.sample_hotels = [
            {
                "id": "hotel_1",
                "name": "Hotel Alpha",
                "booking_url": "https://booking.com/hotel/alpha",
                "direct_url": "https://alpha.com",
                "direct_booking_engine": "hoteladmin",
            },
            {
                "id": "hotel_2",
                "name": "Hotel Beta (Crashing)",
                "booking_url": "https://booking.com/hotel/beta",
                "direct_url": "https://beta.com",
                "direct_booking_engine": "profitroom",
            },
            {
                "id": "hotel_3",
                "name": "Hotel Gamma",
                "booking_url": "https://booking.com/hotel/gamma",
                "direct_url": "https://gamma.com",
                "direct_booking_engine": "hoteladmin",
            },
        ]

    def test_engine_run_with_defensive_recovery_and_queue(self) -> None:
        """Verify that failure in hotel #2 does not stop hotel #3 and progress_queue is populated."""
        progress_q: queue.Queue = queue.Queue()

        # Mock ConfigManager
        mock_config_manager = MagicMock()
        mock_config_manager.get_target_hotels.return_value = self.sample_hotels
        mock_config_manager.get_scraping_rules.return_value = {
            "price_selectors": [".price"],
            "sold_out_selectors": [".sold-out"],
        }

        # Mock BrowserManager and Page
        mock_browser_manager = MagicMock()
        mock_page = MagicMock()
        mock_browser_manager.new_page.return_value = mock_page

        engine = ScrapingEngine(
            job=self.job,
            config_manager=mock_config_manager,
            progress_queue=progress_q,
            browser_manager=mock_browser_manager,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,  # Zero jitter for fast test execution
        )

        # Mock parser returns for each hotel:
        # Hotel Alpha: Booking 200 EUR, Direct 180 EUR
        # Hotel Beta: Exception / Crash
        # Hotel Gamma: Booking 150 EUR, Direct 150 EUR
        def mock_parse_booking(page, rules, hotel_name, **kwargs):
            if "Beta" in hotel_name:
                raise RuntimeError("DOM detached crash during parse")
            return ScrapedResult(hotel_name=hotel_name, booking_price=200.0 if "Alpha" in hotel_name else 150.0)

        def mock_parse_direct(page, rules, hotel_name, direct_url=None, **kwargs):
            return ScrapedResult(hotel_name=hotel_name, direct_price=180.0 if "Alpha" in hotel_name else 150.0)

        with patch.object(engine.booking_parser, "parse_hotel_page", side_effect=mock_parse_booking), \
             patch.object(engine.direct_parser, "parse_direct_hotel", side_effect=mock_parse_direct):

            results = engine.run()

        # 1. Check all 3 hotels were processed despite crash on hotel #2
        self.assertEqual(len(results), 3)

        # Hotel 1 (Alpha): Success
        res1 = results[0]
        self.assertEqual(res1.hotel_name, "Hotel Alpha")
        self.assertEqual(res1.booking_price, 200.0)
        self.assertEqual(res1.direct_price, 180.0)
        self.assertEqual(res1.price_delta, -20.0)
        self.assertEqual(res1.cheaper_channel, "DIRECT")
        self.assertEqual(res1.status, ResultStatus.SUCCESS)

        # Hotel 2 (Beta): Recovered from crash, partial or error recorded
        res2 = results[1]
        self.assertEqual(res2.hotel_name, "Hotel Beta (Crashing)")
        self.assertIn("DOM detached crash", res2.error_message or "")
        # Since Direct succeeded (150.0), it has direct_price
        self.assertEqual(res2.direct_price, 150.0)
        self.assertIsNone(res2.booking_price)

        # Hotel 3 (Gamma): Succeeded after hotel 2 recovered
        res3 = results[2]
        self.assertEqual(res3.hotel_name, "Hotel Gamma")
        self.assertEqual(res3.booking_price, 150.0)
        self.assertEqual(res3.direct_price, 150.0)
        self.assertEqual(res3.price_delta, 0.0)

        # 2. Verify progress_queue messages
        messages = []
        while not progress_q.empty():
            messages.append(progress_q.get_nowait())

        # Should have 3 "running" updates and 1 "completed"
        running_msgs = [m for m in messages if m.get("status") == "running"]
        completed_msgs = [m for m in messages if m.get("status") == "completed"]

        self.assertEqual(len(running_msgs), 3)
        self.assertEqual(len(completed_msgs), 1)

        self.assertEqual(running_msgs[0]["current"], 1)
        self.assertEqual(running_msgs[0]["total"], 3)
        self.assertEqual(running_msgs[0]["percent"], 33.3)

        self.assertEqual(running_msgs[2]["current"], 3)
        self.assertEqual(running_msgs[2]["percent"], 100.0)

        self.assertEqual(completed_msgs[0]["results_count"], 3)

        # 3. Verify browser was cleanly closed in finally block
        mock_browser_manager.close.assert_called_once()

    def test_hotel_identifiers_filtering(self) -> None:
        """Verify engine filters target hotels when hotel_identifiers are specified in job."""
        filtered_job = ScrapingJob(
            check_in=self.check_in,
            check_out=self.check_out,
            hotel_identifiers=["hotel_1"],
        )

        mock_config_manager = MagicMock()
        mock_config_manager.get_target_hotels.return_value = self.sample_hotels
        mock_config_manager.get_scraping_rules.return_value = {}

        mock_browser_manager = MagicMock()

        engine = ScrapingEngine(
            job=filtered_job,
            config_manager=mock_config_manager,
            browser_manager=mock_browser_manager,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        with patch.object(engine, "_scrape_single_hotel") as mock_scrape:
            mock_scrape.return_value = ScrapedResult(hotel_name="Hotel Alpha")
            results = engine.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(mock_scrape.call_count, 1)

    def test_build_price_comparison_records_and_run_preserve_all_per_channel_rows(self) -> None:
        """All Direct and Booking offer variants are saved as separate normalized rows sharing matched_room_key."""
        mock_config_manager = MagicMock()
        mock_config_manager.get_target_hotels.return_value = [self.sample_hotels[0]]
        mock_config_manager.get_scraping_rules.return_value = {}
        mock_config_manager.get_room_mappings.return_value = [
            {
                "hotel_id": "hotel_1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Štandard",
                "booking_room_name": "Double Standard Room",
                "match_score": 95,
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "hotel_1",
                "capacity_group": "family",
                "direct_room_pattern": "Rodinný apartmán",
                "booking_room_name": None,
                "is_exclusive_direct": True,
                "match_score": 0,
                "priority": 2,
                "is_active": True,
            },
        ]
        mock_config_manager.upsert_price_comparisons.return_value = 5

        mock_browser_manager = MagicMock()
        mock_browser_manager.new_page.return_value = MagicMock()

        engine = ScrapingEngine(
            job=self.job,
            config_manager=mock_config_manager,
            browser_manager=mock_browser_manager,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        b_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            booking_price=200.0,
            offers=[
                RoomOffer(
                    room_name="Double Standard Room",
                    price=260.0,
                    meal_plan="Polpenzia",
                    cancellation_policy="Bezplatné storno",
                ),
                RoomOffer(
                    room_name="Double Standard Room",
                    price=200.0,
                    meal_plan="Raňajky",
                    cancellation_policy="Nevratná rezervácia",
                ),
                RoomOffer(
                    room_name="Double Standard Room",
                    price=230.0,
                    meal_plan="Raňajky",
                    cancellation_policy="Bezplatné storno",
                ),
            ],
        )
        d_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            direct_price=215.0,
            direct_offers=[
                RoomOffer(
                    room_name="Izba Štandard",
                    price=215.0,
                    meal_plan="Polpenzia | Wellness v cene",
                    bonuses="Polpenzia | Wellness v cene",
                ),
                RoomOffer(
                    room_name="Rodinný apartmán",
                    price=340.0,
                    meal_plan="Polpenzia | Detský klub",
                    bonuses="Polpenzia | Detský klub",
                ),
            ],
        )

        with patch.object(engine.booking_parser, "parse_hotel_page", return_value=b_res), \
             patch.object(engine.direct_parser, "parse_direct_hotel", return_value=d_res):
            results = engine.run()

        self.assertEqual(len(results), 1)
        mock_config_manager.upsert_price_comparisons.assert_called_once()
        upserted = mock_config_manager.upsert_price_comparisons.call_args[0][0]
        # 2 Direct rows + 3 Booking rows = 5 normalized rows
        self.assertEqual(len(upserted), 5)

        direct_rows = [r for r in upserted if r["channel"] == "Direct"]
        booking_rows = [r for r in upserted if r["channel"] == "Booking"]
        self.assertEqual(len(direct_rows), 2)
        self.assertEqual(len(booking_rows), 3)

        # Paired Direct room shares matched_room_key with Booking rows
        paired_direct = next(r for r in direct_rows if r["room_name"] == "Izba Štandard")
        self.assertEqual(paired_direct["matched_room_key"], "Double Standard Room")
        self.assertEqual(paired_direct["capacity_group"], "2_pax")
        self.assertEqual(paired_direct["price"], 215.0)
        self.assertEqual(paired_direct["bonuses"], "Polpenzia | Wellness v cene")

        # Exclusive Direct room has "Direct Only: <name>" matched_room_key
        exclusive_direct = next(r for r in direct_rows if r["room_name"] == "Rodinný apartmán")
        self.assertEqual(exclusive_direct["matched_room_key"], "Direct Only: Rodinný apartmán")
        self.assertEqual(exclusive_direct["capacity_group"], "family")
        self.assertEqual(exclusive_direct["price"], 340.0)
        self.assertEqual(exclusive_direct["bonuses"], "Polpenzia | Detský klub")

        # All 3 Booking rows share matched_room_key == room_name == "Double Standard Room"
        self.assertEqual(sorted(r["price"] for r in booking_rows), [200.0, 230.0, 260.0])
        for b_row in booking_rows:
            self.assertEqual(b_row["matched_room_key"], "Double Standard Room")
            self.assertEqual(b_row["room_name"], "Double Standard Room")
            self.assertEqual(b_row["capacity_group"], "2_pax")

        built_directly = engine.build_price_comparison_records(results)
        self.assertEqual(len(built_directly), 5)

    def test_direct_not_available_calendar_date_persists_placeholder_row(self) -> None:
        """When DirectParser returns NOT_AVAILABLE (empty direct_offers, price=None), engine writes a Direct placeholder row."""
        mock_config_manager = MagicMock()
        mock_config_manager.get_target_hotels.return_value = [self.sample_hotels[0]]
        mock_config_manager.get_scraping_rules.return_value = {}
        mock_config_manager.get_room_mappings.return_value = []
        mock_config_manager.upsert_price_comparisons.return_value = 2

        mock_browser_manager = MagicMock()
        mock_browser_manager.new_page.return_value = MagicMock()

        engine = ScrapingEngine(
            job=self.job,
            config_manager=mock_config_manager,
            browser_manager=mock_browser_manager,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        b_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            booking_price=220.0,
            offers=[
                RoomOffer(
                    room_name="Double Standard Room",
                    price=220.0,
                    meal_plan="Raňajky",
                )
            ],
        )
        d_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            direct_price=None,
            direct_offers=[],
            is_available=False,
            status=ResultStatus.NOT_AVAILABLE,
            error_message="Termín nie je dostupný v rezervačnom kalendári / vypredané",
        )

        with patch.object(engine.booking_parser, "parse_hotel_page", return_value=b_res), \
             patch.object(engine.direct_parser, "parse_direct_hotel", return_value=d_res):
            results = engine.run()

        self.assertEqual(len(results), 1)
        mock_config_manager.upsert_price_comparisons.assert_called_once()
        upserted = mock_config_manager.upsert_price_comparisons.call_args[0][0]
        self.assertEqual(len(upserted), 2)

        direct_rows = [r for r in upserted if r["channel"] == "Direct"]
        booking_rows = [r for r in upserted if r["channel"] == "Booking"]
        self.assertEqual(len(direct_rows), 1)
        self.assertEqual(len(booking_rows), 1)

        direct_row = direct_rows[0]
        self.assertEqual(direct_row["channel"], "Direct")
        self.assertEqual(direct_row["availability_status"], "NOT_AVAILABLE")
        self.assertEqual(direct_row["room_name"], "Žiadne voľné ubytovanie")
        self.assertIsNone(direct_row["price"])
        self.assertEqual(
            direct_row["bonuses"],
            "Termín nie je dostupný v rezervačnom kalendári / vypredané",
        )

        booking_row = booking_rows[0]
        self.assertEqual(booking_row["availability_status"], "AVAILABLE")
        self.assertEqual(booking_row["price"], 220.0)

    def test_both_channels_not_available_persist_placeholder_rows_with_defaults(self) -> None:
        """When both Booking and Direct have URLs and return no offers, both get NOT_AVAILABLE rows."""
        mock_config_manager = MagicMock()
        mock_config_manager.get_target_hotels.return_value = [self.sample_hotels[0]]
        mock_config_manager.get_scraping_rules.return_value = {}
        mock_config_manager.get_room_mappings.return_value = []
        mock_config_manager.upsert_price_comparisons.return_value = 2

        mock_browser_manager = MagicMock()
        mock_browser_manager.new_page.return_value = MagicMock()

        engine = ScrapingEngine(
            job=self.job,
            config_manager=mock_config_manager,
            browser_manager=mock_browser_manager,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        b_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            booking_price=None,
            offers=[],
            is_available=False,
            status=ResultStatus.NOT_AVAILABLE,
            error_message=None,
        )
        d_res = ScrapedResult(
            hotel_name="Hotel Alpha",
            direct_price=None,
            direct_offers=[],
            is_available=False,
            status=ResultStatus.NOT_AVAILABLE,
            error_message=None,
        )

        with patch.object(engine.booking_parser, "parse_hotel_page", return_value=b_res), \
             patch.object(engine.direct_parser, "parse_direct_hotel", return_value=d_res):
            engine.run()

        mock_config_manager.upsert_price_comparisons.assert_called_once()
        upserted = mock_config_manager.upsert_price_comparisons.call_args[0][0]
        self.assertEqual(len(upserted), 2)

        by_channel = {r["channel"]: r for r in upserted}
        self.assertEqual(by_channel["Booking"]["availability_status"], "NOT_AVAILABLE")
        self.assertEqual(by_channel["Booking"]["room_name"], "Žiadne voľné ubytovanie")
        self.assertIsNone(by_channel["Booking"]["price"])
        self.assertEqual(
            by_channel["Booking"]["bonuses"],
            "Vypredané / nedostupné na zadaný termín",
        )

        self.assertEqual(by_channel["Direct"]["availability_status"], "NOT_AVAILABLE")
        self.assertEqual(by_channel["Direct"]["room_name"], "Žiadne voľné ubytovanie")
        self.assertIsNone(by_channel["Direct"]["price"])
        self.assertEqual(
            by_channel["Direct"]["bonuses"],
            "Termín nie je dostupný v rezervačnom kalendári / vypredané",
        )


if __name__ == "__main__":
    unittest.main()
