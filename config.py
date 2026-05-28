"""Application settings and environment loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _safe_load_dotenv() -> None:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "cp1252", "latin-1"):
        try:
            load_dotenv(encoding=encoding)
            if (
                os.getenv("KCI_SERVICE_KEY")
                or os.getenv("KCI_API_KEY")
                or os.getenv("SCOPUS_API_KEY")
                or os.getenv("SCOPUS_API_KEYS")
                or os.getenv("SCOPUS_FALLBACK_API_KEYS")
            ):
                return
        except UnicodeDecodeError:
            continue


_safe_load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    kci_service_key: str | None
    scopus_api_key: str | None
    scopus_api_keys: tuple[str, ...] = ()
    kci_base_url: str = "http://apis.data.go.kr/B552540/KCIOpenApi/artiInfo/openApiM310List"
    scopus_api_root: str = "https://api.elsevier.com/content"
    request_timeout: int = 30
    kci_page_size: int = 100
    scopus_page_size: int = 25
    kci_max_pages: int | None = None
    scopus_max_pages: int | None = None
    database_url: str = "sqlite:///papers.db"
    export_dir: Path = Path("reports")
    scopus_subject_code_allowlist_path: Path = Path("filters/scopus_subject_codes.json")
    scopus_keyword_filter_keywords_path: Path = Path("filters/keywords_scopus.json")
    scopus_journal_filter_path: Path = Path("filters/scopus_journals.yaml")
    scopus_source_chunk_size: int = 1
    kci_keyword_filter_keywords_path: Path = Path("filters/keywords_ko.json")
    scopus_abstract_max_workers: int = 6
    scopus_requests_per_second: float = 5.0
    state_key_last_success_at: str = "kci:last_success_at"
    state_key_scopus_last_success_at: str = "scopus:last_success_at"

    @property
    def has_service_key(self) -> bool:
        return bool(self.kci_service_key)


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _parse_api_key_list(*values: str | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for token in re.split(r"[\s,;]+", value.strip()):
            key = token.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
    return tuple(result)


@lru_cache(maxsize=1)
def load_settings(validate: bool = True) -> Settings:
    service_key = os.getenv("KCI_SERVICE_KEY") or os.getenv("KCI_API_KEY")
    scopus_api_key = os.getenv("SCOPUS_API_KEY")
    scopus_api_keys = _parse_api_key_list(
        scopus_api_key,
        os.getenv("SCOPUS_API_KEYS"),
        os.getenv("SCOPUS_FALLBACK_API_KEYS"),
    )

    settings = Settings(
        kci_service_key=service_key,
        scopus_api_key=scopus_api_key,
        scopus_api_keys=scopus_api_keys,
        kci_base_url=os.getenv(
            "KCI_BASE_URL",
            "http://apis.data.go.kr/B552540/KCIOpenApi/artiInfo/openApiM310List",
        ),
        scopus_api_root=os.getenv("SCOPUS_API_ROOT") or os.getenv(
            "SCOPUS_BASE_URL",
            "https://api.elsevier.com/content",
        ),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        kci_page_size=int(os.getenv("KCI_PAGE_SIZE", "100")),
        scopus_page_size=int(os.getenv("SCOPUS_PAGE_SIZE", "25")),
        kci_max_pages=_parse_optional_int(os.getenv("KCI_MAX_PAGES")),
        scopus_max_pages=_parse_optional_int(os.getenv("SCOPUS_MAX_PAGES")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///papers.db"),
        export_dir=Path(os.getenv("EXPORT_DIR", "reports")),
        scopus_subject_code_allowlist_path=Path(
            os.getenv("SCOPUS_SUBJECT_CODE_ALLOWLIST_PATH", "filters/scopus_subject_codes.json")
        ),
        scopus_keyword_filter_keywords_path=Path(
            os.getenv("SCOPUS_KEYWORD_FILTER_KEYWORDS_PATH", "filters/keywords_scopus.json")
        ),
        scopus_journal_filter_path=Path(
            os.getenv("SCOPUS_JOURNAL_FILTER_PATH", "filters/scopus_journals.yaml")
        ),
        scopus_source_chunk_size=max(1, int(os.getenv("SCOPUS_SOURCE_CHUNK_SIZE", "1"))),
        kci_keyword_filter_keywords_path=Path("filters/keywords_ko.json"),
        scopus_abstract_max_workers=max(1, int(os.getenv("SCOPUS_ABSTRACT_MAX_WORKERS", "6"))),
        scopus_requests_per_second=max(0.1, float(os.getenv("SCOPUS_REQUESTS_PER_SECOND", "5"))),
        state_key_last_success_at=os.getenv("STATE_KEY_LAST_SUCCESS_AT", "kci:last_success_at"),
        state_key_scopus_last_success_at=os.getenv(
            "STATE_KEY_SCOPUS_LAST_SUCCESS_AT",
            "scopus:last_success_at",
        ),
    )

    if validate and not settings.kci_service_key:
        raise ValueError("KCI_SERVICE_KEY (or KCI_API_KEY) is not set")

    return settings
