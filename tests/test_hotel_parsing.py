"""Unit tests for hotel price parsing and DirectParser room name / bonus cleaning."""

from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from core.models import ResultStatus, ScrapingJob
from scraper.booking_parser import clean_price_text
from scraper.browser import BrowserManager
from scraper.direct_parser import DirectParser, UNAVAILABLE_DATE_MESSAGE


class TestHotelPriceParsing(unittest.TestCase):
    """Test suite for clean_price_text helper function."""

    def test_european_price_formats(self) -> None:
        """Test parsing of common European price formats."""
        test_cases = [
            ("382,00 €", 382.0),
            ("382.00 EUR", 382.0),
            (" 1 250,50 € ", 1250.5),
        ]
        for raw_text, expected_price in test_cases:
            with self.subTest(raw=raw_text):
                result = clean_price_text(raw_text)
                self.assertEqual(result, expected_price)

    def test_price_with_surrounding_text(self) -> None:
        """Test parsing of prices embedded within surrounding sentences."""
        raw_text = "Celková cena: 450,00 € vrátane DPH"
        result = clean_price_text(raw_text)
        self.assertEqual(result, 450.0)

    def test_invalid_and_empty_inputs(self) -> None:
        """Test non-numeric, empty, or None inputs return None."""
        invalid_cases = [
            "",
            None,
            "Vypredané",
        ]
        for raw_text in invalid_cases:
            with self.subTest(raw=raw_text):
                result = clean_price_text(raw_text)  # type: ignore[arg-type]
                self.assertIsNone(result)


