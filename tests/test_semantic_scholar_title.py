from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from PaperPipeline.http_client import (
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.semantic_scholar_title import (
    SEMANTIC_SCHOLAR_MATCH_FIELDS,
    SEMANTIC_SCHOLAR_MATCH_URL,
    SemanticScholarTitleConfig,
    SemanticScholarTitleResolver,
)


@dataclass(frozen=True)
class Reply:
    status: int = 200
    payload: Any = None
    body: bytes | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()
    headers: dict[str, str] | None = None


class FakeHttp:
    def __init__(
        self,
        responses: list[Reply | HttpFetch | BaseException],
    ) -> None:
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
        if isinstance(response, HttpFetch):
            return response
        body = (
            response.body
            if response.body is not None
            else json.dumps(response.payload).encode()
        )
        return HttpFetch(
            requested_url=url,
            final_url=response.final_url or url,
            status_code=response.status,
            headers={
                "Content-Type": "application/json",
                **(response.headers or {}),
            },
            body=body,
            redirects=response.redirects,
        )


def _paper(
    *,
    source_id: str = "ART000000001",
    title: str | None = "디지털 포렌식 연구",
    alternate_titles: tuple[str, ...] = ("A Digital Forensics Study",),
    year: int | None = 2024,
    doi: str | None = None,
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=source_id,
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=f"https://www.kci.go.kr/article/{source_id}",
        abstract="abstract",
        publication_year=year,
        first_page="1",
        final_page="10",
        alternate_titles=alternate_titles,
    )


def _item(
    *,
    title: str = "A Digital Forensics Study",
    year: int = 2024,
    doi: object = "10.1234/study",
    is_open: object = True,
    pdf_url: object = "https://repository.example/study.pdf",
    status: object = "GREEN",
    license_value: object = "CC-BY",
    disclaimer: object = "Repository copy",
) -> dict[str, Any]:
    pdf: object
    if pdf_url is None:
        pdf = None
    else:
        pdf = {
            "url": pdf_url,
            "status": status,
            "license": license_value,
            "disclaimer": disclaimer,
        }
    return {
        "title": title,
        "year": year,
        "externalIds": {"DOI": doi},
        "isOpenAccess": is_open,
        "openAccessPdf": pdf,
    }


def _envelope(*items: object) -> dict[str, list[object]]:
    return {"data": list(items)}


def _resolver(
    http: FakeHttp,
    *,
    cache_dir: Path | None = None,
    key: str = "issued-s2-key",
    gate_calls: list[str] | None = None,
    defer_calls: list[float] | None = None,
    now: datetime | None = None,
    force_refresh: bool = False,
    retries: int = 0,
    retry_backoff_seconds: float = 5.0,
    sleeper_calls: list[float] | None = None,
) -> SemanticScholarTitleResolver:
    calls = gate_calls if gate_calls is not None else []
    return SemanticScholarTitleResolver(
        SemanticScholarTitleConfig(
            api_key=key,
            cache_dir=cache_dir,
            force_refresh=force_refresh,
            retries=retries,
            retry_backoff_seconds=retry_backoff_seconds,
        ),
        request_gate=calls.append,
        defer_gate=(
            defer_calls.append
            if defer_calls is not None
            else None
        ),
        http=http,  # type: ignore[arg-type]
        clock=(lambda: now) if now is not None else None,
        sleeper=(
            sleeper_calls.append
            if sleeper_calls is not None
            else (lambda _seconds: None)
        ),
    )


def _query_title(url: str) -> str:
    return parse_qs(urlsplit(url).query)["query"][0]


def test_searches_distinct_known_titles_and_exposes_exact_oa_pdf() -> None:
    secret = "issued-s2-key"
    paper = _paper(
        alternate_titles=(
            "A Digital Forensics Study",
            "  A   DIGITAL FORENSICS STUDY ",
        )
    )
    http = FakeHttp(
        [
            Reply(status=404, payload={"message": "not found"}),
            Reply(
                payload=_envelope(
                    _item(
                        title="A DIGITAL FORENSICS STUDY",
                        year=2025,
                        doi="https://doi.org/10.1234%2FStudy",
                    )
                )
            ),
        ]
    )
    gate_calls: list[str] = []
    config = SemanticScholarTitleConfig(api_key=secret)
    resolver = SemanticScholarTitleResolver(
        config,
        request_gate=gate_calls.append,
        http=http,  # type: ignore[arg-type]
    )

    result = resolver.resolve(paper)

    assert result.status == "resolved"
    assert result.http_status == 200
    assert result.reason == "exact_title_year_single_doi"
    assert result.resolved is True
    assert result.doi == "10.1234/Study"
    assert result.paper.doi_source == "semantic-scholar-title"
    assert result.matched_title == "A DIGITAL FORENSICS STUDY"
    assert result.matched_year == 2025
    assert result.query_count == 2
    assert result.returned_items == 1
    assert result.exact_title_matches == 1
    assert result.title_year_matches == 1
    assert result.pdf is not None
    assert result.pdf.license == "CC-BY"
    assert result.pdf.status == "GREEN"
    assert result.pdf.disclaimer == "Repository copy"

    candidate = result.candidate
    assert candidate is not None
    assert candidate.url == "https://repository.example/study.pdf"
    assert candidate.resolver == "semantic-scholar-title"
    assert candidate.is_open_access is True
    assert candidate.verified_doi == "10.1234/Study"

    assert [_query_title(call[0]) for call in http.calls] == [
        "디지털 포렌식 연구",
        "A Digital Forensics Study",
    ]
    assert gate_calls == [call[0] for call in http.calls]
    for url, max_bytes, headers in http.calls:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.hostname == "api.semanticscholar.org"
        assert parsed.path == "/graph/v1/paper/search/match"
        assert query["fields"] == [SEMANTIC_SCHOLAR_MATCH_FIELDS]
        assert "api" not in {key.casefold() for key in query}
        assert secret not in url
        assert max_bytes == 2 * 1024 * 1024
        assert headers == {
            "Accept": "application/json",
            "x-api-key": secret,
        }
    assert secret not in repr(config)
    assert secret not in repr(resolver)
    assert SEMANTIC_SCHOLAR_MATCH_URL in http.calls[0][0]


def test_same_doi_across_titles_is_not_ambiguous_and_prefers_oa_metadata() -> None:
    result = _resolver(
        FakeHttp(
            [
                Reply(
                    payload=_envelope(
                        _item(
                            title="디지털 포렌식 연구",
                            doi="10.1234/same",
                            is_open=False,
                        )
                    )
                ),
                Reply(
                    payload=_envelope(
                        _item(
                            title="A Digital Forensics Study",
                            doi="doi:10.1234/SAME",
                        )
                    )
                ),
            ]
        )
    ).resolve(_paper())

    assert result.status == "resolved"
    assert result.doi is not None
    assert result.doi.casefold() == "10.1234/same"
    assert result.candidate is not None
    assert result.candidate.url == "https://repository.example/study.pdf"


def test_different_dois_across_known_titles_are_ambiguous() -> None:
    result = _resolver(
        FakeHttp(
            [
                Reply(
                    payload=_envelope(
                        _item(
                            title="디지털 포렌식 연구",
                            doi="10.1234/korean",
                        )
                    )
                ),
                Reply(
                    payload=_envelope(
                        _item(
                            title="A Digital Forensics Study",
                            doi="10.1234/english",
                        )
                    )
                ),
            ]
        )
    ).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_distinct_dois"
    assert result.doi is None
    assert result.candidate is None


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            Reply(payload=_envelope(_item(title="A Different Study"))),
            "title_mismatch",
        ),
        (
            Reply(
                payload=_envelope(
                    _item(title="디지털 포렌식 연구", year=2026)
                )
            ),
            "year_mismatch",
        ),
        (
            Reply(
                payload=_envelope(
                    _item(title="디지털 포렌식 연구", doi=None)
                )
            ),
            "missing_or_invalid_doi",
        ),
        (
            Reply(status=404, payload={"message": "not found"}),
            "paper_not_found",
        ),
    ],
)
def test_conclusive_nonmatches_do_not_populate_a_doi(
    reply: Reply,
    reason: str,
) -> None:
    paper = _paper(alternate_titles=())
    result = _resolver(FakeHttp([reply])).resolve(paper)

    assert result.status == "no_match"
    assert result.reason == reason
    assert result.paper is paper
    assert result.doi is None


