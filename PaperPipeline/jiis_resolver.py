"""Conservative public-PDF resolution for the official JIIS archive.

The Journal of Intelligence and Information Systems (JIIS) exposes a public
title search and article-detail pages.  This resolver uses only those links:

* it applies only to ``10.13088/jiis.*`` DOIs;
* every distinct known paper title is sent as the ``kw`` form field;
* titles are compared by exact project title normalization, never fuzzily;
* DOI-encoded year/volume/issue/first-page and the DB final page must agree
  with both the search result and the detail page; and
* only an explicit, same-origin ``/files/DLA/*.pdf`` download anchor is used.

JIIS detail pages do not expose a DOI, so a successful candidate deliberately
does not set :attr:`ResolverCandidate.verified_doi`.  The downloader must
therefore validate the downloaded PDF's DOI or title before accepting it.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


JIIS_ORIGIN = "https://jiisonline.org"
JIIS_HOST = "jiisonline.org"
JIIS_SEARCH_URL = f"{JIIS_ORIGIN}/index.php?mnu=search"
JIIS_RESOLVER_NAME = "jiis-public-search"

_DOI_PATTERN = re.compile(
    r"\A10\.13088/jiis\."
    r"(?P<year>[0-9]{4})\."
    r"(?P<volume>[0-9]+)\."
    r"(?P<issue>[0-9]+)\."
    r"(?P<first_page>[0-9]+)\Z",
    re.IGNORECASE,
)
_DECIMAL_PATTERN = re.compile(r"\A[0-9]+\Z")
_PDF_PATH_PATTERN = re.compile(r"\A/files/DLA/[^/]+[.]pdf\Z", re.IGNORECASE)
_SESSION_QUERY_KEY = "phpsessid"


class JiisHttp(Protocol):
    """Small bounded HTTP seam used by :class:`JiisPublicSearchResolver`."""

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """POST one anonymous form without following redirects."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """GET one bounded response without following redirects."""


@dataclass(frozen=True, slots=True)
class JiisResolverConfig:
    """Response limits for the two official HTML endpoints."""

    max_search_response_bytes: int = 2 * 1024 * 1024
    max_detail_response_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class JiisResolution:
    """One auditable JIIS public-search decision."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = JIIS_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    detail_url: str | None = None
    matched_title: str | None = None
    queried_titles: tuple[str, ...] = ()
    query_count: int = 0
    exact_title_matches: int = 0
    bibliographic_matches: int = 0
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.candidate is not None


@dataclass(frozen=True, slots=True)
class _ExpectedIdentity:
    doi: str
    year: int
    volume: str
    issue: str
    first_page: str
    final_page: str


@dataclass(slots=True)
class _SearchResult:
    title: str
    hrefs: tuple[str, ...]
    bibliography_parts: list[str]

    @property
    def bibliography(self) -> str:
        return _visible_text(self.bibliography_parts)


@dataclass(frozen=True, slots=True)
class _DetailAnchor:
    href: str | None
    label: str