class TestDirectRoomCardParsing(unittest.TestCase):
    """Playwright tests for DirectParser cleaning of nested tags in h2 and hidden bonuses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser_manager = BrowserManager(headless=True)
        cls.browser_manager.launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser_manager.close()

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.parser = DirectParser(screenshots_dir=Path(self.test_dir))
        self.page = self.browser_manager.new_page()

    def tearDown(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_studio_komfort_h2_strips_small_and_span_from_and_reads_hidden_bonuses(
        self,
    ) -> None:
        """DirectParser cleans <h2>Studio Komfort<small>...</small><span class='from'>...</span></h2> to 'Studio Komfort'."""
        html = """
        <html><body>
            <div class="oneroomr">
                <h2 class="">Studio Komfort<small class="alertonlyone">Počet dostupných: 4</small><span class="from">od 117 EUR / noc</span></h2>
                <div class="onerate">
                    <h3>Polpenzia v cene</h3>
                    <div class="allfees" style="display: none;">Bezplatné storno do 7 dní</div>
                    <div class="final_price">234,00 EUR</div>
                </div>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "room_card_selectors": ["div.oneroomr"],
            "room_name_selectors": ["h2"],
            "price_selectors": [".onerate .final_price"],
            "bonus_selectors": [".onerate h3", ".onerate .allfees"],
            "price_regex": r"(\d+[\s.,]*\d*)",
        }

        result = self.parser.parse_direct_hotel(
            page=self.page, rules=rules, hotel_name="Hrebienok Resort"
        )
        self.assertTrue(result.is_available)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(len(result.direct_offers), 1)
        self.assertEqual(result.direct_offers[0].room_name, "Studio Komfort")
        self.assertEqual(result.direct_offers[0].price, 234.0)
        self.assertEqual(
            result.direct_offers[0].bonuses,
            "Polpenzia v cene | Bezplatné storno do 7 dní",
        )

    def test_studio_komfort_tatry_extracts_multiple_onerate_packages_without_member_rate(
        self,
    ) -> None:
        """DirectParser extracts all 3 .onerate packages separately and ignores nested .member_rate."""
        html = """
        <html><body>
            <div class="oneroomr">
                <h2>Studio Komfort TATRY<small class="alertonlyone">Počet dostupných: 2</small><span class="from">od 126 EUR</span></h2>
                <div class="onerate">
                    <h3>Ubytovanie bez stravy</h3>
                    <div class="allfees" style="display: none;">Bezplatné storno</div>
                    <div class="final_price">126,00 EUR <p class="member_rate">Po prihlásení: 119,70 EUR</p></div>
                </div>
                <div class="onerate">
                    <h3>Ubytovanie s raňajkami</h3>
                    <div class="allfees" style="display: none;">Raňajky formou bufetu</div>
                    <div class="final_price">154,00 EUR <p class="member_rate">Po prihlásení: 146,30 EUR</p></div>
                </div>
                <div class="onerate">
                    <h3>Ubytovanie s polpenziou</h3>
                    <div class="allfees" style="display: none;">Raňajky a večera v cene</div>
                    <div class="final_price">200,00 EUR <p class="member_rate">Po prihlásení: 190,00 EUR</p></div>
                </div>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        expected_offers = [
            (126.0, "Ubytovanie bez stravy | Bezplatné storno"),
            (154.0, "Ubytovanie s raňajkami | Raňajky formou bufetu"),
            (200.0, "Ubytovanie s polpenziou | Raňajky a večera v cene"),
        ]

        rule_variants = [
            (
                "explicit_offer_item_selectors",
                {
                    "room_card_selectors": ["div.oneroomr"],
                    "room_name_selectors": ["h2"],
                    "offer_item_selectors": [".onerate"],
                    "price_selectors": [".final_price"],
                    "bonus_selectors": ["h3", ".allfees"],
                    "price_regex": r"(\d+[\s.,]*\d*)",
                },
            ),
            (
                "fallback_multi_onerate_detection",
                {
                    "room_card_selectors": ["div.oneroomr"],
                    "room_name_selectors": ["h2"],
                    "price_selectors": [".onerate .final_price"],
                    "bonus_selectors": [".onerate h3", ".onerate .allfees"],
                    "price_regex": r"(\d+[\s.,]*\d*)",
                },
            ),
        ]

        for label, rules in rule_variants:
            with self.subTest(mode=label):
                result = self.parser.parse_direct_hotel(
                    page=self.page, rules=rules, hotel_name="Hrebienok Resort"
                )
                self.assertTrue(result.is_available)
                self.assertEqual(result.status, ResultStatus.SUCCESS)
                self.assertEqual(result.direct_price, 126.0)
                self.assertEqual(len(result.direct_offers), 3)

                for offer, (expected_price, expected_bonuses) in zip(
                    result.direct_offers, expected_offers
                ):
                    self.assertEqual(offer.room_name, "Studio Komfort TATRY")
                    self.assertEqual(offer.price, expected_price)
                    self.assertEqual(offer.bonuses, expected_bonuses)
                    self.assertEqual(offer.meal_plan, expected_bonuses)

    def test_direct_parser_returns_not_available_when_calendar_date_disabled_or_missing(
        self,
    ) -> None:
        """Disabled or missing calendar date returns NOT_AVAILABLE with clear error_message."""
        check_in = date.today() + timedelta(days=5)
        check_out = check_in + timedelta(days=2)
        job = ScrapingJob(check_in=check_in, check_out=check_out, adults=2)

        html = f"""
        <html><body>
            <div class="booking-calendar">
                <button class="calendar-day disabled" data-date="{check_in.isoformat()}" disabled>Check-in</button>
                <button class="calendar-day" data-date="{check_out.isoformat()}">Check-out</button>
            </div>
        </body></html>
        """
        self.page.set_content(html)

        rules = {
            "calendar_day_selector": "button.calendar-day[data-date='{date}']",
            "room_card_selectors": [".room-card"],
            "price_selectors": [".room-price"],
        }

        result = self.parser.parse_direct_hotel(
            page=self.page,
            rules=rules,
            hotel_name="Hotel Permon",
            direct_url="https://booking.hotelpermon.sk",
            job=job,
        )

        self.assertFalse(result.is_available)
        self.assertEqual(result.status, ResultStatus.NOT_AVAILABLE)
        self.assertEqual(result.direct_offers, [])
        self.assertIsNone(result.direct_price)
        self.assertEqual(result.error_message, UNAVAILABLE_DATE_MESSAGE)

    def test_direct_parser_returns_not_available_when_token_api_fails_or_wait_times_out(
        self,
    ) -> None:
        """Failed createSearchToken API or timed-out wait_for_selector returns NOT_AVAILABLE."""
        check_in = date.today() + timedelta(days=5)
        check_out = check_in + timedelta(days=2)
        job = ScrapingJob(check_in=check_in, check_out=check_out, adults=2)

        # Case 1: token_api_post returns HTTP 422 / empty result
        mock_page = MagicMock()
        mock_page.url = "https://booking.hotelpermon.sk"
        mock_page.evaluate.return_value = {"ok": False, "status": 422, "text": ""}
        token_rules = {
            "method": "token_api_post",
            "token_endpoint": "https://booking.hotelpermon.sk/sk/home/createSearchToken",
            "search_url_template": "https://booking.hotelpermon.sk/sk/search/{token}",
            "form_payload": {"date_from": "{check_in}", "date_to": "{check_out}"},
            "room_card_selectors": [".room-card"],
            "price_selectors": [".room-price"],
        }
        token_res = self.parser.parse_direct_hotel(
            page=mock_page,
            rules=token_rules,
            hotel_name="Hotel Permon",
            direct_url="https://booking.hotelpermon.sk",
            job=job,
        )
        self.assertFalse(token_res.is_available)
        self.assertEqual(token_res.status, ResultStatus.NOT_AVAILABLE)
        self.assertEqual(token_res.direct_offers, [])
        self.assertIsNone(token_res.direct_price)
        self.assertEqual(token_res.error_message, UNAVAILABLE_DATE_MESSAGE)

        # Case 2: wait_for_selector times out without showing any room offers
        self.page.set_content("<html><body><div class='empty-results'></div></body></html>")
        timeout_rules = {
            "wait_for_selector": ".room-card",
            "wait_timeout_ms": 200,
            "wait_after_submit": 0,
            "room_card_selectors": [".room-card"],
            "price_selectors": [".room-price"],
        }
        timeout_res = self.parser.parse_direct_hotel(
            page=self.page,
            rules=timeout_rules,
            hotel_name="Hotel Permon",
            direct_url="https://booking.hotelpermon.sk",
            job=job,
        )
        self.assertFalse(timeout_res.is_available)
        self.assertEqual(timeout_res.status, ResultStatus.NOT_AVAILABLE)
        self.assertEqual(timeout_res.direct_offers, [])
        self.assertIsNone(timeout_res.direct_price)
        self.assertEqual(timeout_res.error_message, UNAVAILABLE_DATE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
