from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pytest

from PaperPipeline.elsevier_resolver import (
    DEFAULT_MAX_PDF_BYTES,
    ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL,
    ElsevierArticleResolver,
    ElsevierResolverConfig,
)
from PaperPipeline.http_client import (
    HttpFetch,
    RedirectLimitError,
    ResponseTooLargeError,
    RetryableHttpError,
)
from PaperPipeline.records import PaperCandidate


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b"%PDF-1.7\nbounded test document"
    content_type: str = "application/pdf"
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()


class FakeHttp:
    def __init__(self, responses: list[Reply | BaseException]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, int, dict[str, str] | None]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append((url, max_bytes, headers))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return HttpFetch(
            requested_url=response.requested_url or url,
            final_url=response.final_url or url,
            status_code=response.status,
            headers={"Content-Type": response.content_type},
            body=response.body,
            redirects=response.redirects,
        )


def _paper(
    *,
    doi: str | None = "10.1016/S0167-4048(24)00123-X",
    doi_original: str | None = None,
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART000000001",
        title="A forensic paper",
        doi_original=doi if doi_original is None else doi_original,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=2024,
        first_page="1",
        final_page="10",
    )


def _resolver(
    http: FakeHttp,
    *,
    key: str | None = "elsevier-issued-key",
    institution_token: str | None = None,
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
) -> ElsevierArticleResolver:
    return ElsevierArticleResolver(
        ElsevierResolverConfig(
            api_key=key,
            institution_token=institution_token,
            max_pdf_bytes=max_pdf_bytes,
        ),
        http=http,  # type: ignore[arg-type]
    )


def test_official_request_percent_encodes_doi_and_uses_exact_headers() -> None:
    secret = "elsevier-issued-key"
    http = FakeHttp([Reply()])
    result = _resolver(http, key=f"  {secret}  ").resolve(_paper())

    expected_url = (
        f"{ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL}/"
        "10.1016%2FS0167-4048%2824%2900123-X"
    )
    assert http.calls == [
        (
            expected_url,
            DEFAULT_MAX_PDF_BYTES,
            {
                "X-ELS-APIKey": secret,
                "Accept": "application/pdf",
            },
        )
    ]
    assert result.status == "resolved"
    assert result.reason == "official_elsevier_pdf_retrieved"
    assert result.resolved is True
    assert result.pdf_bytes == b"%PDF-1.7\nbounded test document"
    assert result.content_type == "application/pdf"
    assert result.bytes_received == len(result.pdf_bytes or b"")
    assert result.http_status == 200
    assert result.fetch is not None
    assert result.fetch.requested_url == expected_url
    assert result.fetch.final_url == expected_url
    assert secret not in expected_url
    assert secret not in repr(ElsevierResolverConfig(api_key=secret))
    assert secret not in repr(result)


def test_institution_token_is_header_only_and_redacted_from_repr() -> None:
    token = "elsevier-institution-token"
    http = FakeHttp([Reply()])

    result = _resolver(http, institution_token=f"  {token}  ").resolve(_paper())

    assert result.resolved
    url, _max_bytes, headers = http.calls[0]
    assert headers == {
        "X-ELS-APIKey": "elsevier-issued-key",
        "Accept": "application/pdf",
        "X-ELS-Insttoken": token,
    }
    assert token not in url
    assert token not in repr(
        ElsevierResolverConfig(
            api_key="elsevier-issued-key",
            institution_token=token,
        )
    )
    assert token not in repr(result)


def test_doi_original_is_used_when_canonical_field_is_missing() -> None:
    http = FakeHttp([Reply()])
    result = _resolver(http).resolve(
        _paper(doi=None, doi_original="https://doi.org/10.1016%2Fabc.def")
    )

    assert result.status == "resolved"
    assert result.doi == "10.1016/abc.def"
    assert http.calls[0][0].endswith("/10.1016%2Fabc.def")


