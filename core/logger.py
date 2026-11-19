"""Centralized logging configuration with local rotation and non-blocking Supabase sink."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import platform
import sys
import traceback
from typing import Any, Dict, List, Optional
import uuid

from loguru import logger
import requests

# Ensure logs directory exists
LOGS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
os.makedirs(LOGS_DIR, exist_ok=True)
DEFAULT_LOG_FILE = os.path.join(LOGS_DIR, "app.log")


def get_machine_id() -> str:
    """Generate a stable, unique fingerprint for the current host machine."""
    host_data = f"{platform.node()}-{platform.system()}-{platform.machine()}-{uuid.getnode()}"
    return hashlib.sha256(host_data.encode("utf-8")).hexdigest()[:32]


_active_supabase_sinks: List["SupabaseSink"] = []


def shutdown_all_supabase_sinks(wait: bool = False) -> None:
    """Cleanly terminate all background ThreadPoolExecutors for Supabase sinks."""
    while _active_supabase_sinks:
        sink = _active_supabase_sinks.pop()
        try:
            sink.shutdown(wait=wait)
        except Exception:
            pass


class SupabaseSink:
    """Custom non-blocking Loguru sink pushing critical logs to Supabase hs_app_logs table."""

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        timeout: float = 4.0,
        max_workers: int = 2,
    ) -> None:
        if supabase_url is not None:
            self.supabase_url = supabase_url.rstrip("/")
        else:
            self.supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")

        if supabase_key is not None:
            self.supabase_key = supabase_key
        else:
            self.supabase_key = (
                os.getenv("SUPABASE_KEY")
                or os.getenv("SUPABASE_ANON_KEY")
                or ""
            )
        self.timeout = timeout
        self.machine_id = get_machine_id()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="SupabaseLogSink",
        )
        _active_supabase_sinks.append(self)

    @property
    def is_configured(self) -> bool:
        """Check whether Supabase credentials are provided."""
        return bool(self.supabase_url and self.supabase_key)

    def _format_traceback(self, exception_info: Any) -> Optional[str]:
        """Format exception tuple into a human-readable traceback string."""
        if not exception_info:
            return None
        try:
            exc_type, exc_value, exc_tb = exception_info
            if exc_type is not None:
                return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        except Exception:
            return None
        return None

    def _send_to_supabase(self, payload: Dict[str, Any]) -> None:
        """Send a single log record to the Supabase REST endpoint in background."""
        if not self.is_configured:
            return

        endpoint = f"{self.supabase_url}/rest/v1/hs_app_logs"
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

        try:
            # Defensive network request - short timeout, never crashes host app
            requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except Exception:
            # Silent fallback: errors in logging must never break scraper execution
            pass

    def __call__(self, message: Any) -> None:
        """Loguru sink handler invoked for records matching the configured level."""
        record = message.record
        payload: Dict[str, Any] = {
            "machine_id": self.machine_id,
            "level": record["level"].name,
            "module": record["name"],
            "message": str(record["message"]),
            "traceback": self._format_traceback(record["exception"]),
        }

        # Asynchronously schedule the POST request without blocking caller thread
        self._executor.submit(self._send_to_supabase, payload)

    def shutdown(self, wait: bool = False) -> None:
        """Cleanly terminate background thread pool."""
        self._executor.shutdown(wait=wait)


def setup_logger(
    log_file: str = DEFAULT_LOG_FILE,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    enable_console: bool = True,
    console_level: str = "INFO",
    enable_supabase: bool = True,
) -> Any:
    """Configure Loguru with console, local rotating file, and Supabase sinks.
    
    Returns:
        Configured loguru.logger instance.
    """
    # Remove any existing handlers
    logger.remove()

    # 1. Console Sink (stdout/stderr for developers & CLI)
    if enable_console:
        logger.add(
            sys.stderr,
            level=console_level,
            colorize=True,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            enqueue=True,
        )

    # 2. Local Fallback Rotating File Sink (5 MB size, 3 days retention, compression)
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    logger.add(
        log_file,
        rotation="5 MB",
        retention="3 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    # 3. Supabase Asynchronous Sink (catches WARNING, ERROR, CRITICAL only)
    if enable_supabase:
        supabase_sink = SupabaseSink(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )
        logger.add(
            supabase_sink,
            level="WARNING",
            enqueue=False,  # Sink manages its own ThreadPoolExecutor
        )

    return logger


# Initialize and export application-wide singleton logger
app_logger = setup_logger()

__all__ = [
    "logger",
    "app_logger",
    "setup_logger",
    "SupabaseSink",
    "shutdown_all_supabase_sinks",
    "get_machine_id",
]

