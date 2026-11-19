"""Runtime settings and environment configuration."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Automatically load environment variables from .env if present
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """Immutable application runtime settings."""

    # Paths
    project_root: Path = PROJECT_ROOT
    logs_dir: Path = PROJECT_ROOT / "logs"
    exports_dir: Path = PROJECT_ROOT / "exports"
    cache_dir: Path = PROJECT_ROOT / ".cache"

    # Supabase credentials
    supabase_url: str = field(
        default_factory=lambda: os.getenv("SUPABASE_URL", "").rstrip("/")
    )
    supabase_key: str = field(
        default_factory=lambda: os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    )

    # Scraper performance & anti-bot parameters
    default_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("SCRAPER_TIMEOUT_MS", "30000"))
    )
    min_jitter_sec: float = field(
        default_factory=lambda: float(os.getenv("SCRAPER_MIN_JITTER_SEC", "1.5"))
    )
    max_jitter_sec: float = field(
        default_factory=lambda: float(os.getenv("SCRAPER_MAX_JITTER_SEC", "4.0"))
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("CONFIG_CACHE_TTL_SEC", "3600"))
    )

    # Standard desktop User-Agents for rotation
    user_agents: List[str] = field(
        default_factory=lambda: [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
    )

    def __post_init__(self) -> None:
        """Ensure necessary runtime directories exist on startup."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_supabase_configured(self) -> bool:
        """Check whether Supabase connection parameters are present."""
        return bool(self.supabase_url and self.supabase_key)


_cached_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Retrieve singleton Settings instance."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings

