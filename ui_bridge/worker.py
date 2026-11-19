"""Thread-safe background scraper worker for UI decoupling."""

from datetime import datetime
import os
from pathlib import Path
import queue
import threading
from typing import Any, List, Optional, Union

from core.config_manager import ConfigManager
from core.logger import logger
from core.models import ScrapedResult, ScrapingJob
from exporters.excel_generator import ExcelReportGenerator
from scraper.browser import BrowserManager
from scraper.engine import ScrapingEngine


class ScraperWorkerThread(threading.Thread):
    """Background worker thread executing Playwright scraping without blocking the UI."""

    def __init__(
        self,
        job: ScrapingJob,
        config_manager: ConfigManager,
        progress_queue: queue.Queue,
        output_dir: str = "reports",
        browser_manager: Optional[BrowserManager] = None,
        min_jitter_sec: Optional[float] = None,
        max_jitter_sec: Optional[float] = None,
    ) -> None:
        """Initialize ScraperWorkerThread.

        Args:
            job: ScrapingJob parameters (dates, guests, hotels).
            config_manager: Injected ConfigManager for remote rules.
            progress_queue: Thread-safe queue for UI progress updates.
            output_dir: Directory where the final .xlsx report will be saved.
            browser_manager: Optional custom/mock BrowserManager.
            min_jitter_sec: Optional minimum pause between requests.
            max_jitter_sec: Optional maximum pause between requests.
        """
        super().__init__(name="ScraperWorkerThread", daemon=True)

        self.job = job
        self.config_manager = config_manager
        self.progress_queue = progress_queue
        self.output_dir = Path(output_dir)
        self.browser_manager = browser_manager
        self.min_jitter = min_jitter_sec
        self.max_jitter = max_jitter_sec

        self._stop_event = threading.Event()
        self._is_active = False
        self._results: List[ScrapedResult] = []
        self._report_path: Optional[str] = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        """Check whether the worker is currently executing."""
        with self._lock:
            return self._is_active

    @property
    def results(self) -> List[ScrapedResult]:
        """Return results gathered by the scraper."""
        with self._lock:
            return list(self._results)

    @property
    def report_path(self) -> Optional[str]:
        """Return absolute path to generated Excel report."""
        with self._lock:
            return self._report_path

    @property
    def error(self) -> Optional[str]:
        """Return fatal error message if execution failed."""
        with self._lock:
            return self._error

    def stop(self) -> None:
        """Signal worker and engine to abort scraping safely."""
        logger.info("Signaling ScraperWorkerThread to stop execution...")
        self._stop_event.set()

    def is_stop_requested(self) -> bool:
        """Check if stop signal has been issued."""
        return self._stop_event.is_set()

    def run(self) -> None:
        """Thread execution entry point."""
        with self._lock:
            self._is_active = True
            self._error = None

        logger.info("ScraperWorkerThread starting background execution...")

        try:
            # 1. Send starting status to UI queue
            self.progress_queue.put(
                {
                    "status": "starting",
                    "message": "Inicializácia zberu dát...",
                }
            )

            # 2. Instantiate and execute ScrapingEngine
            engine = ScrapingEngine(
                job=self.job,
                config_manager=self.config_manager,
                progress_queue=self.progress_queue,
                browser_manager=self.browser_manager,
                min_jitter_sec=self.min_jitter,
                max_jitter_sec=self.max_jitter,
                stop_event=self._stop_event,
            )

            results = engine.run()
            with self._lock:
                self._results = results

            # 3. Handle cancellation
            if self._stop_event.is_set():
                logger.warning("ScraperWorkerThread stopped early due to user cancellation.")
                self.progress_queue.put(
                    {
                        "status": "stopped",
                        "message": "Zber dát bol prerušený používateľom.",
                        "total_hotels": len(results),
                    }
                )
                return

            # 4. Generate raw Excel report directly from hs-price_comparisons in Supabase
            self.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            job_slug = self.job.job_id[:8]
            target_filename = f"hotel_prices_{job_slug}_{timestamp}.xlsx"
            target_path = self.output_dir / target_filename

            db_rows = self.config_manager.fetch_all_price_comparisons()
            rows_list = db_rows if isinstance(db_rows, list) else []
            generator = ExcelReportGenerator(exports_dir=self.output_dir)
            report_file = str(
                generator.generate_raw_db_report(
                    rows=rows_list,
                    output_path=target_path,
                )
            )

            with self._lock:
                self._report_path = report_file

            # 5. Send finished status to UI queue
            self.progress_queue.put(
                {
                    "status": "finished",
                    "report_path": report_file,
                    "total_hotels": len(results),
                }
            )
            logger.info(
                f"ScraperWorkerThread finished successfully. Report: {report_file}"
            )

        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            logger.opt(exception=True).critical(
                f"Neočakávaná chyba vo worker vlákne scrapera: {exc}"
            )
            self.progress_queue.put(
                {
                    "status": "failed",
                    "error": str(exc),
                }
            )

        finally:
            with self._lock:
                self._is_active = False
            # Guarantee terminating state so UI never hangs in loading state
            try:
                self.progress_queue.put({"status": "idle", "active": False})
            except Exception:
                pass
            logger.debug("ScraperWorkerThread execution completed.")


# Alias for backward compatibility
ScraperWorker = ScraperWorkerThread
