from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.openalex_identifier import (
    CACHE_SCHEMA_VERSION,
    OPENALEX_MAX_RESULTS,
    OpenAlexIdentifierConfig,
    OpenAlexTitleResolver,
)
from PaperPipeline.records import PaperCandidate


class FakeHttp:
    def __init__(self, responses: list[HttpFetch | BaseException]) -> None:
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
        return response


def _paper(
    *,
    source_id: str = "ART000000001",
    title: str | None = "A Digital Forensics Study",
    year: int | None = 2024,
    doi: str | None = None,
    alternate_titles: tuple[str, ...] = (),
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


def _work(
    title: str,
    year: int,
    doi: str | None,
    *,
    best: object | None = None,
    locations: object | None = None,
    nested_doi: str | None = None,
) -> dict:
    value: dict[str, object] = {
        "display_name": title,
        "publication_year": year,
    }
    if doi is not None:
        value["doi"] = doi
    if nested_doi is not None:
        value["ids"] = {"doi": nested_doi}
    if best is not None:
        value["best_oa_location"] = best
    if locations is not None:
        value["locations"] = locations
    return value


def _location(
    *,
    pdf: str | None = "https://repository.example/paper.pdf",
    landing: str | None = "https://repository.example/record/1",
    is_oa: object = True,
    license_value: object = "cc-by",
    version: object = "publishedVersion",
    source_id: str = "https://openalex.org/S123",
    source_name: str = "Example Repository",
) -> dict:
    return {
        "pdf_url": pdf,
        "landing_page_url": landing,
        "is_oa": is_oa,
        "license": license_value,
        "version": version,
        "source": {
            "id": source_id,
            "display_name": source_name,
        },
    }


def _fetch(
    items: list[object],
    *,
    status: int = 200,
    body: bytes | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url="https://api.openalex.org/works",
        final_url="https://api.openalex.org/works",
        status_code=status,
        headers={"Content-Type": "application/json"},
        body=body if body is not None else json.dumps({"results": items}).encode(),
        redirects=(),
    )


def test_exact_title_year_and_unique_doi_populate_paper_and_locations() -> None:
    paper = _paper(title="  Digital   Forensics Ａ &amp; B ", year=2024)
    best = _location(license_value=None)
    duplicate_with_metadata = _location(license_value="cc-by")
    second_pdf = _location(
        pdf="https://archive.example/accepted.pdf",
        landing="https://archive.example/items/2",
        version="acceptedVersion",
        source_id="https://openalex.org/S456",
        source_name="Archive",
    )
    landing_only = _location(
        pdf=None,
        landing="https://publisher.example/article/3",
        is_oa=None,
    )
    http = FakeHttp(
        [
            _fetch(
                [
                    _work(
                        "Digital Forensics A & B: Extended",
                        2024,
                        "10.9999/fuzzy",
                    ),
                    _work(
                        "DIGITAL FORENSICS A & B",
                        2025,
                        "https://doi.org/10.1234%2FAbC",
                        best=best,
                        locations=[
                            duplicate_with_metadata,
                            second_pdf,
                            landing_only,
                            _location(pdf="http://unsafe.example/paper.pdf", landing=None),
                        ],
                        nested_doi="doi:10.1234/abc",
                    ),
                ]
            )
        ]
    )
    config = OpenAlexIdentifierConfig(api_key="private-openalex-key")
    resolver = OpenAlexTitleResolver(config, http=http)  # type: ignore[arg-type]

    result = resolver.resolve(paper)

    assert result.status == "resolved"
    assert result.reason == "exact_title_and_year_match"
    assert result.resolved is True
    assert result.doi == "10.1234/AbC"
    assert result.doi_original == "https://doi.org/10.1234%2FAbC"
    assert result.paper is not paper
    assert paper.doi is None
    assert result.paper.doi_source == "openalex-title"
    assert result.paper.identifier_resolution == {
        "resolver": "openalex-title",
        "status": "resolved",
        "reason": "exact_title_and_year_match",
        "matched_title": "DIGITAL FORENSICS A & B",
        "matched_year": 2025,
        "cache_hit": False,
    }
    assert result.returned_items == 2
    assert result.exact_title_matches == 1
    assert result.title_year_matches == 1

    assert len(result.locations) == 3
    assert result.locations[0].is_best is True
    assert result.locations[0].license == "cc-by"
    assert result.locations[0].source_name == "Example Repository"
    assert result.locations[2].pdf_url is None
    assert len(result.candidates) == 2
    assert result.pdf_candidates == result.candidates
    assert result.candidates[0].url == "https://repository.example/paper.pdf"
    assert result.candidates[0].landing_url == "https://repository.example/record/1"
    assert result.candidates[0].verified_doi == "10.1234/AbC"
    assert result.candidates[0].is_open_access is True

    assert len(http.calls) == 1
    url, max_bytes, headers = http.calls[0]
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).hostname == "api.openalex.org"
    assert urlsplit(url).path == "/works"
    assert query["search"] == ["Digital   Forensics Ａ &amp; B"]
    assert query["per-page"] == [str(OPENALEX_MAX_RESULTS)]
    assert query["api_key"] == ["private-openalex-key"]
    assert max_bytes == 4 * 1024 * 1024
    assert headers == {"Accept": "application/json"}
    assert "private-openalex-key" not in repr(config)


def test_searches_each_distinct_known_title_and_merges_same_doi_locations() -> None:
    paper = _paper(
        title="디지털 포렌식 연구",
        alternate_titles=(
            "A Digital Forensics Study",
            "  A   DIGITAL FORENSICS STUDY  ",
        ),
    )
    http = FakeHttp(
        [
            _fetch(
                [
                    _work(
                        "디지털 포렌식 연구",
                        2024,
                        "10.1234/same",
                        best=_location(),
                    )
                ]
            ),
            _fetch(
                [
                    _work(
                        "A DIGITAL FORENSICS STUDY",
                        2025,
                        "https://doi.org/10.1234/SAME",
                        locations=[
                            _location(
                                pdf="https://archive.example/english.pdf",
                                landing="https://archive.example/english",
                            )
                        ],
                    )
                ]
            ),
        ]
    )

    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=http,  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.doi == "10.1234/same"
    assert result.returned_items == 2
    assert result.exact_title_matches == 2
    assert result.title_year_matches == 2
    assert [candidate.url for candidate in result.candidates] == [
        "https://repository.example/paper.pdf",
        "https://archive.example/english.pdf",
    ]
    assert [
        parse_qs(urlsplit(url).query)["search"][0]
        for url, _, _ in http.calls
    ] == [
        "디지털 포렌식 연구",
        "A Digital Forensics Study",
    ]


def test_alternate_title_can_resolve_when_primary_title_is_missing() -> None:
    paper = _paper(
        title=None,
        alternate_titles=("An English Database Title",),
    )
    http = FakeHttp(
        [
            _fetch(
                [
                    _work(
                        "AN ENGLISH DATABASE TITLE",
                        2024,
                        "10.1234/alternate",
                    )
                ]
            )
        ]
    )

    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=http,  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.doi == "10.1234/alternate"
    assert parse_qs(urlsplit(http.calls[0][0]).query)["search"] == [
        "An English Database Title"
    ]


def test_distinct_dois_across_known_title_searches_are_ambiguous() -> None:
    paper = _paper(
        title="한국어 제목",
        alternate_titles=("English Title",),
    )
    http = FakeHttp(
        [
            _fetch([_work("한국어 제목", 2024, "10.1234/korean")]),
            _fetch([_work("English Title", 2024, "10.1234/english")]),
        ]
    )

    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=http,  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "ambiguous"
    assert result.reason == "multiple_distinct_dois"
    assert result.doi is None
    assert result.locations == ()
    assert len(http.calls) == 2


