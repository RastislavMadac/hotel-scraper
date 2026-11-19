"""Unit tests for core models (ScrapingJob and ScrapedResult)."""

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import unittest

from core.models import EMAIL_REGEX, ResultStatus, RoomOffer, ScrapedResult, ScrapingJob


class TestScrapingJob(unittest.TestCase):
    """Test suite for ScrapingJob model and validations."""

    def setUp(self) -> None:
        self.today = date.today()
        self.tomorrow = self.today + timedelta(days=1)
        self.next_week = self.today + timedelta(days=7)

    def test_valid_job_creation(self) -> None:
        """Verify successful initialization with valid attributes."""
        job = ScrapingJob(
            check_in=self.tomorrow,
            check_out=self.next_week,
            adults=2,
            children=1,
            email="test.user@example.com",
        )
        self.assertEqual(job.check_in, self.tomorrow)
        self.assertEqual(job.check_out, self.next_week)
        self.assertEqual(job.adults, 2)
        self.assertEqual(job.children, 1)
        self.assertEqual(job.email, "test.user@example.com")
        self.assertEqual(job.nights, 6)
        self.assertEqual(job.total_guests, 3)
        self.assertTrue(job.job_id)

    def test_immutability(self) -> None:
        """Verify that ScrapingJob is strictly immutable."""
        job = ScrapingJob(
            check_in=self.tomorrow,
            check_out=self.next_week,
        )
        with self.assertRaises(FrozenInstanceError):
            job.adults = 4  # type: ignore[misc]

    def test_past_check_in_rejected(self) -> None:
        """Check-in date in the past must raise ValueError."""
        yesterday = self.today - timedelta(days=1)
        with self.assertRaises(ValueError) as ctx:
            ScrapingJob(
                check_in=yesterday,
                check_out=self.tomorrow,
            )
        self.assertIn("cannot be in the past", str(ctx.exception))

    def test_check_out_not_after_check_in_rejected(self) -> None:
        """Check-out equal to or before check-in must raise ValueError."""
        with self.assertRaises(ValueError):
            ScrapingJob(
                check_in=self.tomorrow,
                check_out=self.tomorrow,
            )

        with self.assertRaises(ValueError):
            ScrapingJob(
                check_in=self.next_week,
                check_out=self.tomorrow,
            )

    def test_invalid_date_types_rejected(self) -> None:
        """Non-date types must raise TypeError."""
        with self.assertRaises(TypeError):
            ScrapingJob(
                check_in="2026-12-01",  # type: ignore[arg-type]
                check_out=self.next_week,
            )

    def test_adults_validation(self) -> None:
        """Adult count must be integer >= 1 and <= 50."""
        # Zero adults
        with self.assertRaises(ValueError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, adults=0)

        # Negative adults
        with self.assertRaises(ValueError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, adults=-1)

        # Exceeding limit
        with self.assertRaises(ValueError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, adults=51)

        # Non-int type (boolean or float)
        with self.assertRaises(TypeError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, adults=True)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, adults=2.5)  # type: ignore[arg-type]

    def test_children_validation(self) -> None:
        """Children count must be integer >= 0 and <= 50."""
        # Negative children
        with self.assertRaises(ValueError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, children=-1)

        # Exceeding limit
        with self.assertRaises(ValueError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, children=51)

        # Non-int type
        with self.assertRaises(TypeError):
            ScrapingJob(check_in=self.tomorrow, check_out=self.next_week, children=False)  # type: ignore[arg-type]

    def test_email_validation(self) -> None:
        """Validate email handling: valid emails pass, invalid raise ValueError/TypeError."""
        # Valid variations
        valid_emails = [
            "user@domain.com",
            "first.last+tag@sub.domain.co.uk",
            "admin_123@hotel-booking.org",
        ]
        for email in valid_emails:
            job = ScrapingJob(
                check_in=self.tomorrow,
                check_out=self.next_week,
                email=email,
            )
            self.assertEqual(job.email, email)

        # Invalid emails
        invalid_emails = [
            "not-an-email",
            "@missing-local.com",
            "missing-domain@",
            "spaces in@domain.com",
            "",
            "   ",
        ]
        for email in invalid_emails:
            with self.subTest(email=email):
                with self.assertRaises(ValueError):
                    ScrapingJob(
                        check_in=self.tomorrow,
                        check_out=self.next_week,
                        email=email,
                    )

        # Non-string email
        with self.assertRaises(TypeError):
            ScrapingJob(
                check_in=self.tomorrow,
                check_out=self.next_week,
                email=12345,  # type: ignore[arg-type]
            )


