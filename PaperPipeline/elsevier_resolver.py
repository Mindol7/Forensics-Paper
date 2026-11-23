"""Official Elsevier Article Retrieval PDF resolver.

Only Elsevier DOI-prefix papers (``10.1016/...``) are eligible.  The resolver
downloads the PDF from Elsevier's fixed HTTPS Article Retrieval endpoint and
returns the bounded :class:`~PaperPipeline.http_client.HttpFetch` for later
project-wide DOI/title validation.

The API key is optional at configuration time so the pipeline can report a
configuration decision without making a request.  When configured, it is sent
only in the ``X-ELS-APIKey`` header; it is never included in a URL or result
representation.  Redirects are disabled in the default transport and every
injected response is independently checked against the exact requested
official endpoint before its body is trusted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    RedirectLimitError,
    RetryableHttpError,
    SafeHttpClient,
)
from PaperPipeline.records import PaperCandidate


ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL = (
    "https://api.elsevier.com/content/article/doi"
)
ELSEVIER_DOI_PREFIX = "10.1016"
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
_RESOLVER_NAME = "elsevier-article-retrieval"
_RETRYABLE_HTTP_STATUSES = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)


class ElsevierArticleHttp(Protocol):
    """Small bounded-GET seam implemented by :class:`SafeHttpClient`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Return one bounded response without following redirects."""


@dataclass(frozen=True, slots=True)
class ElsevierResolverConfig:
    """Credentials and transport bounds for Article Retrieval.

    ``repr=False`` is defense in depth: diagnostics can safely include the
    configuration object without exposing the Elsevier API key or an optional
    institution token.  Both credentials are sent only as request headers.
    """

    api_key: str | None = field(default=None, repr=False)
    institution_token: str | None = field(default=None, repr=False)
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    request_gate: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class ElsevierResolution:
    """One auditable Article Retrieval decision.

    A successful resolution retains the complete bounded fetch so integration
    can pass ``pdf_bytes`` into the project's normal PDF DOI/title validator
    before persisting it.  The fetch is excluded from ``repr`` because it may
    contain a large document.
    """

    source_id: str
    doi: str | None
    status: str
    reason: str
    resolver: str = _RESOLVER_NAME
    fetch: HttpFetch | None = field(default=None, repr=False)
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.fetch is not None

    @property
    def pdf_bytes(self) -> bytes | None:
        return self.fetch.body if self.resolved and self.fetch is not None else None

    @property
    def content_type(self) -> str | None:
        if self.fetch is None:
            return None
        return self.fetch.content_type or None

    @property
    def bytes_received(self) -> int:
        return len(self.fetch.body) if self.fetch is not None else 0


class ElsevierArticleResolver:
    """Fetch one ``10.1016`` paper through Elsevier's official PDF endpoint."""

    def __init__(
        self,
        config: ElsevierResolverConfig | None = None,
        *,
        http: ElsevierArticleHttp | None = None,
    ) -> None:
        self.config = config or ElsevierResolverConfig()
        self._api_key = _validated_api_key(self.config.api_key)
        self._institution_token = _validated_secret(
            self.config.institution_token,
            name="institution_token",
        )
        if self.config.max_pdf_bytes < 1:
            raise ValueError("max_pdf_bytes must be positive")
        if self.config.connect_timeout <= 0 or self.config.read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")

        # Retries are left to the pipeline scheduler so every 429/5xx remains
        # visible in the audit trail.  With max_redirects=0, the credential
        # cannot be forwarded to a Location target.
        self.http: ElsevierArticleHttp = http or SafeHttpClient(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            max_redirects=0,
            retries=0,
            allow_http=False,
            request_gate=self.config.request_gate,
        )

    def resolve(self, paper: PaperCandidate) -> ElsevierResolution:
        """Return a bounded PDF fetch or a stable, auditable failure status."""

        try:
            canonical_doi = normalize_doi(paper.doi or paper.doi_original)
        except InvalidDoiError:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=None,
                status="invalid_doi",
                reason="invalid_elsevier_doi",
                error_code="invalid_doi",
            )

        if canonical_doi is None:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=None,
                status="missing_doi",
                reason="missing_doi",
                error_code="missing_doi",
            )
        if not _is_elsevier_doi(canonical_doi):
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="unsupported_doi",
                reason="doi_prefix_not_10_1016",
                error_code="unsupported_doi_prefix",
            )
        if self._api_key is None:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="configuration_error",
                reason="missing_elsevier_api_key",
                error_code="api_key_not_configured",
            )

        url = _article_retrieval_url(canonical_doi)
        if not _is_official_article_url(url):
            # The URL is built from a constant and a percent-encoded DOI path
            # segment, but retain an explicit fail-closed assertion.
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="invalid_response",
                reason="invalid_elsevier_article_url",
                error_code="unsafe_url",
            )

        try:
            headers = {
                "X-ELS-APIKey": self._api_key,
                "Accept": "application/pdf",
            }
            if self._institution_token is not None:
                headers["X-ELS-Insttoken"] = self._institution_token
            fetch = self.http.get_bytes(
                url,
                max_bytes=self.config.max_pdf_bytes,
                headers=headers,
            )
        except RedirectLimitError:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="invalid_response",
                reason="elsevier_redirect_rejected",
                error_code="redirect_not_allowed",
            )
        except RetryableHttpError:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="retryable_error",
                reason="elsevier_request_retryable",
                error_code="retryable_error",
            )
        except HttpClientError as exc:
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="request_error",
                reason="elsevier_article_request_failed",
                error_code=getattr(exc, "code", "http_error"),
            )

        if not _fetch_is_exact_official_response(fetch, url):
            return ElsevierResolution(
                source_id=paper.source_id,
                doi=canonical_doi,
                status="invalid_response",
                reason="elsevier_redirect_rejected",
                fetch=fetch,
                http_status=fetch.status_code,
                error_code="redirect_not_allowed",
            )

        return _resolution_from_fetch(
            source_id=paper.source_id,
            doi=canonical_doi,
            fetch=fetch,
        )