def test_empty_match_envelope_is_a_conclusive_no_match() -> None:
    result = _resolver(
        FakeHttp([Reply(payload=_envelope())])
    ).resolve(_paper(alternate_titles=()))

    assert result.status == "no_match"
    assert result.reason == "paper_not_found"
    assert result.returned_items == 0
    assert result.doi is None


def test_multiple_match_items_are_rejected_as_ambiguous_response(
    tmp_path: Path,
) -> None:
    paper = _paper(alternate_titles=())
    resolver = _resolver(
        FakeHttp(
            [
                Reply(
                    payload=_envelope(
                        _item(
                            title="디지털 포렌식 연구",
                            doi="10.1234/first",
                        ),
                        _item(
                            title="디지털 포렌식 연구",
                            doi="10.1234/second",
                        ),
                    )
                )
            ]
        ),
        cache_dir=tmp_path,
    )

    result = resolver.resolve(paper)

    assert result.status == "ambiguous"
    assert result.reason == "multiple_match_items"
    assert result.error_code == "multiple_match_items"
    assert result.returned_items == 0
    assert result.doi is None
    path = resolver.cache_path_for(paper)
    assert path is not None and path.exists()

    cached = _resolver(
        FakeHttp([]),
        cache_dir=tmp_path,
    ).resolve(paper)
    assert cached.status == "ambiguous"
    assert cached.reason == "multiple_match_items"
    assert cached.cache_hit is True