class TestScrapedResult(unittest.TestCase):
    """Test suite for ScrapedResult model, validations, and calculations."""

    def setUp(self) -> None:
        self.today = date.today()
        self.check_in = self.today + timedelta(days=2)
        self.check_out = self.today + timedelta(days=4)

    def test_valid_scraped_result_creation(self) -> None:
        """Verify proper initialization and default attributes."""
        res = ScrapedResult(
            hotel_id="grand_hotel_01",
            hotel_name="Grand Hotel Kempinski",
            check_in=self.check_in,
            check_out=self.check_out,
            booking_price=420.0,
            direct_price=380.0,
            currency="EUR",
        )
        self.assertEqual(res.hotel_id, "grand_hotel_01")
        self.assertEqual(res.status, ResultStatus.SUCCESS)
        self.assertEqual(res.price_delta, -40.0)
        self.assertEqual(res.price_delta_percent, -9.52)
        self.assertEqual(res.cheaper_channel, "DIRECT")

    def test_immutability(self) -> None:
        """Verify ScrapedResult is immutable."""
        res = ScrapedResult(
            hotel_id="h1",
            hotel_name="Test Hotel",
            check_in=self.check_in,
            check_out=self.check_out,
        )
        with self.assertRaises(FrozenInstanceError):
            res.booking_price = 100.0  # type: ignore[misc]

    def test_price_delta_calculations(self) -> None:
        """Verify price_delta and cheaper_channel edge cases."""
        # Direct cheaper
        res1 = ScrapedResult(
            hotel_id="h1",
            hotel_name="Hotel A",
            check_in=self.check_in,
            check_out=self.check_out,
            booking_price=200.0,
            direct_price=180.0,
        )
        self.assertEqual(res1.price_delta, -20.0)
        self.assertEqual(res1.cheaper_channel, "DIRECT")

        # Booking cheaper
        res2 = ScrapedResult(
            hotel_id="h2",
            hotel_name="Hotel B",
            check_in=self.check_in,
            check_out=self.check_out,
            booking_price=150.0,
            direct_price=175.50,
        )
        self.assertEqual(res2.price_delta, 25.50)
        self.assertEqual(res2.cheaper_channel, "BOOKING")

        # Equal prices
        res3 = ScrapedResult(
            hotel_id="h3",
            hotel_name="Hotel C",
            check_in=self.check_in,
            check_out=self.check_out,
            booking_price=100.0,
            direct_price=100.0,
        )
        self.assertEqual(res3.price_delta, 0.0)
        self.assertEqual(res3.cheaper_channel, "EQUAL")

        # Missing price in one channel
        res4 = ScrapedResult(
            hotel_id="h4",
            hotel_name="Hotel D",
            check_in=self.check_in,
            check_out=self.check_out,
            booking_price=100.0,
            direct_price=None,
        )
        self.assertIsNone(res4.price_delta)
        self.assertIsNone(res4.price_delta_percent)
        self.assertIsNone(res4.cheaper_channel)

    def test_negative_prices_rejected(self) -> None:
        """Negative prices must raise ValueError."""
        with self.assertRaises(ValueError):
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Hotel A",
                check_in=self.check_in,
                check_out=self.check_out,
                booking_price=-10.0,
            )

        with self.assertRaises(ValueError):
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Hotel A",
                check_in=self.check_in,
                check_out=self.check_out,
                direct_price=-5.0,
            )

    def test_status_normalization_and_validation(self) -> None:
        """Ensure string status is coerced to ResultStatus or rejected if invalid."""
        res = ScrapedResult(
            hotel_id="h1",
            hotel_name="Hotel A",
            check_in=self.check_in,
            check_out=self.check_out,
            status="PARTIAL",  # type: ignore[arg-type]
        )
        self.assertEqual(res.status, ResultStatus.PARTIAL)

        with self.assertRaises(ValueError):
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Hotel A",
                check_in=self.check_in,
                check_out=self.check_out,
                status="UNKNOWN_STATUS",  # type: ignore[arg-type]
            )

    def test_offers_field_and_lowest_price_calculation(self) -> None:
        """Verify that offers list is stored and auto-populates booking_price if omitted."""
        offer1 = RoomOffer(room_name="Izba Deluxe", price=250.0, meal_plan="Raňajky v cene")
        offer2 = RoomOffer(room_name="Štandardná izba", price=180.0, cancellation_policy="Bezplatné storno")

        res = ScrapedResult(
            hotel_id="h1",
            hotel_name="Grand Hotel",
            offers=[offer1, offer2],
        )

        self.assertEqual(len(res.offers), 2)
        self.assertEqual(res.booking_price, 180.0)
        self.assertEqual(res.offers[0].room_name, "Izba Deluxe")
        self.assertEqual(res.offers[1].cancellation_policy, "Bezplatné storno")

    def test_invalid_offers_type_rejected(self) -> None:
        """Non-list or lists containing non-RoomOffer objects must raise TypeError."""
        with self.assertRaises(TypeError):
            ScrapedResult(
                hotel_name="Hotel X",
                offers="not-a-list",  # type: ignore[arg-type]
            )

        with self.assertRaises(TypeError):
            ScrapedResult(
                hotel_name="Hotel X",
                offers=[{"room": "deluxe", "price": 100}],  # type: ignore[list-item]
            )


