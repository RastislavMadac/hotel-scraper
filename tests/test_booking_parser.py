"""Unit and Playwright DOM tests for BookingParser occupancy and URL building."""

from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from urllib.parse import parse_qsl, urlparse

from core.models import ScrapingJob
from scraper.booking_parser import BookingParser
from scraper.browser import BrowserManager


class TestBookingParserChildrenAndOccupancy(unittest.TestCase):
    """Test suite for Booking.com child age URLs and adult+child occupancy parsing."""

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

    def test_build_booking_url_with_children_ages(self) -> None:
        """Verify build_booking_url appends repeated age=<n> query params for children."""
        check_in = date.today() + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        job = ScrapingJob(
            check_in=check_in,
            check_out=check_out,
            adults=2,
            children=2,
            children_ages=[1, 4],
        )

        base_url = "https://www.booking.com/hotel/sk/example.sk.html?aid=12345&age=9&group_adults=1"
        built_url = self.parser.build_booking_url(base_url, job)

        self.assertIn("age=1&age=4", built_url)
        query_pairs = parse_qsl(urlparse(built_url).query)
        self.assertIn(("aid", "12345"), query_pairs)
        self.assertIn(("checkin", check_in.strftime("%Y-%m-%d")), query_pairs)
        self.assertIn(("checkout", check_out.strftime("%Y-%m-%d")), query_pairs)
        self.assertIn(("group_adults", "2"), query_pairs)
        self.assertIn(("group_children", "2"), query_pairs)
        self.assertIn(("no_rooms", "1"), query_pairs)
        age_values = [val for key, val in query_pairs if key == "age"]
        self.assertEqual(age_values, ["1", "4"])

    def test_parse_hotel_page_filters_real_booking_html_adults_and_children(self) -> None:
        """Verify real Booking.com occupancy markup keeps only 2 adults + 2 children for 4 guests."""
        html = """
        <html>
            <body>
                <table class="hprt-table">
                    <tbody>
                        <tr data-block-id="room_2_adults">
                            <td class="hprt-table-cell-roomtype">
                                <a class="hprt-roomtype-link">Dvojlôžková izba Standard</a>
                            </td>
                            <td class="hprt-table-cell-occupancy">
                                <div class="hprt-occupancy-occupancy-info">
                                    <span class="c-occupancy-icons__adults">
                                        <i class="bicon bicon-occupancy"></i>
                                        <i class="bicon bicon-occupancy"></i>
                                    </span>
                                    <span class="bui-u-sr-only">Max. dospelých: 2</span>
                                </div>
                            </td>
                            <td>
                                <span class="bui-price-display__value">180,00&nbsp;€</span>
                            </td>
                        </tr>
                        <tr data-block-id="room_3_adults">
                            <td class="hprt-table-cell-roomtype">
                                <a class="hprt-roomtype-link">Trojlôžková izba Comfort</a>
                            </td>
                            <td class="hprt-table-cell-occupancy">
                                <div class="hprt-occupancy-occupancy-info">
                                    <span class="c-occupancy-icons__adults">
                                        <i class="bicon bicon-occupancy"></i>
                                        <i class="bicon bicon-occupancy"></i>
                                        <i class="bicon bicon-occupancy"></i>
                                    </span>
                                    <span class="bui-u-sr-only">Max. dospelých: 3</span>
                                </div>
                            </td>
                            <td>
                                <span class="bui-price-display__value">240,00&nbsp;€</span>
                            </td>
                        </tr>
                        <tr data-block-id="room_2_adults_2_children">
                            <td class="hprt-table-cell-roomtype">
                                <a class="hprt-roomtype-link">Rodinná izba Deluxe</a>
                            </td>
                            <td class="hprt-table-cell-occupancy">
                                <div class="hprt-occupancy-occupancy-info">
                                    <span class="c-occupancy-icons__adults">
                                        <i class="bicon bicon-occupancy"></i>
                                        <i class="bicon bicon-occupancy"></i>
                                    </span>
                                    <span class="c-occupancy-icons__children">
                                        <i class="bicon bicon-occupancychild"></i>
                                        <i class="bicon bicon-occupancychild"></i>
                                    </span>
                                    <span class="bui-u-sr-only">Max. dospelých: 2 &lt;br&gt; Max. detí: 2</span>
                                </div>
                            </td>
                            <td>
                                <span class="bui-price-display__value">320,00&nbsp;€</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html)

        check_in = date.today() + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        job = ScrapingJob(
            check_in=check_in,
            check_out=check_out,
            adults=2,
            children=2,
            children_ages=[1, 4],
        )

        # Call via requested_guests=4
        res_by_count = self.parser.parse_hotel_page(
            page=self.page,
            rules={},
            hotel_name="Hotel Family Booking Test",
            requested_guests=4,
        )
        self.assertTrue(res_by_count.is_available)
        self.assertEqual(res_by_count.booking_price, 320.0)
        self.assertEqual(len(res_by_count.offers), 1)
        self.assertEqual(res_by_count.offers[0].room_name, "Rodinná izba Deluxe")
        self.assertEqual(res_by_count.offers[0].occupancy, 4)

        # Call via job (total_guests == 4)
        res_by_job = self.parser.parse_hotel_page(
            page=self.page,
            rules={},
            hotel_name="Hotel Family Booking Test",
            requested_guests=job.adults,
            job=job,
        )
        self.assertTrue(res_by_job.is_available)
        self.assertEqual(res_by_job.booking_price, 320.0)
        self.assertEqual(len(res_by_job.offers), 1)
        self.assertEqual(res_by_job.offers[0].room_name, "Rodinná izba Deluxe")
        self.assertEqual(res_by_job.offers[0].occupancy, 4)

    def test_extract_occupancy_combined_icons_fallback(self) -> None:
        """Verify _extract_occupancy sums adult and child icons when sr-only text is absent."""
        html = """
        <html>
            <body>
                <table class="hprt-table">
                    <tbody>
                        <tr id="row-adults-and-children">
                            <td class="hprt-table-cell-occupancy">
                                <span class="c-occupancy-icons__adults">
                                    <i class="bicon bicon-occupancy"></i>
                                    <i class="bicon bicon-occupancy"></i>
                                </span>
                                <span class="c-occupancy-icons__children">
                                    <i class="bicon bicon-occupancychild"></i>
                                    <i class="bicon bicon-occupancychild"></i>
                                </span>
                            </td>
                        </tr>
                        <tr id="row-adults-only">
                            <td class="hprt-table-cell-occupancy">
                                <span class="c-occupancy-icons__adults">
                                    <i class="bicon bicon-occupancy"></i>
                                    <i class="bicon bicon-occupancy"></i>
                                </span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        self.page.set_content(html)

        occ_selectors = ["td.hprt-table-cell-occupancy"]
        icon_selectors = [".bicon-occupancy", "i.bicon-occupancy", "span.bicon-occupancy"]

        row_4 = self.page.locator("#row-adults-and-children")
        row_2 = self.page.locator("#row-adults-only")

        self.assertEqual(
            self.parser._extract_occupancy(row_4, occ_selectors, icon_selectors),
            4,
        )
        self.assertEqual(
            self.parser._extract_occupancy(row_2, occ_selectors, icon_selectors),
            2,
        )


if __name__ == "__main__":
    unittest.main()

