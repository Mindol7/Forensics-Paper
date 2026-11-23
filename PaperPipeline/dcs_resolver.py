"""Conservative public-PDF resolver for the Journal of Digital Contents Society.

The DCS journal currently exposes an anonymous reader over plain HTTP.  This
module therefore stays disabled unless its caller explicitly opts into HTTP.
It follows a DOI only for the ``10.9728/dcs.`` namespace and accepts a PDF
candidate only after two official HTML pages independently bind the same DOI,
known title, ``aidx``, and ``bidx``.

No reader or PDF URL is guessed.  Both must occur as explicit anchors in the
fetched HTML, and every accepted URL is restricted to the exact public DCS
host, scheme, and path shapes documented below.
"""

from __future__ import annotations

import codecs
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import (
    parse_qsl,
    quote,
    urldefrag,
    urljoin,
    urlsplit,
)

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, HttpFetch, redact_url
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


DCS_DOI_PREFIX = "10.9728/dcs."
DCS_HOST = "journal.dcs.or.kr"
DCS_LANDING_PATH = "/_common/do.php"
DCS_READER_PATH = "/_PR/view/index.php"
DCS_RESOLVER_NAME = "dcs-public-reader"
DEFAULT_MAX_HTML_BYTES = 2 * 1024 * 1024

_DCS_PDF_PATH_RE = re.compile(r"/xml/(?P<directory>[0-9]+)/(?P<file>[0-9]+)\.pdf\Z")
_CHARSET_RE = re.compile(
    r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)",
    flags=re.IGNORECASE,
)


class DcsHttp(Protocol):
    """The bounded GET seam required by :class:`DcsPublicPdfResolver`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one URL, following redirects according to the injected client."""