def test_http_or_malformed_oa_url_never_becomes_a_candidate() -> None:
    for item in (
        _item(
            title="디지털 포렌식 연구",
            pdf_url="http://repository.example/paper.pdf",
        ),
        _item(
            title="디지털 포렌식 연구",
            status={"invalid": True},
        ),
        _item(
            title="디지털 포렌식 연구",
            is_open=False,
        ),
    ):
        result = _resolver(FakeHttp([Reply(payload=_envelope(item))])).resolve(
            _paper(alternate_titles=())
        )
        assert result.status == "resolved"
        assert result.doi == "10.1234/study"
        assert result.pdf is None
        assert result.candidate is None


def test_existing_doi_and_missing_metadata_make_no_requests() -> None:
    http = FakeHttp([])
    gate_calls: list[str] = []
    resolver = _resolver(http, gate_calls=gate_calls)

    existing = resolver.resolve(_paper(doi="10.1234/existing"))
    missing_title = resolver.resolve(
        _paper(source_id="ART000000002", title=None, alternate_titles=())
    )
    missing_year = resolver.resolve(
        _paper(source_id="ART000000003", year=None)
    )

    assert existing.status == "already_present"
    assert missing_title.reason == "missing_title"
    assert missing_year.reason == "missing_publication_year"
    assert not http.calls
    assert not gate_calls


@pytest.mark.parametrize(
    ("status", "reason", "error_code"),
    [
        (429, "semantic_scholar_rate_limited", "rate_limited"),
        (500, "semantic_scholar_server_error", "server_error"),
        (503, "semantic_scholar_server_error", "server_error"),
    ],
)
def test_429_and_5xx_are_retryable_uncached_http_errors(
    tmp_path: Path,
    status: int,
    reason: str,
    error_code: str,
) -> None:
    paper = _paper(alternate_titles=())
    http = FakeHttp([Reply(status=status, payload={"message": "temporary"})])
    resolver = _resolver(http, cache_dir=tmp_path)

    result = resolver.resolve(paper)

    assert result.status == "http_error"
    assert result.reason == reason
    assert result.http_status == status
    assert result.error_code == error_code
    assert resolver.cache_path_for(paper) is not None
    assert not resolver.cache_path_for(paper).exists()  # type: ignore[union-attr]


