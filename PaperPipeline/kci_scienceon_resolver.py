"""Resolve KCI records through the KCI UI's official ScienceON full-text link.

This resolver is permission-gated by the caller.  It does not search or log in
to ScienceON.  For one exact KCI ``ART`` identifier it:

* POSTs the same fixed KCI form used by the public "원문 찾아보기" UI;
* accepts only an exact title and publication-year match;
* follows only a KCI-returned ``OpenAPIFullTextView`` identifier;
* opens the fixed HTTPS ScienceON viewer; and
* accepts only the viewer's explicit, same-origin ``orgDocDown.do`` PDF URL.

The returned candidate is bound to the KCI source id.  The common downloader
still parses the PDF and rejects a different DOI or otherwise invalid file.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


KCI_SCIENCEON_LOOKUP_URL = (
    "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
    "ciSereArtiScienceOnInfo.kci"
)
SCIENCEON_ORIGIN = "https://scienceon.kisti.re.kr"
SCIENCEON_VIEWER_PATH = "/commons/util/originalView.do"
SCIENCEON_DOWNLOAD_PATH = "/commons/util/orgDocDown.do"
KCI_SCIENCEON_RESOLVER_NAME = "kci-scienceon"

_ART_ID_RE = re.compile(r"\AART[0-9]{9}\Z")
_CN_RE = re.compile(r"\AJAKO[0-9]{15,24}\Z")
_DOCURL_RE = re.compile(
    r"""\bvar\s+docurl\s*=\s*(?P<quote>['"])(?P<url>.*?)(?P=quote)\s*;""",
    re.IGNORECASE | re.DOTALL,
)


class KciScienceOnHttp(Protocol):
    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """POST one bounded anonymous form."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """GET one bounded anonymous response."""


@dataclass(frozen=True, slots=True)
class KciScienceOnConfig:
    max_lookup_bytes: int = 4 * 1024 * 1024
    max_viewer_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class KciScienceOnAttempt:
    stage: str
    status: str
    url: str
    http_status: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KciScienceOnResolution:
    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = KCI_SCIENCEON_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    matched_title: str | None = None
    matched_year: int | None = None
    returned_items: int = 0
    exact_matches: int = 0
    fulltext_links: int = 0
    attempts: tuple[KciScienceOnAttempt, ...] = ()
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.candidate is not None


@dataclass(frozen=True, slots=True)
class _ExactRecord:
    title: str
    year: int | None
    viewer_urls: tuple[str, ...]


class KciScienceOnResolver:
    """Conservative adapter for the KCI UI's ScienceON lookup."""

    def __init__(
        self,
        config: KciScienceOnConfig | None = None,
        *,
        http: KciScienceOnHttp | None = None,
    ) -> None:
        self.config = config or KciScienceOnConfig()
        if self.config.max_lookup_bytes < 1:
            raise ValueError("max_lookup_bytes must be positive")
        if self.config.max_viewer_bytes < 1:
            raise ValueError("max_viewer_bytes must be positive")
        self.http = http or SafeHttpClient()

    def resolve(self, paper: PaperCandidate) -> KciScienceOnResolution:
        if paper.source.casefold() != "kci" or not _ART_ID_RE.fullmatch(
            paper.source_id
        ):
            return KciScienceOnResolution(
                status="not_applicable",
                reason="not_a_kci_art_record",
                paper=paper,
            )
        known_titles = {
            normalized for _title, normalized in paper_title_queries(paper)
        }
        if not known_titles:
            return KciScienceOnResolution(
                status="missing_metadata",
                reason="missing_title",
                paper=paper,
            )

        attempts: list[KciScienceOnAttempt] = []
        try:
            lookup = self.http.post_form_bytes(
                KCI_SCIENCEON_LOOKUP_URL,
                data={"sereArticleSearchBean.artiId": paper.source_id},
                max_bytes=self.config.max_lookup_bytes,
                headers={"Accept": "application/json"},
            )
        except HttpClientError as exc:
            return KciScienceOnResolution(
                status="request_error",
                reason="kci_scienceon_lookup_failed",
                paper=paper,
                error_code=getattr(exc, "code", "http_error"),
            )
        attempts.append(
            KciScienceOnAttempt(
                stage="lookup",
                status=_http_status(lookup),
                url=KCI_SCIENCEON_LOOKUP_URL,
                http_status=lookup.status_code,
            )
        )
        if not 200 <= lookup.status_code < 300:
            return KciScienceOnResolution(
                status="http_error",
                reason="kci_scienceon_lookup_http_error",
                paper=paper,
                attempts=tuple(attempts),
                error_code=(
                    "retryable_error"
                    if lookup.status_code in {408, 425, 429}
                    or lookup.status_code >= 500
                    else "http_error"
                ),
            )
        try:
            payload = json.loads(lookup.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return KciScienceOnResolution(
                status="invalid_response",
                reason="invalid_kci_scienceon_json",
                paper=paper,
                attempts=tuple(attempts),
                error_code="invalid_json",
            )
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            return KciScienceOnResolution(
                status="invalid_response",
                reason="invalid_kci_scienceon_envelope",
                paper=paper,
                attempts=tuple(attempts),
                error_code="invalid_json",
            )

        exact_records: list[_ExactRecord] = []
        fulltext_link_count = 0
        for item in payload:
            record = _exact_record(item, known_titles, paper.publication_year)
            if record is None:
                continue
            fulltext_link_count += len(record.viewer_urls)
            exact_records.append(record)
        common = {
            "paper": paper,
            "returned_items": len(payload),
            "exact_matches": len(exact_records),
            "fulltext_links": fulltext_link_count,
            "attempts": tuple(attempts),
        }
        if not exact_records:
            return KciScienceOnResolution(
                status="no_match",
                reason="no_exact_kci_scienceon_record",
                **common,
            )

        viewer_records = [
            (record, viewer_url)
            for record in exact_records
            for viewer_url in record.viewer_urls
        ]
        unique_viewers = {
            viewer_url: record for record, viewer_url in viewer_records
        }
        if not unique_viewers:
            first = exact_records[0]
            return KciScienceOnResolution(
                status="not_found",
                reason="exact_record_has_no_scienceon_fulltext",
                matched_title=first.title,
                matched_year=first.year,
                **common,
            )
        if len(unique_viewers) != 1:
            return KciScienceOnResolution(
                status="ambiguous",
                reason="multiple_exact_scienceon_fulltexts",
                **common,
            )

        viewer_url, record = next(iter(unique_viewers.items()))
        try:
            viewer = self.http.get_bytes(
                viewer_url,
                max_bytes=self.config.max_viewer_bytes,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except HttpClientError as exc:
            return KciScienceOnResolution(
                status="request_error",
                reason="scienceon_viewer_request_failed",
                matched_title=record.title,
                matched_year=record.year,
                error_code=getattr(exc, "code", "http_error"),
                **common,
            )
        attempts.append(
            KciScienceOnAttempt(
                stage="viewer",
                status=_http_status(viewer),
                url=viewer_url,
                http_status=viewer.status_code,
            )
        )
        common["attempts"] = tuple(attempts)
        if not 200 <= viewer.status_code < 300:
            return KciScienceOnResolution(
                status="http_error",
                reason="scienceon_viewer_http_error",
                matched_title=record.title,
                matched_year=record.year,
                error_code=(
                    "retryable_error"
                    if viewer.status_code in {408, 425, 429}
                    or viewer.status_code >= 500
                    else "http_error"
                ),
                **common,
            )
        download_url = _download_url_from_viewer(viewer.body, viewer_url)
        if download_url is None:
            return KciScienceOnResolution(
                status="not_found",
                reason="scienceon_viewer_has_no_safe_pdf",
                matched_title=record.title,
                matched_year=record.year,
                **common,
            )
        candidate = ResolverCandidate(
            url=download_url,
            resolver=KCI_SCIENCEON_RESOLVER_NAME,
            landing_url=viewer_url,
            is_open_access=None,
            verified_source_id=paper.source_id,
        )
        return KciScienceOnResolution(
            status="resolved",
            reason="exact_kci_record_and_explicit_scienceon_pdf",
            candidate=candidate,
            matched_title=record.title,
            matched_year=record.year,
            **common,
        )


def _exact_record(
    item: Mapping[str, object],
    known_titles: set[str],
    expected_year: int | None,
) -> _ExactRecord | None:
    title_container = item.get("articleTitle")
    if not isinstance(title_container, Mapping):
        return None
    titles = [
        value.strip()
        for key in ("articleTitle", "articleTitle2")
        if isinstance((value := title_container.get(key)), str) and value.strip()
    ]
    matching_titles = [
        title for title in titles if normalize_title(title) in known_titles
    ]
    if not matching_titles:
        return None
    raw_year = item.get("year")
    year = (
        int(raw_year)
        if isinstance(raw_year, str)
        and raw_year.isdecimal()
        and len(raw_year) == 4
        else raw_year if isinstance(raw_year, int) else None
    )
    if expected_year is not None and year != expected_year:
        return None
    raw_urls = item.get("urlInfo")
    urls = raw_urls if isinstance(raw_urls, list) else []
    viewers = tuple(
        dict.fromkeys(
            viewer
            for value in urls
            if isinstance(value, str)
            for viewer in [_scienceon_viewer_url(value)]
            if viewer is not None
        )
    )
    return _ExactRecord(
        title=matching_titles[0],
        year=year,
        viewer_urls=viewers,
    )


def _scienceon_viewer_url(value: str) -> str | None:
    parsed = urlsplit(html.unescape(value).strip())
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host != "click.ndsl.kr"
        or parsed.path != "/servlet/OpenAPIFullTextView"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _value in pairs}) != len(pairs):
        return None
    query = dict(pairs)
    cn = query.get("cn", "")
    if not _CN_RE.fullmatch(cn) or query.get("dbt", "").upper() != "JAKO":
        return None
    return urlunsplit(
        (
            "https",
            "scienceon.kisti.re.kr",
            SCIENCEON_VIEWER_PATH,
            urlencode({"cn": cn, "dbt": "JAKO"}),
            "",
        )
    )


def _download_url_from_viewer(body: bytes, viewer_url: str) -> str | None:
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    matches = {
        html.unescape(match.group("url")).strip()
        for match in _DOCURL_RE.finditer(document)
    }
    safe = {
        normalized
        for value in matches
        for normalized in [_safe_download_url(value, viewer_url)]
        if normalized is not None
    }
    return next(iter(safe)) if len(safe) == 1 else None


def _safe_download_url(value: str, viewer_url: str) -> str | None:
    parsed = urlsplit(value)
    viewer_query = dict(parse_qsl(urlsplit(viewer_url).query))
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").rstrip(".").casefold()
        != "scienceon.kisti.re.kr"
        or parsed.path != SCIENCEON_DOWNLOAD_PATH
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _value in pairs}) != len(pairs):
        return None
    query = dict(pairs)
    if set(query) != {"url", "ex", "filename", "cn"}:
        return None
    if query.get("cn") != viewer_query.get("cn") or query.get("ex") != "pdf":
        return None
    inner = query.get("url", "")
    if (
        not inner.startswith("/soc_img/")
        or not inner.casefold().endswith(".pdf")
        or ".." in inner.split("/")
        or "\\" in inner
    ):
        return None
    return urlunsplit(
        (
            "https",
            "scienceon.kisti.re.kr",
            SCIENCEON_DOWNLOAD_PATH,
            urlencode(
                [
                    ("url", inner),
                    ("ex", "pdf"),
                    ("filename", query.get("filename", "")),
                    ("cn", query["cn"]),
                ]
            ),
            "",
        )
    )


def _http_status(fetch: HttpFetch) -> str:
    return "ok" if 200 <= fetch.status_code < 300 else "http_error"
