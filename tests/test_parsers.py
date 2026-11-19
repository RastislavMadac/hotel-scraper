"""Unit and integration tests for Booking.com dynamic parser."""

from pathlib import Path
import shutil
import tempfile
import unittest

from core.models import ResultStatus
from scraper.booking_parser import BookingParser, clean_price_text
from scraper.browser import BrowserManager


class TestPriceCleaner(unittest.TestCase):
    """Test suite for price cleaning and normalization helper."""

    def test_clean_price_formats(self) -> None:
        """Verify handling of diverse European and international price formats."""
        cases = [
            ("120 €", None, 120.0),
            ("€ 149.99", None, 149.99),
            ("1 250,50 €", None, 1250.50),
            ("1.450,00 EUR", None, 1450.0),
            ("1,450.00 EUR", None, 1450.0),
            ("EUR 89", None, 89.0),
            ("Price: 320,5 €", r"(\d+(?:[.,]\d+)?)", 320.5),
            ("Total: 500 € for 2 nights", r"(\d+)\s*€", 500.0),
        ]
        for raw, regex, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(clean_price_text(raw, price_regex=regex), expected)

    def test_invalid_price_returns_none(self) -> None:
        """Non-numeric strings or empty strings must return None."""
        self.assertIsNone(clean_price_text(""))
        self.assertIsNone(clean_price_text("Sold out"))
        self.assertIsNone(clean_price_text(None))  # type: ignore[arg-type]