@dataclass(frozen=True, slots=True)
class DcsResolverAttempt:
    """One redacted, auditable network attempt."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DcsResolution:
    """A conservative DCS resolution decision."""

    status: str
    reason: str
    candidate: ResolverCandidate | None = None
    attempts: tuple[DcsResolverAttempt, ...] = ()
    resolver: str = DCS_RESOLVER_NAME

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.candidate is not None


class DcsPublicPdfResolver:
    """Resolve an explicitly advertised DCS PDF through its public reader.

    ``allow_http`` defaults to false because all three accepted DCS URLs are
    required to use plain HTTP.  In production, the injected HTTP client must
    separately be configured to permit HTTP redirects and requests; otherwise
    its HTTPS upgrade will fail the exact URL checks below.
    """

    def __init__(
        self,
        http: DcsHttp,
        *,
        allow_http: bool = False,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    ) -> None:
        if max_html_bytes < 1:
            raise ValueError("max_html_bytes must be positive")
        self.http = http
        self.allow_http = bool(allow_http)
        self.max_html_bytes = int(max_html_bytes)

    def resolve(self, paper: PaperCandidate) -> DcsResolution:
        """Return one verified PDF candidate or an auditable failure."""

        if not self.allow_http:
            return DcsResolution(
                status="skipped",
                reason="http_not_explicitly_allowed",
            )

        doi = _paper_doi(paper)
        if doi is None:
            return DcsResolution(
                status="missing_metadata",
                reason="missing_or_invalid_doi",
            )
        if not doi.casefold().startswith(DCS_DOI_PREFIX):
            return DcsResolution(
                status="unsupported",
                reason="unsupported_doi_prefix",
            )

        known_titles = frozenset(
            normalized for _title, normalized in paper_title_queries(paper)
        )
        if not known_titles:
            return DcsResolution(
                status="missing_metadata",
                reason="missing_known_title",
            )

        attempts: list[DcsResolverAttempt] = []
        doi_url = f"https://doi.org/{quote(doi, safe='/():;._-')}"
        landing, failure = self._fetch_html(
            stage="doi-landing",
            url=doi_url,
            attempts=attempts,
        )
        if failure is not None:
            return _with_attempts(failure, attempts)
        assert landing is not None

        landing_ids, url_error = _official_page_ids(
            landing.final_url,
            DCS_LANDING_PATH,
        )
        if url_error is not None:
            return _failure(
                "identity_mismatch",
                f"landing_{url_error}",
                attempts,
            )
        assert landing_ids is not None

        landing_parser = _parse_document(landing)
        metadata_error = _metadata_identity_error(
            landing_parser,
            expected_doi=doi,
            known_titles=known_titles,
        )
        if metadata_error is not None:
            return _failure(
                "identity_mismatch",
                f"landing_{metadata_error}",
                attempts,
            )

        reader_url, reader_error = _select_reader_url(
            landing_parser,
            base_url=landing.final_url,
            expected_ids=landing_ids,
        )
        if reader_error is not None:
            status = (
                "not_found"
                if reader_error == "missing_reader_link"
                else "identity_mismatch"
            )
            return _failure(status, reader_error, attempts)
        assert reader_url is not None

        reader, failure = self._fetch_html(
            stage="dcs-reader",
            url=reader_url,
            attempts=attempts,
        )
        if failure is not None:
            return _with_attempts(failure, attempts)
        assert reader is not None

        reader_ids, url_error = _official_page_ids(
            reader.final_url,
            DCS_READER_PATH,
        )
        if url_error is not None:
            return _failure(
                "identity_mismatch",
                f"reader_final_{url_error}",
                attempts,
            )
        if reader_ids != landing_ids:
            return _failure(
                "identity_mismatch",
                "reader_final_id_mismatch",
                attempts,
            )

        reader_parser = _parse_document(reader)
        metadata_error = _metadata_identity_error(
            reader_parser,
            expected_doi=doi,
            known_titles=known_titles,
            allow_explicit_doi_link=True,
        )
        if metadata_error is not None:
            return _failure(
                "identity_mismatch",
                f"reader_{metadata_error}",
                attempts,
            )

        pdf_url, pdf_error = _select_pdf_url(
            reader_parser,
            base_url=reader.final_url,
            article_id=landing_ids[0],
        )
        if pdf_error is not None:
            status = (
                "not_found"
                if pdf_error == "missing_pdf_link"
                else "identity_mismatch"
            )
            return _failure(status, pdf_error, attempts)
        assert pdf_url is not None

        return DcsResolution(
            status="resolved",
            reason="official_reader_exact_identity",
            candidate=ResolverCandidate(
                url=pdf_url,
                resolver=DCS_RESOLVER_NAME,
                landing_url=landing.final_url,
                is_open_access=True,
                verified_doi=doi,
            ),
            attempts=tuple(attempts),
        )

    def _fetch_html(
        self,
        *,
        stage: str,
        url: str,
        attempts: list[DcsResolverAttempt],
    ) -> tuple[HttpFetch | None, DcsResolution | None]:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=self.max_html_bytes,
                max_html_bytes=self.max_html_bytes,
                headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9",
                },
            )
        except HttpClientError as exc:
            attempts.append(
                DcsResolverAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="failed",
                    error_code=getattr(exc, "code", "http_error"),
                )
            )
            return None, DcsResolution(
                status="request_error",
                reason=f"{stage}_request_failed",
            )

        attempts.append(
            DcsResolverAttempt(
                stage=stage,
                url=redact_url(url),
                status="fetched",
                http_status=fetch.status_code,
                final_url=redact_url(fetch.final_url),
            )
        )
        if len(fetch.body) > self.max_html_bytes:
            return None, DcsResolution(
                status="invalid_response",
                reason=f"{stage}_response_too_large",
            )
        if not 200 <= fetch.status_code < 300:
            return None, DcsResolution(
                status="http_error",
                reason=f"{stage}_http_status",
            )
        if not _is_html(fetch):
            return None, DcsResolution(
                status="invalid_response",
                reason=f"{stage}_not_html",
            )
        return fetch, None


class _DcsHtmlParser(HTMLParser):
    """Collect only identity metadata and explicit anchors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.dc_identifiers: list[str] = []
        self.titles: list[str] = []
        self.anchors: list[str] = []
        self._title_capture_depth = 0
        self._title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        lowered_tag = tag.casefold()
        if self._title_capture_depth:
            self._title_capture_depth += 1
        elif (
            lowered_tag == "title"
            or "tl-document"
            in (attributes.get("class") or "").casefold().split()
        ):
            self._title_capture_depth = 1
            self._title_parts = []
        if lowered_tag == "meta":
            field = (
                attributes.get("name")
                or attributes.get("property")
                or ""
            ).strip().casefold()
            content = attributes.get("content")
            if not content:
                return
            if field == "dc.identifier":
                self.dc_identifiers.append(content)
            elif field in {"dc.title", "dcterms.title", "citation_title"}:
                self.titles.append(content)
        elif lowered_tag == "a":
            href = attributes.get("href")
            if href:
                self.anchors.append(href)

    def handle_data(self, data: str) -> None:
        if self._title_capture_depth:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        del tag
        if not self._title_capture_depth:
            return
        self._title_capture_depth -= 1
        if self._title_capture_depth == 0:
            title = " ".join("".join(self._title_parts).split())
            if title:
                self.titles.append(title)
            self._title_parts = []


