"""Unit tests for thread-safe QueueManager."""

from datetime import date
import unittest

from core.models import ResultStatus, ScrapedResult
from services.queue_manager import EventType, ProgressEvent, QueueManager


class TestQueueManager(unittest.TestCase):
    """Test suite for event queue dispatch and cancellation signals."""

    def setUp(self) -> None:
        self.qm = QueueManager(maxsize=50)

    def test_publish_and_retrieve_event(self) -> None:
        """Verify FIFO publishing and retrieval of ProgressEvent."""
        self.qm.publish_job_started(total_hotels=5)
        event = self.qm.get_event(block=False)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.event_type, EventType.JOB_STARTED)
        self.assertEqual(event.percentage, 0.0)
        self.assertIn("Total hotels: 5", event.message)

    def test_hotel_progress_events(self) -> None:
        """Verify hotel start and completion event dispatches."""
        self.qm.publish_hotel_started("Hotel Grand", 1, 4)
        e1 = self.qm.get_event(block=False)
        self.assertIsNotNone(e1)
        assert e1 is not None
        self.assertEqual(e1.event_type, EventType.HOTEL_STARTED)
        self.assertEqual(e1.percentage, 0.0)
        self.assertEqual(e1.current_hotel, "Hotel Grand")

        res = ScrapedResult(
            hotel_id="h1",
            hotel_name="Hotel Grand",
            check_in=date(2026, 12, 1),
            check_out=date(2026, 12, 5),
            booking_price=200.0,
            direct_price=180.0,
            status=ResultStatus.SUCCESS,
        )
        self.qm.publish_hotel_completed("Hotel Grand", 1, 4, res)
        e2 = self.qm.get_event(block=False)
        self.assertIsNotNone(e2)
        assert e2 is not None
        self.assertEqual(e2.event_type, EventType.HOTEL_COMPLETED)
        self.assertEqual(e2.percentage, 25.0)
        self.assertEqual(e2.result, res)

    def test_cancellation_flow(self) -> None:
        """Verify cancellation request signaling and reset."""
        self.assertFalse(self.qm.is_stop_requested())
        self.qm.request_stop()
        self.assertTrue(self.qm.is_stop_requested())

        self.qm.publish_job_stopped("Interrupted by operator")
        e = self.qm.get_event(block=False)
        self.assertIsNotNone(e)
        assert e is not None
        self.assertEqual(e.event_type, EventType.JOB_STOPPED)

        self.qm.reset()
        self.assertFalse(self.qm.is_stop_requested())


if __name__ == "__main__":
    unittest.main()