class JiisPublicSearchResolver:
    """Resolve a JIIS DOI through exact official public-search metadata."""

    def __init__(
        self,
        config: JiisResolverConfig | None = None,
        *,
        http: JiisHttp | None = None,
    ) -> None:
        self.config = config or JiisResolverConfig()
        if self.config.max_search_response_bytes < 1:
            raise ValueError("max_search_response_bytes must be positive")
        if self.config.max_detail_response_bytes < 1:
            raise ValueError("max_detail_response_bytes must be positive")
        self.http: JiisHttp = http or _StatelessJiisHttp()

    def resolve(self, paper: PaperCandidate) -> JiisResolution:
        """Return a public PDF candidate only after all identity checks pass."""

        titles = paper_title_queries(paper)
        queried_titles = tuple(title for title, _normalized in titles)
        known_titles = frozenset(normalized for _title, normalized in titles)
        common = {
            "paper": paper,
            "queried_titles": queried_titles,
        }

        identity, eligibility = _expected_identity(paper)
        if eligibility is not None:
            return JiisResolution(**common, **eligibility)
        assert identity is not None

        if not titles:
            return JiisResolution(
                status="missing_metadata",
                reason="missing_title",
                **common,
            )

        exact_title_matches = 0
        bibliographic_matches = 0
        matched_results: dict[str, str] = {}
        query_count = 0

        for query_title, _query_normalized in titles:
            try:
                query_count += 1
                fetch = self.http.post_form_bytes(
                    JIIS_SEARCH_URL,
                    data={"kw": query_title},
                    max_bytes=self.config.max_search_response_bytes,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
            except HttpClientError as exc:
                return JiisResolution(
                    status="request_error",
                    reason="jiis_search_request_failed",
                    query_count=query_count,
                    exact_title_matches=exact_title_matches,
                    bibliographic_matches=bibliographic_matches,
                    error_code=getattr(exc, "code", "http_error"),
                    **common,
                )

            transport_error = _fetch_error(
                fetch,
                requested_url=JIIS_SEARCH_URL,
                max_bytes=self.config.max_search_response_bytes,
            )
            if transport_error is not None:
                return JiisResolution(
                    query_count=query_count,
                    exact_title_matches=exact_title_matches,
                    bibliographic_matches=bibliographic_matches,
                    **common,
                    **transport_error,
                )
            if not 200 <= fetch.status_code < 300:
                return JiisResolution(
                    status="http_error",
                    reason="jiis_search_http_error",
                    query_count=query_count,
                    exact_title_matches=exact_title_matches,
                    bibliographic_matches=bibliographic_matches,
                    http_status=fetch.status_code,
                    **common,
                )

            document = _decode_html(fetch.body)
            if document is None:
                return JiisResolution(
                    status="invalid_response",
                    reason="invalid_jiis_search_html",
                    query_count=query_count,
                    exact_title_matches=exact_title_matches,
                    bibliographic_matches=bibliographic_matches,
                    http_status=fetch.status_code,
                    error_code="invalid_html",
                    **common,
                )
            parser = _SearchResultsParser()
            try:
                parser.feed(document)
                parser.close()
            except (AssertionError, ValueError):
                return JiisResolution(
                    status="invalid_response",
                    reason="invalid_jiis_search_html",
                    query_count=query_count,
                    exact_title_matches=exact_title_matches,
                    bibliographic_matches=bibliographic_matches,
                    http_status=fetch.status_code,
                    error_code="invalid_html",
                    **common,
                )

            for result in parser.results:
                normalized_result_title = normalize_title(result.title)
                if normalized_result_title not in known_titles:
                    continue
                exact_title_matches += 1

                if len(result.hrefs) != 1:
                    return JiisResolution(
                        status="invalid_response",
                        reason=(
                            "missing_jiis_detail_key"
                            if not result.hrefs
                            else "multiple_jiis_detail_links"
                        ),
                        query_count=query_count,
                        exact_title_matches=exact_title_matches,
                        bibliographic_matches=bibliographic_matches,
                        http_status=fetch.status_code,
                        error_code="invalid_detail_url",
                        **common,
                    )
                try:
                    detail_url = _canonical_detail_url(result.hrefs[0])
                except ValueError as exc:
                    return JiisResolution(
                        status="invalid_response",
                        reason=str(exc),
                        query_count=query_count,
                        exact_title_matches=exact_title_matches,
                        bibliographic_matches=bibliographic_matches,
                        http_status=fetch.status_code,
                        error_code="unsafe_url",
                        **common,
                    )

                if not _bibliography_matches(result.bibliography, identity):
                    continue
                bibliographic_matches += 1
                matched_results.setdefault(detail_url, result.title)

        result_common = {
            **common,
            "query_count": query_count,
            "exact_title_matches": exact_title_matches,
            "bibliographic_matches": bibliographic_matches,
        }
        if not matched_results:
            if exact_title_matches == 0:
                reason = "title_mismatch"
            else:
                reason = "search_bibliography_mismatch"
            return JiisResolution(status="no_match", reason=reason, **result_common)
        if len(matched_results) != 1:
            return JiisResolution(
                status="ambiguous",
                reason="multiple_jiis_detail_urls",
                **result_common,
            )

        detail_url, search_title = next(iter(matched_results.items()))
        try:
            detail_fetch = self.http.get_bytes(
                detail_url,
                max_bytes=self.config.max_detail_response_bytes,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except HttpClientError as exc:
            return JiisResolution(
                status="request_error",
                reason="jiis_detail_request_failed",
                detail_url=detail_url,
                matched_title=search_title,
                error_code=getattr(exc, "code", "http_error"),
                **result_common,
            )

        transport_error = _fetch_error(
            detail_fetch,
            requested_url=detail_url,
            max_bytes=self.config.max_detail_response_bytes,
        )
        if transport_error is not None:
            return JiisResolution(
                detail_url=detail_url,
                matched_title=search_title,
                **result_common,
                **transport_error,
            )
        if not 200 <= detail_fetch.status_code < 300:
            return JiisResolution(
                status="http_error",
                reason="jiis_detail_http_error",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )

        detail_document = _decode_html(detail_fetch.body)
        if detail_document is None:
            return JiisResolution(
                status="invalid_response",
                reason="invalid_jiis_detail_html",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                error_code="invalid_html",
                **result_common,
            )
        detail_parser = _DetailPageParser()
        try:
            detail_parser.feed(detail_document)
            detail_parser.close()
        except (AssertionError, ValueError):
            return JiisResolution(
                status="invalid_response",
                reason="invalid_jiis_detail_html",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                error_code="invalid_html",
                **result_common,
            )

        if not any(
            normalize_title(title) in known_titles
            for title in detail_parser.titles
        ):
            return JiisResolution(
                status="no_match",
                reason="detail_title_mismatch",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )
        if not _bibliography_matches(detail_parser.visible_text, identity):
            return JiisResolution(
                status="no_match",
                reason="detail_bibliography_mismatch",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )
        if not re.search(
            rf"(?<![0-9]){identity.year}(?![0-9])",
            detail_parser.visible_text,
        ):
            return JiisResolution(
                status="no_match",
                reason="detail_year_mismatch",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )

        pdf_urls: set[str] = set()
        marked_pdf_anchors = 0
        for anchor in detail_parser.anchors:
            if not _is_marked_pdf_download(anchor):
                continue
            marked_pdf_anchors += 1
            if anchor.href is None:
                return JiisResolution(
                    status="invalid_response",
                    reason="missing_jiis_pdf_url",
                    detail_url=detail_url,
                    matched_title=search_title,
                    http_status=detail_fetch.status_code,
                    error_code="invalid_pdf_url",
                    **result_common,
                )
            try:
                pdf_urls.add(_canonical_pdf_url(anchor.href, detail_url))
            except ValueError as exc:
                return JiisResolution(
                    status="invalid_response",
                    reason=str(exc),
                    detail_url=detail_url,
                    matched_title=search_title,
                    http_status=detail_fetch.status_code,
                    error_code="unsafe_url",
                    **result_common,
                )

        if not marked_pdf_anchors or not pdf_urls:
            return JiisResolution(
                status="no_match",
                reason="missing_explicit_jiis_pdf_download",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )
        if len(pdf_urls) != 1:
            return JiisResolution(
                status="ambiguous",
                reason="multiple_jiis_pdf_urls",
                detail_url=detail_url,
                matched_title=search_title,
                http_status=detail_fetch.status_code,
                **result_common,
            )

        pdf_url = next(iter(pdf_urls))
        candidate = ResolverCandidate(
            url=pdf_url,
            resolver=JIIS_RESOLVER_NAME,
            landing_url=detail_url,
            is_open_access=True,
            # Intentionally omitted: JIIS detail HTML does not expose a DOI.
            verified_doi=None,
        )
        return JiisResolution(
            status="resolved",
            reason="exact_official_title_bibliography_and_pdf_link",
            candidate=candidate,
            detail_url=detail_url,
            matched_title=search_title,
            http_status=detail_fetch.status_code,
            **result_common,
        )


class _StatelessJiisHttp:
    """Default transport that never carries cookies between JIIS requests."""

    @staticmethod
    def _client() -> SafeHttpClient:
        return SafeHttpClient(
            max_redirects=0,
            retries=0,
            allow_http=False,
        )

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        client = self._client()
        try:
            return client.post_form_bytes(
                url,
                data=data,
                max_bytes=max_bytes,
                headers=headers,
            )
        finally:
            client.session.close()

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        client = self._client()
        try:
            return client.get_bytes(
                url,
                max_bytes=max_bytes,
                headers=headers,
            )
        finally:
            client.session.close()


class _SearchResultsParser(HTMLParser):
    """Extract search-result title cells and their following bibliography."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[_SearchResult] = []
        self._current: _SearchResult | None = None
        self._in_title_cell = False
        self._title_parts: list[str] = []
        self._title_hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)
        if lowered == "td":
            classes = frozenset(attributes.get("class", "").casefold().split())
            if {"dot1", "tit"}.issubset(classes):
                self._finish_current()
                self._in_title_cell = True
                self._title_parts = []
                self._title_hrefs = []
        elif lowered == "a" and self._in_title_cell:
            href = attributes.get("href", "").strip()
            if href:
                self._title_hrefs.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_title_cell:
            self._title_parts.append(data)
        elif self._current is not None:
            self._current.bibliography_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "td" or not self._in_title_cell:
            return
        self._in_title_cell = False
        title = _visible_text(self._title_parts)
        if title:
            self._current = _SearchResult(
                title=title,
                hrefs=tuple(dict.fromkeys(self._title_hrefs)),
                bibliography_parts=[],
            )
        self._title_parts = []
        self._title_hrefs = []

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is not None:
            self.results.append(self._current)
            self._current = None


class _DetailPageParser(HTMLParser):
    """Extract article identity and explicit links from ``#printArea`` only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self.anchors: list[_DetailAnchor] = []
        self._visible_parts: list[str] = []
        self._print_area_depth = 0
        self._in_title_cell = False
        self._title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] | None = None

    @property
    def visible_text(self) -> str:
        return _visible_text(self._visible_parts)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)
        if lowered == "div":
            if self._print_area_depth:
                self._print_area_depth += 1
            elif attributes.get("id", "").casefold() == "printarea":
                self._print_area_depth = 1
        if not self._print_area_depth:
            return

        if lowered == "td":
            classes = frozenset(attributes.get("class", "").casefold().split())
            if "text_achive" in classes or {"dot1", "tit"}.issubset(classes):
                self._in_title_cell = True
                self._title_parts = []
        elif lowered == "a":
            self._finish_anchor()
            self._anchor_href = attributes.get("href", "").strip() or None
            self._anchor_parts = []
        elif lowered == "img" and self._anchor_parts is not None:
            for key in ("title", "alt"):
                value = attributes.get(key, "").strip()
                if value:
                    self._anchor_parts.append(value)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)
        if (
            self._print_area_depth
            and lowered == "img"
            and self._anchor_parts is not None
        ):
            for key in ("title", "alt"):
                value = attributes.get(key, "").strip()
                if value:
                    self._anchor_parts.append(value)

    def handle_data(self, data: str) -> None:
        if not self._print_area_depth:
            return
        self._visible_parts.append(data)
        if self._in_title_cell:
            self._title_parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if not self._print_area_depth:
            return
        if lowered == "a":
            self._finish_anchor()
        elif lowered == "td" and self._in_title_cell:
            title = _visible_text(self._title_parts)
            if title:
                self.titles.append(title)
            self._in_title_cell = False
            self._title_parts = []
        if lowered == "div":
            self._print_area_depth -= 1

    def close(self) -> None:
        super().close()
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        if self._anchor_parts is None:
            return
        self.anchors.append(
            _DetailAnchor(
                href=self._anchor_href,
                label=_visible_text(self._anchor_parts),
            )
        )
        self._anchor_href = None
        self._anchor_parts = None


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, dict[str, object] | None]:
    raw_doi = paper.doi or paper.doi_original
    if not isinstance(raw_doi, str) or not raw_doi.strip():
        return None, {
            "status": "not_applicable",
            "reason": "missing_doi",
        }
    try:
        doi = normalize_doi(raw_doi)
    except InvalidDoiError:
        return None, {
            "status": "not_applicable",
            "reason": "invalid_doi",
        }
    if doi is None:
        return None, {
            "status": "not_applicable",
            "reason": "missing_doi",
        }
    match = _DOI_PATTERN.fullmatch(doi)
    if match is None:
        reason = (
            "unsupported_doi_prefix"
            if not doi.casefold().startswith("10.13088/jiis")
            else "unsupported_jiis_doi_format"
        )
        return None, {"status": "not_applicable", "reason": reason}

    expected_year = int(match.group("year"))
    volume = str(int(match.group("volume")))
    issue = str(int(match.group("issue")))
    first_page = str(int(match.group("first_page")))

    final_page = _decimal_text(paper.final_page)
    if final_page is None:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_final_page",
        }
    paper_first_page = _optional_decimal_text(paper.first_page)
    if paper.first_page is not None and paper_first_page is None:
        return None, {
            "status": "missing_metadata",
            "reason": "invalid_first_page",
        }
    if paper_first_page is not None and paper_first_page != first_page:
        return None, {
            "status": "identity_mismatch",
            "reason": "doi_and_paper_first_page_mismatch",
        }
    if (
        paper.publication_year is not None
        and (
            isinstance(paper.publication_year, bool)
            or not isinstance(paper.publication_year, int)
            or paper.publication_year != expected_year
        )
    ):
        return None, {
            "status": "identity_mismatch",
            "reason": "doi_and_paper_year_mismatch",
        }
    return (
        _ExpectedIdentity(
            doi=doi,
            year=expected_year,
            volume=volume,
            issue=issue,
            first_page=first_page,
            final_page=final_page,
        ),
        None,
    )