def test_later_known_title_failure_prevents_partial_resolution(
    tmp_path: Path,
) -> None:
    paper = _paper(
        title="Primary Title",
        alternate_titles=("Alternate Title",),
    )
    http = FakeHttp(
        [
            _fetch([_work("Primary Title", 2024, "10.1234/partial")]),
            _fetch([], status=503),
        ]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    result = resolver.resolve(paper)

    assert result.status == "http_error"
    assert result.http_status == 503
    assert result.doi is None
    assert len(http.calls) == 2
    cache_path = resolver.cache_path_for(paper)
    assert cache_path is not None and not cache_path.exists()


@pytest.mark.parametrize(
    ("items", "reason", "exact_count", "year_count"),
    [
        (
            [_work("A Digital Forensics Study: Extended", 2024, "10.1234/one")],
            "title_mismatch",
            0,
            0,
        ),
        (
            [_work("A Digital Forensics Study", 2026, "10.1234/one")],
            "year_mismatch",
            1,
            0,
        ),
        (
            [_work("A Digital Forensics Study", 2024, "not-a-doi")],
            "missing_or_invalid_doi",
            1,
            1,
        ),
    ],
)
def test_fuzzy_year_and_invalid_doi_results_are_rejected(
    items: list[object],
    reason: str,
    exact_count: int,
    year_count: int,
) -> None:
    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=FakeHttp([_fetch(items)]),  # type: ignore[arg-type]
    ).resolve(_paper())

    assert result.status == "no_match"
    assert result.reason == reason
    assert result.resolved is False
    assert result.doi is None
    assert result.locations == ()
    assert result.exact_title_matches == exact_count
    assert result.title_year_matches == year_count


