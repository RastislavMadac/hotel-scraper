"""Data models for the hotel price scraper."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import re
from typing import List, Optional
import uuid

# RFC 5322 compliant simplified email regex
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$"
)


class ResultStatus(str, Enum):
    """Execution status for an individual hotel scrape."""
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ScrapingJob:
    """Immutable job specification defining parameters for a scrape run.

    Attributes:
        check_in: Target arrival date (must be today or future).
        check_out: Target departure date (must be strictly after check_in).
        adults: Number of adult guests (minimum 1).
        children: Number of child guests (minimum 0).
        children_ages: Explicit list of ages for each child (e.g. [6, 10]).
        email: Optional recipient notification email address.
        hotel_identifiers: Optional list of hotel IDs or names to filter execution.
        job_id: Unique identifier for tracking job state.
        created_at: UTC timestamp when the job was instantiated.
    """

    check_in: date
    check_out: date
    adults: int = 2
    children: int = 0
    children_ages: List[int] = field(default_factory=list)
    email: Optional[str] = None
    hotel_identifiers: Optional[List[str]] = None
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        """Perform strict defensive validation on all input fields."""
        if not isinstance(self.check_in, date) or isinstance(self.check_in, datetime):
            raise TypeError("check_in must be a datetime.date instance.")

        if not isinstance(self.check_out, date) or isinstance(self.check_out, datetime):
            raise TypeError("check_out must be a datetime.date instance.")

        today = date.today()
        if self.check_in < today:
            raise ValueError(
                f"check_in date ({self.check_in}) cannot be in the past (today is {today})."
            )

        if self.check_out <= self.check_in:
            raise ValueError(
                f"check_out date ({self.check_out}) must be strictly after check_in date ({self.check_in})."
            )

        # Validate adults count
        if not isinstance(self.adults, int) or isinstance(self.adults, bool):
            raise TypeError("adults count must be an integer.")
        if self.adults < 1:
            raise ValueError(f"adults count must be at least 1 (got {self.adults}).")
        if self.adults > 50:
            raise ValueError(f"adults count exceeds maximum limit of 50 (got {self.adults}).")

        # Validate children count and children_ages
        if not isinstance(self.children, int) or isinstance(self.children, bool):
            raise TypeError("children count must be an integer.")
        if self.children < 0:
            raise ValueError(f"children count cannot be negative (got {self.children}).")
        if self.children > 50:
            raise ValueError(f"children count exceeds maximum limit of 50 (got {self.children}).")

        if not isinstance(self.children_ages, (list, tuple)):
            raise TypeError("children_ages must be a list or tuple of integers.")

        for age in self.children_ages:
            if not isinstance(age, int) or isinstance(age, bool):
                raise TypeError(f"All child ages must be integers (got {type(age)}).")
            if age < 0 or age > 17:
                raise ValueError(f"Child age must be between 0 and 17 (got {age}).")

        # Keep children and children_ages synchronized
        if self.children_ages and self.children != len(self.children_ages):
            object.__setattr__(self, "children", len(self.children_ages))

        # Validate email if provided
        if self.email is not None:
            if not isinstance(self.email, str):
                raise TypeError("email must be a string.")
            normalized_email = self.email.strip()
            if not normalized_email:
                raise ValueError("email cannot be empty or whitespace only.")
            if not EMAIL_REGEX.match(normalized_email):
                raise ValueError(f"Invalid email address format: '{self.email}'.")

    @property
    def nights(self) -> int:
        """Return the number of nights for this stay."""
        return (self.check_out - self.check_in).days

    @property
    def total_guests(self) -> int:
        """Return total number of guests."""
        return self.adults + len(self.children_ages) if self.children_ages else (self.adults + self.children)


@dataclass(frozen=True)
class RoomOffer:
    """Individual room offer / price option for a hotel.

    Attributes:
        room_name: Name or description of room category.
        occupancy: Number of guests accommodated (capacity).
        price: Total price for the room offer.
        currency: ISO 4217 currency code (default: EUR).
        meal_plan: Optional meal specification (e.g. 'Raňajky v cene').
        cancellation_policy: Optional cancellation terms (e.g. 'Bezplatné storno').
        bed_info: Optional bed configuration description.
        capacity_group: Optional normalized capacity group (e.g. '2_pax', 'suite').
        matched_room_name: Optional paired room name from the opposite channel.
        matched_direct_price: Optional paired direct website price for this room.
        match_score: Optional matching confidence score (0-100).
        bonuses: Optional extracted bonus/offer details for this room offer.
        matched_direct_bonuses: Optional paired direct website bonuses for this room.
    """

    room_name: str
    occupancy: int = 2
    price: float = 0.0
    currency: str = "EUR"
    meal_plan: Optional[str] = None
    cancellation_policy: Optional[str] = None
    bed_info: Optional[str] = None
    capacity_group: Optional[str] = None
    matched_room_name: Optional[str] = None
    matched_direct_price: Optional[float] = None
    match_score: Optional[int] = None
    bonuses: Optional[str] = None
    matched_direct_bonuses: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate room offer attributes."""
        if not isinstance(self.room_name, str) or not self.room_name.strip():
            raise ValueError("room_name must be a non-empty string.")

        if not isinstance(self.occupancy, int) or isinstance(self.occupancy, bool):
            raise TypeError("occupancy must be an integer.")
        if self.occupancy < 1:
            raise ValueError(f"occupancy must be at least 1 (got {self.occupancy}).")

        if not isinstance(self.price, (int, float)) or isinstance(self.price, bool):
            raise TypeError("price must be a numeric value.")
        if self.price < 0.0:
            raise ValueError(f"price cannot be negative (got {self.price}).")

        if not isinstance(self.currency, str) or len(self.currency.strip()) < 3:
            raise ValueError(f"currency must be a valid 3-letter code (got '{self.currency}').")

        if self.meal_plan is not None and not isinstance(self.meal_plan, str):
            raise TypeError("meal_plan must be a string or None.")

        if self.cancellation_policy is not None and not isinstance(self.cancellation_policy, str):
            raise TypeError("cancellation_policy must be a string or None.")

        if self.bed_info is not None and not isinstance(self.bed_info, str):
            raise TypeError("bed_info must be a string or None.")

        if self.capacity_group is not None and not isinstance(self.capacity_group, str):
            raise TypeError("capacity_group must be a string or None.")

        if self.matched_room_name is not None and not isinstance(self.matched_room_name, str):
            raise TypeError("matched_room_name must be a string or None.")

        if self.matched_direct_price is not None:
            if not isinstance(self.matched_direct_price, (int, float)) or isinstance(self.matched_direct_price, bool):
                raise TypeError("matched_direct_price must be a numeric value or None.")
            if self.matched_direct_price < 0.0:
                raise ValueError(
                    f"matched_direct_price cannot be negative (got {self.matched_direct_price})."
                )

        if self.match_score is not None:
            if not isinstance(self.match_score, int) or isinstance(self.match_score, bool):
                raise TypeError("match_score must be an integer or None.")

        if self.bonuses is not None and not isinstance(self.bonuses, str):
            raise TypeError("bonuses must be a string or None.")

        if self.matched_direct_bonuses is not None and not isinstance(
            self.matched_direct_bonuses, str
        ):
            raise TypeError("matched_direct_bonuses must be a string or None.")


