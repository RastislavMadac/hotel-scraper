"""Playwright browser lifecycle and anti-bot stealth manager."""

import random
from typing import Any, Dict, List, Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright_stealth import Stealth

from config.settings import get_settings
from core.logger import logger


class BrowserInitError(Exception):
    """Raised when browser initialization or context creation fails."""
    pass


class BrowserManager:
    """Manages stealth-enabled Chromium browser instances and contexts."""

    # Default browser flags to bypass bot detection heuristics
    DEFAULT_CHROMIUM_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-notifications",
        "--no-first-run",
        "--ignore-certificate-errors",
    ]

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: Optional[int] = None,
        user_agents: Optional[List[str]] = None,
        viewport: Optional[Dict[str, int]] = None,
    ) -> None:
        """Initialize BrowserManager settings.

        Args:
            headless: Whether to run Chromium in headless mode.
            timeout_ms: Default timeout for context and page actions.
            user_agents: List of desktop User-Agents to pick from.
            viewport: Viewport dictionary with 'width' and 'height'.
        """
        settings = get_settings()
        self.headless = headless
        self.timeout_ms = timeout_ms if timeout_ms is not None else settings.default_timeout_ms
        self.user_agents = user_agents or settings.user_agents
        self.viewport = viewport or {"width": 1920, "height": 1080}

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._stealth = Stealth()

    @property
    def context(self) -> Optional[BrowserContext]:
        """Return the active BrowserContext if launched."""
        return self._context

    @property
    def is_running(self) -> bool:
        """Check if browser process is actively running."""
        return self._browser is not None and self._browser.is_connected()

    def launch_browser(self, headless: Optional[bool] = None) -> BrowserContext:
        """Launch Chromium with stealth configurations and create a BrowserContext.

        Args:
            headless: Override instance headless configuration if specified.

        Returns:
            Initialized and stealth-patched BrowserContext.

        Raises:
            BrowserInitError: If browser launch or context creation fails.
        """
        run_headless = self.headless if headless is None else headless
        selected_ua = random.choice(self.user_agents) if self.user_agents else None

        try:
            logger.debug("Starting Playwright driver...")
            self._playwright = sync_playwright().start()

            logger.debug(
                f"Launching Chromium (headless={run_headless}) with stealth flags..."
            )
            self._browser = self._playwright.chromium.launch(
                headless=run_headless,
                args=self.DEFAULT_CHROMIUM_ARGS,
            )

            context_kwargs: Dict[str, Any] = {
                "viewport": self.viewport,
                "locale": "en-US",
                "timezone_id": "Europe/Bratislava",
                "device_scale_factor": 1.0,
                "has_touch": False,
                "is_mobile": False,
            }
            if selected_ua:
                context_kwargs["user_agent"] = selected_ua

            logger.debug(f"Creating browser context with UA: {selected_ua}...")
            self._context = self._browser.new_context(**context_kwargs)

            # Set default timeout for all operations in this context
            self._context.set_default_timeout(self.timeout_ms)
            self._context.set_default_navigation_timeout(self.timeout_ms)

            # Apply stealth evasion scripts to context
            self._stealth.apply_stealth_sync(self._context)

            logger.info("Chromium stealth browser context initialized successfully.")
            return self._context

        except Exception as exc:
            logger.critical(f"Failed to initialize Playwright browser: {exc}")
            self.close()
            raise BrowserInitError(f"Failed to initialize Playwright browser: {exc}") from exc

    def new_page(self) -> Page:
        """Create a new page in the active context and apply stealth evasions.

        Returns:
            Stealth-enabled Playwright Page.

        Raises:
            BrowserInitError: If browser context is not active.
        """
        if self._context is None:
            self.launch_browser()

        assert self._context is not None
        page = self._context.new_page()
        self._stealth.apply_stealth_sync(page)
        return page

    def close(self) -> None:
        """Safely close context, browser, and Playwright driver without leaking processes."""
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception as exc:
                    logger.debug(f"Error closing browser context: {exc}")
                self._context = None

            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception as exc:
                    logger.debug(f"Error closing browser: {exc}")
                self._browser = None

            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception as exc:
                    logger.debug(f"Error stopping Playwright: {exc}")
                self._playwright = None

            logger.debug("BrowserManager resources released cleanly.")
        except Exception as exc:
            logger.warning(f"Unexpected error while closing browser resources: {exc}")

    def __enter__(self) -> "BrowserManager":
        """Enter context manager."""
        self.launch_browser()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context manager ensuring resource release."""
        self.close()