@pytest.mark.parametrize(
    ("key", "expected_message"),
    [
        ("", "api_key"),
        ("   ", "api_key"),
        ("key with space", "api_key"),
        ("key\nvalue", "api_key"),
        ("clé", "api_key"),
        (123, "api_key"),
    ],
)
def test_invalid_api_keys_are_rejected(
    key: object,
    expected_message: str,
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        ElsevierArticleResolver(
            ElsevierResolverConfig(api_key=key),  # type: ignore[arg-type]
            http=FakeHttp([]),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("token", ["", "   ", "has space", "bad\nvalue", "토큰", 1])
def test_invalid_institution_tokens_are_rejected(token: object) -> None:
    with pytest.raises(ValueError, match="institution_token"):
        ElsevierArticleResolver(
            ElsevierResolverConfig(
                api_key="issued-key",
                institution_token=token,  # type: ignore[arg-type]
            ),
            http=FakeHttp([]),  # type: ignore[arg-type]
        )


def test_missing_key_is_a_configuration_decision_without_request() -> None:
    http = FakeHttp([])
    result = _resolver(http, key=None).resolve(_paper())

    assert result.status == "configuration_error"
    assert result.reason == "missing_elsevier_api_key"
    assert result.error_code == "api_key_not_configured"
    assert not http.calls


@pytest.mark.parametrize(
    ("paper", "status", "reason"),
    [
        (_paper(doi=None, doi_original=None), "missing_doi", "missing_doi"),
        (
            _paper(doi="not-a-doi"),
            "invalid_doi",
            "invalid_elsevier_doi",
        ),
        (
            _paper(doi="10.5555/not-elsevier"),
            "unsupported_doi",
            "doi_prefix_not_10_1016",
        ),
        (
            _paper(doi="10.10160/lookalike-prefix"),
            "unsupported_doi",
            "doi_prefix_not_10_1016",
        ),
    ],
)
def test_ineligible_doi_never_reaches_http(
    paper: PaperCandidate,
    status: str,
    reason: str,
) -> None:
    http = FakeHttp([])
    result = _resolver(http).resolve(paper)

    assert result.status == status
    assert result.reason == reason
    assert not http.calls


@pytest.mark.parametrize(
    ("status_code", "status", "reason", "error_code"),
    [
        (
            401,
            "authentication_required",
            "elsevier_authentication_required",
            "authentication_required",
        ),
        (403, "not_entitled", "elsevier_not_entitled", "not_entitled"),
        (404, "not_found", "elsevier_article_not_found", "not_found"),
        (408, "retryable_error", "elsevier_retryable_http_error", "retryable_error"),
        (425, "retryable_error", "elsevier_retryable_http_error", "retryable_error"),
        (429, "retryable_error", "elsevier_rate_limited", "retryable_error"),
        (500, "retryable_error", "elsevier_retryable_http_error", "retryable_error"),
        (503, "retryable_error", "elsevier_retryable_http_error", "retryable_error"),
        (400, "http_error", "elsevier_article_http_error", "http_error"),
    ],
)
def test_http_statuses_are_classified_for_pipeline_retry_and_audit(
    status_code: int,
    status: str,
    reason: str,
    error_code: str,
) -> None:
    result = _resolver(FakeHttp([Reply(status=status_code, body=b"")])).resolve(
        _paper()
    )

    assert result.status == status
    assert result.reason == reason
    assert result.error_code == error_code
    assert result.http_status == status_code
    assert result.resolved is False
    assert result.pdf_bytes is None


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            Reply(
                requested_url="https://evil.example/collect",
                final_url="https://evil.example/collect",
            ),
            "elsevier_redirect_rejected",
        ),
        (
            Reply(final_url="https://evil.example/collect"),
            "elsevier_redirect_rejected",
        ),
        (
            Reply(
                redirects=("https://api.elsevier.com/content/article/other",)
            ),
            "elsevier_redirect_rejected",
        ),
        (
            Reply(status=302, body=b""),
            "elsevier_redirect_rejected",
        ),
    ],
)
def test_redirected_or_unsafe_response_identity_is_rejected(
    reply: Reply,
    reason: str,
) -> None:
    result = _resolver(FakeHttp([reply])).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == reason
    assert result.error_code == "redirect_not_allowed"
    assert result.resolved is False
    assert result.pdf_bytes is None


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>access page</body></html>",
        b'{"error": "not a PDF"}',
        b"",
        b"x" * 1024 + b"%PDF-1.7",
    ],
)
def test_successful_non_pdf_response_is_never_exposed_as_pdf(body: bytes) -> None:
    result = _resolver(
        FakeHttp([Reply(body=body, content_type="text/html")])
    ).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "elsevier_non_pdf_response"
    assert result.error_code == "invalid_pdf"
    assert result.pdf_bytes is None


def test_pdf_magic_within_first_kib_is_retained_for_full_validation_later() -> None:
    body = b"transport preamble\n" + b"%PDF-1.7\nfake"
    result = _resolver(
        FakeHttp([Reply(body=body, content_type="application/octet-stream")])
    ).resolve(_paper())

    assert result.status == "resolved"
    assert result.pdf_bytes == body
    assert result.content_type == "application/octet-stream"


@pytest.mark.parametrize(
    ("failure", "status", "reason", "error_code"),
    [
        (
            RedirectLimitError("redirect"),
            "invalid_response",
            "elsevier_redirect_rejected",
            "redirect_not_allowed",
        ),
        (
            RetryableHttpError("temporary"),
            "retryable_error",
            "elsevier_request_retryable",
            "retryable_error",
        ),
        (
            ResponseTooLargeError("large"),
            "request_error",
            "elsevier_article_request_failed",
            "too_large",
        ),
    ],
)
def test_transport_failures_do_not_leak_exception_text(
    failure: BaseException,
    status: str,
    reason: str,
    error_code: str,
) -> None:
    secret = "never-print-this-key"
    result = _resolver(FakeHttp([failure]), key=secret).resolve(_paper())

    assert result.status == status
    assert result.reason == reason
    assert result.error_code == error_code
    assert secret not in repr(result)
    assert "temporary" not in repr(result)


def test_custom_pdf_bound_and_config_validation() -> None:
    http = FakeHttp([Reply()])
    result = _resolver(http, max_pdf_bytes=12345).resolve(_paper())

    assert result.resolved
    assert http.calls[0][1] == 12345
    with pytest.raises(ValueError, match="max_pdf_bytes"):
        ElsevierArticleResolver(ElsevierResolverConfig(max_pdf_bytes=0))
    with pytest.raises(ValueError, match="timeouts"):
        ElsevierArticleResolver(
            ElsevierResolverConfig(connect_timeout=0)
        )


def test_default_transport_disables_redirects_and_retries() -> None:
    gate = lambda _url: None
    resolver = ElsevierArticleResolver(
        ElsevierResolverConfig(
            api_key="issued-key",
            request_gate=gate,
        )
    )

    assert getattr(resolver.http, "max_redirects") == 0
    assert getattr(resolver.http, "retries") == 0
    assert getattr(resolver.http, "allow_http") is False
    assert getattr(resolver.http, "_request_gate") is gate