def test_429_stops_alternate_queries_and_defers_the_shared_gate() -> None:
    http = FakeHttp(
        [
            Reply(
                status=429,
                payload={"message": "limited"},
                headers={"Retry-After": "7"},
            )
        ]
    )
    defer_calls: list[float] = []

    result = _resolver(http, defer_calls=defer_calls).resolve(_paper())

    assert result.status == "http_error"
    assert result.http_status == 429
    assert len(http.calls) == 1
    assert defer_calls == [7.0]


def test_429_retries_same_query_then_resolves_through_shared_gates() -> None:
    secret = "issued-retry-key"
    http = FakeHttp(
        [
            Reply(
                status=429,
                payload={"message": "limited"},
                headers={"Retry-After": "7"},
            ),
            Reply(
                payload=_envelope(_item(title="디지털 포렌식 연구"))
            ),
        ]
    )
    gate_calls: list[str] = []
    defer_calls: list[float] = []
    sleeper_calls: list[float] = []

    result = _resolver(
        http,
        key=secret,
        gate_calls=gate_calls,
        defer_calls=defer_calls,
        sleeper_calls=sleeper_calls,
        retries=1,
    ).resolve(_paper(alternate_titles=()))

    assert result.status == "resolved"
    assert result.query_count == 2
    assert len(http.calls) == 2
    assert gate_calls == [call[0] for call in http.calls]
    assert defer_calls == [7.0]
    assert sleeper_calls == []
    assert all(secret not in url for url in gate_calls)
    assert all(call[2] == {
        "Accept": "application/json",
        "x-api-key": secret,
    } for call in http.calls)


def test_retry_exhaustion_counts_every_request_and_defers_final_failure() -> None:
    http = FakeHttp(
        [
            Reply(
                status=429,
                payload={"message": "limited"},
                headers={"Retry-After": "3"},
            ),
            Reply(status=503, payload={"message": "unavailable"}),
            Reply(
                status=429,
                payload={"message": "still limited"},
                headers={"Retry-After": "9"},
            ),
        ]
    )
    gate_calls: list[str] = []
    defer_calls: list[float] = []

    result = _resolver(
        http,
        gate_calls=gate_calls,
        defer_calls=defer_calls,
        retries=2,
    ).resolve(_paper(alternate_titles=()))

    assert result.status == "http_error"
    assert result.reason == "semantic_scholar_rate_limited"
    assert result.http_status == 429
    assert result.query_count == 3
    assert len(http.calls) == 3
    assert gate_calls == [call[0] for call in http.calls]
    assert defer_calls == [3.0, 10.0, 9.0]


def test_retry_without_defer_gate_sleeps_before_shared_gate_retry() -> None:
    http = FakeHttp(
        [
            Reply(status=503, payload={"message": "unavailable"}),
            Reply(
                payload=_envelope(_item(title="디지털 포렌식 연구"))
            ),
        ]
    )
    gate_calls: list[str] = []
    sleeper_calls: list[float] = []

    result = _resolver(
        http,
        gate_calls=gate_calls,
        retries=1,
        retry_backoff_seconds=4.0,
        sleeper_calls=sleeper_calls,
    ).resolve(_paper(alternate_titles=()))

    assert result.status == "resolved"
    assert result.query_count == 2
    assert gate_calls == [call[0] for call in http.calls]
    assert sleeper_calls == [4.0]


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failures_are_never_retried(status: int) -> None:
    http = FakeHttp(
        [
            Reply(status=status, payload={"message": "unauthorized"}),
            Reply(
                payload=_envelope(_item(title="디지털 포렌식 연구"))
            ),
        ]
    )
    gate_calls: list[str] = []
    defer_calls: list[float] = []
    sleeper_calls: list[float] = []

    result = _resolver(
        http,
        gate_calls=gate_calls,
        defer_calls=defer_calls,
        sleeper_calls=sleeper_calls,
        retries=3,
    ).resolve(_paper())

    assert result.status == "http_error"
    assert result.reason == "semantic_scholar_authentication_failed"
    assert result.http_status == status
    assert result.query_count == 1
    assert len(http.calls) == 1
    assert len(gate_calls) == 1
    assert defer_calls == []
    assert sleeper_calls == []


