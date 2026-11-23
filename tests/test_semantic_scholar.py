from __future__ import annotations

import json
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.records import PaperCandidate
from PaperPipeline.semantic_scholar import (
    CACHE_SCHEMA_VERSION,
    SEMANTIC_SCHOLAR_BATCH_URL,
    SEMANTIC_SCHOLAR_MAX_BATCH,
    SemanticScholarBatchResolver,
    SemanticScholarConfig,
)


class FakeHttp:
    def __init__(self, responses: list[HttpFetch | BaseException]) -> None:
        self.responses = deque(responses)
        self.calls: list[
            tuple[str, dict[str, object], int, dict[str, str] | None]
        ] = []

    def post_json_bytes(
        self,
        url: str,
        *,
        json_body: dict[str, object],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append((url, json_body, max_bytes, headers))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _paper(
    index: int = 1,
    *,
    doi: str | None = "10.1234/Study",
    title: str | None = "A Digital Forensics Study",
    year: int | None = 2024,
    source_id: str | None = None,
) -> PaperCandidate:
    return PaperCandidate(
        database_id=index,
        source="kci",
        source_id=source_id or f"ART{index:09d}",
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=year,
        first_page=None,
        final_page=None,
    )


def _item(
    *,
    doi: str = "10.1234/study",
    title: str = "A Digital Forensics Study",
    year: int = 2024,
    is_open: bool = True,
    pdf_url: str | None = "https://example.org/paper.pdf",
    status: str | None = "GREEN",
    license_value: str | None = "CCBY",
    disclaimer: str | None = "Publisher-provided metadata",
) -> dict[str, Any]:
    pdf = None
    if pdf_url is not None:
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


def _fetch(
    payload: Any,
    *,
    status: int = 200,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url=SEMANTIC_SCHOLAR_BATCH_URL,
        final_url=SEMANTIC_SCHOLAR_BATCH_URL,
        status_code=status,
        headers=headers or {"Content-Type": "application/json"},
        body=json.dumps(payload).encode() if body is None else body,
        redirects=(),
    )


def test_exact_identity_and_oa_https_pdf_are_exposed_with_metadata() -> None:
    paper = _paper(
        doi="https://doi.org/10.1234%2FStudy",
        title="  Ａ Digital   Forensics Study ",
        year=2024,
    )
    http = FakeHttp(
        [
            _fetch(
                [
                    _item(
                        doi="doi:10.1234/study",
                        title="A DIGITAL FORENSICS STUDY",
                        year=2025,
                    )
                ]
            )
        ]
    )
    resolver = SemanticScholarBatchResolver(http=http)  # type: ignore[arg-type]

    index = resolver.resolve([paper])
    result = index[paper.source_id]

    assert result.status == "resolved"
    assert result.reason == "exact_doi_title_year_open_access_match"
    assert result.resolved is True
    assert result.cache_hit is False
    assert result.http_status == 200
    assert result.pdf is not None
    assert result.pdf.doi == "10.1234/study"
    assert result.pdf.title == "A DIGITAL FORENSICS STUDY"
    assert result.pdf.year == 2025
    assert result.pdf.url == "https://example.org/paper.pdf"
    assert result.pdf.status == "GREEN"
    assert result.pdf.license == "CCBY"
    assert result.pdf.disclaimer == "Publisher-provided metadata"

    candidate = index.candidate_for(paper)
    assert candidate is not None
    assert candidate.resolver == "semantic-scholar"
    assert candidate.is_open_access is True
    assert candidate.verified_doi == "10.1234/study"
    assert candidate.version == "GREEN"
    assert candidate.license == "CCBY"
    assert index.pdf_for_doi("10.1234/STUDY") == result.pdf
    assert index.open_access_pdfs["10.1234/study"] == result.pdf

    assert len(http.calls) == 1
    url, body, max_bytes, headers = http.calls[0]
    assert url == SEMANTIC_SCHOLAR_BATCH_URL
    assert body == {"ids": ["DOI:10.1234/Study"]}
    assert max_bytes == 16 * 1024 * 1024
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def test_exact_alternate_title_is_valid_identity() -> None:
    paper = replace(
        _paper(title="디지털 포렌식 연구"),
        alternate_titles=("A Digital Forensics Study",),
    )
    result = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([_item()])])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]

    assert result.status == "resolved"
    assert result.candidate is not None


@pytest.mark.parametrize(
    ("item", "status", "reason"),
    [
        (
            _item(doi="10.9999/other"),
            "identity_mismatch",
            "external_doi_mismatch",
        ),
        (
            _item(title="A Digital Forensics Study: Extended"),
            "identity_mismatch",
            "title_mismatch",
        ),
        (
            _item(year=2026),
            "identity_mismatch",
            "year_mismatch",
        ),
        (
            _item(is_open=False),
            "not_open_access",
            "is_open_access_not_true",
        ),
        (
            _item(pdf_url=None),
            "missing_pdf_url",
            "open_access_pdf_missing",
        ),
        (
            _item(pdf_url="http://example.org/paper.pdf"),
            "missing_pdf_url",
            "open_access_pdf_url_not_https",
        ),
    ],
)
def test_identity_oa_and_https_requirements_are_conservative(
    item: dict[str, Any],
    status: str,
    reason: str,
) -> None:
    paper = _paper()
    index = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([item])])  # type: ignore[arg-type]
    ).resolve([paper])

    result = index[paper.source_id]
    assert result.status == status
    assert result.reason == reason
    assert result.pdf is None
    assert result.candidate is None


@pytest.mark.parametrize("field", ["status", "license", "disclaimer"])
def test_malformed_optional_pdf_metadata_is_rejected(field: str) -> None:
    item = _item()
    assert isinstance(item["openAccessPdf"], dict)
    item["openAccessPdf"][field] = {"unexpected": True}
    result = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([item])])  # type: ignore[arg-type]
    ).resolve([_paper()])["ART000000001"]

    assert result.status == "invalid_pdf_metadata"
    assert result.pdf is None


def test_requests_are_deduplicated_by_doi_and_chunked_at_500() -> None:
    papers = [
        _paper(
            index,
            doi=f"10.1234/{index}",
            title=f"Study {index}",
            year=2024,
        )
        for index in range(1, 502)
    ]
    # A second paper with the first DOI should not add another API identifier.
    duplicate = _paper(
        9999,
        doi="10.1234/1",
        title="Study 1",
        year=2024,
    )
    papers.append(duplicate)
    first_payload = [
        _item(
            doi=f"10.1234/{index}",
            title=f"Study {index}",
            year=2024,
        )
        for index in range(1, 501)
    ]
    second_payload = [
        _item(doi="10.1234/501", title="Study 501", year=2024)
    ]
    http = FakeHttp([_fetch(first_payload), _fetch(second_payload)])

    index = SemanticScholarBatchResolver(http=http).resolve(papers)  # type: ignore[arg-type]

    assert len(index) == 502
    assert all(result.resolved for result in index.values())
    assert len(http.calls) == 2
    assert len(http.calls[0][1]["ids"]) == SEMANTIC_SCHOLAR_MAX_BATCH
    assert len(http.calls[1][1]["ids"]) == 1
    assert http.calls[0][1]["ids"][0] == "DOI:10.1234/1"
    assert http.calls[1][1]["ids"] == ["DOI:10.1234/501"]


def test_optional_api_key_uses_official_header_but_anonymous_is_default() -> None:
    keyed_http = FakeHttp([_fetch([_item()])])
    SemanticScholarBatchResolver(
        SemanticScholarConfig(api_key="  official-key  "),
        http=keyed_http,  # type: ignore[arg-type]
    ).resolve([_paper()])
    anonymous_http = FakeHttp([_fetch([_item()])])
    SemanticScholarBatchResolver(
        http=anonymous_http,  # type: ignore[arg-type]
    ).resolve([_paper()])

    assert keyed_http.calls[0][3] is not None
    assert keyed_http.calls[0][3]["x-api-key"] == "official-key"
    assert anonymous_http.calls[0][3] is not None
    assert "x-api-key" not in anonymous_http.calls[0][3]


def test_default_transport_enforces_below_one_rps_gate() -> None:
    resolver = SemanticScholarBatchResolver(
        SemanticScholarConfig(requests_per_second=0.8)
    )

    assert getattr(resolver.http, "_request_gate", None) is not None


def test_default_transport_accepts_a_shared_cumulative_request_gate() -> None:
    calls: list[str] = []

    def shared_gate(url: str) -> None:
        calls.append(url)

    resolver = SemanticScholarBatchResolver(request_gate=shared_gate)

    assert getattr(resolver.http, "_request_gate", None) is shared_gate