def _validated_secret(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")
    stripped = value.strip()
    if (
        not stripped
        or not stripped.isascii()
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in stripped
        )
    ):
        raise ValueError(
            f"{name} must be nonempty ASCII without whitespace or controls"
        )
    return stripped


def _validated_api_key(value: Any) -> str | None:
    return _validated_secret(value, name="api_key")


def _is_elsevier_doi(doi: str) -> bool:
    prefix, separator, _suffix = doi.partition("/")
    return bool(separator) and prefix.casefold() == ELSEVIER_DOI_PREFIX


def _article_retrieval_url(doi: str) -> str:
    # ``safe=""`` makes the entire DOI one path segment, including its slash.
    encoded_doi = quote(doi, safe="")
    return f"{ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL}/{encoded_doi}"


def _is_official_article_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    path_prefix = urlsplit(ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL).path + "/"
    encoded_doi = parsed.path[len(path_prefix) :] if parsed.path.startswith(path_prefix) else ""
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "api.elsevier.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith(path_prefix)
        and bool(encoded_doi)
        and "/" not in encoded_doi
        and not parsed.query
        and not parsed.fragment
    )


def _fetch_is_exact_official_response(
    fetch: HttpFetch,
    requested_url: str,
) -> bool:
    return (
        fetch.requested_url == requested_url
        and fetch.final_url == requested_url
        and not fetch.redirects
        and _is_official_article_url(fetch.final_url)
    )


def _resolution_from_fetch(
    *,
    source_id: str,
    doi: str,
    fetch: HttpFetch,
) -> ElsevierResolution:
    common = {
        "source_id": source_id,
        "doi": doi,
        "fetch": fetch,
        "http_status": fetch.status_code,
    }
    status_code = fetch.status_code
    if status_code == 401:
        return ElsevierResolution(
            status="authentication_required",
            reason="elsevier_authentication_required",
            error_code="authentication_required",
            **common,
        )
    if status_code == 403:
        return ElsevierResolution(
            status="not_entitled",
            reason="elsevier_not_entitled",
            error_code="not_entitled",
            **common,
        )
    if status_code == 404:
        return ElsevierResolution(
            status="not_found",
            reason="elsevier_article_not_found",
            error_code="not_found",
            **common,
        )
    if status_code in _RETRYABLE_HTTP_STATUSES:
        reason = (
            "elsevier_rate_limited"
            if status_code == 429
            else "elsevier_retryable_http_error"
        )
        return ElsevierResolution(
            status="retryable_error",
            reason=reason,
            error_code="retryable_error",
            **common,
        )
    if 300 <= status_code <= 399:
        return ElsevierResolution(
            status="invalid_response",
            reason="elsevier_redirect_rejected",
            error_code="redirect_not_allowed",
            **common,
        )
    if not 200 <= status_code < 300:
        return ElsevierResolution(
            status="http_error",
            reason="elsevier_article_http_error",
            error_code="http_error",
            **common,
        )
    if not _looks_like_pdf(fetch.body):
        return ElsevierResolution(
            status="invalid_response",
            reason="elsevier_non_pdf_response",
            error_code="invalid_pdf",
            **common,
        )
    return ElsevierResolution(
        status="resolved",
        reason="official_elsevier_pdf_retrieved",
        **common,
    )


def _looks_like_pdf(body: bytes) -> bool:
    return body.find(b"%PDF-", 0, 1024) >= 0
