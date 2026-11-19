"""Unit and integration tests for room catalog sync, RoomMatcher, and deterministic mapping."""

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from core.room_matcher import RoomMatcher
from main import main, parse_arguments
from scraper.booking_parser import BookingParser
from scraper.browser import BrowserManager
from scraper.direct_parser import DirectParser
from scraper.engine import ScrapingEngine


class TestRoomMatcherLogic(unittest.TestCase):
    """Unit tests for RoomMatcher classification, catalog matching, and runtime application."""

    def setUp(self) -> None:
        self.matcher = RoomMatcher()
        self.hotel = {
            "id": "hotel_permon",
            "name": "Grand Hotel Permon",
            "matching_rules": {
                "min_fuzzy_score": 60,
                "synonyms": {"manzelska": "dvojlozkova"},
                "stop_words": ["izba", "hotel"],
            },
        }

    def test_capacity_group_classification_and_priority(self) -> None:
        """Verify priority order: family > suite > 2_plus_1 > 1_pax > 2_pax."""
        self.assertEqual(
            self.matcher.classify_capacity_group("Rodinný apartmán 2+2"),
            "family",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("Apartmán Deluxe"),
            "suite",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("Dvojlôžková izba s prístelkou"),
            "2_plus_1",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("Jednolôžková izba Štandard"),
            "1_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group(
                "Izba Štandard", bed_info="1 veľká manželská posteľ"
            ),
            "2_pax",
        )
        # Ensure 'booking' does not falsely trigger 'king'
        self.assertEqual(
            self.matcher.classify_capacity_group("Jednolôžková izba Booking Promo"),
            "1_pax",
        )

    def test_build_catalog_mappings_respects_capacity_boundaries_and_family_exclusivity(
        self,
    ) -> None:
        """Rooms only match within identical capacity_group; family is exclusive direct."""
        booking_offers = [
            RoomOffer(
                room_name="Jednolôžková izba Standard",
                price=0.0,
                bed_info="1 jednolôžková posteľ",
            ),
            RoomOffer(
                room_name="Dvojlôžková izba Standard",
                price=0.0,
                bed_info="1 manželská posteľ",
            ),
            RoomOffer(
                room_name="Apartmán Prezidentský",
                price=0.0,
                bed_info="1 extra veľká manželská posteľ",
            ),
        ]
        direct_offers = [
            RoomOffer(room_name="Jednolôžková izba Štandard", price=0.0),
            RoomOffer(room_name="Dvojlôžková izba Štandard s výhľadom", price=0.0),
            RoomOffer(room_name="Rodinná izba 2+2", price=0.0),
            RoomOffer(room_name="Prezidentský apartmán", price=0.0),
        ]

        mappings = self.matcher.build_catalog_mappings(
            hotel=self.hotel,
            booking_offers=booking_offers,
            direct_offers=direct_offers,
            global_rules={"min_fuzzy_score": 65},
            existing_mappings=[],
        )

        self.assertEqual(len(mappings), 4)
        by_pattern = {m["direct_room_pattern"]: m for m in mappings}

        # 1_pax must match 1_pax (not 2_pax)
        m_1pax = by_pattern["Jednolôžková izba Štandard"]
        self.assertEqual(m_1pax["capacity_group"], "1_pax")
        self.assertEqual(m_1pax["booking_room_name"], "Jednolôžková izba Standard")
        self.assertFalse(m_1pax["is_exclusive_direct"])
        self.assertGreaterEqual(m_1pax["match_score"], 60)

        # 2_pax matches 2_pax
        m_2pax = by_pattern["Dvojlôžková izba Štandard s výhľadom"]
        self.assertEqual(m_2pax["capacity_group"], "2_pax")
        self.assertEqual(m_2pax["booking_room_name"], "Dvojlôžková izba Standard")
        self.assertFalse(m_2pax["is_exclusive_direct"])

        # family must stay exclusive direct
        m_family = by_pattern["Rodinná izba 2+2"]
        self.assertEqual(m_family["capacity_group"], "family")
        self.assertIsNone(m_family["booking_room_name"])
        self.assertTrue(m_family["is_exclusive_direct"])
        self.assertEqual(m_family["match_score"], 0)

        # suite matches suite
        m_suite = by_pattern["Prezidentský apartmán"]
        self.assertEqual(m_suite["capacity_group"], "suite")
        self.assertEqual(m_suite["booking_room_name"], "Apartmán Prezidentský")
        self.assertFalse(m_suite["is_exclusive_direct"])

    def test_permon_capacity_group_classification(self) -> None:
        """Verify Permon capacity rules: PLUS->2_plus_1, Suita->suite, bed_info->1_pax, family."""
        self.assertEqual(
            self.matcher.classify_capacity_group("2-lôžková Deluxe Kriváň PLUS"),
            "2_plus_1",
        )
        self.assertNotEqual(
            self.matcher.classify_capacity_group("2-lôžková Deluxe Kriváň PLUS"),
            "2_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("2-lôžková Premium PLUS"),
            "2_plus_1",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("2-lôžková Deluxe Premium PLUS"),
            "2_plus_1",
        )

        # Suita Premium Deluxe must ALWAYS be suite, never 2_pax or 2_plus_1
        self.assertEqual(
            self.matcher.classify_capacity_group("Suita Premium Deluxe"),
            "suite",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group(
                "Suita Premium Deluxe",
                bed_info="1 veľká manželská posteľ + 1 rozkladacia pohovka",
            ),
            "suite",
        )
        self.assertNotEqual(
            self.matcher.classify_capacity_group("Suita Premium Deluxe"),
            "2_pax",
        )

        # Family always wins over PLUS / Suite
        self.assertEqual(
            self.matcher.classify_capacity_group("2-lôžková Deluxe Family PLUS"),
            "family",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("Suite Deluxe Family"),
            "family",
        )

        # 1_pax via direct name or Booking bed_info (takes priority over sofa/2_plus_1)
        self.assertEqual(
            self.matcher.classify_capacity_group("1-lôžková Premium/Premium Grande"),
            "1_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group(
                "Izba Economy", bed_info="1 jednolôžková posteľ"
            ),
            "1_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group(
                "Izba Economy", bed_info="1 jednolôžková posteľ + 1 rozkladacia pohovka"
            ),
            "1_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group(
                "Izba s 2 oddelenými posteľami", bed_info="2 jednolôžkové postele"
            ),
            "2_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("2-lôžková Premium Mini"),
            "2_pax",
        )
        self.assertEqual(
            self.matcher.classify_capacity_group("Malá izba s dvojlôžkom"),
            "2_pax",
        )

    def test_fuzzy_score_58_68_and_n_to_1_fallback_within_group(self) -> None:
        """Verify lowered min_fuzzy_score=55, attribute normalization, and N:1 fallback."""
        hotel = {
            "id": "hotel_permon",
            "name": "Grand Hotel Permon",
            "matching_rules": {},
        }
        # Legacy global min_fuzzy_score=70 falls back to default 55
        merged = self.matcher.merge_rules(global_rules={"min_fuzzy_score": 70}, hotel_rules={})
        self.assertEqual(merged["min_fuzzy_score"], 55)

        booking_offers = [
            RoomOffer(
                room_name="Suita Deluxe Kriváň",
                price=0.0,
                bed_info="1 manželská posteľ",
            ),
            RoomOffer(
                room_name="Prezidentská suita",
                price=0.0,
                bed_info="1 extra veľká manželská posteľ",
            ),
            RoomOffer(
                room_name="Izba Premium s balkónom",
                price=0.0,
                bed_info="1 manželská posteľ + 1 rozkladacia pohovka",
            ),
        ]
        direct_offers = [
            RoomOffer(room_name="Suite Deluxe Kriváň", price=0.0),
            RoomOffer(room_name="Suite Prestige", price=0.0),
            RoomOffer(room_name="Suite Unikátna Kategória", price=0.0),
            RoomOffer(room_name="2-lôžková Premium PLUS", price=0.0),
            RoomOffer(room_name="2-lôžková Špeciálna Ponuka PLUS", price=0.0),
            RoomOffer(room_name="2-lôžková Deluxe Family PLUS", price=0.0),
        ]

        mappings = self.matcher.build_catalog_mappings(
            hotel=hotel,
            booking_offers=booking_offers,
            direct_offers=direct_offers,
            global_rules={"min_fuzzy_score": 70},
            existing_mappings=[],
        )
        by_pat = {m["direct_room_pattern"]: m for m in mappings}

        # Normalized attributes give high score for Suite Deluxe Kriváň vs Suita Deluxe Kriváň
        m_deluxe_krivan = by_pat["Suite Deluxe Kriváň"]
        self.assertEqual(m_deluxe_krivan["capacity_group"], "suite")
        self.assertEqual(m_deluxe_krivan["booking_room_name"], "Suita Deluxe Kriváň")
        self.assertEqual(m_deluxe_krivan["booking_bed_info"], "1 manželská posteľ")
        self.assertGreaterEqual(m_deluxe_krivan["match_score"], 90)

        # Score in 58-68 range passes threshold (>= 55)
        m_prestige = by_pat["Suite Prestige"]
        self.assertEqual(m_prestige["capacity_group"], "suite")
        self.assertEqual(m_prestige["booking_room_name"], "Prezidentská suita")
        self.assertGreaterEqual(m_prestige["match_score"], 58)
        self.assertLessEqual(m_prestige["match_score"], 68)
        self.assertFalse(m_prestige["is_exclusive_direct"])

        # 2-lôžková Premium PLUS -> 2_plus_1, high score against Izba Premium s balkónom
        m_prem_plus = by_pat["2-lôžková Premium PLUS"]
        self.assertEqual(m_prem_plus["capacity_group"], "2_plus_1")
        self.assertEqual(m_prem_plus["booking_room_name"], "Izba Premium s balkónom")
        self.assertEqual(
            m_prem_plus["booking_bed_info"],
            "1 manželská posteľ + 1 rozkladacia pohovka",
        )
        self.assertGreaterEqual(m_prem_plus["match_score"], 80)

        # N:1 fallback in same group when best_score < 55
        m_suite_fallback = by_pat["Suite Unikátna Kategória"]
        self.assertEqual(m_suite_fallback["capacity_group"], "suite")
        self.assertIsNotNone(m_suite_fallback["booking_room_name"])
        self.assertLess(m_suite_fallback["match_score"], 55)
        self.assertFalse(m_suite_fallback["is_exclusive_direct"])

        m_2p1_fallback = by_pat["2-lôžková Špeciálna Ponuka PLUS"]
        self.assertEqual(m_2p1_fallback["capacity_group"], "2_plus_1")
        self.assertEqual(m_2p1_fallback["booking_room_name"], "Izba Premium s balkónom")
        self.assertLess(m_2p1_fallback["match_score"], 55)
        self.assertFalse(m_2p1_fallback["is_exclusive_direct"])

        # family stays exclusive direct even if candidates exist
        m_family = by_pat["2-lôžková Deluxe Family PLUS"]
        self.assertEqual(m_family["capacity_group"], "family")
        self.assertIsNone(m_family["booking_room_name"])
        self.assertIsNone(m_family["booking_bed_info"])
        self.assertTrue(m_family["is_exclusive_direct"])

    def test_build_catalog_mappings_preserves_manual_override(self) -> None:
        """Manual override entries in existing_mappings must not be overwritten."""
        booking_offers = [
            RoomOffer(room_name="Dvojlôžková izba Standard", price=0.0),
            RoomOffer(room_name="Dvojlôžková izba Superior", price=0.0),
        ]
        direct_offers = [
            RoomOffer(room_name="Izba Classic", price=0.0),
        ]
        existing = [
            {
                "hotel_id": "hotel_permon",
                "capacity_group": "suite",
                "direct_room_pattern": "Izba Classic",
                "booking_room_name": "Dvojlôžková izba Superior",
                "is_manual_override": True,
                "is_exclusive_direct": False,
                "match_score": 100,
                "priority": 1,
                "is_active": True,
            }
        ]

        mappings = self.matcher.build_catalog_mappings(
            hotel=self.hotel,
            booking_offers=booking_offers,
            direct_offers=direct_offers,
            global_rules={},
            existing_mappings=existing,
        )
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["booking_room_name"], "Dvojlôžková izba Superior")
        self.assertEqual(mappings[0]["capacity_group"], "suite")
        self.assertTrue(mappings[0]["is_manual_override"])

    def test_apply_saved_mappings_deterministic_and_immutable(self) -> None:
        """Daily scraping uses saved mappings deterministically without fuzzy matching."""
        booking_offers = [
            RoomOffer(
                room_name="Dvojlôžková izba Standard",
                price=240.0,
                meal_plan="Raňajky",
            ),
            RoomOffer(
                room_name="Dvojlôžková izba Standard",
                price=275.0,
                meal_plan="Polpenzia",
            ),
            RoomOffer(
                room_name="Nezmapovaná izba",
                price=199.0,
            ),
        ]
        direct_offers = [
            RoomOffer(room_name="Izba Štandard", price=210.0),
            RoomOffer(room_name="Izba Štandard Promo", price=195.0),
            RoomOffer(room_name="Rodinná izba 2+2", price=340.0),
        ]
        saved_mappings = [
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Štandard",
                "booking_room_name": "Dvojlôžková izba Standard",
                "match_score": 88,
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Štandard Promo",
                "booking_room_name": "Dvojlôžková izba Standard",
                "match_score": 84,
                "priority": 2,
                "is_active": True,
            },
            {
                "hotel_id": "h1",
                "capacity_group": "family",
                "direct_room_pattern": "Rodinná izba 2+2",
                "booking_room_name": None,
                "match_score": 0,
                "is_exclusive_direct": True,
                "priority": 3,
                "is_active": True,
            },
        ]

        out_booking, out_direct = self.matcher.apply_saved_mappings(
            booking_offers, direct_offers, saved_mappings
        )

        # Immutable frozen dataclass preserved
        with self.assertRaises(FrozenInstanceError):
            out_booking[0].matched_direct_price = 999.0  # type: ignore[misc]

        # N:1 picks cheapest matching direct offer (195.0)
        self.assertEqual(out_booking[0].capacity_group, "2_pax")
        self.assertEqual(out_booking[0].matched_room_name, "Izba Štandard Promo")
        self.assertEqual(out_booking[0].matched_direct_price, 195.0)
        self.assertEqual(out_booking[0].match_score, 84)

        self.assertEqual(out_booking[1].matched_direct_price, 195.0)
        self.assertIsNone(out_booking[2].matched_room_name)
        self.assertIsNone(out_booking[2].matched_direct_price)

        # Direct offers enriched with capacity_group and paired booking room name
        self.assertEqual(out_direct[0].capacity_group, "2_pax")
        self.assertEqual(out_direct[0].matched_room_name, "Dvojlôžková izba Standard")
        self.assertEqual(out_direct[2].capacity_group, "family")
        self.assertIsNone(out_direct[2].matched_room_name)

    def test_apply_saved_mappings_transfers_direct_bonuses(self) -> None:
        """apply_saved_mappings copies matched direct offer bonuses onto booking_offer."""
        booking_offers = [
            RoomOffer(
                room_name="Dvojlôžková izba Standard",
                price=240.0,
                meal_plan="Raňajky",
            ),
        ]
        direct_offers = [
            RoomOffer(
                room_name="Izba Štandard",
                price=215.0,
                meal_plan="Polpenzia | Wellness",
                bonuses="Polpenzia | Wellness",
            ),
            RoomOffer(
                room_name="Izba Štandard Promo",
                price=195.0,
                meal_plan="Polpenzia | Aquapark vstup",
                bonuses="Polpenzia | Aquapark vstup",
            ),
        ]
        saved_mappings = [
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Štandard",
                "booking_room_name": "Dvojlôžková izba Standard",
                "match_score": 88,
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Štandard Promo",
                "booking_room_name": "Dvojlôžková izba Standard",
                "match_score": 84,
                "priority": 2,
                "is_active": True,
            },
        ]

        out_booking, _ = self.matcher.apply_saved_mappings(
            booking_offers, direct_offers, saved_mappings
        )
        self.assertEqual(out_booking[0].matched_room_name, "Izba Štandard Promo")
        self.assertEqual(out_booking[0].matched_direct_price, 195.0)
        self.assertEqual(
            out_booking[0].matched_direct_bonuses, "Polpenzia | Aquapark vstup"
        )

    def test_permon_full_catalog_capacity_first_matching(self) -> None:
        """Integration test with real Grand Hotel Permon Booking vs Direct catalogs."""
        hotel = {
            "id": "8235402c-05a2-4b18-b691-f25a18c7ad73",
            "name": "Grand Hotel Permon",
            "matching_rules": {},
        }
        booking_offers = [
            RoomOffer(
                room_name="Malá izba s dvojlôžkom alebo s 2 oddelenými lôžkami",
                price=0.0,
                bed_info="1 jednolôžková posteľ + 1 pohovka",
            ),
            RoomOffer(
                room_name="Izba Premium Mini s balkónom",
                price=0.0,
                bed_info="1 veľká manželská posteľ",
            ),
            RoomOffer(
                room_name="Izba Deluxe s balkónom",
                price=0.0,
                bed_info="1 pohovka + 1 veľká manželská posteľ",
            ),
            RoomOffer(
                room_name="Izba Premium s výhľadom na hory",
                price=0.0,
                bed_info="1 pohovka + 1 veľká manželská posteľ",
            ),
            RoomOffer(
                room_name="Izba Premium Kriváň s balkónom",
                price=0.0,
                bed_info="1 pohovka + 1 veľká manželská posteľ",
            ),
            RoomOffer(
                room_name="Apartmán s výhľadom na hory",
                price=0.0,
                bed_info="1 veľká manželská posteľ + 1 pohovka",
            ),
            RoomOffer(
                room_name="Suita Prestige",
                price=0.0,
                bed_info="1 veľká manželská posteľ + 1 pohovka",
            ),
            RoomOffer(
                room_name="Prezidentská suita",
                price=0.0,
                bed_info="1 veľká manželská posteľ + 1 pohovka",
            ),
            RoomOffer(
                room_name="Suita Penthouse",
                price=0.0,
                bed_info="1 veľká manželská posteľ + 1 pohovka",
            ),
            RoomOffer(
                room_name="Suita Premium Deluxe",
                price=0.0,
                bed_info="1 veľká manželská posteľ + 1 pohovka",
            ),
        ]
        direct_offers = [
            # 1_pax (2)
            RoomOffer(room_name="1-lôžková Premium/Premium Grande"),
            RoomOffer(room_name="1-lôžková Premium Kriváň/Premium „Kriváň“ Grande"),
            # 2_pax (2)
            RoomOffer(room_name="2-lôžková Premium Mini"),
            RoomOffer(room_name="2-lôžková Premium Kriváň Mini"),
            # 2_plus_1 (6)
            RoomOffer(room_name="2-lôžková Deluxe Kriváň PLUS"),
            RoomOffer(room_name="2-lôžková Premium s prístelkou"),
            RoomOffer(room_name="2-lôžková Premium PLUS"),
            RoomOffer(room_name="2-lôžková Premium Kriváň s prístelkou"),
            RoomOffer(room_name="2-lôžková Premium Kriváň PLUS"),
            RoomOffer(room_name="2-lôžková Deluxe Premium PLUS"),
            # family (2)
            RoomOffer(room_name="2-lôžková Deluxe Family PLUS"),
            RoomOffer(room_name="Suite Deluxe Family"),
            # suite (7)
            RoomOffer(room_name="Suite Deluxe"),
            RoomOffer(room_name="Suite Deluxe Kriváň"),
            RoomOffer(room_name="Suite Deluxe Premium"),
            RoomOffer(room_name="Suite Premium Kriváň"),
            RoomOffer(room_name="Suite Prestige"),
            RoomOffer(room_name="Suite President"),
            RoomOffer(room_name="Suite Penthouse"),
        ]

        mappings = self.matcher.build_catalog_mappings(
            hotel=hotel,
            booking_offers=booking_offers,
            direct_offers=direct_offers,
            global_rules={
                "min_fuzzy_score": 70,
                "capacity_priority_order": ["family", "suite", "2_plus_1", "1_pax", "2_pax"],
                "exclusive_direct_groups": ["family"],
                "normalization_stop_words": [
                    "izba",
                    "room",
                    "s výhľadom",
                    "bez balkóna",
                    "s balkónom",
                ],
            },
            existing_mappings=[],
        )

        self.assertEqual(len(mappings), 19)
        by_pat = {m["direct_room_pattern"]: m for m in mappings}

        # 1_pax: both 1-lôžková rooms pair with Malá izba (classified as 1_pax via bed_info)
        for name in (
            "1-lôžková Premium/Premium Grande",
            "1-lôžková Premium Kriváň/Premium „Kriváň“ Grande",
        ):
            self.assertEqual(by_pat[name]["capacity_group"], "1_pax")
            self.assertEqual(
                by_pat[name]["booking_room_name"],
                "Malá izba s dvojlôžkom alebo s 2 oddelenými lôžkami",
            )
            self.assertFalse(by_pat[name]["is_exclusive_direct"])

        # 2_pax: both Mini rooms pair with Izba Premium Mini s balkónom
        for name in ("2-lôžková Premium Mini", "2-lôžková Premium Kriváň Mini"):
            self.assertEqual(by_pat[name]["capacity_group"], "2_pax")
            self.assertEqual(
                by_pat[name]["booking_room_name"],
                "Izba Premium Mini s balkónom",
            )
            self.assertFalse(by_pat[name]["is_exclusive_direct"])

        # 2_plus_1: all PLUS and s prístelkou rooms pair strictly within 2_plus_1
        expected_2p1 = {
            "2-lôžková Deluxe Kriváň PLUS": "Izba Deluxe s balkónom",
            "2-lôžková Premium s prístelkou": "Izba Premium s výhľadom na hory",
            "2-lôžková Premium PLUS": "Izba Premium s výhľadom na hory",
            "2-lôžková Premium Kriváň s prístelkou": "Izba Premium Kriváň s balkónom",
            "2-lôžková Premium Kriváň PLUS": "Izba Premium Kriváň s balkónom",
            "2-lôžková Deluxe Premium PLUS": "Izba Premium s výhľadom na hory",
        }
        booking_2p1_names = {
            "Izba Deluxe s balkónom",
            "Izba Premium s výhľadom na hory",
            "Izba Premium Kriváň s balkónom",
        }
        for name, expected_booking in expected_2p1.items():
            self.assertEqual(by_pat[name]["capacity_group"], "2_plus_1")
            self.assertIn(by_pat[name]["booking_room_name"], booking_2p1_names)
            self.assertEqual(by_pat[name]["booking_room_name"], expected_booking)
            self.assertFalse(by_pat[name]["is_exclusive_direct"])

        # family: exclusive direct, booking_room_name=None
        for name in ("2-lôžková Deluxe Family PLUS", "Suite Deluxe Family"):
            self.assertEqual(by_pat[name]["capacity_group"], "family")
            self.assertIsNone(by_pat[name]["booking_room_name"])
            self.assertTrue(by_pat[name]["is_exclusive_direct"])

        # suite: all 7 Suite rooms pair strictly within suite
        expected_suites = {
            "Suite Deluxe": "Suita Premium Deluxe",
            "Suite Deluxe Kriváň": "Suita Premium Deluxe",
            "Suite Deluxe Premium": "Suita Premium Deluxe",
            "Suite Premium Kriváň": "Suita Premium Deluxe",
            "Suite Prestige": "Suita Prestige",
            "Suite President": "Prezidentská suita",
            "Suite Penthouse": "Suita Penthouse",
        }
        booking_suite_names = {
            "Apartmán s výhľadom na hory",
            "Suita Prestige",
            "Prezidentská suita",
            "Suita Penthouse",
            "Suita Premium Deluxe",
        }
        for name, expected_booking in expected_suites.items():
            self.assertEqual(by_pat[name]["capacity_group"], "suite")
            self.assertIn(by_pat[name]["booking_room_name"], booking_suite_names)
            self.assertEqual(by_pat[name]["booking_room_name"], expected_booking)
            self.assertFalse(by_pat[name]["is_exclusive_direct"])