@pytest.mark.parametrize(
    ("reply", "reason", "error_code"),
    [
        (
            Reply(body=b"{"),
            "invalid_semantic_scholar_title_json",
            "invalid_json",
        ),
        (
            Reply(payload=[]),
            "invalid_semantic_scholar_title_payload",
            "invalid_payload",
        ),
        (
            Reply(payload=_item(title="디지털 포렌식 연구")),
            "invalid_semantic_scholar_title_payload",
            "invalid_payload",
        ),
        (
            Reply(payload={"data": {"data": []}}),
            "invalid_semantic_scholar_title_payload",
            "invalid_payload",
        ),
        (
            Reply(payload={"data": [None]}),
            "invalid_semantic_scholar_title_payload",
            "invalid_payload",
        ),
        (
            Reply(payload={"data": [], "unexpected": []}),
            "invalid_semantic_scholar_title_payload",
            "invalid_payload",
        ),
    ],
)
def test_invalid_200_payloads_are_not_cached(
    tmp_path: Path,
    reply: Reply,
    reason: str,
    error_code: str,
) -> None:
    paper = _paper(alternate_titles=())
    resolver = _resolver(FakeHttp([reply]), cache_dir=tmp_path)

    result = resolver.resolve(paper)

    assert result.status == "invalid_response"
    assert result.reason == reason
    assert result.error_code == error_code
    path = resolver.cache_path_for(paper)
    assert path is not None and not path.exists()


def test_unrecognized_response_fields_cannot_leak_into_result_or_cache(
    tmp_path: Path,
) -> None:
    response_secret = "server-side-debug-secret"
    item = _item(title="디지털 포렌식 연구")
    item["unrequestedDebugValue"] = response_secret
    paper = _paper(alternate_titles=())
    resolver = _resolver(
        FakeHttp([Reply(payload=_envelope(item))]),
        cache_dir=tmp_path,
    )

    result = resolver.resolve(paper)

    assert result.status == "resolved"
    assert response_secret not in repr(result)
    path = resolver.cache_path_for(paper)
    assert path is not None
    assert response_secret not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (RetryableHttpError("temporary"), "retryable_error"),
        (ResponseTooLargeError("too large"), "too_large"),
    ],
)
def test_request_and_size_failures_are_safe_results(
    error: BaseException,
    error_code: str,
) -> None:
    result = _resolver(FakeHttp([error])).resolve(
        _paper(alternate_titles=())
    )

    assert result.status == "request_error"
    assert result.reason == "semantic_scholar_title_request_failed"
    assert result.error_code == error_code
    assert result.doi is None


def test_redirected_or_cross_host_response_is_rejected() -> None:
    original = (
        f"{SEMANTIC_SCHOLAR_MATCH_URL}?"
        "query=Digital&fields=title"
    )
    fetch = HttpFetch(
        requested_url=original,
        final_url="https://evil.example/collect",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            _envelope(_item(title="디지털 포렌식 연구"))
        ).encode(),
        redirects=("https://evil.example/collect",),
    )
    result = _resolver(FakeHttp([fetch])).resolve(
        _paper(alternate_titles=())
    )

    assert result.status == "invalid_response"
    assert result.reason == "semantic_scholar_redirect_rejected"
    assert result.error_code == "redirect_not_allowed"
    assert result.doi is None


def test_default_transport_disables_redirects_retries_and_internal_gate() -> None:
    external_gate: list[str] = []
    resolver = SemanticScholarTitleResolver(
        SemanticScholarTitleConfig(api_key="issued-key"),
        request_gate=external_gate.append,
    )

    assert getattr(resolver.http, "max_redirects") == 0
    assert getattr(resolver.http, "retries") == 0
    assert getattr(resolver.http, "_request_gate") is None


def test_positive_cache_rehydrates_pdf_and_never_persists_secret(
    tmp_path: Path,
) -> None:
    secret = "very-private-s2-key"
    paper = _paper(alternate_titles=())
    first_http = FakeHttp(
        [
            Reply(
                payload=_envelope(_item(title="디지털 포렌식 연구"))
            )
        ]
    )
    first = _resolver(
        first_http,
        cache_dir=tmp_path,
        key=secret,
    ).resolve(paper)
    assert first.status == "resolved"

    cached_http = FakeHttp([])
    cached = _resolver(
        cached_http,
        cache_dir=tmp_path,
        key=secret,
    ).resolve(paper)

    assert cached.status == "resolved"
    assert cached.cache_hit is True
    assert cached.doi == first.doi
    assert cached.candidate == first.candidate
    assert not cached_http.calls
    path = tmp_path / (
        f"{paper.source_id}.semantic-scholar-title.json"
    )
    cache_text = path.read_text(encoding="utf-8")
    assert secret not in cache_text
    assert "x-api-key" not in cache_text.casefold()


