"""Application settings and environment loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent


def _reference_path(
    env_name: str,
    primary: str,
    *legacy: str,
) -> Path:
    """Return an explicit override, the canonical refs path, or a legacy path.

    Paths remain repository-relative for compatibility with the existing
    pipeline.  The existence check itself is anchored to this module so the
    choice does not depend on the caller's current working directory.
    """

    configured = os.getenv(env_name)
    if configured and configured.strip():
        return Path(configured.strip())
    primary_path = Path(primary)
    if (PROJECT_ROOT / primary_path).is_file():
        return primary_path
    for value in legacy:
        legacy_path = Path(value)
        if (PROJECT_ROOT / legacy_path).is_file():
            return legacy_path
    return primary_path


def _safe_load_dotenv() -> None:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "cp1252", "latin-1"):
        try:
            load_dotenv(encoding=encoding)
            if os.getenv("KCI_OPEN_API_KEY"):
                return
        except UnicodeDecodeError:
            continue


_safe_load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    kci_open_api_key: str | None
    kci_open_api_url: str = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
    request_timeout: int = 30
    kci_page_size: int = 100
    kci_max_pages: int | None = None
    kci_recent_years: int = 5
    kci_start_year: int = 2022
    kci_end_year: int = 2026
    kci_max_workers: int = 4
    kci_sweep_max_workers: int = 1
    kci_enable_sweep: bool = True
    kci_rate_limit_rps: float = 2.0
    database_url: str = "sqlite:///papers.db"
    export_dir: Path = Path("reports")
    kci_keyword_filter_keywords_path: Path = Path(
        "Processor/refs/keywords_json.ko"
    )
    kci_keyword_filter_english_keywords_path: Path = Path(
        "Processor/refs/keywords_json.en"
    )
    kci_blacklist_path: Path = Path("Processor/refs/blacklist.list")
    state_key_last_success_at: str = "kci:last_success_at"

    @property
    def has_kci_key(self) -> bool:
        return bool(self.kci_open_api_key)


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _default_end_year(value: str | None) -> int:
    """Resolve the collection window's last year.

    An explicit ``KCI_END_YEAR`` is honoured as-is.  Otherwise the window ends
    at the current calendar year so the scheduled monthly increment never has
    to be re-configured when a new year begins; the historical floor of 2026
    keeps the documented five-year default intact for older clocks.
    """

    if value is not None and value.strip():
        return int(value)
    return max(2026, date.today().year)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().casefold() in {"1", "true", "yes", "y", "on"}


@lru_cache(maxsize=1)
def load_settings(validate: bool = True) -> Settings:
    kci_key = os.getenv("KCI_OPEN_API_KEY")

    settings = Settings(
        kci_open_api_key=kci_key,
        kci_open_api_url=os.getenv(
            "KCI_OPEN_API_URL",
            "https://open.kci.go.kr/po/openapi/openApiSearch.kci",
        ),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        kci_page_size=int(os.getenv("KCI_PAGE_SIZE", "100")),
        kci_max_pages=_parse_optional_int(os.getenv("KCI_MAX_PAGES")),
        kci_recent_years=int(os.getenv("KCI_RECENT_YEARS", "5")),
        kci_start_year=int(os.getenv("KCI_START_YEAR", "2022")),
        kci_end_year=_default_end_year(os.getenv("KCI_END_YEAR")),
        kci_max_workers=max(1, int(os.getenv("KCI_MAX_WORKERS", "4"))),
        kci_sweep_max_workers=max(1, int(os.getenv("KCI_SWEEP_MAX_WORKERS", "1"))),
        kci_enable_sweep=_parse_bool(os.getenv("KCI_ENABLE_SWEEP"), default=True),
        kci_rate_limit_rps=max(0.0, float(os.getenv("KCI_RATE_LIMIT_RPS", "2"))),
        database_url=os.getenv("DATABASE_URL", "sqlite:///papers.db"),
        export_dir=Path(os.getenv("EXPORT_DIR", "reports")),
        kci_keyword_filter_keywords_path=_reference_path(
            "KCI_KEYWORD_FILTER_KEYWORDS_PATH",
            "Processor/refs/keywords_json.ko",
            "refs/keywords_json.ko",
            "filters/keywords_ko.json",
        ),
        kci_keyword_filter_english_keywords_path=_reference_path(
            "KCI_KEYWORD_FILTER_ENGLISH_KEYWORDS_PATH",
            "Processor/refs/keywords_json.en",
            "refs/keywords_json.en",
            "filters/keywords_en.json",
        ),
        kci_blacklist_path=_reference_path(
            "KCI_BLACKLIST_PATH",
            "Processor/refs/blacklist.list",
            "refs/blacklist.list",
            "filters/blacklist.txt",
        ),
        state_key_last_success_at=os.getenv("STATE_KEY_LAST_SUCCESS_AT", "kci:last_success_at"),
    )

    if validate and not settings.kci_open_api_key:
        raise ValueError("KCI_OPEN_API_KEY is not set")

    return settings
