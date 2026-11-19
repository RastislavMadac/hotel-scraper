"""Thread-safe queue manager for communication between background scraper worker and UI."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import queue
import threading
from typing import List, Optional

from core.logger import logger
from core.models import ScrapedResult


class EventType(str, Enum):
    """Types of progress and status events sent by the scraping engine."""
    JOB_STARTED = "JOB_STARTED"
    HOTEL_STARTED = "HOTEL_STARTED"
    HOTEL_PROGRESS = "HOTEL_PROGRESS"
    HOTEL_COMPLETED = "HOTEL_COMPLETED"
    WARNING = "WARNING"
    ERROR = "ERROR"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_STOPPED = "JOB_STOPPED"


@dataclass(frozen=True)
class ProgressEvent:
    """Immutable event payload delivered to UI thread."""
    event_type: EventType
    percentage: float = 0.0
    current_hotel: Optional[str] = None
    message: str = ""
    result: Optional[ScrapedResult] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if not isinstance(self.percentage, (int, float)):
            raise TypeError("percentage must be numeric.")
        if not (0.0 <= self.percentage <= 100.0):
            raise ValueError(f"percentage must be between 0.0 and 100.0 (got {self.percentage}).")


class QueueManager:
    """Thread-safe message bus managing UI dispatch and worker cancellation."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: queue.Queue[ProgressEvent] = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()

    def publish(self, event: ProgressEvent) -> None:
        """Post a generic ProgressEvent into the queue."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("QueueManager queue is full, dropping oldest event.")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception as exc:
                logger.error(f"Failed to enqueue event: {exc}")

    def publish_job_started(self, total_hotels: int, message: str = "Starting scraping job...") -> None:
        """Notify that scraping job has commenced."""
        event = ProgressEvent(
            event_type=EventType.JOB_STARTED,
            percentage=0.0,
            message=f"{message} (Total hotels: {total_hotels})",
        )
        self.publish(event)

    def publish_hotel_started(self, hotel_name: str, current_index: int, total_hotels: int) -> None:
        """Notify that scraping for a specific hotel has begun."""
        pct = round(((current_index - 1) / max(total_hotels, 1)) * 100, 1)
        event = ProgressEvent(
            event_type=EventType.HOTEL_STARTED,
            percentage=pct,
            current_hotel=hotel_name,
            message=f"Scraping [{current_index}/{total_hotels}]: {hotel_name}",
        )
        self.publish(event)

    def publish_hotel_completed(
        self,
        hotel_name: str,
        current_index: int,
        total_hotels: int,
        result: ScrapedResult,
    ) -> None:
        """Notify that an individual hotel scrape finished with a result."""
        pct = round((current_index / max(total_hotels, 1)) * 100, 1)
        event = ProgressEvent(
            event_type=EventType.HOTEL_COMPLETED,
            percentage=pct,
            current_hotel=hotel_name,
            message=f"Completed [{current_index}/{total_hotels}]: {hotel_name} - Status: {result.status.value}",
            result=result,
        )
        self.publish(event)

    def publish_warning(self, message: str, current_hotel: Optional[str] = None) -> None:
        """Send a non-terminating warning event."""
        event = ProgressEvent(
            event_type=EventType.WARNING,
            current_hotel=current_hotel,
            message=message,
        )
        self.publish(event)

    def publish_error(self, message: str, current_hotel: Optional[str] = None) -> None:
        """Send an error notification event."""
        event = ProgressEvent(
            event_type=EventType.ERROR,
            current_hotel=current_hotel,
            message=message,
        )
        self.publish(event)

    def publish_job_completed(self, total_scraped: int, message: str = "Job finished successfully.") -> None:
        """Notify that the entire scraping job has completed."""
        event = ProgressEvent(
            event_type=EventType.JOB_COMPLETED,
            percentage=100.0,
            message=f"{message} (Processed {total_scraped} hotels)",
        )
        self.publish(event)

    def publish_job_stopped(self, message: str = "Scraping was cancelled by user.") -> None:
        """Notify that scraping job was aborted."""
        event = ProgressEvent(
            event_type=EventType.JOB_STOPPED,
            message=message,
        )
        self.publish(event)

    def get_event(self, block: bool = True, timeout: Optional[float] = None) -> Optional[ProgressEvent]:
        """Fetch next available event from the queue."""
        try:
            return self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def get_all_events(self) -> List[ProgressEvent]:
        """Drain and return all currently pending events in queue."""
        events: List[ProgressEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    # -------------------------------------------------------------------------
    # Worker cancellation controls
    # -------------------------------------------------------------------------

    def request_stop(self) -> None:
        """Signal background worker thread to stop execution."""
        logger.info("Cancellation requested via QueueManager.")
        self._stop_event.set()

    def is_stop_requested(self) -> bool:
        """Check whether cancellation was signaled."""
        return self._stop_event.is_set()

    def reset(self) -> None:
        """Reset cancellation signal and drain pending items."""
        self._stop_event.clear()
        self.get_all_events()

