# kci.py #
"""
    - KCI Open API (open.kci.go.kr/po/openapi/openApiSearch.kci, apiCode=articleSearch)
    - 페이지네이션·재시도 처리, XML 응답 파싱 -> KciRawPaper 리스트 반환
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, asdict, field
from html import unescape
from typing import Any, Callable

import requests
from lxml import etree

from Core.query_builder import KciQuery


ProgressCallback = Callable[[str], None]

# KCI articleSearch returns at most 10,000 results per query (100 pages × 100/page).
# Requesting page 101 returns an empty response, so we cap pagination defensively.
MAX_PAGES_PER_QUERY: int = 100
MAX_RESULTS_PER_QUERY: int = MAX_PAGES_PER_QUERY * 100


class _TokenBucketLimiter:
    """Global rate limiter shared across all worker threads.

    KCI rate-limits per service key. Without pacing, N workers burst N
    simultaneous requests, which trips KCI's burst limiter and triggers
    slow-drip throttling on subsequent requests for the rest of the run.
    """

    def __init__(self, rate_per_second: float, burst: float | None = None) -> None:
        self.max_rate = max(0.001, float(rate_per_second))  # configured ceiling
        self.rate = self.max_rate
        self.min_rate = min(0.5, self.max_rate)
        self.capacity = max(1.0, float(burst) if burst is not None else 1.0)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        # AIMD recovery: after this many consecutive fast responses, nudge the
        # rate back up by `_increase_step` toward the ceiling.
        self._consecutive_ok = 0
        self._ramp_after = 5
        self._increase_step = max(0.05, self.max_rate * 0.1)

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                self._lock.release()
                try:
                    time.sleep(wait)
                finally:
                    self._lock.acquire()

    def slow_down(self) -> None:
        """Globally back off after KCI starts slow-dripping or timing out."""
        with self._lock:
            self.rate = max(self.min_rate, self.rate * 0.5)
            self.capacity = 1.0
            self._tokens = 0.0
            self._last_refill = time.monotonic()
            self._consecutive_ok = 0

    def speed_up(self) -> None:
        """Additive recovery toward the configured ceiling after sustained fast
        responses.

        `slow_down()` alone is one-way: a single transient KCI hiccup would
        otherwise halve the rate for the rest of a multi-hour run (the cause of
        the "gets slower the longer it runs" symptom). AIMD lets the rate climb
        back once KCI is healthy again."""
        with self._lock:
            if self.rate >= self.max_rate:
                self._consecutive_ok = 0
                return
            self._consecutive_ok += 1
            if self._consecutive_ok >= self._ramp_after:
                self._consecutive_ok = 0
                self.rate = min(self.max_rate, self.rate + self._increase_step)


class KciApiError(RuntimeError):
    """Raised when KCI returns a non-success response."""


@dataclass
class KciRawPaper:
    """Raw paper data returned by articleSearch."""

    matched_query: str
    matched_field: str           # "title" / "keyword" / "sweep" / "journal" / "detail"
    arti_id: str | None
    journal_id: str | None       # only present in articleDetail; None for search
    journal_name: str | None
    publisher_name: str | None
    pub_year: str | None
    pub_mon: str | None
    volume: str | None
    issue: str | None
    article_categories: str | None
    article_regularity: str | None
    title_original: str | None
    title_foreign: str | None
    title_english: str | None
    abstract_original: str | None
    abstract_english: str | None
    keywords: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    uci: str | None = None
    url: str | None = None
    first_page: str | None = None
    last_page: str | None = None
    issn: str | None = None
    eissn: str | None = None
    citation_count_kci: str | None = None
    citation_count_wos: str | None = None
    is_open_access: str | None = None
    verified: str | None = None
    raw_xml: str | None = None


class KciCollector:
    """Client for the open.kci.go.kr articleSearch endpoint."""

    API_CODE = "articleSearch"
    DETAIL_API_CODE = "articleDetail"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://open.kci.go.kr/po/openapi/openApiSearch.kci",
        timeout: int = 30,
        page_size: int = 100,
        session: requests.Session | None = None,
        retry_count: int = 3,
        retry_backoff_seconds: float = 2.0,
        rate_limit_rps: float | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.page_size = min(max(1, page_size), 100)  # API max is 100
        self.session = session
        self._thread_local = threading.local()
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds
        self._rate_limiter = (
            _TokenBucketLimiter(rate_limit_rps) if rate_limit_rps and rate_limit_rps > 0 else None
        )

    # ---------- Public API ----------

    def collect(
        self,
        query: KciQuery,
        *,
        max_pages: int | None = None,
        on_page: ProgressCallback | None = None,
    ) -> list[KciRawPaper]:
        """Paginate through articleSearch results for a single query."""
        papers: list[KciRawPaper] = []
        for page_items, _total_count in self.iter_pages(
            query,
            max_pages=max_pages,
            on_page=on_page,
        ):
            papers.extend(page_items)
        return papers

    def iter_pages(
        self,
        query: KciQuery,
        *,
        max_pages: int | None = None,
        on_page: ProgressCallback | None = None,
    ) -> Iterator[tuple[list[KciRawPaper], int]]:
        """Yield articleSearch pages without retaining the whole query result."""
        if not query.title and not query.keyword:
            raise ValueError("KciQuery must have at least one of title/keyword set")

        matched_field = "title" if query.title and query.title != "*" else ("keyword" if query.keyword else "sweep")
        if query.title == "*":
            matched_field = "sweep"
        matched_query = query.keyword or query.title

        # Cap pagination at the API's 10,000-result hard limit unless the caller
        # asked for an even smaller window. Going beyond is a wasted round-trip.
        effective_cap = MAX_PAGES_PER_QUERY
        if max_pages is not None:
            effective_cap = min(effective_cap, max_pages)

        page_no = 1
        fetched_count = 0
        while True:
            root = self._request_page(query=query, page_no=page_no)
            page_items, total_count = self._parse_items(
                root,
                matched_query=matched_query,
                matched_field=matched_field,
            )
            fetched_count += len(page_items)
            if on_page is not None:
                on_page(
                    f"page {page_no}: +{len(page_items)} "
                    f"(total so far {fetched_count} / {total_count})"
                )
            yield page_items, total_count
            if not page_items:
                break
            if fetched_count >= total_count:
                break
            if page_no >= effective_cap:
                break
            page_no += 1

    def probe_total(self, query: KciQuery) -> int:
        """Fetch just the first page to learn the total result count.

        Used to detect oversized monthly sweeps that exceed the 10K result cap.
        """
        if not query.title and not query.keyword:
            raise ValueError("KciQuery must have at least one of title/keyword set")
        root = self._request_page(query=query, page_no=1)
        total_text = self._find_text(root, ".//result/total")
        try:
            return int(total_text) if total_text else 0
        except ValueError:
            return 0

    def fetch_article_detail(self, article_id: str) -> KciRawPaper | None:
        """Fetch articleDetail so fields omitted by articleSearch, especially keywords, are available."""
        root = self._request_detail(article_id)
        records = root.findall(".//record")
        if not records:
            return None
        return self._parse_record(
            records[0],
            matched_query=article_id,
            matched_field="detail",
        )

    @staticmethod
    def merge_detail_into(base: KciRawPaper, detail: KciRawPaper | None) -> KciRawPaper:
        if detail is None:
            return base

        for attr in (
            "journal_id",
            "journal_name",
            "publisher_name",
            "pub_year",
            "pub_mon",
            "volume",
            "issue",
            "article_categories",
            "article_regularity",
            "title_original",
            "title_foreign",
            "title_english",
            "abstract_original",
            "abstract_english",
            "doi",
            "uci",
            "url",
            "first_page",
            "last_page",
            "issn",
            "eissn",
            "citation_count_kci",
            "citation_count_wos",
            "is_open_access",
            "verified",
            "raw_xml",
        ):
            if getattr(base, attr) in (None, "", []):
                setattr(base, attr, getattr(detail, attr))

        seen_authors = {author.casefold() for author in base.authors}
        for author in detail.authors:
            key = author.casefold()
            if key in seen_authors:
                continue
            seen_authors.add(key)
            base.authors.append(author)

        seen_keywords = {keyword.casefold() for keyword in base.keywords}
        for keyword in detail.keywords:
            key = keyword.casefold()
            if key in seen_keywords:
                continue
            seen_keywords.add(key)
            base.keywords.append(keyword)

        return base

    # ---------- HTTP ----------

    def _build_params(self, query: KciQuery, page_no: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "apiCode": self.API_CODE,
            "key": self.api_key,
            "page": page_no,
            "displayCount": self.page_size,
        }
        if query.title:
            params["title"] = query.title
        if query.keyword:
            params["keyword"] = query.keyword
        if query.date_from:
            params["dateFrom"] = query.date_from
        if query.date_to:
            params["dateTo"] = query.date_to
        if query.reg_date_from:
            params["regDateFrom"] = query.reg_date_from
        if query.reg_date_to:
            params["regDateTo"] = query.reg_date_to
        return params

    def _request_page(self, query: KciQuery, page_no: int) -> etree._Element:
        params = self._build_params(query, page_no)
        return self._request_xml(params)

    def _request_detail(self, article_id: str) -> etree._Element:
        params = {
            "apiCode": self.DETAIL_API_CODE,
            "key": self.api_key,
            "id": article_id,
        }
        return self._request_xml(params)

    def _request_xml(self, params: dict[str, Any]) -> etree._Element:
        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                if self._rate_limiter is not None:
                    self._rate_limiter.acquire()
                started_at = time.monotonic()
                # Enforce a total wallclock deadline. `requests.get(timeout=N)`
                # only sets a per-chunk read timeout — KCI throttles by drip-
                # sending tiny chunks every <N seconds, so individual requests
                # can stretch for several minutes. We stream the response and
                # abort if the cumulative time exceeds `self.timeout`.
                deadline = time.monotonic() + self.timeout
                with self._get_session().get(
                    self.base_url,
                    params=params,
                    timeout=self.timeout,
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    for chunk in response.iter_content(chunk_size=16384):
                        if time.monotonic() > deadline:
                            response.close()
                            raise requests.Timeout(
                                f"Total wallclock exceeded {self.timeout}s (slow-drip)"
                            )
                        if chunk:
                            chunks.append(chunk)
                    content = b"".join(chunks)
                root = etree.fromstring(content)
                server_msg = self._find_text(root, ".//resultMsg") or self._find_text(root, ".//message")
                if server_msg and server_msg not in ("정상 처리되었습니다.", "OK", "success"):
                    # "검색 조건이 없습니다" / "No Data" treated as empty result, not error
                    if server_msg in ("검색 조건이 없습니다.", "No Data"):
                        return root
                    raise KciApiError(f"KCI API error: {server_msg}")
                elapsed = time.monotonic() - started_at
                if self._rate_limiter is not None:
                    if elapsed >= max(5.0, self.timeout * 0.5):
                        # A very slow successful response is KCI's usual prelude
                        # to timeout-level slow-drip throttling. Back off before
                        # the next page starts stalling the whole sweep.
                        self._rate_limiter.slow_down()
                    else:
                        # Healthy response — recover toward the configured rate.
                        self._rate_limiter.speed_up()
                return root
            except (requests.RequestException, etree.XMLSyntaxError, KciApiError) as exc:
                last_error = exc
                if self._rate_limiter is not None:
                    self._rate_limiter.slow_down()
                if attempt >= self.retry_count:
                    break
                # Exponential backoff with jitter — avoids the thundering-herd
                # pattern where all workers retry in unison after a KCI hiccup.
                backoff = self.retry_backoff_seconds * (2 ** attempt)
                time.sleep(backoff + random.uniform(0, backoff))
        raise KciApiError(f"Failed to fetch KCI data: {last_error}")

    def _get_session(self) -> requests.Session:
        if self.session is not None:
            return self.session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return session

    # ---------- Parsing ----------

    def _parse_items(
        self,
        root: etree._Element,
        *,
        matched_query: str,
        matched_field: str,
    ) -> tuple[list[KciRawPaper], int]:
        total_text = self._find_text(root, ".//result/total")
        try:
            total_count = int(total_text) if total_text else 0
        except ValueError:
            total_count = 0
        records = root.findall(".//record")
        parsed = [
            self._parse_record(record, matched_query=matched_query, matched_field=matched_field)
            for record in records
        ]
        return parsed, total_count

    def _parse_record(
        self,
        record: etree._Element,
        *,
        matched_query: str,
        matched_field: str,
    ) -> KciRawPaper:
        article_info = record.find("./articleInfo")
        journal_info = record.find("./journalInfo")

        arti_id = article_info.get("article-id") if article_info is not None else None

        titles = self._extract_title_group(record)
        abstracts = self._extract_abstract_group(record)
        keywords = self._extract_keywords(record)
        authors = self._extract_authors(record)

        citation_node = record.find(".//articleInfo/citation-count")
        cited_kci = citation_node.get("kci") if citation_node is not None else None
        cited_wos = citation_node.get("wos") if citation_node is not None else None

        return KciRawPaper(
            matched_query=matched_query,
            matched_field=matched_field,
            arti_id=arti_id,
            journal_id=journal_info.get("journal-id") if journal_info is not None else None,
            journal_name=self._find_text(journal_info, "./journal-name"),
            publisher_name=self._find_text(journal_info, "./publisher-name"),
            pub_year=self._find_text(journal_info, "./pub-year"),
            pub_mon=self._find_text(journal_info, "./pub-mon"),
            volume=self._find_text(journal_info, "./volume"),
            issue=self._find_text(journal_info, "./issue"),
            article_categories=self._find_text(article_info, "./article-categories"),
            article_regularity=self._find_text(article_info, "./article-regularity"),
            title_original=titles.get("original"),
            title_foreign=titles.get("foreign"),
            title_english=titles.get("english"),
            abstract_original=abstracts.get("original"),
            abstract_english=abstracts.get("english"),
            keywords=keywords,
            authors=authors,
            doi=self._find_text(article_info, "./doi"),
            uci=self._find_text(article_info, "./uci"),
            url=self._find_text(article_info, "./url"),
            first_page=self._find_text(article_info, "./fpage"),
            last_page=self._find_text(article_info, "./lpage"),
            issn=self._find_text(journal_info, "./issn"),
            eissn=self._find_text(journal_info, "./eissn"),
            citation_count_kci=cited_kci,
            citation_count_wos=cited_wos,
            is_open_access=self._find_text(article_info, "./orte-open-yn"),
            verified=self._find_text(article_info, "./verified"),
        )

    @staticmethod
    def _extract_title_group(record: etree._Element) -> dict[str, str]:
        result: dict[str, str] = {}
        for title in record.findall(".//title-group/article-title"):
            lang = title.get("lang") or "original"
            text = KciCollector._clean_text(title.text)
            if text:
                result[lang] = text
        return result

    @staticmethod
    def _extract_abstract_group(record: etree._Element) -> dict[str, str]:
        result: dict[str, str] = {}
        # New API returns <abstract-group><abstract lang="..."> for search,
        # but older articleDetail may return a single <abstract> directly.
        for abstract in record.findall(".//abstract-group/abstract"):
            lang = abstract.get("lang") or "original"
            text = KciCollector._clean_text(abstract.text)
            if text:
                result[lang] = text
        if not result:
            single = record.find(".//abstract")
            if single is not None:
                text = KciCollector._clean_text(single.text)
                if text:
                    result["original"] = text
        return result

    @staticmethod
    def _extract_keywords(record: etree._Element) -> list[str]:
        keywords: list[str] = []
        seen: set[str] = set()
        for keyword in record.findall(".//keyword-group/keyword"):
            text = KciCollector._clean_text(keyword.text)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(text)
        return keywords

    @staticmethod
    def _extract_authors(record: etree._Element) -> list[str]:
        authors: list[str] = []
        for author in record.findall(".//author-group/author"):
            text = KciCollector._clean_text(author.text)
            if text:
                authors.append(text)
        return authors

    @staticmethod
    def _find_text(root: etree._Element | None, xpath: str) -> str | None:
        if root is None:
            return None
        node = root.find(xpath)
        if node is None or node.text is None:
            return None
        return KciCollector._clean_text(node.text)

    @staticmethod
    def _clean_text(text: str | None) -> str | None:
        if text is None:
            return None
        cleaned = unescape(text).strip()
        return cleaned or None

    @staticmethod
    def to_dict(paper: KciRawPaper) -> dict[str, Any]:
        return asdict(paper)
