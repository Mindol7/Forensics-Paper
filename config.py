# config.py #
"""
 - 환경 변수 기반 설정 관리 모듈
 - 프로젝트 전반에서 사용하는 설정을 Settings (불변 객체) 통합, 캐싱하여 재사용
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True) # immutable -> 실행 중 변경 불가
class Settings:
    """Application settings loaded from environment variables.

    Why this shape:
    - import-time side effects are avoided, so tests can import safely
    - one object can be passed through the whole pipeline
    - local development stays easy while production can still use PostgreSQL
    """

    kci_service_key: str | None
    scopus_api_key: str | None
    kci_base_url: str = "http://apis.data.go.kr/B552540/KCIOpenApi/artiInfo/openApiM310List"
    scopus_api_root: str = "https://api.elsevier.com/content"
    request_timeout: int = 30
    kci_page_size: int = 100 # 한번 요청 시 가져오는 논문의 수
    scopus_page_size: int = 25
    kci_max_pages: int | None = None # 쿼리 당 요청하는 최대 페이지 수
    scopus_max_pages: int | None = None
    database_url: str = "sqlite:///kci_pipeline.db"
    export_dir: Path = Path("reports")
    scopus_subject_code_allowlist_path: Path = Path("filters/scopus_subject_codes.json")
    scopus_keyword_filter_keywords_path: Path = Path("filters/scopus_keyword_filter_keywords.json")
    scopus_abstract_max_workers: int = 6
    state_key_last_success_at: str = "kci:last_success_at"
    state_key_scopus_last_success_at: str = "scopus:last_success_at"
    extra_query_terms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_service_key(self) -> bool:
        return bool(self.kci_service_key)

# 환경변수 파싱 util 함수들 #

# 문자열 -> 정수 변환
def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)

# CSV 문자열 -> tuple 변환
def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@lru_cache(maxsize=1) # LRU 알고리즘: 최근 사용 데이터 캐싱하여 유지, 가장 오래된 데이터는 자동 삭제
def load_settings(validate: bool = True) -> Settings:
    service_key = os.getenv("KCI_SERVICE_KEY") or os.getenv("KCI_API_KEY")
    scopus_api_key = os.getenv("SCOPUS_API_KEY")
    scopus_keyword_filter_keywords_path_raw = os.getenv("SCOPUS_KEYWORD_FILTER_KEYWORDS_PATH")

    settings = Settings(
        kci_service_key=service_key,
        scopus_api_key=scopus_api_key,
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
        database_url=os.getenv("DATABASE_URL", "sqlite:///kci_pipeline.db"),
        export_dir=Path(os.getenv("EXPORT_DIR", "reports")),
        scopus_subject_code_allowlist_path=Path(
            os.getenv("SCOPUS_SUBJECT_CODE_ALLOWLIST_PATH", "filters/scopus_subject_codes.json")
        ),
        scopus_keyword_filter_keywords_path=Path(scopus_keyword_filter_keywords_path_raw or ""),
        scopus_abstract_max_workers=max(1, int(os.getenv("SCOPUS_ABSTRACT_MAX_WORKERS", "6"))),
        state_key_last_success_at=os.getenv("STATE_KEY_LAST_SUCCESS_AT", "kci:last_success_at"),
        state_key_scopus_last_success_at=os.getenv(
            "STATE_KEY_SCOPUS_LAST_SUCCESS_AT",
            "scopus:last_success_at",
        ),
        extra_query_terms=_parse_csv(os.getenv("KCI_EXTRA_TERMS")),
    )

    if validate and not settings.kci_service_key:
        raise ValueError("KCI_SERVICE_KEY (or KCI_API_KEY) is not set")
    if not (scopus_keyword_filter_keywords_path_raw or "").strip():
        raise ValueError("SCOPUS_KEYWORD_FILTER_KEYWORDS_PATH is not set")

    return settings