def test_missing_or_invalid_inputs_do_not_make_an_http_request() -> None:
    papers = [
        _paper(1, doi=None),
        _paper(2, title=None),
        _paper(3, year=None),
        _paper(4, doi="not-a-doi"),
    ]
    http = FakeHttp([])

    index = SemanticScholarBatchResolver(http=http).resolve(papers)  # type: ignore[arg-type]

    assert not http.calls
    assert index[papers[0].source_id].reason == "missing_doi"
    assert index[papers[1].source_id].reason == "missing_title"
    assert index[papers[2].source_id].reason == "missing_publication_year"
    assert index[papers[3].source_id].status == "invalid_doi"


def test_null_is_not_found_but_malformed_batch_is_a_safe_error() -> None:
    paper = _paper()
    not_found = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([None])])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]
    malformed = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([])])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]
    invalid_json = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch([], body=b"{")])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]

    assert not_found.status == "not_found"
    assert malformed.status == "invalid_response"
    assert malformed.reason == "invalid_semantic_scholar_payload"
    assert invalid_json.status == "invalid_response"
    assert invalid_json.reason == "invalid_semantic_scholar_json"


def test_request_and_http_failures_return_results_instead_of_raising() -> None:
    paper = _paper()
    request_result = SemanticScholarBatchResolver(
        http=FakeHttp([RetryableHttpError("temporary")])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]
    http_result = SemanticScholarBatchResolver(
        http=FakeHttp([_fetch({"message": "limited"}, status=429)])  # type: ignore[arg-type]
    ).resolve([paper])[paper.source_id]

    assert request_result.status == "request_error"
    assert request_result.error_code == "retryable_error"
    assert http_result.status == "http_error"
    assert http_result.http_status == 429


def test_final_429_still_defers_the_shared_limiter() -> None:
    deferrals: list[float] = []
    result = SemanticScholarBatchResolver(
        SemanticScholarConfig(retries=0),
        http=FakeHttp(
            [
                _fetch(
                    {"message": "limited"},
                    status=429,
                    headers={
                        "Content-Type": "application/json",
                        "Retry-After": "9",
                    },
                )
            ]
        ),  # type: ignore[arg-type]
        defer_gate=deferrals.append,
    ).resolve([_paper()])["ART000000001"]

    assert result.status == "http_error"
    assert result.http_status == 429
    assert deferrals == [9.0]


def test_retry_after_429_is_honored_before_success() -> None:
    sleeps: list[float] = []
    deferrals: list[float] = []
    http = FakeHttp(
        [
            _fetch(
                {"message": "limited"},
                status=429,
                headers={
                    "Content-Type": "application/json",
                    "Retry-After": "3",
                },
            ),
            _fetch([_item()]),
        ]
    )
    result = SemanticScholarBatchResolver(
        SemanticScholarConfig(retries=1),
        http=http,  # type: ignore[arg-type]
        sleeper=sleeps.append,
        defer_gate=deferrals.append,
    ).resolve([_paper()])["ART000000001"]

    assert result.status == "resolved"
    assert len(http.calls) == 2
    assert sleeps == [3.0]
    assert deferrals == [3.0]