class TestRoomOffer(unittest.TestCase):
    """Test suite for RoomOffer dataclass."""

    def test_valid_room_offer_creation(self) -> None:
        """Valid room offer should instantiate properly."""
        offer = RoomOffer(
            room_name="Apartmán s výhľadom na jazero",
            occupancy=3,
            price=299.99,
            currency="EUR",
            meal_plan="Polpenzia",
            cancellation_policy="Storno do 24h pred príchodom",
        )
        self.assertEqual(offer.room_name, "Apartmán s výhľadom na jazero")
        self.assertEqual(offer.occupancy, 3)
        self.assertEqual(offer.price, 299.99)
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(offer.meal_plan, "Polpenzia")
        self.assertEqual(offer.cancellation_policy, "Storno do 24h pred príchodom")

    def test_room_offer_validation(self) -> None:
        """Invalid room_name, occupancy, or negative price must raise errors."""
        # Empty name
        with self.assertRaises(ValueError):
            RoomOffer(room_name="", price=100.0)

        with self.assertRaises(ValueError):
            RoomOffer(room_name="   ", price=100.0)

        # Invalid occupancy
        with self.assertRaises(ValueError):
            RoomOffer(room_name="Izba", occupancy=0, price=100.0)

        with self.assertRaises(TypeError):
            RoomOffer(room_name="Izba", occupancy="two", price=100.0)  # type: ignore[arg-type]

        # Negative price
        with self.assertRaises(ValueError):
            RoomOffer(room_name="Izba", price=-50.0)

        # Non-numeric price
        with self.assertRaises(TypeError):
            RoomOffer(room_name="Izba", price="100")  # type: ignore[arg-type]

        # Invalid meal_plan type
        with self.assertRaises(TypeError):
            RoomOffer(room_name="Izba", price=100.0, meal_plan=123)  # type: ignore[arg-type]

    def test_room_offer_immutability(self) -> None:
        """RoomOffer must be strictly immutable."""
        offer = RoomOffer(room_name="Izba", price=100.0)
        with self.assertRaises(FrozenInstanceError):
            offer.price = 120.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