def _fetch_error(
    fetch: HttpFetch,
    *,
    requested_url: str,
    max_bytes: int,
) -> dict[str, object] | None:
    if (
        fetch.requested_url != requested_url
        or fetch.final_url != requested_url
        or fetch.redirects
    ):
        return {
            "status": "invalid_response",
            "reason": "jiis_redirect_or_url_change_rejected",
            "http_status": fetch.status_code,
            "error_code": "redirect_not_allowed",
        }
    if len(fetch.body) > max_bytes:
        return {
            "status": "invalid_response",
            "reason": "jiis_response_too_large",
            "http_status": fetch.status_code,
            "error_code": "too_large",
        }
    return None


def _canonical_detail_url(href: str) -> str:
    parsed = _official_url(urljoin(JIIS_ORIGIN + "/", html.unescape(href)))
    if parsed.path != "/index.php" or parsed.fragment:
        raise ValueError("unsafe_jiis_detail_path")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    retained: dict[str, str] = {}
    for key, value in pairs:
        lowered = key.casefold()
        if lowered == _SESSION_QUERY_KEY:
            continue
        if key not in {"mnu", "archiveId"} or key in retained:
            raise ValueError("unsafe_jiis_detail_query")
        retained[key] = value
    if retained.get("mnu") != "archive":
        raise ValueError("missing_jiis_detail_key")
    archive_id = retained.get("archiveId")
    if archive_id is None or not _DECIMAL_PATTERN.fullmatch(archive_id):
        raise ValueError("missing_jiis_detail_key")
    query = urlencode({"mnu": "archive", "archiveId": str(int(archive_id))})
    return f"{JIIS_ORIGIN}/index.php?{query}"