def test_negative_cache_fingerprint_includes_all_known_titles(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    paper = _paper()
    first_http = FakeHttp(
        [
            Reply(status=404, payload={"message": "not found"}),
            Reply(status=404, payload={"message": "not found"}),
        ]
    )
    first = _resolver(
        first_http,
        cache_dir=tmp_path,
        now=now,
    ).resolve(paper)
    assert first.status == "no_match"

    cached_http = FakeHttp([])
    cached = _resolver(
        cached_http,
        cache_dir=tmp_path,
        now=now + timedelta(hours=1),
    ).resolve(paper)
    assert cached.cache_hit is True
    assert not cached_http.calls

    changed = replace(
        paper,
        alternate_titles=("A Revised Digital Forensics Study",),
    )
    changed_http = FakeHttp(
        [
            Reply(status=404, payload={"message": "not found"}),
            Reply(status=404, payload={"message": "not found"}),
        ]
    )
    changed_result = _resolver(
        changed_http,
        cache_dir=tmp_path,
        now=now + timedelta(hours=1),
    ).resolve(changed)

    assert changed_result.cache_hit is False
    assert len(changed_http.calls) == 2


def test_expired_negative_cache_is_queried_again(tmp_path: Path) -> None:
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    paper = _paper(alternate_titles=())
    _resolver(
        FakeHttp([Reply(status=404, payload={})]),
        cache_dir=tmp_path,
        now=now,
    ).resolve(paper)
    later_http = FakeHttp([Reply(status=404, payload={})])

    result = _resolver(
        later_http,
        cache_dir=tmp_path,
        now=now + timedelta(days=2),
    ).resolve(paper)

    assert result.cache_hit is False
    assert len(later_http.calls) == 1


def test_legacy_flat_response_cache_schema_is_invalidated(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    paper = _paper(alternate_titles=())
    first_resolver = _resolver(
        FakeHttp([Reply(status=404, payload={})]),
        cache_dir=tmp_path,
        now=now,
    )
    first = first_resolver.resolve(paper)
    assert first.status == "no_match"

    path = first_resolver.cache_path_for(paper)
    assert path is not None
    cached = json.loads(path.read_text(encoding="utf-8"))
    assert cached["schema_version"] == "2.0"
    cached["schema_version"] = "1.0"
    path.write_text(json.dumps(cached), encoding="utf-8")

    next_http = FakeHttp(
        [
            Reply(
                payload=_envelope(_item(title="디지털 포렌식 연구"))
            )
        ]
    )
    result = _resolver(
        next_http,
        cache_dir=tmp_path,
        now=now + timedelta(hours=1),
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.cache_hit is False
    assert result.doi == "10.1234/study"
    assert len(next_http.calls) == 1


def test_one_failed_title_prevents_partial_single_doi_adoption() -> None:
    result = _resolver(
        FakeHttp(
            [
                Reply(
                    payload=_envelope(
                        _item(
                            title="디지털 포렌식 연구",
                            doi="10.1234/partial",
                        )
                    )
                ),
                Reply(status=500, payload={"message": "temporary"}),
            ]
        )
    ).resolve(_paper())

    assert result.status == "http_error"
    assert result.reason == "semantic_scholar_server_error"
    assert result.doi is None
    assert result.candidate is None


@pytest.mark.parametrize(
    "key",
    ["", "   ", "has whitespace", "비ASCII키", "key\ninjection"],
)
def test_invalid_api_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="api_key"):
        SemanticScholarTitleResolver(
            SemanticScholarTitleConfig(api_key=key),
            request_gate=lambda _url: None,
            http=FakeHttp([]),  # type: ignore[arg-type]
        )