def test_multiple_distinct_dois_are_ambiguous() -> None:
    paper = _paper()
    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch(
                    [
                        _work(paper.title or "", 2023, "10.1234/one"),
                        _work(paper.title or "", 2024, "10.1234/two"),
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert result.status == "ambiguous"
    assert result.reason == "multiple_distinct_dois"
    assert result.doi is None
    assert result.locations == ()
    assert result.title_year_matches == 2


def test_duplicate_doi_is_one_identity_and_its_locations_are_combined() -> None:
    paper = _paper()
    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch(
                    [
                        _work(
                            paper.title or "",
                            2023,
                            "10.1234/SAME",
                            best=_location(),
                        ),
                        _work(
                            paper.title or "",
                            2025,
                            "https://doi.org/10.1234/same",
                            locations=[
                                _location(
                                    pdf="https://archive.example/other.pdf",
                                    landing="https://archive.example/other",
                                )
                            ],
                        ),
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.doi == "10.1234/SAME"
    assert result.title_year_matches == 2
    assert [candidate.url for candidate in result.candidates] == [
        "https://repository.example/paper.pdf",
        "https://archive.example/other.pdf",
    ]


def test_conflicting_direct_and_nested_dois_are_ambiguous() -> None:
    paper = _paper()
    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch(
                    [
                        _work(
                            paper.title or "",
                            2024,
                            "10.1234/one",
                            nested_doi="10.1234/two",
                        )
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert result.status == "ambiguous"
    assert result.reason == "multiple_distinct_dois"


def test_only_requested_maximum_results_are_considered() -> None:
    paper = _paper()
    items = [
        _work(f"Unrelated title {index}", 2024, f"10.1234/{index}")
        for index in range(OPENALEX_MAX_RESULTS)
    ]
    items.append(_work(paper.title or "", 2024, "10.1234/eleventh"))
    result = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key"),
        http=FakeHttp([_fetch(items)]),  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "no_match"
    assert result.reason == "title_mismatch"
    assert result.returned_items == OPENALEX_MAX_RESULTS


def test_existing_doi_missing_metadata_and_missing_key_make_no_requests(
    tmp_path: Path,
) -> None:
    http = FakeHttp([])
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    existing = resolver.resolve(_paper(doi="10.1234/existing"))
    assert existing.status == "already_present"
    assert existing.resolved is True

    missing_paper = _paper(title=None, year=None, source_id="ART-MISSING")
    missing = resolver.resolve(missing_paper)
    assert missing.status == "missing_metadata"
    assert missing.reason == "missing_title_and_publication_year"
    assert resolver.resolve(missing_paper).cache_hit is True

    no_key = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(),
        http=http,  # type: ignore[arg-type]
    ).resolve(_paper(source_id="ART-NO-KEY"))
    assert no_key.status == "configuration_error"
    assert no_key.reason == "missing_openalex_api_key"
    assert not http.calls


def test_positive_cache_is_atomic_rehydrates_locations_and_never_stores_key(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
    paper = _paper(source_id="ART/CACHE")
    secret = "openalex-secret-value"
    http = FakeHttp(
        [
            _fetch(
                [
                    _work(
                        paper.title or "",
                        2024,
                        "doi:10.1234/Cached",
                        best=_location(),
                    )
                ]
            )
        ]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(
            api_key=secret,
            cache_dir=tmp_path,
            positive_ttl_seconds=3600,
        ),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    first = resolver.resolve(paper)
    cached_without_key = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(cache_dir=tmp_path),
        http=FakeHttp([]),  # type: ignore[arg-type]
        clock=lambda: now,
    ).resolve(paper)

    assert first.status == "resolved"
    assert first.cache_hit is False
    assert cached_without_key.status == "resolved"
    assert cached_without_key.cache_hit is True
    assert cached_without_key.doi == "10.1234/Cached"
    assert cached_without_key.paper.doi_source == "openalex-title"
    assert cached_without_key.paper.identifier_resolution is not None
    assert cached_without_key.paper.identifier_resolution["cache_hit"] is True
    assert cached_without_key.candidates[0].verified_doi == "10.1234/Cached"
    assert len(http.calls) == 1

    cache_path = resolver.cache_path_for(paper)
    assert cache_path is not None and cache_path.is_file()
    cache_text = cache_path.read_text(encoding="utf-8")
    value = json.loads(cache_text)
    assert value["schema_version"] == CACHE_SCHEMA_VERSION
    assert value["source_id"] == paper.source_id
    assert value["input"]["normalized_titles"] == [
        "a digital forensics study"
    ]
    assert "normalized_title" not in value["input"]
    assert value["result"]["status"] == "resolved"
    assert value["result"]["doi"] == "10.1234/Cached"
    assert value["expires_at"] == "2026-07-27T06:00:00Z"
    assert secret not in cache_text
    assert "api_key" not in cache_text
    assert not list(tmp_path.glob(f".{cache_path.name}.*.part"))


def test_negative_cache_expiry_and_force_refresh_retry_search(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)]
    paper = _paper()
    http = FakeHttp(
        [
            _fetch([_work(paper.title or "", 2030, "10.1234/too-new")]),
            _fetch([_work(paper.title or "", 2024, "10.1234/found")]),
            _fetch([_work(paper.title or "", 2024, "10.1234/refreshed")]),
        ]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(
            api_key="key",
            cache_dir=tmp_path,
            negative_ttl_seconds=60,
        ),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now[0],
    )

    first = resolver.resolve(paper)
    cached = resolver.resolve(paper)
    now[0] += timedelta(seconds=61)
    after_expiry = resolver.resolve(paper)
    forced = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(
            api_key="key",
            cache_dir=tmp_path,
            force_refresh=True,
        ),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now[0],
    ).resolve(paper)

    assert first.status == "no_match"
    assert first.cache_hit is False
    assert cached.status == "no_match"
    assert cached.cache_hit is True
    assert after_expiry.doi == "10.1234/found"
    assert forced.doi == "10.1234/refreshed"
    assert len(http.calls) == 3


def test_cache_input_identity_prevents_reuse_after_title_change(
    tmp_path: Path,
) -> None:
    first_paper = _paper(title="First title", source_id="ART-SAME")
    changed_paper = _paper(title="Corrected title", source_id="ART-SAME")
    http = FakeHttp(
        [
            _fetch([_work("Unrelated", 2024, "10.1234/no")]),
            _fetch([_work("Corrected title", 2024, "10.1234/yes")]),
        ]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    assert resolver.resolve(first_paper).status == "no_match"
    changed = resolver.resolve(changed_paper)

    assert changed.status == "resolved"
    assert changed.cache_hit is False
    assert changed.doi == "10.1234/yes"
    assert len(http.calls) == 2


def test_cache_input_identity_includes_alternate_titles(tmp_path: Path) -> None:
    first_paper = _paper(
        title="Stable primary",
        alternate_titles=("Old alternate",),
        source_id="ART-ALTERNATE-CACHE",
    )
    changed_paper = _paper(
        title="Stable primary",
        alternate_titles=("New alternate",),
        source_id="ART-ALTERNATE-CACHE",
    )
    http = FakeHttp(
        [
            _fetch([]),
            _fetch([]),
            _fetch([]),
            _fetch([_work("New alternate", 2024, "10.1234/new-title")]),
        ]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="private-key", cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    assert resolver.resolve(first_paper).status == "no_match"
    changed = resolver.resolve(changed_paper)

    assert changed.status == "resolved"
    assert changed.cache_hit is False
    assert changed.doi == "10.1234/new-title"
    assert len(http.calls) == 4
    cache_path = resolver.cache_path_for(changed_paper)
    assert cache_path is not None
    cache_text = cache_path.read_text(encoding="utf-8")
    value = json.loads(cache_text)
    assert value["input"]["normalized_titles"] == [
        "stable primary",
        "new alternate",
    ]
    assert "private-key" not in cache_text


def test_transient_and_invalid_responses_are_not_negative_cached(
    tmp_path: Path,
) -> None:
    paper = _paper()
    request_http = FakeHttp(
        [RetryableHttpError("temporary"), RetryableHttpError("temporary")]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path / "request"),
        http=request_http,  # type: ignore[arg-type]
    )

    first = resolver.resolve(paper)
    second = resolver.resolve(paper)
    assert first.status == second.status == "request_error"
    assert first.error_code == second.error_code == "retryable_error"
    assert len(request_http.calls) == 2
    path = resolver.cache_path_for(paper)
    assert path is not None and not path.exists()

    invalid_http = FakeHttp(
        [
            _fetch([], body=b"{"),
            _fetch([], body=json.dumps({"results": {}}).encode()),
        ]
    )
    invalid_resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path / "invalid"),
        http=invalid_http,  # type: ignore[arg-type]
    )
    assert invalid_resolver.resolve(paper).reason == "invalid_openalex_json"
    assert invalid_resolver.resolve(paper).reason == "invalid_openalex_payload"
    assert len(invalid_http.calls) == 2


def test_http_errors_do_not_fill_or_cache_a_doi(tmp_path: Path) -> None:
    paper = _paper()
    http = FakeHttp([_fetch([], status=429), _fetch([], status=503)])
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    first = resolver.resolve(paper)
    second = resolver.resolve(paper)

    assert first.status == second.status == "http_error"
    assert first.http_status == 429
    assert second.http_status == 503
    assert first.doi is None
    assert len(http.calls) == 2
    path = resolver.cache_path_for(paper)
    assert path is not None and not path.exists()


def test_corrupt_cached_identity_is_ignored_and_refetched(tmp_path: Path) -> None:
    now = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
    paper = _paper()
    first_http = FakeHttp(
        [_fetch([_work(paper.title or "", 2024, "10.1234/original")])]
    )
    resolver = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=first_http,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    assert resolver.resolve(paper).status == "resolved"
    path = resolver.cache_path_for(paper)
    assert path is not None
    value = json.loads(path.read_text(encoding="utf-8"))
    value["result"]["matched_title"] = "Different title"
    path.write_text(json.dumps(value), encoding="utf-8")

    retry_http = FakeHttp(
        [_fetch([_work(paper.title or "", 2024, "10.1234/refetched")])]
    )
    refetched = OpenAlexTitleResolver(
        OpenAlexIdentifierConfig(api_key="key", cache_dir=tmp_path),
        http=retry_http,  # type: ignore[arg-type]
        clock=lambda: now,
    ).resolve(paper)

    assert refetched.doi == "10.1234/refetched"
    assert refetched.cache_hit is False
    assert len(retry_http.calls) == 1


@pytest.mark.parametrize(
    "config",
    [
        OpenAlexIdentifierConfig(api_key=" "),
        OpenAlexIdentifierConfig(api_key="bad\nkey"),
        OpenAlexIdentifierConfig(max_response_bytes=0),
        OpenAlexIdentifierConfig(positive_ttl_seconds=-1),
        OpenAlexIdentifierConfig(negative_ttl_seconds=-1),
    ],
)
def test_rejects_invalid_configuration(config: OpenAlexIdentifierConfig) -> None:
    with pytest.raises(ValueError):
        OpenAlexTitleResolver(config)
