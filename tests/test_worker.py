"""Unit tests for ScraperWorkerThread background execution and UI decoupling."""

from datetime import date, timedelta
from pathlib import Path
import queue
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from core.models import ResultStatus, ScrapedResult, ScrapingJob
from ui_bridge.worker import ScraperWorkerThread


class TestScraperWorkerThread(unittest.TestCase):
    """Test suite for background worker thread lifecycle, queue dispatch, and error handling."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.test_dir) / "reports"
        self.progress_queue: queue.Queue = queue.Queue()

        self.today = date.today()
        self.job = ScrapingJob(
            check_in=self.today + timedelta(days=2),
            check_out=self.today + timedelta(days=5),
            adults=2,
            children=0,
        )

        self.mock_config_manager = MagicMock()

    def tearDown(self) -> None:
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_worker_successful_lifecycle(self) -> None:
        """Verify non-blocking execution, queue message order, and report creation."""
        mock_results = [
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Grand Hotel",
                booking_price=200.0,
                direct_price=180.0,
                status=ResultStatus.SUCCESS,
            ),
            ScrapedResult(
                hotel_id="h2",
                hotel_name="Boutique Palace",
                booking_price=150.0,
                direct_price=150.0,
                status=ResultStatus.SUCCESS,
            ),
        ]

        worker = ScraperWorkerThread(
            job=self.job,
            config_manager=self.mock_config_manager,
            progress_queue=self.progress_queue,
            output_dir=str(self.output_dir),
            min_jitter_sec=0.0,
            max_jitter_sec=0.0,
        )

        with patch("scraper.engine.ScrapingEngine.run", return_value=mock_results):
            # 1. Non-blocking start
            worker.start()
            self.assertTrue(worker.is_alive())

            # 2. Wait for background thread to conclude
            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())
            self.assertFalse(worker.is_active)

        # 3. Check report generation
        self.assertIsNotNone(worker.report_path)
        assert worker.report_path is not None
        self.assertTrue(Path(worker.report_path).exists())
        self.assertEqual(len(worker.results), 2)
        self.assertIsNone(worker.error)

        # 4. Check queue messages received
        messages = []
        while not self.progress_queue.empty():
            messages.append(self.progress_queue.get_nowait())

        self.assertGreaterEqual(len(messages), 3)

        # First message: starting
        self.assertEqual(messages[0]["status"], "starting")
        self.assertIn("Inicializácia", messages[0]["message"])

        # Finished message
        finished_msg = next((m for m in messages if m.get("status") == "finished"), None)
        self.assertIsNotNone(finished_msg)
        assert finished_msg is not None
        self.assertEqual(finished_msg["total_hotels"], 2)
        self.assertEqual(finished_msg["report_path"], worker.report_path)

        # Last message: idle (guaranteed by finally)
        self.assertEqual(messages[-1]["status"], "idle")
        self.assertFalse(messages[-1]["active"])

    def test_worker_failure_handling(self) -> None:
        """Verify that unexpected exceptions in worker thread are caught, logged, and posted to queue."""
        worker = ScraperWorkerThread(
            job=self.job,
            config_manager=self.mock_config_manager,
            progress_queue=self.progress_queue,
            output_dir=str(self.output_dir),
        )

        with patch(
            "scraper.engine.ScrapingEngine.run",
            side_effect=RuntimeError("Simulated unhandled driver failure"),
        ):
            worker.start()
            worker.join(timeout=5.0)

        # Verify worker recorded error and stopped
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(worker.error)
        self.assertIn("Simulated unhandled driver failure", worker.error or "")

        # Check queue received 'failed' and 'idle'
        messages = []
        while not self.progress_queue.empty():
            messages.append(self.progress_queue.get_nowait())

        failed_msg = next((m for m in messages if m.get("status") == "failed"), None)
        self.assertIsNotNone(failed_msg)
        assert failed_msg is not None
        self.assertIn("Simulated unhandled driver failure", failed_msg["error"])

        self.assertEqual(messages[-1]["status"], "idle")

    def test_worker_stop_signal(self) -> None:
        """Verify that calling stop() signals cancellation to engine."""
        worker = ScraperWorkerThread(
            job=self.job,
            config_manager=self.mock_config_manager,
            progress_queue=self.progress_queue,
        )

        self.assertFalse(worker.is_stop_requested())
        worker.stop()
        self.assertTrue(worker.is_stop_requested())


if __name__ == "__main__":
    unittest.main()