def _canonical_pdf_url(href: str, detail_url: str) -> str:
    absolute = urljoin(detail_url, html.unescape(href))
    parsed = _official_url(absolute)
    if parsed.fragment:
        raise ValueError("unsafe_jiis_pdf_url")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() != _SESSION_QUERY_KEY:
            raise ValueError("unsafe_jiis_pdf_query")
    decoded_path = unicodedata.normalize("NFKC", unquote(parsed.path))
    if (
        "\\" in decoded_path
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or not _PDF_PATH_PATTERN.fullmatch(decoded_path)
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        raise ValueError("unsafe_jiis_pdf_path")
    # Quote Unicode filenames while preserving already escaped octets.
    safe_path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit(("https", JIIS_HOST, safe_path, "", ""))


def _official_url(url: str):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("unsafe_jiis_host") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != JIIS_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("unsafe_jiis_host")
    return parsed


def _bibliography_matches(text: str, identity: _ExpectedIdentity) -> bool:
    if not text:
        return False
    dash = r"(?:~|-|\N{EN DASH}|\N{EM DASH}|\N{MINUS SIGN}|to)"
    pattern = re.compile(
        rf"(?<![A-Za-z])Vol(?:ume)?[.]?\s*{re.escape(identity.volume)}"
        rf"\s*,?\s*No[.]?\s*{re.escape(identity.issue)}"
        rf"\s*,?\s*(?:Pages?|Pp?[.]?)\s*:?\s*"
        rf"{re.escape(identity.first_page)}\s*{dash}\s*"
        rf"{re.escape(identity.final_page)}(?![0-9])",
        re.IGNORECASE,
    )
    return pattern.search(text) is not None


def _is_marked_pdf_download(anchor: _DetailAnchor) -> bool:
    label = normalize_title(anchor.label) or ""
    href = html.unescape(anchor.href or "")
    has_download_marker = "download" in label or "다운로드" in label
    has_pdf_marker = (
        "pdf" in label
        or "full-text" in label
        or "full text" in label
        or ".pdf" in urlsplit(href).path.casefold()
    )
    return has_download_marker and has_pdf_marker


def _decode_html(body: bytes) -> str | None:
    try:
        return body.decode("utf-8-sig")
    except UnicodeError:
        return None


def _attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str]:
    return {
        key.casefold(): html.unescape(value or "")
        for key, value in attrs
    }


def _visible_text(parts: list[str]) -> str:
    value = " ".join(parts)
    value = unicodedata.normalize("NFKC", html.unescape(value))
    return " ".join(value.split())


def _decimal_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not _DECIMAL_PATTERN.fullmatch(stripped):
        return None
    return str(int(stripped))


def _optional_decimal_text(value: object) -> str | None:
    if value is None:
        return None
    return _decimal_text(value)
