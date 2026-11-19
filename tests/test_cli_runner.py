"""Smoke tests for autonomous CLI runner and argument parsing."""

from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.models import ResultStatus, ScrapedResult, ScrapingJob
from main import main, parse_arguments


class TestCliRunner(unittest.TestCase):
    """Smoke test suite verifying CLI arguments parsing, job setup, and execution flow."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.test_dir) / "reports"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_parse_arguments_defaults(self) -> None:
        """Verify default CLI arguments: tomorrow arrival, +2 days departure, 2 guests."""
        args = parse_arguments([])
        tomorrow = date.today() + timedelta(days=1)
        expected_checkout = tomorrow + timedelta(days=2)

        self.assertEqual(args.check_in, tomorrow.strftime("%Y-%m-%d"))
        self.assertEqual(args.check_out, expected_checkout.strftime("%Y-%m-%d"))
        self.assertEqual(args.guests, 2)
        self.assertEqual(args.children, 0)
        self.assertIsNone(args.email)
        self.assertIsNone(args.hotels)
        self.assertTrue(args.headless)
        self.assertEqual(args.output_dir, "reports")

    def test_parse_arguments_custom(self) -> None:
        """Verify custom CLI flags are parsed into expected namespace values."""
        custom_args = [
            "--check-in", "2026-12-05",
            "--check-out", "2026-12-10",
            "--guests", "4",
            "--children", "2",
            "--email", "notif@hotelscraper.sk",
            "--hotels", "kempinski,marrols",
            "--no-headless",
            "--output-dir", str(self.output_dir),
        ]
        args = parse_arguments(custom_args)

        self.assertEqual(args.check_in, "2026-12-05")
        self.assertEqual(args.check_out, "2026-12-10")
        self.assertEqual(args.guests, 4)
        self.assertEqual(args.children, 2)
        self.assertEqual(args.email, "notif@hotelscraper.sk")
        self.assertEqual(args.hotels, "kempinski,marrols")
        self.assertFalse(args.headless)
        self.assertEqual(args.output_dir, str(self.output_dir))

    def test_invalid_dates_return_code_1(self) -> None:
        """Invalid date format must be safely rejected with return code 1."""
        code = main(["--check-in", "not-a-date"])
        self.assertEqual(code, 1)

    def test_past_date_rejected_by_scraping_job(self) -> None:
        """Dates in the past must fail ScrapingJob validation with code 1."""
        past_date = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
        code = main(["--check-in", past_date])
        self.assertEqual(code, 1)

    @patch("main.ScraperWorkerThread")
    def test_smoke_main_execution_success(self, mock_worker_cls: MagicMock) -> None:
        """Smoke test verifying end-to-end main() execution with mock worker."""
        # Create a real dummy report file
        dummy_report = self.output_dir / "test_report.xlsx"
        dummy_report.touch()

        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = False
        mock_worker.results = [
            ScrapedResult(
                hotel_id="h1",
                hotel_name="Grand Hotel",
                booking_price=200.0,
                direct_price=190.0,
                status=ResultStatus.SUCCESS,
            )
        ]
        mock_worker.report_path = str(dummy_report)
        mock_worker_cls.return_value = mock_worker

        # Simulate messages on worker start
        def mock_start():
            # Worker puts finish and idle messages
            worker_instance = mock_worker_cls.call_args[1]["progress_queue"]
            worker_instance.put({"status": "starting", "message": "Starting..."})
            worker_instance.put({"status": "running", "current": 1, "total": 1, "hotel": "Grand Hotel", "percent": 100.0})
            worker_instance.put({"status": "finished", "report_path": str(dummy_report), "total_hotels": 1})
            worker_instance.put({"status": "idle"})

        mock_worker.start.side_effect = mock_start

        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        checkout = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")

        exit_code = main(
            [
                "--check-in", tomorrow,
                "--check-out", checkout,
                "--guests", "2",
                "--output-dir", str(self.output_dir),
            ]
        )

        self.assertEqual(exit_code, 0)
        mock_worker.start.assert_called_once()
        mock_worker.join.assert_called_once()


if __name__ == "__main__":
    unittest.main()