def _paper_doi(paper: PaperCandidate) -> str | None:
    raw = paper.doi or paper.doi_original
    try:
        return normalize_doi(raw)
    except InvalidDoiError:
        return None


def _parse_document(fetch: HttpFetch) -> _DcsHtmlParser:
    parser = _DcsHtmlParser()
    parser.feed(fetch.body.decode(_response_encoding(fetch), errors="replace"))
    parser.close()
    return parser


def _response_encoding(fetch: HttpFetch) -> str:
    content_type = _header(fetch.headers, "content-type")
    match = _CHARSET_RE.search(content_type)
    if match is not None:
        candidate = match.group(1)
        try:
            return codecs.lookup(candidate).name
        except LookupError:
            pass
    return "utf-8"


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def _is_html(fetch: HttpFetch) -> bool:
    content_type = _header(fetch.headers, "content-type").casefold()
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        return True
    prefix = fetch.body[:512].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _metadata_identity_error(
    parser: _DcsHtmlParser,
    *,
    expected_doi: str,
    known_titles: frozenset[str],
    allow_explicit_doi_link: bool = False,
) -> str | None:
    observed_dois: set[str] = set()
    for value in parser.dc_identifiers:
        try:
            normalized = normalize_doi(value)
        except InvalidDoiError:
            continue
        if normalized is not None:
            observed_dois.add(normalized.casefold())
    expected_doi_folded = expected_doi.casefold()
    if observed_dois and observed_dois != {expected_doi_folded}:
        return "doi_mismatch"
    if not observed_dois:
        linked_dois = {
            linked_doi.casefold()
            for href in parser.anchors
            if (linked_doi := _official_doi_link(href)) is not None
        }
        if (
            not allow_explicit_doi_link
            or expected_doi_folded not in linked_dois
        ):
            return "missing_dc_identifier_doi"

    observed_titles = {
        normalized
        for value in parser.titles
        if (normalized := normalize_title(value)) is not None
    }
    if not observed_titles.intersection(known_titles):
        return "title_mismatch"
    return None


def _official_doi_link(href: str) -> str | None:
    value = html.unescape(href).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "doi.org"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        return normalize_doi(parsed.path.lstrip("/"))
    except InvalidDoiError:
        return None