def test_atomic_cache_rehydrates_positive_and_negative_results(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
    cache_path = tmp_path / "semantic-scholar.json"
    resolved_paper = _paper(1)
    negative_paper = _paper(
        2,
        doi="10.1234/missing",
        title="Missing Study",
    )
    http = FakeHttp([_fetch([_item(), None])])
    resolver = SemanticScholarBatchResolver(
        SemanticScholarConfig(cache_path=cache_path),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    first = resolver.resolve([resolved_paper, negative_paper])
    second = resolver.resolve([resolved_paper, negative_paper])

    assert first[resolved_paper.source_id].cache_hit is False
    assert first[negative_paper.source_id].status == "not_found"
    assert second[resolved_paper.source_id].cache_hit is True
    assert second[negative_paper.source_id].cache_hit is True
    assert second[resolved_paper.source_id].pdf is not None
    assert second[resolved_paper.source_id].pdf.disclaimer == (
        "Publisher-provided metadata"
    )
    assert len(http.calls) == 1

    value = json.loads(cache_path.read_text(encoding="utf-8"))
    assert value["schema_version"] == CACHE_SCHEMA_VERSION
    assert value["input_fingerprint"] == first.input_fingerprint
    assert len(value["input_fingerprint"]) == 64
    assert value["inputs"][0]["source_id"] == resolved_paper.source_id
    assert value["results"][resolved_paper.source_id]["result"][
        "open_access_pdf"
    ]["license"] == "CCBY"
    assert (
        value["results"][resolved_paper.source_id]["expires_at"]
        == "2026-08-26T05:00:00Z"
    )
    assert (
        value["results"][negative_paper.source_id]["expires_at"]
        == "2026-07-28T05:00:00Z"
    )
    assert not list(tmp_path.glob(f".{cache_path.name}.*.part"))


def test_cache_fingerprint_ttl_and_force_refresh(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)]
    cache_path = tmp_path / "semantic-scholar.json"
    paper = _paper()
    changed = _paper(title="Corrected title")
    http = FakeHttp(
        [
            _fetch([None]),
            _fetch(
                [
                    _item(
                        title="Corrected title",
                        pdf_url="https://example.org/corrected.pdf",
                    )
                ]
            ),
            _fetch(
                [
                    _item(
                        title="Corrected title",
                        pdf_url="https://example.org/refreshed.pdf",
                    )
                ]
            ),
            _fetch(
                [
                    _item(
                        title="Corrected title",
                        pdf_url="https://example.org/forced.pdf",
                    )
                ]
            ),
        ]
    )
    config = SemanticScholarConfig(
        cache_path=cache_path,
        negative_ttl_seconds=60,
        positive_ttl_seconds=60,
    )
    resolver = SemanticScholarBatchResolver(
        config,
        http=http,  # type: ignore[arg-type]
        clock=lambda: now[0],
    )

    first = resolver.resolve([paper])
    changed_result = resolver.resolve([changed])
    now[0] += timedelta(seconds=61)
    expired = resolver.resolve([changed])
    forced = SemanticScholarBatchResolver(
        SemanticScholarConfig(
            cache_path=cache_path,
            force_refresh=True,
        ),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now[0],
    ).resolve([changed])

    assert first[paper.source_id].status == "not_found"
    assert changed_result.input_fingerprint != first.input_fingerprint
    assert changed_result[changed.source_id].pdf is not None
    assert changed_result[changed.source_id].pdf.url.endswith("corrected.pdf")
    assert expired[changed.source_id].cache_hit is False
    assert expired[changed.source_id].pdf is not None
    assert expired[changed.source_id].pdf.url.endswith("refreshed.pdf")
    assert forced[changed.source_id].pdf is not None
    assert forced[changed.source_id].pdf.url.endswith("forced.pdf")
    assert len(http.calls) == 4


def test_transient_errors_are_not_cached(tmp_path: Path) -> None:
    paper = _paper()
    http = FakeHttp(
        [
            RetryableHttpError("temporary"),
            _fetch([_item()]),
        ]
    )
    resolver = SemanticScholarBatchResolver(
        SemanticScholarConfig(cache_path=tmp_path / "cache.json"),
        http=http,  # type: ignore[arg-type]
    )

    first = resolver.resolve([paper])
    second = resolver.resolve([paper])

    assert first[paper.source_id].status == "request_error"
    assert second[paper.source_id].status == "resolved"
    assert len(http.calls) == 2


def test_duplicate_source_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate source_id"):
        SemanticScholarBatchResolver(http=FakeHttp([])).resolve(  # type: ignore[arg-type]
            [_paper(1), _paper(2, source_id="ART000000001")]
        )


@pytest.mark.parametrize(
    "config",
    [
        SemanticScholarConfig(batch_size=0),
        SemanticScholarConfig(batch_size=501),
        SemanticScholarConfig(max_response_bytes=0),
        SemanticScholarConfig(connect_timeout=0),
        SemanticScholarConfig(read_timeout=0),
        SemanticScholarConfig(requests_per_second=0),
        SemanticScholarConfig(requests_per_second=1),
        SemanticScholarConfig(retries=-1),
        SemanticScholarConfig(retry_backoff_seconds=-1),
        SemanticScholarConfig(max_retry_after_seconds=-1),
        SemanticScholarConfig(api_key=" \t "),
        SemanticScholarConfig(positive_ttl_seconds=-1),
        SemanticScholarConfig(negative_ttl_seconds=-1),
    ],
)
def test_invalid_configuration_is_rejected(config: SemanticScholarConfig) -> None:
    with pytest.raises(ValueError):
        SemanticScholarBatchResolver(config)
