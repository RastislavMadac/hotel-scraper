"""Core package for hotel scraper."""

from core.config_manager import ConfigError, ConfigManager
from core.logger import SupabaseSink, app_logger, get_machine_id, logger, setup_logger
from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob

__all__ = [
    "ScrapingJob",
    "ScrapedResult",
    "RoomOffer",
    "ResultStatus",
    "logger",
    "app_logger",
    "setup_logger",
    "SupabaseSink",
    "get_machine_id",
    "ConfigManager",
    "ConfigError",
]