class TestCatalogParsersIntegration(unittest.TestCase):
    """Playwright-based integration tests for BookingParser and DirectParser catalog modes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser_manager = BrowserManager(headless=True)
        cls.browser_manager.launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser_manager.close()

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.booking_parser = BookingParser(screenshots_dir=Path(self.test_dir))
        self.direct_parser = DirectParser(screenshots_dir=Path(self.test_dir))
        self.page = self.browser_manager.new_page()

    def tearDown(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_build_catalog_booking_url_strips_date_params(self) -> None:
        """Catalog URL must strip checkin/checkout/group_adults/no_rooms params."""
        url = (
            "https://www.booking.com/hotel/sk/grand-permon.sk.html"
            "?checkin=2026-12-01&checkout=2026-12-03&group_adults=2&no_rooms=1&lang=sk"
        )
        cleaned = self.booking_parser.build_catalog_booking_url(url)
        self.assertNotIn("checkin", cleaned)
        self.assertNotIn("checkout", cleaned)
        self.assertNotIn("group_adults", cleaned)
        self.assertIn("lang=sk", cleaned)

    def test_booking_parse_room_catalog_and_bed_inheritance(self) -> None:
        """Verify catalog extraction with .b08850ce41, bed_info joining, and price=0.0."""
        html = """
        <html><body>
            <table class="hprt-table">
                <tbody>
                    <tr class="js-rt-block-row">
                        <td>
                            <span class="b08850ce41">Dvojlôžková izba Deluxe</span>
                            <div class="ddaf087582">1 veľká manželská posteľ</div>
                            <div class="ddaf087582">1 rozkladacia pohovka</div>
                        </td>
                        <td class="hprt-table-cell-occupancy">
                            <span class="bicon-occupancy"></span>
                            <span class="bicon-occupancy"></span>
                        </td>
                        <td><span class="bui-price-display__value">210,00 €</span></td>
                    </tr>
                    <tr class="js-rt-block-row">
                        <td></td>
                        <td></td>
                        <td><span class="bui-price-display__value">235,00 €</span></td>
                    </tr>
                    <tr class="js-rt-block-row">
                        <td>
                            <a class="hprt-roomtype-link">Apartmán Exclusive</a>
                            <div class="ddaf087582">1 manželská posteľ</div>
                        </td>
                        <td class="hprt-table-cell-occupancy">
                            <span class="bicon-occupancy"></span>
                            <span class="bicon-occupancy"></span>
                        </td>
                        <td><span class="bui-price-display__value">350,00 €</span></td>
                    </tr>
                </tbody>
            </table>
        </body></html>
        """
        self.page.set_content(html)

        catalog = self.booking_parser.parse_room_catalog(
            page=self.page, rules={}, hotel_name="Test Hotel"
        )
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog[0].room_name, "Dvojlôžková izba Deluxe")
        self.assertEqual(catalog[0].price, 0.0)
        self.assertEqual(
            catalog[0].bed_info, "1 veľká manželská posteľ + 1 rozkladacia pohovka"
        )
        self.assertEqual(catalog[1].room_name, "Apartmán Exclusive")
        self.assertEqual(catalog[1].bed_info, "1 manželská posteľ")

        # Also verify bed_info inheritance on sub-rows in parse_hotel_page
        dated_res = self.booking_parser.parse_hotel_page(
            page=self.page, rules={}, hotel_name="Test Hotel", requested_guests=2
        )
        self.assertEqual(len(dated_res.offers), 3)
        self.assertEqual(
            dated_res.offers[1].bed_info,
            "1 veľká manželská posteľ + 1 rozkladacia pohovka",
        )

    def test_booking_parse_room_catalog_undated_ancestor_tr_bed_info(self) -> None:
        """Undated Booking layout without table.hprt-table extracts bed_info via ancestor::tr[1]."""
        html = """
        <html><body>
            <table class="undated-rooms-table">
                <tbody>
                    <tr>
                        <td><span class="b08850ce41">Izba Single Economy</span></td>
                        <td><span class="ddaf087582">1 jednolôžková posteľ</span></td>
                    </tr>
                    <tr>
                        <td><span class="b08850ce41">Izba Premium s balkónom</span></td>
                        <td>
                            <span class="ddaf087582">1 manželská posteľ</span>
                            <span class="ddaf087582">1 rozkladacia pohovka</span>
                        </td>
                    </tr>
                    <tr>
                        <td><span class="b08850ce41">Suita Premium Deluxe</span></td>
                        <td>
                            <span class="ddaf087582">1 veľká manželská posteľ</span>
                            <span class="ddaf087582">1 rozkladacia pohovka</span>
                        </td>
                    </tr>
                </tbody>
            </table>
        </body></html>
        """
        self.page.set_content(html)

        catalog = self.booking_parser.parse_room_catalog(
            page=self.page, rules={}, hotel_name="Grand Hotel Permon"
        )
        self.assertEqual(len(catalog), 3)
        self.assertEqual(catalog[0].room_name, "Izba Single Economy")
        self.assertEqual(catalog[0].bed_info, "1 jednolôžková posteľ")
        self.assertEqual(catalog[1].room_name, "Izba Premium s balkónom")
        self.assertEqual(
            catalog[1].bed_info, "1 manželská posteľ + 1 rozkladacia pohovka"
        )
        self.assertEqual(catalog[2].room_name, "Suita Premium Deluxe")
        self.assertEqual(
            catalog[2].bed_info, "1 veľká manželská posteľ + 1 rozkladacia pohovka"
        )

        matcher = RoomMatcher()
        mappings = matcher.build_catalog_mappings(
            hotel={"id": "permon", "name": "Grand Hotel Permon", "matching_rules": {}},
            booking_offers=catalog,
            direct_offers=[
                RoomOffer(room_name="1-lôžková Premium/Premium Grande"),
                RoomOffer(room_name="2-lôžková Deluxe Kriváň PLUS"),
                RoomOffer(room_name="Suite Deluxe Premium"),
            ],
            global_rules={"min_fuzzy_score": 70},
            existing_mappings=[],
        )
        by_pat = {m["direct_room_pattern"]: m for m in mappings}

        self.assertEqual(by_pat["1-lôžková Premium/Premium Grande"]["capacity_group"], "1_pax")
        self.assertEqual(
            by_pat["1-lôžková Premium/Premium Grande"]["booking_room_name"],
            "Izba Single Economy",
        )
        self.assertEqual(
            by_pat["1-lôžková Premium/Premium Grande"]["booking_bed_info"],
            "1 jednolôžková posteľ",
        )

        self.assertEqual(by_pat["2-lôžková Deluxe Kriváň PLUS"]["capacity_group"], "2_plus_1")
        self.assertEqual(
            by_pat["2-lôžková Deluxe Kriváň PLUS"]["booking_room_name"],
            "Izba Premium s balkónom",
        )
        self.assertEqual(
            by_pat["2-lôžková Deluxe Kriváň PLUS"]["booking_bed_info"],
            "1 manželská posteľ + 1 rozkladacia pohovka",
        )

        self.assertEqual(by_pat["Suite Deluxe Premium"]["capacity_group"], "suite")
        self.assertEqual(
            by_pat["Suite Deluxe Premium"]["booking_room_name"],
            "Suita Premium Deluxe",
        )
        self.assertEqual(
            by_pat["Suite Deluxe Premium"]["booking_bed_info"],
            "1 veľká manželská posteľ + 1 rozkladacia pohovka",
        )

    def test_booking_parse_room_catalog_ancestor_div_and_xpath_call(self) -> None:
        """Verify row-based catalog extraction across tr, property-card, li and footer filtering."""
        html = """
        <html><body>
            <table>
                <tbody>
                    <tr>
                        <td><span class="b08850ce41">Malá izba s dvojlôžkom</span></td>
                        <td>
                            <span class="ddaf087582">1 jednolôžková posteľ</span>
                            <span class="ddaf087582">1 pohovka</span>
                        </td>
                    </tr>
                </tbody>
            </table>
            <div data-testid="property-card">
                <div><span class="b08850ce41">Izba Mini</span></div>
                <div><span class="ddaf087582">1 manželská posteľ</span></div>
            </div>
            <ul>
                <li>
                    <span class="b08850ce41">Apartmán Kriváň</span>
                    <div data-testid="bed-info">1 extra veľká manželská posteľ</div>
                </li>
                <li>
                    <span class="b08850ce41">Izba Mini</span>
                    <span class="ddaf087582">1 manželská posteľ</span>
                </li>
            </ul>
            <footer>
                <ul>
                    <li><span class="b08850ce41">Mestá</span></li>
                    <li><span class="b08850ce41">Podpora</span></li>
                </ul>
                <div class="b08850ce41">Objavujte</div>
            </footer>
        </body></html>
        """
        self.page.set_content(html)
        catalog = self.booking_parser.parse_room_catalog(
            page=self.page, rules={}, hotel_name="Row Layout Hotel"
        )
        self.assertEqual(len(catalog), 3)
        self.assertEqual(catalog[0].room_name, "Malá izba s dvojlôžkom")
        self.assertEqual(catalog[0].bed_info, "1 jednolôžková posteľ + 1 pohovka")
        self.assertEqual(catalog[1].room_name, "Izba Mini")
        self.assertEqual(catalog[1].bed_info, "1 manželská posteľ")
        self.assertEqual(catalog[2].room_name, "Apartmán Kriváň")
        self.assertEqual(catalog[2].bed_info, "1 extra veľká manželská posteľ")
        self.assertNotIn("Mestá", [o.room_name for o in catalog])
        self.assertNotIn("Podpora", [o.room_name for o in catalog])
        self.assertNotIn("Objavujte", [o.room_name for o in catalog])

    def test_direct_parse_room_catalog_without_token_api_post(self) -> None:
        """DirectParser.parse_room_catalog ignores token_api_post and reads static rooms."""
        html = """
        <html><body>
            <div class="catalog">
                <h3 class="room-title">Izba Standard</h3>
                <h3 class="room-title">Rodinný apartmán</h3>
                <h3 class="room-title">Izba Standard</h3>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "method": "token_api_post",
            "catalog_room_selectors": [".room-title"],
        }
        with patch.object(self.direct_parser, "_handle_token_api_post") as mock_token:
            catalog = self.direct_parser.parse_room_catalog(
                page=self.page, rules=rules, hotel_name="Permon"
            )
            mock_token.assert_not_called()

        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog[0].room_name, "Izba Standard")
        self.assertEqual(catalog[0].price, 0.0)
        self.assertEqual(catalog[1].room_name, "Rodinný apartmán")

    def test_direct_parse_room_catalog_extracts_hidden_tabs(self) -> None:
        """DirectParser.parse_room_catalog extracts rooms from display:none tabs, normalizes text, and deduplicates."""
        html = """
        <html><body>
            <div class="w-tabs-section">
                <h3 class="room-title">  Izba   Standard  </h3>
                <h3 class="room-title">   </h3>
            </div>
            <div class="w-tabs-section" style="display: none;">
                <h3 class="room-title">Rodinný apartmán</h3>
                <h3 class="room-title">izba standard</h3>
                <h3 class="room-title">Prezidentský\n\tApartmán</h3>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "catalog_room_selectors": [".w-tabs-section h3.room-title", ".room-title"],
        }
        catalog = self.direct_parser.parse_room_catalog(
            page=self.page, rules=rules, hotel_name="Permon"
        )

        self.assertEqual(
            [offer.room_name for offer in catalog],
            ["Izba Standard", "Rodinný apartmán", "Prezidentský Apartmán"],
        )
        self.assertTrue(all(offer.price == 0.0 for offer in catalog))

    def test_direct_parse_hotel_extracts_direct_offers_from_room_cards(self) -> None:
        """Daily direct parse extracts room-level direct_offers when room_card_selectors set."""
        html = """
        <html><body>
            <div class="room-card">
                <h3 class="room-name">Izba Standard</h3>
                <span class="room-price">189,00 €</span>
            </div>
            <div class="room-card">
                <h3 class="room-name">Rodinná izba 2+2</h3>
                <span class="room-price">320,00 €</span>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "room_card_selectors": [".room-card"],
            "room_name_selectors": [".room-name"],
            "price_selectors": [".room-price"],
            "price_regex": r"([\d\s.,]+)",
        }
        result = self.direct_parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Permon"
        )
        self.assertTrue(result.is_available)
        self.assertEqual(result.direct_price, 189.0)
        self.assertEqual(len(result.direct_offers), 2)
        self.assertEqual(result.direct_offers[0].room_name, "Izba Standard")
        self.assertEqual(result.direct_offers[0].price, 189.0)
        self.assertEqual(result.direct_offers[1].room_name, "Rodinná izba 2+2")
        self.assertEqual(result.direct_offers[1].price, 320.0)

    def test_direct_parse_hotel_extracts_room_name_attribute_and_hidden_bonuses(
        self,
    ) -> None:
        """DirectParser reads room_name_attribute with selector fallback and extracts hidden div[data-offer-wrapper] bonuses."""
        html = """
        <html><body>
            <div class="room-card" data-room_name="2-lôžková Premium Kriváň">
                <h3 class="room-title">Ignorovaný titulok</h3>
                <span class="room-price">219,00 €</span>
                <div data-offer-wrapper>  Raňajky   v cene  </div>
                <div data-offer-wrapper style="display: none;">
                    Vstup   do wellness
                </div>
                <div data-offer-wrapper style="display: none;">Raňajky v cene</div>
            </div>
            <div class="room-card">
                <h3 class="room-title">Suita Fallback</h3>
                <span class="room-price">350,00 €</span>
                <div data-offer-wrapper style="display: none;">Polpenzia</div>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "room_card_selectors": [".room-card"],
            "room_name_attribute": "data-room_name",
            "room_name_selectors": [".room-title"],
            "price_selectors": [".room-price"],
            "bonus_selectors": ["div[data-offer-wrapper]"],
            "price_regex": r"([\d\s.,]+)",
        }
        result = self.direct_parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Permon"
        )
        self.assertTrue(result.is_available)
        self.assertEqual(len(result.direct_offers), 2)

        first = result.direct_offers[0]
        self.assertEqual(first.room_name, "2-lôžková Premium Kriváň")
        self.assertEqual(first.price, 219.0)
        self.assertEqual(first.bonuses, "Raňajky v cene | Vstup do wellness")
        self.assertEqual(first.meal_plan, "Raňajky v cene | Vstup do wellness")

        second = result.direct_offers[1]
        self.assertEqual(second.room_name, "Suita Fallback")
        self.assertEqual(second.price, 350.0)
        self.assertEqual(second.bonuses, "Polpenzia")
        self.assertEqual(second.meal_plan, "Polpenzia")

    def test_handle_token_api_post_dynamic_children_zero_and_multiple(self) -> None:
        """_handle_token_api_post removes childrens[1][*] for 0 children and sets each age for N children."""
        tomorrow = date.today() + timedelta(days=2)
        checkout = tomorrow + timedelta(days=2)
        rules = {
            "token_endpoint": "https://booking.hotelpermon.sk/sk/home/createSearchToken",
            "search_url_template": "https://booking.hotelpermon.sk/sk/search/{token}",
            "date_format": "%Y-%m-%d",
            "form_payload": {
                "date_from": "{check_in}",
                "date_to": "{check_out}",
                "adults[1]": "{adults}",
                "childrens[1][1]": "{child_age_1}",
            },
        }

        mock_page = MagicMock()
        mock_page.evaluate.return_value = '{"token": "12345678-1234-1234-1234-1234567890ab"}'

        # Case 1: 0 children -> no childrens[1][*] keys in payload
        job_zero = ScrapingJob(
            check_in=tomorrow,
            check_out=checkout,
            adults=2,
            children=0,
            children_ages=[],
        )
        ok_zero = self.direct_parser._handle_token_api_post(mock_page, rules, job_zero)
        self.assertTrue(ok_zero)
        payload_zero = mock_page.evaluate.call_args[0][1]["payload"]
        self.assertEqual(payload_zero["adults[1]"], "2")
        self.assertFalse(any(k.startswith("childrens[1][") for k in payload_zero))

        # Case 2: 2 children with ages [1, 4] -> childrens[1][1]="1", childrens[1][2]="4"
        mock_page.reset_mock()
        job_two = ScrapingJob(
            check_in=tomorrow,
            check_out=checkout,
            adults=2,
            children=2,
            children_ages=[1, 4],
        )
        ok_two = self.direct_parser._handle_token_api_post(mock_page, rules, job_two)
        self.assertTrue(ok_two)
        payload_two = mock_page.evaluate.call_args[0][1]["payload"]
        self.assertEqual(payload_two["adults[1]"], "2")
        self.assertEqual(payload_two["childrens[1][1]"], "1")
        self.assertEqual(payload_two["childrens[1][2]"], "4")

    def test_direct_parse_hotel_ignores_calendar_price_without_room_card(self) -> None:
        """Calendar/banner price (e.g. 'od 296 €') without room cards must yield NOT_AVAILABLE and None."""
        html = """
        <html><body>
            <div class="sold-out-notice">Na vybraný termín nie sú voľné izby</div>
            <div class="calendar-widget">
                <span class="calendar-price">od 296,00 €</span>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "room_card_selectors": [".room-card"],
            "room_name_selectors": [".room-name"],
            "price_selectors": [".calendar-price", ".room-price"],
            "price_regex": r"([\d\s.,]+)",
        }
        result = self.direct_parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Permon"
        )
        self.assertFalse(result.is_available)
        self.assertEqual(result.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(result.direct_price)
        self.assertEqual(result.direct_offers, [])

    def test_direct_parse_hotel_discards_offers_without_valid_room_name(self) -> None:
        """Room cards or offers with empty/'—'/None room_name are discarded and yield NOT_AVAILABLE."""
        html = """
        <html><body>
            <div class="room-card">
                <h3 class="room-name">—</h3>
                <span class="room-price">296,00 €</span>
            </div>
            <div class="room-card">
                <h3 class="room-name">   </h3>
                <span class="room-price">315,00 €</span>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "room_card_selectors": [".room-card"],
            "room_name_selectors": [".room-name"],
            "price_selectors": [".room-price"],
            "price_regex": r"([\d\s.,]+)",
        }
        result = self.direct_parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Permon"
        )
        self.assertFalse(result.is_available)
        self.assertEqual(result.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(result.direct_price)
        self.assertEqual(result.direct_offers, [])

        # Also verify post-extraction filter drops offers with room_name=None
        none_offer = RoomOffer(room_name="temp", price=296.0)
        object.__setattr__(none_offer, "room_name", None)
        with patch.object(
            self.direct_parser, "_extract_direct_offers", return_value=[none_offer]
        ):
            filtered_res = self.direct_parser.parse_direct_hotel(
                page=self.page, rules=rules, hotel_name="Permon"
            )
        self.assertFalse(filtered_res.is_available)
        self.assertEqual(filtered_res.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(filtered_res.direct_price)
        self.assertEqual(filtered_res.direct_offers, [])

    def test_direct_parse_hrebienok_oneroomr_card_and_url_template(self) -> None:
        """Verify Hrebienok url_template date formatting, clean h2 room name, and .final_price extraction."""
        rules = {
            "date_format": "%d.%m.%Y",
            "url_template": (
                "https://hrebienok-resort.sk/booking/"
                "?checkin={check_in}&checkout={check_out}&rooms=1&1-adults={adults}&children={children}"
            ),
            "room_card_selectors": ["div.oneroomr"],
            "room_name_selectors": ["h2"],
            "price_selectors": [".onerate .final_price", ".final_price"],
            "bonus_selectors": [".onerate h3", ".onerate .allfees"],
            "price_regex": r"(\d+[\s.,]*\d*)",
        }

        job = ScrapingJob(
            check_in=date(2026, 12, 1),
            check_out=date(2026, 12, 3),
            adults=2,
            children=0,
        )
        expected_url = (
            "https://hrebienok-resort.sk/booking/"
            "?checkin=01.12.2026&checkout=03.12.2026&rooms=1&1-adults=2&children=0"
        )
        self.assertEqual(
            self.direct_parser.build_direct_url("https://hrebienok-resort.sk/", job, rules),
            expected_url,
        )
        self.assertEqual(
            self.direct_parser._build_direct_url("https://hrebienok-resort.sk/", job, rules),
            expected_url,
        )

        self.assertEqual(
            DirectParser._clean_room_name_text(
                "Studio Komfort Počet dostupných: 4 od 117 EUR / noc"
            ),
            "Studio Komfort",
        )
        self.assertEqual(
            DirectParser._clean_room_name_text(
                "Studio Klasik - Depandance HR1***od 117 EUR / noc"
            ),
            "Studio Klasik - Depandance HR1***",
        )

        html = """
        <html><body>
            <div class="oneroomr">
                <h2>Studio Klasik - Depandance HR1***<small class="alertonlyone">Počet dostupných: 4</small><span class="from">od 117 EUR / noc</span></h2>
                <div class="onerate">
                    <h3>Raňajky v cene</h3>
                    <div class="allfees">Vrátane DPH</div>
                    <div class="final_price">
                        <p class="member_rate">234,00 EUR</p>
                    </div>
                </div>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        result = self.direct_parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Hrebienok", job=job
        )
        self.assertTrue(result.is_available)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(len(result.direct_offers), 1)
        self.assertEqual(
            result.direct_offers[0].room_name,
            "Studio Klasik - Depandance HR1***",
        )
        self.assertEqual(result.direct_offers[0].price, 234.0)
        self.assertEqual(result.direct_offers[0].bonuses, "Raňajky v cene | Vrátane DPH")
        self.assertEqual(result.direct_price, 234.0)


