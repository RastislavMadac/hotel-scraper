"""Unit tests for the centralized logger and Supabase sink."""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from loguru import logger
import requests

from core.logger import SupabaseSink, get_machine_id, setup_logger, shutdown_all_supabase_sinks


class TestLogger(unittest.TestCase):
    """Test suite for Loguru setup and Supabase sink integration."""

    def setUp(self) -> None:
        logger.remove()
        shutdown_all_supabase_sinks(wait=True)
        self.test_dir = tempfile.mkdtemp()
        self.test_log_file = os.path.join(self.test_dir, "test_app.log")

    def tearDown(self) -> None:
        logger.remove()
        shutdown_all_supabase_sinks(wait=True)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_machine_id(self) -> None:
        """Verify machine_id generation is deterministic and non-empty."""
        m_id1 = get_machine_id()
        m_id2 = get_machine_id()
        self.assertEqual(m_id1, m_id2)
        self.assertEqual(len(m_id1), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in m_id1))

    def test_local_file_logging(self) -> None:
        """Verify log messages are written to the rotating local log file."""
        setup_logger(
            log_file=self.test_log_file,
            enable_console=False,
            enable_supabase=False,
        )

        test_msg_info = "Informational test message #1234"
        test_msg_error = "Failure test message #5678"

        logger.info(test_msg_info)
        logger.error(test_msg_error)

        # Allow enqueue sink to flush
        logger.complete()
        time.sleep(0.1)

        self.assertTrue(os.path.exists(self.test_log_file))
        with open(self.test_log_file, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn(test_msg_info, content)
        self.assertIn(test_msg_error, content)
        self.assertIn("INFO", content)
        self.assertIn("ERROR", content)

    @patch("requests.post")
    def test_supabase_sink_dispatch_on_warning_and_error(self, mock_post: MagicMock) -> None:
        """Verify Supabase sink receives WARNING and ERROR but ignores INFO."""
        sink = SupabaseSink(
            supabase_url="https://xyzcompany.supabase.co",
            supabase_key="fake-anon-key-12345",
        )
        self.assertTrue(sink.is_configured)

        logger.remove()
        logger.add(sink, level="WARNING")
        mock_post.reset_mock()

        # 1. INFO message should be ignored by level="WARNING" filter
        logger.info("This is an info message that should NOT be sent to Supabase")
        time.sleep(0.05)
        self.assertEqual(mock_post.call_count, 0)

        # 2. WARNING message should trigger dispatch
        logger.warning("Warning: rate limit threshold approaching")
        time.sleep(0.15)
        self.assertGreaterEqual(mock_post.call_count, 1)

        # Inspect payload
        call_args = mock_post.call_args_list[0]
        url = call_args[0][0]
        kwargs = call_args[1]

        self.assertEqual(url, "https://xyzcompany.supabase.co/rest/v1/hs_app_logs")
        self.assertEqual(kwargs["json"]["level"], "WARNING")
        self.assertIn("rate limit threshold approaching", kwargs["json"]["message"])
        self.assertEqual(kwargs["json"]["machine_id"], sink.machine_id)
        self.assertEqual(kwargs["headers"]["apikey"], "fake-anon-key-12345")

        sink.shutdown(wait=True)

    @patch("requests.post")
    def test_supabase_sink_exception_traceback_capture(self, mock_post: MagicMock) -> None:
        """Verify unhandled exception stack traces are captured and formatted."""
        sink = SupabaseSink(
            supabase_url="https://xyzcompany.supabase.co",
            supabase_key="fake-anon-key-12345",
        )

        logger.remove()
        logger.add(sink, level="ERROR")
        mock_post.reset_mock()

        try:
            _ = 1 / 0
        except ZeroDivisionError:
            logger.opt(exception=True).error("Encountered division by zero")

        time.sleep(0.15)
        self.assertGreaterEqual(mock_post.call_count, 1)

        last_call = mock_post.call_args_list[-1]
        payload = last_call[1]["json"]
        self.assertEqual(payload["level"], "ERROR")
        self.assertIsNotNone(payload["traceback"])
        self.assertIn("ZeroDivisionError: division by zero", payload["traceback"])

        sink.shutdown(wait=True)

    @patch("requests.post", side_effect=requests.exceptions.ConnectionError("Network unreachable"))
    def test_supabase_sink_silent_failure_when_offline(self, mock_post: MagicMock) -> None:
        """Verify that offline / network connection failure never raises or crashes the app."""
        sink = SupabaseSink(
            supabase_url="https://xyzcompany.supabase.co",
            supabase_key="fake-anon-key-12345",
        )

        logger.remove()
        logger.add(sink, level="WARNING")

        # This should execute cleanly without raising any exception
        try:
            logger.error("Network is down test error")
            logger.critical("Critical outage simulation")
            time.sleep(0.15)
        except Exception as e:
            self.fail(f"Logger raised an exception during network failure: {e}")

        sink.shutdown(wait=True)

    @patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_ANON_KEY": ""}, clear=False)
    def test_unconfigured_supabase_sink_does_not_call_network(self) -> None:
        """Verify that missing credentials result in clean no-op."""
        sink = SupabaseSink(supabase_url="", supabase_key="")
        self.assertFalse(sink.is_configured)

        with patch("requests.post") as mock_post:
            mock_post.reset_mock()
            logger.remove()
            logger.add(sink, level="WARNING")
            logger.error("Should not send anywhere")
            time.sleep(0.1)
            mock_post.assert_not_called()

        sink.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

