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
    _global_pause_until_monotonic = 0.0
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
        api_key: str,
        *,
        api_root: str,
        timeout: int = 120,
        page_size: int = 25,
        session: requests.Session | None = None,
        retry_count: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.api_root = api_root.rstrip("/")
        self.search_url = f"{self.api_root}/search/scopus"
        self.timeout = timeout
        self.page_size = max(1, page_size)
        self.session = session or requests.Session()
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds

    @classmethod
    def _wait_for_global_pause(cls) -> None:
        while True:
            with cls._rate_limit_lock:
                remaining = cls._global_pause_until_monotonic - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    @classmethod
    def _extend_global_pause(cls, seconds: float) -> None:
        if seconds <= 0:
            return
        with cls._rate_limit_lock:
            candidate = time.monotonic() + seconds
            if candidate > cls._global_pause_until_monotonic:
                cls._global_pause_until_monotonic = candidate

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
        base = self.retry_backoff_seconds * (2**attempt)
        jitter = random.uniform(0.0, max(0.1, self.retry_backoff_seconds))
        delay = base + jitter
        retry_after = self._parse_retry_after_seconds(response) if response is not None else None
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, 120.0)

    def _request_json_with_retries(
        self,
        *,
        url: str,
        params: dict[str, Any],
        expected_root_key: str,
    ) -> dict[str, Any]:
        headers = {
            "X-ELS-APIKey": self.api_key,
            "Accept": "application/json",
        }
        last_error: Exception | None = None

        for attempt in range(self.retry_count + 1):
            self._wait_for_global_pause()
            response: requests.Response | None = None
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    delay = self._compute_retry_delay(attempt=attempt, response=response)
                    self._extend_global_pause(delay)
                    last_error = ScopusApiError(
                        f"Scopus rate-limited request (429): url={url} retry_after={response.headers.get('Retry-After')}"
                    )
                    if attempt >= self.retry_count:
                        break
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                payload = response.json()
                if expected_root_key not in payload:
                    raise ScopusApiError(f"Scopus response missing {expected_root_key}")
                return payload
            except (requests.RequestException, ValueError, ScopusApiError) as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                delay = self._compute_retry_delay(attempt=attempt, response=response)
                if response is not None and response.status_code in (429, 503):
                    self._extend_global_pause(delay)
                time.sleep(delay)

        raise ScopusApiError(f"Failed to fetch Scopus data: {last_error}")

    def collect(
        self,
        query: ScopusQuery,
        *,
        max_pages: int | None = None,
        start_offset: int = 0,
    ) -> list[ScopusRawPaper]:
        page_no = 1
        start = max(0, start_offset)
        papers: list[ScopusRawPaper] = []

        while True:
            payload = self._request_page(query=query, start=start)
            page_items, total_count = self._parse_items(payload, matched_query=query.query)

            if not page_items:
                break

            papers.extend(page_items)
            if len(papers) >= total_count:
                break

            page_no += 1
            if max_pages is not None and page_no > max_pages:
                break
            start += self.page_size

        return papers

    def collect_many(
        self,
        queries: list[ScopusQuery],
        *,
        max_pages: int | None = None,
        start_offset: int = 0,
        existing_source_ids: set[str] | None = None,
        overlap_count: int = 20,
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
                    overlap_count=overlap_count,
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
        overlap_count: int,
        exclude_erratum: bool,
    ) -> list[ScopusRawPaper]:
        page_no = 1
        start = max(0, start_offset)
        papers: list[ScopusRawPaper] = []
        effective_overlap = max(0, min(overlap_count, self.page_size - 1))
        step = max(1, self.page_size - effective_overlap)

        while True:
            payload = self._request_page(query=query, start=start)
            page_items, total_count = self._parse_items(payload, matched_query=query.query)
            if not page_items:
                break

            for paper in page_items:
                source_id = paper.scopus_id
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

            if len(papers) >= total_count:
                break

            page_no += 1
            if max_pages is not None and page_no > max_pages:
                break
            start += step

        return papers

    def _request_page(self, query: ScopusQuery, *, start: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query.query,
            "start": start,
            "count": self.page_size,
        }
        if query.view:
            params["view"] = query.view

        return self._request_json_with_retries(
            url=self.search_url,
            params=params,
            expected_root_key="search-results",
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
