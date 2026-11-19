"""Unit tests for BrowserManager lifecycle, stealth setup, and process cleanup."""

import unittest
from unittest.mock import MagicMock, patch

from scraper.browser import BrowserInitError, BrowserManager


class TestBrowserManager(unittest.TestCase):
    """Test suite for Playwright browser launcher and stealth manager."""

    def test_browser_launch_and_clean_close(self) -> None:
        """Verify successful launch, page creation, and clean resource release."""
        manager = BrowserManager(
            headless=True,
            timeout_ms=10000,
            viewport={"width": 1280, "height": 720},
        )

        try:
            context = manager.launch_browser()
            self.assertIsNotNone(context)
            self.assertTrue(manager.is_running)

            page = manager.new_page()
            self.assertIsNotNone(page)

            # Test page interaction
            page.set_content("<html><body><h1 id='title'>Stealth Test</h1></body></html>")
            title = page.locator("#title").text_content()
            self.assertEqual(title, "Stealth Test")

            page.close()
        finally:
            manager.close()

        self.assertFalse(manager.is_running)
        self.assertIsNone(manager.context)

    def test_context_manager_usage(self) -> None:
        """Verify pythonic 'with BrowserManager() as bm:' lifecycle."""
        with BrowserManager(headless=True) as manager:
            self.assertTrue(manager.is_running)
            self.assertIsNotNone(manager.context)
            page = manager.new_page()
            self.assertIsNotNone(page)
            page.close()

        # After exiting block, resources must be cleared
        self.assertFalse(manager.is_running)
        self.assertIsNone(manager.context)

    @patch("scraper.browser.sync_playwright")
    def test_browser_init_failure_raises_browser_init_error(
        self, mock_playwright: MagicMock
    ) -> None:
        """Verify that launch failure logs critical error and raises BrowserInitError."""
        mock_driver = MagicMock()
        mock_driver.chromium.launch.side_effect = RuntimeError("Chromium binary missing")
        mock_playwright.return_value.start.return_value = mock_driver

        manager = BrowserManager(headless=True)
        with self.assertRaises(BrowserInitError) as ctx:
            manager.launch_browser()

        self.assertIn("Chromium binary missing", str(ctx.exception))
        self.assertFalse(manager.is_running)


if __name__ == "__main__":
    unittest.main()