@dataclass(frozen=True)
class ScrapedResult:
    """Immutable data model representing the scraped pricing for an individual hotel.

    Attributes:
        hotel_name: Display name of the hotel.
        hotel_id: Identifier of the target hotel (from remote config or slug).
        check_in: Optional arrival date matching the scraping job.
        check_out: Optional departure date matching the scraping job.
        booking_price: Final total lowest price from Booking.com.
        direct_price: Final total lowest price from direct hotel website.
        currency: ISO 4217 currency code (default: EUR).
        booking_url: Resolved Booking.com search/offer URL.
        direct_url: Resolved direct hotel booking URL.
        status: Scraping outcome (SUCCESS, PARTIAL, NOT_AVAILABLE, FAILED).
        is_available: Flag indicating whether hotel has availability.
        error_message: Optional description of extraction or network failure.
        offers: List of parsed room offers with prices and details.
        direct_offers: List of parsed room offers from the direct hotel website.
        scraped_at: UTC timestamp when the extraction finished.
    """

    hotel_name: str
    hotel_id: str = ""
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    booking_price: Optional[float] = None
    direct_price: Optional[float] = None
    currency: str = "EUR"
    booking_url: Optional[str] = None
    direct_url: Optional[str] = None
    status: ResultStatus = ResultStatus.SUCCESS
    is_available: bool = True
    error_message: Optional[str] = None
    offers: List[RoomOffer] = field(default_factory=list)
    direct_offers: List[RoomOffer] = field(default_factory=list)
    scraped_at: datetime = field(default_factory=datetime.utcnow)
    booking_error_message: Optional[str] = None
    direct_error_message: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate result attributes."""
        if not isinstance(self.hotel_name, str) or not self.hotel_name.strip():
            raise ValueError("hotel_name must be a non-empty string.")

        if not self.hotel_id:
            object.__setattr__(
                self,
                "hotel_id",
                re.sub(r"[^\w]+", "_", self.hotel_name.strip().lower()).strip("_"),
            )
        elif not isinstance(self.hotel_id, str):
            raise ValueError("hotel_id must be a string.")

        if self.check_in is not None:
            if not isinstance(self.check_in, date) or isinstance(self.check_in, datetime):
                raise TypeError("check_in must be a datetime.date instance.")

        if self.check_out is not None:
            if not isinstance(self.check_out, date) or isinstance(self.check_out, datetime):
                raise TypeError("check_out must be a datetime.date instance.")

        if self.check_in is not None and self.check_out is not None:
            if self.check_out <= self.check_in:
                raise ValueError("check_out must be after check_in.")

        if self.booking_price is not None:
            if not isinstance(self.booking_price, (int, float)) or isinstance(self.booking_price, bool):
                raise TypeError("booking_price must be a numeric value.")
            if self.booking_price < 0.0:
                raise ValueError(f"booking_price cannot be negative (got {self.booking_price}).")

        if self.direct_price is not None:
            if not isinstance(self.direct_price, (int, float)) or isinstance(self.direct_price, bool):
                raise TypeError("direct_price must be a numeric value.")
            if self.direct_price < 0.0:
                raise ValueError(f"direct_price cannot be negative (got {self.direct_price}).")

        if not isinstance(self.currency, str) or len(self.currency.strip()) < 3:
            raise ValueError(f"currency must be a valid 3-letter code (got '{self.currency}').")

        if not isinstance(self.offers, (list, tuple)):
            raise TypeError("offers must be a list or tuple of RoomOffer instances.")
        for item in self.offers:
            if not isinstance(item, RoomOffer):
                raise TypeError(f"All items in offers must be RoomOffer instances (got {type(item)}).")

        if not isinstance(self.direct_offers, (list, tuple)):
            raise TypeError("direct_offers must be a list or tuple of RoomOffer instances.")
        for item in self.direct_offers:
            if not isinstance(item, RoomOffer):
                raise TypeError(
                    f"All items in direct_offers must be RoomOffer instances (got {type(item)})."
                )

        if self.booking_price is None and self.offers:
            valid_prices = [o.price for o in self.offers if o.price is not None and o.price > 0]
            if valid_prices:
                object.__setattr__(self, "booking_price", min(valid_prices))

        if self.direct_price is None and self.direct_offers:
            valid_direct_prices = [
                o.price for o in self.direct_offers if o.price is not None and o.price > 0
            ]
            if valid_direct_prices:
                object.__setattr__(self, "direct_price", min(valid_direct_prices))

        if not self.is_available and self.status == ResultStatus.SUCCESS:
            if self.error_message:
                object.__setattr__(self, "status", ResultStatus.FAILED)
            else:
                object.__setattr__(self, "status", ResultStatus.NOT_AVAILABLE)

        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", ResultStatus(self.status))
            except ValueError:
                valid_statuses = [s.value for s in ResultStatus]
                raise ValueError(
                    f"Invalid status '{self.status}'. Must be one of: {valid_statuses}."
                )
        elif not isinstance(self.status, ResultStatus):
            raise TypeError("status must be a ResultStatus enum or string value.")

    @property
    def price_delta(self) -> Optional[float]:
        """Calculate direct_price - booking_price."""
        if self.direct_price is not None and self.booking_price is not None:
            return round(self.direct_price - self.booking_price, 2)
        return None

    @property
    def price_delta_percent(self) -> Optional[float]:
        """Percentage difference relative to Booking.com price."""
        if (
            self.direct_price is not None
            and self.booking_price is not None
            and self.booking_price > 0
        ):
            delta = self.direct_price - self.booking_price
            return round((delta / self.booking_price) * 100, 2)
        return None

    @property
    def cheaper_channel(self) -> Optional[str]:
        """Determine which channel offers the better rate (DIRECT, BOOKING, EQUAL)."""
        if self.direct_price is None or self.booking_price is None:
            return None
        if self.direct_price < self.booking_price:
            return "DIRECT"
        if self.booking_price < self.direct_price:
            return "BOOKING"
        return "EQUAL"