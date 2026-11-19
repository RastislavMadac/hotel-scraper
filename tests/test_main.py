"""Unit tests for main.py CLI arguments parsing and execution loop."""

from datetime import date, timedelta
import unittest
from unittest.mock import MagicMock, patch

from main import main, parse_arguments


class TestMainCLI(unittest.TestCase):
    """Test suite for CLI argument parser and entry point handling."""

    def test_default_cli_arguments(self) -> None:
        """Verify default CLI options produce valid stay dates."""
        with patch("sys.argv", ["main.py"]):
            args = parse_arguments()

        self.assertEqual(args.adults, 2)
        self.assertEqual(args.children, 0)
        self.assertIsNone(args.email)
        self.assertTrue(args.headless)
        self.assertEqual(args.output_dir, "reports")

    def test_custom_cli_arguments(self) -> None:
        """Verify custom CLI flags are parsed properly."""
        test_args = [
            "main.py",
            "--check-in", "2026-12-01",
            "--check-out", "2026-12-08",
            "--adults", "3",
            "--children", "1",
            "--email", "admin@hotels.com",
            "--hotels", "h1,h2",
            "--no-headless",
            "--min-jitter", "2.0",
            "--max-jitter", "4.0",
        ]
        with patch("sys.argv", test_args):
            args = parse_arguments()

        self.assertEqual(args.check_in, "2026-12-01")
        self.assertEqual(args.check_out, "2026-12-08")
        self.assertEqual(args.adults, 3)
        self.assertEqual(args.children, 1)
        self.assertEqual(args.email, "admin@hotels.com")
        self.assertEqual(args.hotels, "h1,h2")
        self.assertFalse(args.headless)
        self.assertEqual(args.min_jitter, 2.0)
        self.assertEqual(args.max_jitter, 4.0)

    def test_main_with_invalid_date_returns_code_1(self) -> None:
        """Invalid date format must be rejected with return code 1."""
        test_args = ["main.py", "--check-in", "invalid-date"]
        with patch("sys.argv", test_args):
            code = main()
        self.assertEqual(code, 1)

    def test_main_with_past_date_returns_code_1(self) -> None:
        """Check-in in the past must be rejected by ScrapingJob validation."""
        past_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
        test_args = ["main.py", "--check-in", past_date]
        with patch("sys.argv", test_args):
            code = main()
        self.assertEqual(code, 1)

    @patch("main.ScraperWorkerThread")
    def test_main_successful_execution_returns_code_0(self, mock_worker_cls: MagicMock) -> None:
        """Verify that successful execution through queue loop exits with code 0."""
        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = False
        mock_worker_cls.return_value = mock_worker

        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        next_week = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")

        test_args = ["main.py", "--check-in", tomorrow, "--check-out", next_week]
        with patch("sys.argv", test_args):
            code = main()

        self.assertEqual(code, 0)
        mock_worker.start.assert_called_once()
        mock_worker.join.assert_called_once()


if __name__ == "__main__":
    unittest.main()