def _official_page_ids(
    url: str,
    expected_path: str,
) -> tuple[tuple[str, str] | None, str | None]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None, "host_mismatch"
    if parsed.scheme.casefold() != "http":
        return None, "scheme_mismatch"
    if (
        (parsed.hostname or "").casefold() != DCS_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80}
    ):
        return None, "host_mismatch"
    if parsed.path != expected_path:
        return None, "path_mismatch"

    query = parse_qsl(parsed.query, keep_blank_values=True)
    article_ids = [value for key, value in query if key.casefold() == "aidx"]
    issue_ids = [value for key, value in query if key.casefold() == "bidx"]
    if (
        len(article_ids) != 1
        or len(issue_ids) != 1
        or re.fullmatch(r"[0-9]+", article_ids[0]) is None
        or re.fullmatch(r"[0-9]+", issue_ids[0]) is None
    ):
        return None, "id_mismatch"
    return (article_ids[0], issue_ids[0]), None


def _anchor_url(base_url: str, href: str) -> str | None:
    value = html.unescape(href).strip()
    if not value or any(ord(character) < 32 for character in value):
        return None
    absolute, _fragment = urldefrag(urljoin(base_url, value))
    return absolute


def _select_reader_url(
    parser: _DcsHtmlParser,
    *,
    base_url: str,
    expected_ids: tuple[str, str],
) -> tuple[str | None, str | None]:
    valid: list[str] = []
    errors: list[str] = []
    for href in parser.anchors:
        absolute = _anchor_url(base_url, href)
        if absolute is None or urlsplit(absolute).path != DCS_READER_PATH:
            continue
        ids, error = _official_page_ids(absolute, DCS_READER_PATH)
        if error is not None:
            errors.append(error)
        elif ids != expected_ids:
            errors.append("id_mismatch")
        else:
            valid.append(absolute)
    unique = list(dict.fromkeys(valid))
    if len(unique) == 1:
        return unique[0], None
    if len(unique) > 1:
        return None, "ambiguous_reader_link"
    if errors:
        distinct = set(errors)
        detail = errors[0] if len(distinct) == 1 else "link_mismatch"
        return None, f"reader_{detail}"
    return None, "missing_reader_link"


def _select_pdf_url(
    parser: _DcsHtmlParser,
    *,
    base_url: str,
    article_id: str,
) -> tuple[str | None, str | None]:
    expected_path = f"/xml/{article_id}/{article_id}.pdf"
    valid: list[str] = []
    errors: list[str] = []
    for href in parser.anchors:
        absolute = _anchor_url(base_url, href)
        if absolute is None:
            continue
        try:
            parsed = urlsplit(absolute)
            port = parsed.port
        except ValueError:
            errors.append("host_mismatch")
            continue
        path_match = _DCS_PDF_PATH_RE.fullmatch(parsed.path)
        if path_match is None:
            continue
        if parsed.scheme.casefold() != "http":
            errors.append("scheme_mismatch")
            continue
        if (
            (parsed.hostname or "").casefold() != DCS_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 80}
        ):
            errors.append("host_mismatch")
            continue
        if (
            parsed.path != expected_path
            or path_match.group("directory") != path_match.group("file")
            or parsed.query
        ):
            errors.append("id_mismatch")
            continue
        valid.append(absolute)

    unique = list(dict.fromkeys(valid))
    if len(unique) == 1:
        return unique[0], None
    if len(unique) > 1:
        return None, "ambiguous_pdf_link"
    if errors:
        distinct = set(errors)
        detail = errors[0] if len(distinct) == 1 else "link_mismatch"
        return None, f"pdf_{detail}"
    return None, "missing_pdf_link"


def _failure(
    status: str,
    reason: str,
    attempts: list[DcsResolverAttempt],
) -> DcsResolution:
    return DcsResolution(
        status=status,
        reason=reason,
        attempts=tuple(attempts),
    )


def _with_attempts(
    resolution: DcsResolution,
    attempts: list[DcsResolverAttempt],
) -> DcsResolution:
    return DcsResolution(
        status=resolution.status,
        reason=resolution.reason,
        candidate=resolution.candidate,
        attempts=tuple(attempts),
        resolver=resolution.resolver,
    )
