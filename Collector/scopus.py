from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import time
import random
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from Core.query_builder import ScopusQuery


class ScopusApiError(RuntimeError):
    """Raised when Scopus returns a non-success response."""


class ScopusRateLimitError(ScopusApiError):
    """Raised when Scopus keeps returning HTTP 429 after retries."""


@dataclass
class ScopusRawPaper:
    """Raw paper data returned by Scopus Search API."""

    matched_query: str
    eid: str | None
    scopus_id: str | None
    doi: str | None
    title: str | None
    abstract: str | None
    url: str | None
    cover_date: str | None
    publication_name: str | None
    volume: str | None
    issue_identifier: str | None
    issn: str | None
    eissn: str | None
    author_names: list[str]
    keywords: list[str]
    cited_by_count: int | None
    subtype: str | None
    subtype_description: str | None
    raw_item: dict[str, Any]


@dataclass
class ScopusAbstractDetail:
    title: str | None
    abstract: str | None
    url: str | None
    cover_date: str | None
    publication_name: str | None
    publisher: str | None
    issn: str | None
    eissn: str | None
    author_names: list[str]
    keywords: list[str]
    subject_areas: list[dict[str, str]]
    cited_by_count: int | None
    raw_payload: dict[str, Any]


class ScopusCollector:
    _rate_limit_lock = threading.Lock()
    _next_key_index_by_family: dict[str, int] = {}
    _next_request_at_by_key: dict[tuple[str, str], float] = {}
    _pause_until_by_key: dict[tuple[str, str], float] = {}
    _exhausted_api_keys_by_family: set[tuple[str, str]] = set()
    _abstract_field_aliases = (
        "url",
        "title",
        "description",
        "coverDate",
        "publicationName",
        "publisher",
        "issn",
        "eIssn",
        "authors",
        "authkeywords",
        "subject-area",
    )

    def __init__(
        self,
        api_key: str | None,
        *,
        api_keys: tuple[str, ...] | None = None,
        api_root: str,
        timeout: int = 120,
        page_size: int = 25,
        session: requests.Session | None = None,
        retry_count: int = 5,
        retry_backoff_seconds: float = 1.0,
        requests_per_second: float = 5.0,
        search_requests_per_second: float | None = None,
        abstract_requests_per_second: float | None = None,
        abstract_require_response: bool = False,
    ) -> None:
        self.api_keys = self._normalize_api_keys(api_key, api_keys)
        self.api_root = api_root.rstrip("/")
        self.search_url = f"{self.api_root}/search/scopus"
        self.timeout = timeout
        self.page_size = max(1, page_size)
        self.session = session or requests.Session()
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds
        search_rps = search_requests_per_second if search_requests_per_second is not None else requests_per_second
        abstract_rps = abstract_requests_per_second if abstract_requests_per_second is not None else requests_per_second
        self.min_request_interval_seconds_by_family = {
            "search": 1.0 / search_rps if search_rps > 0 else 0.0,
            "abstract": 1.0 / abstract_rps if abstract_rps > 0 else 0.0,
        }
        self.require_terminal_response_by_family = {
            "search": False,
            "abstract": abstract_require_response,
        }

    @staticmethod
    def _normalize_api_keys(api_key: str | None, api_keys: tuple[str, ...] | None) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for candidate in (api_key, *(api_keys or ())):
            if not candidate:
                continue
            key = candidate.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        if not result:
            raise ValueError("SCOPUS_API_KEY or SCOPUS_FALLBACK_API_KEYS is required")
        return tuple(result)

    @classmethod
    def _reserve_api_key_slot(
        cls,
        *,
        api_keys: tuple[str, ...],
        family: str,
        min_interval_seconds: float,
    ) -> str:
        while True:
            with cls._rate_limit_lock:
                now = time.monotonic()
                active_keys = [
                    key
                    for key in api_keys
                    if (family, key) not in cls._exhausted_api_keys_by_family
                ]
                if not active_keys:
                    raise ScopusRateLimitError(f"All Scopus API keys are exhausted for {family}")

                start_index = cls._next_key_index_by_family.get(family, 0)
                best_wait_seconds: float | None = None
                for offset in range(len(active_keys)):
                    active_index = (start_index + offset) % len(active_keys)
                    api_key = active_keys[active_index]
                    key = (family, api_key)
                    ready_at = max(
                        cls._pause_until_by_key.get(key, 0.0),
                        cls._next_request_at_by_key.get(key, 0.0),
                    )
                    wait_seconds = ready_at - now
                    if wait_seconds <= 0:
                        cls._next_key_index_by_family[family] = (active_index + 1) % len(active_keys)
                        cls._next_request_at_by_key[key] = now + max(0.0, min_interval_seconds)
                        return api_key
                    if best_wait_seconds is None or wait_seconds < best_wait_seconds:
                        best_wait_seconds = wait_seconds
            wait_seconds = best_wait_seconds if best_wait_seconds is not None else 1.0
            time.sleep(min(wait_seconds, 1.0))

    @classmethod
    def _mark_api_key_exhausted(cls, family: str, api_key: str) -> None:
        with cls._rate_limit_lock:
            cls._exhausted_api_keys_by_family.add((family, api_key))

    @classmethod
    def _pause_api_key(cls, family: str, api_key: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with cls._rate_limit_lock:
            key = (family, api_key)
            candidate = time.monotonic() + seconds
            if candidate > cls._pause_until_by_key.get(key, 0.0):
                cls._pause_until_by_key[key] = candidate

    @classmethod
    def _has_available_api_key(cls, family: str, api_keys: tuple[str, ...]) -> bool:
        with cls._rate_limit_lock:
            return any((family, api_key) not in cls._exhausted_api_keys_by_family for api_key in api_keys)

    @staticmethod
    def _is_quota_exceeded_response(response: requests.Response) -> bool:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                return int(remaining) <= 0
            except ValueError:
                pass
        return (
            response.status_code == 429
            and not response.headers.get("Retry-After")
            and not response.headers.get("X-RateLimit-Limit")
            and not response.headers.get("X-RateLimit-Remaining")
        )

    @staticmethod
    def _parse_retry_after_seconds(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delta = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)

    def _compute_retry_delay(self, *, attempt: int, response: requests.Response | None) -> float:
        base = self.retry_backoff_seconds * (2 ** min(attempt, 7))
        jitter = random.uniform(0.0, max(0.1, self.retry_backoff_seconds))
        delay = base + jitter
        retry_after = self._parse_retry_after_seconds(response) if response is not None else None
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, 120.0)

    def _requires_terminal_response(self, *, request_family: str, response: requests.Response | None) -> bool:
        if not self.require_terminal_response_by_family.get(request_family, False):
            return False
        if response is None:
            return True
        return response.status_code == 429 or response.status_code >= 500

    def _request_json_with_retries(
        self,
        *,
        url: str,
        params: dict[str, Any],
        expected_root_key: str,
        request_family: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempt = 0

        while True:
            try:
                api_key = self._reserve_api_key_slot(
                    api_keys=self.api_keys,
                    family=request_family,
                    min_interval_seconds=self.min_request_interval_seconds_by_family.get(request_family, 0.0),
                )
            except ScopusRateLimitError as exc:
                last_error = exc
                break
            headers = {
                "X-ELS-APIKey": api_key,
                "Accept": "application/json",
            }
            response: requests.Response | None = None
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    last_error = ScopusRateLimitError(
                        f"Scopus rate-limited request (429): url={url} retry_after={response.headers.get('Retry-After')}"
                    )
                    if self._is_quota_exceeded_response(response):
                        self._mark_api_key_exhausted(request_family, api_key)
                        if self._has_available_api_key(request_family, self.api_keys):
                            attempt = 0
                            continue
                        break
                    delay = self._compute_retry_delay(attempt=attempt, response=response)
                    self._pause_api_key(request_family, api_key, delay)
                    if attempt >= self.retry_count and not self._requires_terminal_response(
                        request_family=request_family,
                        response=response,
                    ):
                        break
                    attempt += 1
                    continue

                response.raise_for_status()
                payload = response.json()
                if expected_root_key not in payload:
                    raise ScopusApiError(f"Scopus response missing {expected_root_key}")
                return payload
            except (requests.RequestException, ValueError, ScopusApiError) as exc:
                last_error = exc
                if response is not None and 400 <= response.status_code < 500 and response.status_code != 429:
                    break
                if attempt >= self.retry_count and not self._requires_terminal_response(
                    request_family=request_family,
                    response=response,
                ):
                    break
                delay = self._compute_retry_delay(attempt=attempt, response=response)
                if response is not None and response.status_code in (429, 503):
                    self._pause_api_key(request_family, api_key, delay)
                time.sleep(delay)
                attempt += 1

        if isinstance(last_error, ScopusRateLimitError):
            raise ScopusRateLimitError(f"Failed to fetch Scopus data: {last_error}")
        raise ScopusApiError(f"Failed to fetch Scopus data: {last_error}")

    def collect(
        self,
        query: ScopusQuery,
        *,
        max_pages: int | None = None,
        start_offset: int = 0,
    ) -> list[ScopusRawPaper]:
        _ = start_offset
        pages_fetched = 0
        cursor = "*"
        papers: list[ScopusRawPaper] = []

        while True:
            payload = self._request_page(query=query, cursor=cursor)
            page_items, total_count = self._parse_items(payload, matched_query=query.query)
            pages_fetched += 1

            if not page_items:
                break

            papers.extend(page_items)
            next_cursor = self._extract_next_cursor(payload)
            if not next_cursor or next_cursor == cursor:
                break

            if max_pages is not None and pages_fetched >= max_pages:
                break
            if len(papers) >= total_count:
                break
            cursor = next_cursor

        return papers

    def collect_many(
        self,
        queries: list[ScopusQuery],
        *,
        max_pages: int | None = None,
        start_offset: int = 0,
        existing_source_ids: set[str] | None = None,
        exclude_erratum: bool = True,
    ) -> list[ScopusRawPaper]:
        aggregated: list[ScopusRawPaper] = []
        seen_source_ids: set[str] = set()
        known_source_ids = existing_source_ids or set()
        for query in queries:
            aggregated.extend(
                self._collect_incremental(
                    query=query,
                    max_pages=max_pages,
                    start_offset=start_offset,
                    existing_source_ids=known_source_ids,
                    run_seen_source_ids=seen_source_ids,
                    exclude_erratum=exclude_erratum,
                )
            )
        return aggregated

    def _collect_incremental(
        self,
        *,
        query: ScopusQuery,
        max_pages: int | None,
        start_offset: int,
        existing_source_ids: set[str],
        run_seen_source_ids: set[str],
        exclude_erratum: bool,
    ) -> list[ScopusRawPaper]:
        _ = start_offset
        pages_fetched = 0
        cursor = "*"
        papers: list[ScopusRawPaper] = []

        while True:
            payload = self._request_page(query=query, cursor=cursor)
            page_items, total_count = self._parse_items(payload, matched_query=query.query)
            pages_fetched += 1
            if not page_items:
                break

            for paper in page_items:
                source_id = paper.scopus_id or paper.eid or paper.doi
                is_erratum = (paper.subtype_description or "").casefold() == "erratum"

                if exclude_erratum and is_erratum:
                    continue

                if source_id and source_id in run_seen_source_ids:
                    continue

                if source_id and source_id in existing_source_ids:
                    continue

                papers.append(paper)
                if source_id:
                    run_seen_source_ids.add(source_id)

            next_cursor = self._extract_next_cursor(payload)
            if not next_cursor or next_cursor == cursor:
                break

            if max_pages is not None and pages_fetched >= max_pages:
                break
            if len(papers) >= total_count:
                break
            cursor = next_cursor

        return papers

    @staticmethod
    def _extract_next_cursor(payload: dict[str, Any]) -> str | None:
        search_results = payload.get("search-results", {})
        cursor = search_results.get("cursor") if isinstance(search_results, dict) else None
        if isinstance(cursor, dict):
            next_cursor = cursor.get("@next")
            if isinstance(next_cursor, str) and next_cursor:
                return next_cursor
        links = search_results.get("link") if isinstance(search_results, dict) else None
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict) or link.get("@ref") != "next":
                    continue
                href = link.get("@href")
                if not isinstance(href, str):
                    continue
                marker = "cursor="
                if marker not in href:
                    continue
                return href.split(marker, 1)[1].split("&", 1)[0]
        return None

    def _request_page(self, query: ScopusQuery, *, cursor: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query.query,
            "cursor": cursor,
            "count": self.page_size,
        }
        if query.view:
            params["view"] = query.view
        if query.sort:
            params["sort"] = query.sort
        if query.date:
            params["date"] = query.date
        if query.subj:
            params["subj"] = query.subj

        return self._request_json_with_retries(
            url=self.search_url,
            params=params,
            expected_root_key="search-results",
            request_family="search",
        )

    def _parse_items(self, payload: dict[str, Any], *, matched_query: str) -> tuple[list[ScopusRawPaper], int]:
        search_results = payload.get("search-results", {})
        total_results = search_results.get("opensearch:totalResults") or "0"
        try:
            total_count = int(total_results)
        except (TypeError, ValueError):
            total_count = 0

        entries = search_results.get("entry", [])
        if not isinstance(entries, list):
            entries = []

        parsed = [self._parse_item(entry, matched_query=matched_query) for entry in entries]
        return parsed, total_count

    def _parse_item(self, item: dict[str, Any], *, matched_query: str) -> ScopusRawPaper:
        eid = self._clean_text(item.get("eid"))
        scopus_id = self._extract_scopus_id(item)
        title = self._clean_text(item.get("dc:title"))
        abstract = self._clean_text(item.get("dc:description"))
        cover_date = self._clean_text(item.get("prism:coverDate"))

        cited_by_count: int | None = None
        cited_by_raw = self._clean_text(item.get("citedby-count"))
        if cited_by_raw is not None:
            try:
                cited_by_count = int(cited_by_raw)
            except ValueError:
                cited_by_count = None

        return ScopusRawPaper(
            matched_query=matched_query,
            eid=eid,
            scopus_id=scopus_id,
            doi=self._clean_text(item.get("prism:doi")),
            title=title,
            abstract=abstract,
            url=self._extract_url(item),
            cover_date=cover_date,
            publication_name=self._clean_text(item.get("prism:publicationName")),
            volume=self._clean_text(item.get("prism:volume")),
            issue_identifier=self._clean_text(item.get("prism:issueIdentifier")),
            issn=self._clean_text(item.get("prism:issn")),
            eissn=self._clean_text(item.get("prism:eIssn")),
            author_names=self._extract_authors(item),
            keywords=self._extract_keywords(item),
            cited_by_count=cited_by_count,
            subtype=self._clean_text(item.get("subtype")),
            subtype_description=self._clean_text(item.get("subtypeDescription")),
            raw_item=item,
        )

    def fetch_abstract_detail(
        self,
        *,
        scopus_id: str | None,
        doi: str | None,
    ) -> ScopusAbstractDetail | None:
        """Fetch detailed abstract payload for a paper.

        Identifier priority:
        1) scopus_id
        2) doi
        """
        candidates: list[tuple[str, str]] = []
        if scopus_id:
            candidates.append(("scopus_id", scopus_id))
        if doi:
            candidates.append(("doi", doi))

        last_error: Exception | None = None
        for id_type, id_value in candidates:
            try:
                payload = self._request_abstract(identifier_type=id_type, identifier=id_value)
                return self._parse_abstract_detail(payload)
            except ScopusRateLimitError as exc:
                raise exc
            except (requests.RequestException, ValueError, ScopusApiError) as exc:
                last_error = exc
        if last_error:
            print(f"[Scopus] Abstract retrieval failed ({scopus_id or doi}): {last_error}")
        return None

    def _request_abstract(self, *, identifier_type: str, identifier: str) -> dict[str, Any]:
        url = f"{self.api_root}/abstract/{identifier_type}/{identifier}"
        params = {
            "view": "FULL",
            "field": ",".join(self._abstract_field_aliases),
        }
        return self._request_json_with_retries(
            url=url,
            params=params,
            expected_root_key="abstracts-retrieval-response",
            request_family="abstract",
        )

    def _parse_abstract_detail(self, payload: dict[str, Any]) -> ScopusAbstractDetail:
        response = payload.get("abstracts-retrieval-response", {})
        core = response.get("coredata", {})

        title = self._clean_text(core.get("dc:title"))
        abstract = self._clean_text(core.get("dc:description"))
        cover_date = self._clean_text(core.get("prism:coverDate"))
        publication_name = self._clean_text(core.get("prism:publicationName"))
        publisher = self._clean_text(core.get("dc:publisher"))
        issn = self._clean_text(core.get("prism:issn"))
        eissn = self._clean_text(core.get("prism:eIssn"))
        url = self._extract_url(core)

        cited_by_count: int | None = None
        cited_by_raw = self._clean_text(core.get("citedby-count"))
        if cited_by_raw is not None:
            try:
                cited_by_count = int(cited_by_raw)
            except ValueError:
                cited_by_count = None

        author_names = self._extract_authors_from_abstract_response(response)
        keywords = self._extract_keywords_from_abstract_response(response)
        subject_areas = self._extract_subject_areas_from_abstract_response(response)

        return ScopusAbstractDetail(
            title=title,
            abstract=abstract,
            url=url,
            cover_date=cover_date,
            publication_name=publication_name,
            publisher=publisher,
            issn=issn,
            eissn=eissn,
            author_names=author_names,
            keywords=keywords,
            subject_areas=subject_areas,
            cited_by_count=cited_by_count,
            raw_payload=payload,
        )

    @staticmethod
    def _extract_scopus_id(item: dict[str, Any]) -> str | None:
        identifier = ScopusCollector._clean_text(item.get("dc:identifier"))
        if not identifier:
            return None
        if identifier.startswith("SCOPUS_ID:"):
            return identifier.split(":", 1)[1]
        return identifier

    @staticmethod
    def _extract_url(item: dict[str, Any]) -> str | None:
        links = item.get("link")
        if not isinstance(links, list):
            return None
        for link in links:
            if not isinstance(link, dict):
                continue
            href = ScopusCollector._clean_text(link.get("@href"))
            ref = ScopusCollector._clean_text(link.get("@ref"))
            if href and ref == "scopus":
                return href
        for link in links:
            if not isinstance(link, dict):
                continue
            href = ScopusCollector._clean_text(link.get("@href"))
            if href:
                return href
        return None

    @staticmethod
    def _extract_authors(item: dict[str, Any]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        creator = ScopusCollector._clean_text(item.get("dc:creator"))
        if creator:
            key = creator.casefold()
            seen.add(key)
            names.append(creator)

        authors_block = item.get("author")
        if isinstance(authors_block, list):
            for author in authors_block:
                if not isinstance(author, dict):
                    continue
                name = ScopusCollector._clean_text(author.get("authname") or author.get("ce:indexed-name"))
                if not name:
                    continue
                key = name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                names.append(name)

        return names

    @staticmethod
    def _extract_keywords(item: dict[str, Any]) -> list[str]:
        values = [
            ScopusCollector._clean_text(item.get("authkeywords")),
            ScopusCollector._clean_text(item.get("idxterms")),
        ]
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            for token in value.replace("|", ";").split(";"):
                cleaned = token.strip()
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key in seen:
                    continue
                seen.add(key)
                result.append(cleaned)
        return result

    @staticmethod
    def _extract_authors_from_abstract_response(response: dict[str, Any]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        authors_node = response.get("authors", {})
        authors = authors_node.get("author") if isinstance(authors_node, dict) else []
        if isinstance(authors, dict):
            authors = [authors]
        if not isinstance(authors, list):
            return names
        for author in authors:
            if not isinstance(author, dict):
                continue
            candidate = author.get("ce:indexed-name") or author.get("ce:surname")
            name = ScopusCollector._clean_text(candidate)
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    @staticmethod
    def _extract_keywords_from_abstract_response(response: dict[str, Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        auth_keywords = response.get("authkeywords", {})
        keyword_nodes = auth_keywords.get("author-keyword") if isinstance(auth_keywords, dict) else []
        if isinstance(keyword_nodes, dict):
            keyword_nodes = [keyword_nodes]
        if isinstance(keyword_nodes, list):
            for keyword_node in keyword_nodes:
                if isinstance(keyword_node, dict):
                    candidate = keyword_node.get("$")
                else:
                    candidate = keyword_node
                keyword = ScopusCollector._clean_text(candidate)
                if not keyword:
                    continue
                key = keyword.casefold()
                if key in seen:
                    continue
                seen.add(key)
                result.append(keyword)

        return result

    @staticmethod
    def _extract_subject_areas_from_abstract_response(response: dict[str, Any]) -> list[dict[str, str]]:
        normalized_result: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        subject_areas = response.get("subject-areas", {})
        subject_nodes = subject_areas.get("subject-area") if isinstance(subject_areas, dict) else []
        if isinstance(subject_nodes, dict):
            subject_nodes = [subject_nodes]
        if not isinstance(subject_nodes, list):
            return normalized_result

        for subject_node in subject_nodes:
            if not isinstance(subject_node, dict):
                continue
            # Elsevier JSON usually exposes XML attributes with @-prefix.
            code = ScopusCollector._clean_text(subject_node.get("@code") or subject_node.get("code"))
            abbrev = ScopusCollector._clean_text(subject_node.get("@abbrev") or subject_node.get("abbrev"))
            name = ScopusCollector._clean_text(subject_node.get("$") or subject_node.get("name"))
            if not code:
                continue
            key = (code, abbrev or "", name or "")
            if key in seen:
                continue
            seen.add(key)
            payload: dict[str, str] = {"code": code}
            if abbrev:
                payload["abbrev"] = abbrev
            if name:
                payload["name"] = name
            normalized_result.append(payload)

        return normalized_result

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def to_dict(paper: ScopusRawPaper) -> dict[str, Any]:
        return asdict(paper)