class TestBookingParser(unittest.TestCase):
    """Integration test suite for BookingParser using Playwright headless context."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser_manager = BrowserManager(headless=True)
        cls.browser_manager.launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser_manager.close()

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.screenshots_dir = Path(self.test_dir) / "screenshots"
        self.parser = BookingParser(screenshots_dir=self.screenshots_dir)
        self.page = self.browser_manager.new_page()

    def tearDown(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_extract_price_success(self) -> None:
        """Verify successful price extraction when valid price element is present."""
        html = """
        <html>
            <body>
                <div class="hotel-card">
                    <h2>Grand Hotel River Park</h2>
                    <div class="bui-price-display__value">185,50&nbsp;€</div>
                </div>
            </body>
        </html>
        """
        self.page.set_content(html)

        rules = {
            "sold_out_selectors": [".sold-out-banner", ".bui-alert--error"],
            "price_selectors": [
                ".non-existent-selector",
                ".bui-price-display__value",
            ],
            "price_regex": r"([\d\s.,]+)",
        }

        result = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Grand Hotel River Park",
        )

        self.assertEqual(result.hotel_name, "Grand Hotel River Park")
        self.assertTrue(result.is_available)
        self.assertEqual(result.booking_price, 185.50)
        self.assertIsNone(result.error_message)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(len(result.offers), 1)

    def test_extract_multiple_room_offers_success(self) -> None:
        """Verify extraction of multiple room offers with detailed meal plans and cancellation policies."""
        html = """
        <html>
            <body>
                <h2>Grand Hotel Kempinski High Tatras</h2>
                <table class="hprt-table">
                    <tbody>
                        <tr class="js-rt-block-row">
                            <td>
                                <a class="hprt-roomtype-link">Štandardná dvojlôžková izba</a>
                            </td>
                            <td>
                                <div class="hprt-condition-detail">Raňajky v cene</div>
                                <div class="hprt-cancellation-policy">Bezplatné storno</div>
                            </td>
                            <td>
                                <span class="bui-price-display__value">160,00&nbsp;€</span>
                            </td>
                        </tr>
                        <tr class="js-rt-block-row">
                            <td>
                                <a class="hprt-roomtype-link">Apartmán Deluxe</a>
                            </td>
                            <td>
                                <div class="hprt-condition-detail">Polpenzia</div>
                                <div class="hprt-cancellation-policy">Nevratná platba</div>
                            </td>
                            <td>
                                <span class="bui-price-display__value">280,00&nbsp;€</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html)

        rules = {
            "room_row_selectors": ["table.hprt-table tbody tr.js-rt-block-row"],
            "room_name_selectors": [".hprt-roomtype-link"],
            "price_selectors": [".bui-price-display__value"],
            "meal_plan_selectors": [".hprt-condition-detail"],
            "cancellation_selectors": [".hprt-cancellation-policy"],
            "price_regex": r"([\d\s.,]+)",
        }

        result = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Grand Hotel Kempinski High Tatras",
        )

        self.assertTrue(result.is_available)
        self.assertEqual(result.booking_price, 160.0)
        self.assertEqual(len(result.offers), 2)

        offer1 = result.offers[0]
        self.assertEqual(offer1.room_name, "Štandardná dvojlôžková izba")
        self.assertEqual(offer1.price, 160.0)
        self.assertEqual(offer1.meal_plan, "Raňajky v cene")
        self.assertEqual(offer1.cancellation_policy, "Bezplatné storno")

        offer2 = result.offers[1]
        self.assertEqual(offer2.room_name, "Apartmán Deluxe")
        self.assertEqual(offer2.price, 280.0)
        self.assertEqual(offer2.meal_plan, "Polpenzia")
        self.assertEqual(offer2.cancellation_policy, "Nevratná platba")

    def test_extract_room_offers_filters_by_requested_guests(self) -> None:
        """Verify strict capacity filtering: offers for 1 or 4 guests are excluded when 2 guests requested."""
        html = """
        <html>
            <body>
                <h2>Hotel Grand Capacity Test</h2>
                <table class="hprt-table">
                    <tbody>
                        <!-- 1 Guest Room -->
                        <tr class="js-rt-block-row">
                            <td>
                                <a class="hprt-roomtype-link">Jednolôžková izba Economy</a>
                            </td>
                            <td class="hprt-table-cell-occupancy">
                                <span class="bicon-occupancy" title="1 hosť"></span>
                            </td>
                            <td>
                                <span class="bui-price-display__value">90,00&nbsp;€</span>
                            </td>
                        </tr>
                        <!-- 2 Guests Room: Base rate -->
                        <tr class="js-rt-block-row">
                            <td>
                                <a class="hprt-roomtype-link">Dvojlôžková izba Standard</a>
                            </td>
                            <td class="hprt-table-cell-occupancy">
                                <span class="bicon-occupancy"></span>
                                <span class="bicon-occupancy"></span>
                            </td>
                            <td>
                                <span class="bui-price-display__value">140,00&nbsp;€</span>
                            </td>
                        </tr>
                        <!-- 2 Guests Room: Rate with breakfast (inherits room name and occupancy) -->
                        <tr class="js-rt-block-row">
                            <td></td>
                            <td></td>
                            <td>
                                <div class="hprt-condition-detail">Raňajky v cene</div>
                                <span class="bui-price-display__value">165,00&nbsp;€</span>
                            </td>
                        </tr>
                        <!-- 4 Guests Room: Family suite -->
                        <tr class="js-rt-block-row">
                            <td>
                                <a class="hprt-roomtype-link">Rodinný Apartmán</a>
                            </td>
                            <td class="hprt-table-cell-occupancy" aria-label="Max. počet hostí: 4">
                                <span class="bicon-occupancy"></span>
                                <span class="bicon-occupancy"></span>
                                <span class="bicon-occupancy"></span>
                                <span class="bicon-occupancy"></span>
                            </td>
                            <td>
                                <span class="bui-price-display__value">310,00&nbsp;€</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html)

        rules = {
            "room_row_selectors": ["table.hprt-table tbody tr.js-rt-block-row"],
            "occupancy_selectors": ["td.hprt-table-cell-occupancy"],
            "occupancy_icon_selectors": [".bicon-occupancy"],
            "room_name_selectors": [".hprt-roomtype-link"],
            "price_selectors": [".bui-price-display__value"],
            "meal_plan_selectors": [".hprt-condition-detail"],
            "price_regex": r"([\d\s.,]+)",
        }

        # 1. Test requested_guests = 2
        result_2 = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Hotel Grand Capacity Test",
            requested_guests=2,
        )
        self.assertTrue(result_2.is_available)
        self.assertEqual(len(result_2.offers), 2)
        self.assertEqual(result_2.booking_price, 140.0)
        self.assertEqual(result_2.offers[0].occupancy, 2)
        self.assertEqual(result_2.offers[0].price, 140.0)
        self.assertEqual(result_2.offers[1].occupancy, 2)
        self.assertEqual(result_2.offers[1].price, 165.0)
        self.assertEqual(result_2.offers[1].room_name, "Dvojlôžková izba Standard")
        self.assertEqual(result_2.offers[1].meal_plan, "Raňajky v cene")

        # 2. Test requested_guests = 4
        result_4 = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Hotel Grand Capacity Test",
            requested_guests=4,
        )
        self.assertTrue(result_4.is_available)
        self.assertEqual(len(result_4.offers), 1)
        self.assertEqual(result_4.booking_price, 310.0)
        self.assertEqual(result_4.offers[0].room_name, "Rodinný Apartmán")
        self.assertEqual(result_4.offers[0].occupancy, 4)

        # 3. Test requested_guests = 5 (No offers match)
        result_5 = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Hotel Grand Capacity Test",
            requested_guests=5,
        )
        self.assertFalse(result_5.is_available)
        self.assertIsNone(result_5.booking_price)
        self.assertEqual(len(result_5.offers), 0)
        self.assertIn("Cena ani stav nedostupnosti neboli identifikované", result_5.error_message or "")
        self.assertIn("5", result_5.error_message or "")

    def test_detect_sold_out_banner(self) -> None:
        """Verify sold out detection returns is_available=False and booking_price=None."""
        html = """
        <html>
            <body>
                <div class="hotel-header">
                    <h2>Boutique Hotel Marrol's</h2>
                    <div class="sold-out-alert">Žiaľ, v tomto ubytovaní nie sú voľné izby</div>
                    <div class="hidden-old-price" style="display:none">120 €</div>
                </div>
            </body>
        </html>
        """
        self.page.set_content(html)

        rules = {
            "sold_out_selectors": [".sold-out-alert", ".no-availability-banner"],
            "price_selectors": [".hidden-old-price"],
        }

        result = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Boutique Hotel Marrol's",
        )

        self.assertEqual(result.hotel_name, "Boutique Hotel Marrol's")
        self.assertFalse(result.is_available)
        self.assertIsNone(result.booking_price)
        self.assertIsNone(result.error_message)
        self.assertEqual(result.status, ResultStatus.NOT_AVAILABLE)

    def test_fallback_when_price_and_sold_out_missing(self) -> None:
        """When neither price nor sold-out is identified, save screenshot and return warning error."""
        html = """
        <html>
            <body>
                <div class="unknown-layout">
                    <h2>Unknown Layout Hotel</h2>
                    <p>Some unrelated text without pricing or availability</p>
                </div>
            </body>
        </html>
        """
        self.page.set_content(html)

        rules = {
            "sold_out_selectors": [".sold-out-alert"],
            "price_selectors": [".price-tag"],
        }

        result = self.parser.parse_hotel_page(
            page=self.page,
            rules=rules,
            hotel_name="Unknown Layout Hotel",
        )

        self.assertEqual(result.hotel_name, "Unknown Layout Hotel")
        self.assertFalse(result.is_available)
        self.assertIsNone(result.booking_price)
        self.assertIn("Cena ani stav nedostupnosti neboli identifikované", result.error_message or "")
        self.assertEqual(result.status, ResultStatus.FAILED)

        # Verify diagnostic screenshot was created
        saved_screenshots = list(self.screenshots_dir.glob("booking_unknown_layout_hotel_*.png"))
        self.assertGreaterEqual(len(saved_screenshots), 1)

    def test_booking_discards_price_without_room_name_or_dash_as_not_available(self) -> None:
        """Prices in table rows or calendar widgets without a valid room name must yield NOT_AVAILABLE."""
        # Case 1: Table rows have prices (296,00 €) but missing or '—' room names
        html_table = """
        <html>
            <body>
                <table class="hprt-table">
                    <tbody>
                        <tr class="js-rt-block-row">
                            <td><a class="hprt-roomtype-link">—</a></td>
                            <td><span class="bui-price-display__value">296,00&nbsp;€</span></td>
                        </tr>
                        <tr class="js-rt-block-row">
                            <td></td>
                            <td><span class="bui-price-display__value">310,00&nbsp;€</span></td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html_table)
        rules = {
            "room_row_selectors": ["table.hprt-table tbody tr.js-rt-block-row"],
            "room_name_selectors": [".hprt-roomtype-link"],
            "price_selectors": [".bui-price-display__value"],
            "price_regex": r"([\d\s.,]+)",
        }

        res_table = self.parser.parse_hotel_page(
            page=self.page, rules=rules, hotel_name="Nameless Table Hotel"
        )
        self.assertFalse(res_table.is_available)
        self.assertEqual(res_table.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(res_table.booking_price)
        self.assertEqual(res_table.offers, [])

        # Case 2: Non-table calendar price without any room name element
        html_calendar = """
        <html>
            <body>
                <div class="calendar-widget">
                    <span class="bui-price-display__value">296,00&nbsp;€</span>
                </div>
            </body>
        </html>
        """
        self.page.set_content(html_calendar)
        res_calendar = self.parser.parse_hotel_page(
            page=self.page, rules=rules, hotel_name="Calendar Price Hotel"
        )
        self.assertFalse(res_calendar.is_available)
        self.assertEqual(res_calendar.status, ResultStatus.NOT_AVAILABLE)
        self.assertIsNone(res_calendar.booking_price)
        self.assertEqual(res_calendar.offers, [])

    def test_booking_discards_dash_row_while_keeping_valid_room_row(self) -> None:
        """A nameless/dash row is discarded without suppressing valid room offers."""
        html = """
        <html>
            <body>
                <table class="hprt-table">
                    <tbody>
                        <tr class="js-rt-block-row">
                            <td><a class="hprt-roomtype-link">—</a></td>
                            <td><span class="bui-price-display__value">296,00&nbsp;€</span></td>
                        </tr>
                        <tr class="js-rt-block-row">
                            <td><a class="hprt-roomtype-link">Dvojlôžková izba Deluxe</a></td>
                            <td><span class="bui-price-display__value">350,00&nbsp;€</span></td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html)
        rules = {
            "room_row_selectors": ["table.hprt-table tbody tr.js-rt-block-row"],
            "room_name_selectors": [".hprt-roomtype-link"],
            "price_selectors": [".bui-price-display__value"],
            "price_regex": r"([\d\s.,]+)",
        }

        result = self.parser.parse_hotel_page(
            page=self.page, rules=rules, hotel_name="Mixed Rows Hotel"
        )
        self.assertTrue(result.is_available)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.booking_price, 350.0)
        self.assertEqual(len(result.offers), 1)
        self.assertEqual(result.offers[0].room_name, "Dvojlôžková izba Deluxe")


if __name__ == "__main__":
    unittest.main()

