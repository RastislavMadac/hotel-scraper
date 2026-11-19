"""Desktop UI bridge providing high-level interface between frontend views and background scraper workers."""

from datetime import date, datetime
from pathlib import Path
import queue
from typing import Any, Dict, List, Optional

from config.settings import get_settings
from core.config_manager import ConfigManager
from core.logger import logger
from core.models import ScrapedResult, ScrapingJob
from scraper.browser import BrowserManager
from ui_bridge.worker import ScraperWorkerThread


class UIBridge:
    """High-level desktop UI coordinator managing background scraper lifecycles."""

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        output_dir: str = "reports",
    ) -> None:
        """Initialize UIBridge.

        Args:
            config_manager: ConfigManager instance (or created automatically).
            output_dir: Directory where final Excel reports will be saved.
        """
        self.config_manager = config_manager or ConfigManager()
        self.output_dir = output_dir
        self.progress_queue: queue.Queue = queue.Queue()
        self.worker: Optional[ScraperWorkerThread] = None

    @property
    def is_running(self) -> bool:
        """Check whether a background scraping task is currently executing."""
        return self.worker is not None and self.worker.is_active

    def start_job(
        self,
        check_in: date,
        check_out: date,
        adults: int = 2,
        children: int = 0,
        email: Optional[str] = None,
        hotels: Optional[List[str]] = None,
        headless: bool = True,
    ) -> Dict[str, Any]:
        """Validate parameters and trigger a new background scraping worker.

        Args:
            check_in: Arrival date.
            check_out: Departure date.
            adults: Number of adults.
            children: Number of children.
            email: Optional notification email.
            hotels: Optional list of hotel IDs/names.
            headless: Whether Chromium should be launched headless.

        Returns:
            Dictionary with job status information.
        """
        if self.is_running:
            logger.warning("Attempted to start new job while worker is already running.")
            return {"status": "error", "message": "Scraper is already active."}

        try:
            job = ScrapingJob(
                check_in=check_in,
                check_out=check_out,
                adults=adults,
                children=children,
                email=email,
                hotel_identifiers=hotels,
            )
        except (ValueError, TypeError) as exc:
            logger.error(f"UIBridge job validation failed: {exc}")
            return {"status": "error", "message": str(exc)}

        # Clear any stale messages from queue
        while not self.progress_queue.empty():
            try:
                self.progress_queue.get_nowait()
            except queue.Empty:
                break

        browser_manager = BrowserManager(
            headless=headless,
            timeout_ms=get_settings().default_timeout_ms,
        )

        self.worker = ScraperWorkerThread(
            job=job,
            config_manager=self.config_manager,
            progress_queue=self.progress_queue,
            output_dir=self.output_dir,
            browser_manager=browser_manager,
        )

        logger.info(f"UIBridge initiating job {job.job_id[:8]} on background thread...")
        self.worker.start()

        return {
            "status": "started",
            "job_id": job.job_id,
            "nights": job.nights,
            "total_guests": job.total_guests,
        }

    def stop_job(self) -> Dict[str, Any]:
        """Signal the active scraper worker to stop execution."""
        if not self.is_running or self.worker is None:
            return {"status": "idle", "message": "No active scraping job to stop."}

        logger.info("UIBridge received stop request from user interface.")
        self.worker.stop()
        return {"status": "stopping", "message": "Stop signal delivered."}

    def poll_messages(self, max_messages: int = 50) -> List[Dict[str, Any]]:
        """Non-blocking retrieval of pending progress events for UI consumption."""
        messages: List[Dict[str, Any]] = []
        for _ in range(max_messages):
            try:
                msg = self.progress_queue.get_nowait()
                messages.append(msg)
            except queue.Empty:
                break
        return messages

    def get_results(self) -> List[ScrapedResult]:
        """Retrieve results from the completed worker."""
        if self.worker is not None:
            return self.worker.results
        return []

    def get_report_path(self) -> Optional[str]:
        """Return the path to the generated Excel report if completed."""
        if self.worker is not None:
            return self.worker.report_path
        return None