class TestSyncPipelineAndCLI(unittest.TestCase):
    """Tests for ScrapingEngine.sync_room_catalog, runtime mapping application, and --sync-rooms."""

    def test_sync_room_catalog_and_cli_flag(self) -> None:
        """Verify --sync-rooms CLI flag runs catalog sync without needing dates."""
        args = parse_arguments(["--sync-rooms", "--hotels", "h1"])
        self.assertTrue(args.sync_rooms)
        self.assertEqual(args.hotels, "h1")

        with patch("main.ScrapingEngine") as mock_engine_cls, patch("main.ConfigManager"), patch(
            "main.BrowserManager"
        ):
            mock_engine = MagicMock()
            mock_engine.sync_room_catalog.return_value = 5
            mock_engine_cls.return_value = mock_engine

            code = main(["--sync-rooms", "--hotels", "h1"])

        self.assertEqual(code, 0)
        mock_engine.sync_room_catalog.assert_called_once_with(hotel_identifiers=["h1"])

    def test_engine_sync_room_catalog_orchestration(self) -> None:
        """ScrapingEngine.sync_room_catalog fetches catalogs, matches, and upserts mappings."""
        mock_config = MagicMock()
        mock_config.get_target_hotels.return_value = [
            {
                "id": "h1",
                "name": "Hotel One",
                "booking_url": "https://booking.com/hotel/one?checkin=2026-12-01",
                "direct_url": "https://hotelone.sk/booking",
                "rooms_catalog_url": "https://hotelone.sk/izby",
                "direct_booking_engine": "permon_direct",
                "matching_rules": {},
            }
        ]
        mock_config.get_scraping_rules.return_value = {}
        mock_config.get_room_mappings.return_value = []
        mock_config.upsert_room_mappings.return_value = True

        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_browser.new_page.return_value = mock_page

        engine = ScrapingEngine(
            config_manager=mock_config,
            browser_manager=mock_browser,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        with patch.object(
            engine.booking_parser,
            "parse_room_catalog",
            return_value=[RoomOffer(room_name="Dvojlôžková izba Standard", price=0.0)],
        ), patch.object(
            engine.direct_parser,
            "parse_room_catalog",
            return_value=[
                RoomOffer(room_name="Dvojlôžková izba Štandard", price=0.0),
                RoomOffer(room_name="Rodinná izba", price=0.0),
            ],
        ):
            synced = engine.sync_room_catalog()

        self.assertEqual(synced, 2)
        mock_page.goto.assert_any_call(
            "https://booking.com/hotel/one", wait_until="domcontentloaded"
        )
        mock_page.goto.assert_any_call(
            "https://hotelone.sk/izby", wait_until="domcontentloaded"
        )
        mock_config.upsert_room_mappings.assert_called_once()
        upserted = mock_config.upsert_room_mappings.call_args[0][0]
        self.assertEqual(len(upserted), 2)
        self.assertEqual(upserted[0]["booking_room_name"], "Dvojlôžková izba Standard")
        self.assertEqual(upserted[1]["capacity_group"], "family")
        self.assertTrue(upserted[1]["is_exclusive_direct"])
        mock_browser.close.assert_called_once()

    def test_engine_sync_room_catalog_url_priorities(self) -> None:
        """sync_room_catalog prefers booking_rooms_url & rooms_catalog_url, falling back when None."""
        mock_config = MagicMock()
        mock_config.get_target_hotels.return_value = [
            {
                "id": "h_manual",
                "name": "Hotel Manual URLs",
                "booking_url": "https://booking.com/hotel/default-one?checkin=2026-12-01",
                "booking_rooms_url": "https://booking.com/hotel/manual-rooms?checkin=2026-12-01&checkout=2026-12-03&lang=sk",
                "direct_url": "https://hotelmanual.sk/book",
                "rooms_catalog_url": "https://hotelmanual.sk/izby",
                "direct_booking_engine": "generic",
                "matching_rules": {},
            },
            {
                "id": "h_fallback",
                "name": "Hotel Fallback URLs",
                "booking_url": "https://booking.com/hotel/fallback-two?checkin=2026-12-01&checkout=2026-12-03",
                "booking_rooms_url": None,
                "direct_url": "https://hotelfallback.sk/ubytovanie",
                "rooms_catalog_url": None,
                "direct_booking_engine": "generic",
                "matching_rules": {},
            },
        ]
        mock_config.get_scraping_rules.return_value = {}
        mock_config.get_room_mappings.return_value = []
        mock_config.upsert_room_mappings.return_value = True

        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_browser.new_page.return_value = mock_page

        engine = ScrapingEngine(
            config_manager=mock_config,
            browser_manager=mock_browser,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        with patch.object(
            engine.booking_parser,
            "parse_room_catalog",
            return_value=[RoomOffer(room_name="Dvojlôžková izba", price=0.0)],
        ), patch.object(
            engine.direct_parser,
            "parse_room_catalog",
            return_value=[RoomOffer(room_name="Dvojlôžková izba", price=0.0)],
        ):
            synced = engine.sync_room_catalog()

        self.assertEqual(synced, 2)
        visited_urls = [call.args[0] for call in mock_page.goto.call_args_list]
        self.assertEqual(
            visited_urls,
            [
                "https://booking.com/hotel/manual-rooms?lang=sk",
                "https://hotelmanual.sk/izby",
                "https://booking.com/hotel/fallback-two",
                "https://hotelfallback.sk/ubytovanie",
            ],
        )

    def test_engine_daily_scrape_applies_saved_mappings(self) -> None:
        """Daily _scrape_single_hotel applies saved hs-room_mappings deterministically."""
        tomorrow = date.today() + timedelta(days=1)
        checkout = tomorrow + timedelta(days=2)
        job = ScrapingJob(check_in=tomorrow, check_out=checkout, adults=2)

        mock_config = MagicMock()
        mock_config.get_scraping_rules.return_value = {}
        mock_config.get_room_mappings.return_value = [
            {
                "hotel_id": "h1",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Classic",
                "booking_room_name": "Double Classic Room",
                "match_score": 92,
                "priority": 1,
                "is_active": True,
            }
        ]

        engine = ScrapingEngine(
            job=job,
            config_manager=mock_config,
            browser_manager=MagicMock(),
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )
        mock_page = MagicMock()

        hotel = {
            "id": "h1",
            "name": "Hotel One",
            "booking_url": "https://booking.com/hotel/one",
            "direct_url": "https://hotelone.sk",
            "direct_booking_engine": "generic",
        }
        b_res = ScrapedResult(
            hotel_name="Hotel One",
            booking_price=250.0,
            offers=[RoomOffer(room_name="Double Classic Room", price=250.0)],
        )
        d_res = ScrapedResult(
            hotel_name="Hotel One",
            direct_price=215.0,
            direct_offers=[RoomOffer(room_name="Izba Classic", price=215.0)],
        )

        with patch.object(engine.booking_parser, "parse_hotel_page", return_value=b_res), patch.object(
            engine.direct_parser, "parse_direct_hotel", return_value=d_res
        ):
            res = engine._scrape_single_hotel(hotel, mock_page, {})

        self.assertEqual(res.status, ResultStatus.SUCCESS)
        self.assertEqual(res.offers[0].capacity_group, "2_pax")
        self.assertEqual(res.offers[0].matched_room_name, "Izba Classic")
        self.assertEqual(res.offers[0].matched_direct_price, 215.0)
        self.assertEqual(res.direct_offers[0].matched_room_name, "Double Classic Room")

    def test_engine_upserts_price_comparisons_with_bonuses(self) -> None:
        """ScrapingEngine.run builds separate Direct & Booking rows sharing matched_room_key and calls upsert_price_comparisons."""
        tomorrow = date.today() + timedelta(days=2)
        checkout = tomorrow + timedelta(days=2)
        job = ScrapingJob(
            check_in=tomorrow,
            check_out=checkout,
            adults=2,
            children=1,
            children_ages=[6],
        )

        mock_config = MagicMock()
        mock_config.get_target_hotels.return_value = [
            {
                "id": "h_permon",
                "name": "Grand Hotel Permon",
                "booking_url": "https://booking.com/hotel/permon",
                "direct_url": "https://booking.hotelpermon.sk",
                "direct_booking_engine": "permon",
            }
        ]
        mock_config.get_scraping_rules.return_value = {}
        mock_config.get_room_mappings.return_value = [
            {
                "hotel_id": "h_permon",
                "capacity_group": "2_pax",
                "direct_room_pattern": "Izba Classic",
                "booking_room_name": "Double Classic Room",
                "match_score": 92,
                "priority": 1,
                "is_active": True,
            },
            {
                "hotel_id": "h_permon",
                "capacity_group": "family",
                "direct_room_pattern": "Rodinný apartmán",
                "booking_room_name": None,
                "is_exclusive_direct": True,
                "match_score": 0,
                "priority": 2,
                "is_active": True,
            },
        ]
        mock_config.upsert_price_comparisons.return_value = 3

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = MagicMock()
        engine = ScrapingEngine(
            job=job,
            config_manager=mock_config,
            browser_manager=mock_browser,
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        b_res = ScrapedResult(
            hotel_name="Grand Hotel Permon",
            booking_price=250.0,
            offers=[
                RoomOffer(
                    room_name="Double Classic Room",
                    price=250.0,
                    meal_plan="Raňajky",
                    cancellation_policy="Bezplatné storno",
                )
            ],
        )
        d_res = ScrapedResult(
            hotel_name="Grand Hotel Permon",
            direct_price=215.0,
            direct_offers=[
                RoomOffer(
                    room_name="Izba Classic",
                    price=215.0,
                    meal_plan="Polpenzia | Wellness",
                    bonuses="Polpenzia | Wellness",
                ),
                RoomOffer(
                    room_name="Rodinný apartmán",
                    price=340.0,
                    meal_plan="Polpenzia | Detský klub",
                    bonuses="Polpenzia | Detský klub",
                ),
            ],
        )

        with patch.object(engine.booking_parser, "parse_hotel_page", return_value=b_res), patch.object(
            engine.direct_parser, "parse_direct_hotel", return_value=d_res
        ):
            engine.run()

        mock_config.upsert_price_comparisons.assert_called_once()
        upserted = mock_config.upsert_price_comparisons.call_args[0][0]
        # 2 Direct rows + 1 Booking row = 3 normalized rows
        self.assertEqual(len(upserted), 3)

        by_key = {(r["channel"], r["room_name"]): r for r in upserted}

        direct_paired = by_key[("Direct", "Izba Classic")]
        self.assertEqual(direct_paired["hotel_id"], "h_permon")
        self.assertEqual(direct_paired["capacity_group"], "2_pax")
        self.assertEqual(direct_paired["matched_room_key"], "Double Classic Room")
        self.assertEqual(direct_paired["price"], 215.0)
        self.assertEqual(direct_paired["bonuses"], "Polpenzia | Wellness")
        self.assertEqual(direct_paired["children_ages"], "6")

        booking_paired = by_key[("Booking", "Double Classic Room")]
        self.assertEqual(booking_paired["hotel_id"], "h_permon")
        self.assertEqual(booking_paired["capacity_group"], "2_pax")
        self.assertEqual(booking_paired["matched_room_key"], "Double Classic Room")
        self.assertEqual(booking_paired["price"], 250.0)
        self.assertEqual(booking_paired["bonuses"], "Raňajky | Bezplatné storno")
        self.assertEqual(booking_paired["children_ages"], "6")

        # Direct and Booking rows share the exact same matched_room_key for Pivot Table joining
        self.assertEqual(
            direct_paired["matched_room_key"], booking_paired["matched_room_key"]
        )

        direct_exclusive = by_key[("Direct", "Rodinný apartmán")]
        self.assertEqual(direct_exclusive["capacity_group"], "family")
        self.assertEqual(
            direct_exclusive["matched_room_key"], "Direct Only: Rodinný apartmán"
        )
        self.assertEqual(direct_exclusive["price"], 340.0)
        self.assertEqual(direct_exclusive["bonuses"], "Polpenzia | Detský klub")

    def test_build_price_comparison_records_normalized_channels_and_sold_out_omission(
        self,
    ) -> None:
        """Normalized builder emits separate Direct/Booking rows and omits hotels with no valid offers."""
        tomorrow = date.today() + timedelta(days=2)
        checkout = tomorrow + timedelta(days=2)
        job = ScrapingJob(check_in=tomorrow, check_out=checkout, adults=2)
        engine = ScrapingEngine(
            job=job,
            config_manager=MagicMock(),
            browser_manager=MagicMock(),
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        sold_out_res = ScrapedResult(
            hotel_id="h_sold",
            hotel_name="Sold Out Hotel",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=None,
            direct_price=None,
            status=ResultStatus.NOT_AVAILABLE,
            is_available=False,
            offers=[],
            direct_offers=[],
        )
        booking_only_res = ScrapedResult(
            hotel_id="h_b_only",
            hotel_name="Booking Only Hotel",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=190.0,
            direct_price=None,
            status=ResultStatus.PARTIAL,
            is_available=True,
            offers=[RoomOffer(room_name="Standard Double", price=190.0, capacity_group="2_pax")],
            direct_offers=[],
        )
        mixed_res = ScrapedResult(
            hotel_id="h_mixed",
            hotel_name="Mixed Hotel",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=240.0,
            direct_price=210.0,
            status=ResultStatus.SUCCESS,
            is_available=True,
            offers=[
                RoomOffer(
                    room_name="Double Room",
                    price=240.0,
                    capacity_group="2_pax",
                    matched_room_name="Izba Classic",
                )
            ],
            direct_offers=[
                RoomOffer(
                    room_name="Izba Classic",
                    price=210.0,
                    capacity_group="2_pax",
                    matched_room_name="Double Room",
                ),
                RoomOffer(
                    room_name="Rodinná izba",
                    price=310.0,
                    capacity_group="family",
                    matched_room_name=None,
                ),
            ],
        )

        records = engine.build_price_comparison_records(
            [sold_out_res, booking_only_res, mixed_res]
        )
        # h_sold -> 0 rows; h_b_only -> 1 Booking row; h_mixed -> 2 Direct + 1 Booking = 3 rows
        self.assertEqual(len(records), 4)

        by_hotel: dict = {}
        for rec in records:
            by_hotel.setdefault(rec["hotel_id"], []).append(rec)

        self.assertNotIn("h_sold", by_hotel)

        b_only = by_hotel["h_b_only"][0]
        self.assertEqual(b_only["channel"], "Booking")
        self.assertEqual(b_only["room_name"], "Standard Double")
        self.assertEqual(b_only["matched_room_key"], "Standard Double")
        self.assertEqual(b_only["price"], 190.0)

        mixed_recs = by_hotel["h_mixed"]
        self.assertEqual(len(mixed_recs), 3)
        channels = [r["channel"] for r in mixed_recs]
        self.assertEqual(channels.count("Direct"), 2)
        self.assertEqual(channels.count("Booking"), 1)

    def test_cli_export_db_only_flag_without_browser(self) -> None:
        """Verify --export-db-only exports 16-column single-sheet raw DB Excel without launching BrowserManager."""
        import openpyxl

        args = parse_arguments(["--export-db-only", "--output-dir", "out_db"])
        self.assertTrue(args.export_db_only)
        self.assertEqual(args.output_dir, "out_db")

        tmp_dir = tempfile.mkdtemp()
        try:
            sample_rows = [
                {
                    "scraped_date": "2026-11-17",
                    "scraped_at": "2026-11-17T22:00:00",
                    "hotel_name": "Grand Hotel Permon",
                    "check_in": "2026-12-01",
                    "check_out": "2026-12-03",
                    "nights": 2,
                    "lead_time": 14,
                    "adults": 2,
                    "children": 0,
                    "children_ages": "",
                    "channel": "Direct",
                    "capacity_group": "2_pax",
                    "matched_room_key": "Double Classic",
                    "room_name": "Izba Classic",
                    "price": 210.0,
                    "bonuses": "Wellness v cene",
                },
                {
                    "scraped_date": "2026-11-17",
                    "scraped_at": "2026-11-17T22:00:00",
                    "hotel_name": "Grand Hotel Permon",
                    "check_in": "2026-12-01",
                    "check_out": "2026-12-03",
                    "nights": 2,
                    "lead_time": 14,
                    "adults": 2,
                    "children": 0,
                    "children_ages": "",
                    "channel": "Booking",
                    "capacity_group": "2_pax",
                    "matched_room_key": "Double Classic",
                    "room_name": "Double Classic",
                    "price": 240.0,
                    "bonuses": "Raňajky",
                },
            ]
            with patch("main.ConfigManager") as mock_cm_cls, patch(
                "main.BrowserManager"
            ) as mock_bm_cls:
                mock_cm_cls.return_value.fetch_all_price_comparisons.return_value = (
                    sample_rows
                )
                code = main(["--export-db-only", "--output-dir", tmp_dir])

            self.assertEqual(code, 0)
            mock_bm_cls.assert_not_called()
            mock_cm_cls.return_value.fetch_all_price_comparisons.assert_called_once()

            xlsx_files = list(Path(tmp_dir).glob("hotel_prices_db_*.xlsx"))
            self.assertEqual(len(xlsx_files), 1)
            wb = openpyxl.load_workbook(xlsx_files[0])
            self.assertEqual(wb.sheetnames, ["Surové dáta"])
            ws = wb["Surové dáta"]
            self.assertIsNone(ws.auto_filter.ref)
            self.assertEqual(ws.freeze_panes, "A2")
            self.assertEqual(ws.max_column, 16)
            # Row 2: Booking (channel ASC before Direct)
            self.assertEqual(ws.cell(row=2, column=3).value, "Grand Hotel Permon")
            self.assertEqual(ws.cell(row=2, column=11).value, "Booking")
            self.assertEqual(ws.cell(row=2, column=13).value, "Double Classic")
            self.assertEqual(ws.cell(row=2, column=14).value, "Double Classic")
            self.assertEqual(ws.cell(row=2, column=15).value, 240.0)
            self.assertEqual(ws.cell(row=2, column=16).value, "Raňajky")
            # Row 3: Direct
            self.assertEqual(ws.cell(row=3, column=11).value, "Direct")
            self.assertEqual(ws.cell(row=3, column=13).value, "Double Classic")
            self.assertEqual(ws.cell(row=3, column=14).value, "Izba Classic")
            self.assertEqual(ws.cell(row=3, column=15).value, 210.0)
            self.assertEqual(ws.cell(row=3, column=16).value, "Wellness v cene")
            wb.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_strict_validation_price_without_room_name_is_discarded(
        self,
    ) -> None:
        """Prices without assigned room names (e.g. 296.0 EUR) must be discarded cleanly."""
        tomorrow = date.today() + timedelta(days=2)
        checkout = tomorrow + timedelta(days=2)
        job = ScrapingJob(check_in=tomorrow, check_out=checkout, adults=2)

        mock_config = MagicMock()
        mock_config.get_scraping_rules.return_value = {}
        mock_config.get_room_mappings.return_value = []
        engine = ScrapingEngine(
            job=job,
            config_manager=mock_config,
            browser_manager=MagicMock(),
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )
        engine._in_run_loop = True

        raw_only_res = ScrapedResult(
            hotel_id="h_raw_296",
            hotel_name="Hotel Calendar Fallback",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=296.0,
            direct_price=296.0,
            offers=[],
            direct_offers=[],
            status=ResultStatus.SUCCESS,
            is_available=True,
        )

        none_b_offer = RoomOffer(room_name="temp", price=296.0)
        object.__setattr__(none_b_offer, "room_name", None)
        dash_d_offer = RoomOffer(room_name="—", price=296.0)
        nameless_offers_res = ScrapedResult(
            hotel_id="h_nameless_296",
            hotel_name="Hotel Nameless Room",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=296.0,
            direct_price=296.0,
            offers=[none_b_offer],
            direct_offers=[dash_d_offer],
            status=ResultStatus.SUCCESS,
            is_available=True,
        )

        partial_valid_res = ScrapedResult(
            hotel_id="h_partial_valid",
            hotel_name="Hotel Booking Valid Direct Empty",
            check_in=tomorrow,
            check_out=checkout,
            booking_price=350.0,
            direct_price=296.0,
            offers=[
                RoomOffer(
                    room_name="Dvojlôžková izba",
                    price=350.0,
                    capacity_group="2_pax",
                    matched_room_name="Izba Classic",
                )
            ],
            direct_offers=[],
            status=ResultStatus.SUCCESS,
            is_available=True,
        )

        records = engine.build_price_comparison_records(
            [raw_only_res, nameless_offers_res, partial_valid_res]
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["hotel_id"], "h_partial_valid")
        self.assertEqual(records[0]["channel"], "Booking")
        self.assertEqual(records[0]["room_name"], "Dvojlôžková izba")
        self.assertEqual(records[0]["matched_room_key"], "Dvojlôžková izba")
        self.assertEqual(records[0]["price"], 350.0)

        hotel_cfg = {
            "id": "h_single",
            "name": "Hotel Single Nameless",
            "booking_url": "https://booking.com/hotel/single",
            "direct_url": "https://single.sk",
            "direct_booking_engine": "generic",
        }
        with patch.object(
            engine.booking_parser,
            "parse_hotel_page",
            return_value=ScrapedResult(
                hotel_name="Hotel Single Nameless",
                booking_price=296.0,
                offers=[none_b_offer],
                is_available=True,
            ),
        ), patch.object(
            engine.direct_parser,
            "parse_direct_hotel",
            return_value=ScrapedResult(
                hotel_name="Hotel Single Nameless",
                direct_price=296.0,
                direct_offers=[dash_d_offer],
                is_available=True,
            ),
        ):
            scraped = engine._scrape_single_hotel(hotel_cfg, MagicMock(), {})

        self.assertFalse(scraped.is_available)
        self.assertEqual(scraped.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(scraped.booking_price)
        self.assertIsNone(scraped.direct_price)
        self.assertEqual(scraped.offers, [])
        self.assertEqual(scraped.direct_offers, [])


if __name__ == "__main__":
    unittest.main()

