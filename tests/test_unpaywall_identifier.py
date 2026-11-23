from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.records import PaperCandidate
from PaperPipeline.unpaywall_identifier import (
    CACHE_SCHEMA_VERSION,
    UNPAYWALL_MAX_RESULTS,
    UnpaywallIdentifierConfig,
    UnpaywallTitleResolver,
)


class FakeHttp:
    def __init__(self, responses: list[HttpFetch | BaseException]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, int, Mapping[str, str] | None]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
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
    alternate_titles: tuple[str, ...] = (),
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


def _result(
    title: str,
    year: int | object,
    doi: str | None,
    *,
    is_oa: bool = False,
    best_oa_location: object | None = None,
    oa_locations: object | None = None,
) -> dict:
    response: dict[str, object] = {
        "title": title,
        "year": year,
        "is_oa": is_oa,
    }
    if doi is not None:
        response["doi"] = doi
    if best_oa_location is not None:
        response["best_oa_location"] = best_oa_location
    if oa_locations is not None:
        response["oa_locations"] = oa_locations
    return {
        "score": 0.999,
        "snippet": "<b>untrusted display text</b>",
        "response": response,
    }


def _fetch(
    results: list[object],
    *,
    status: int = 200,
    body: bytes | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url="https://api.unpaywall.org/v2/search",
        final_url="https://api.unpaywall.org/v2/search",
        status_code=status,
        headers={"Content-Type": "application/json"},
        body=body
        if body is not None
        else json.dumps({"results": results, "elapsed_seconds": 0.01}).encode(),
        redirects=(),
    )


def _config(
    *,
    email: str = "researcher@example.org",
    cache_dir: Path | None = None,
    positive_ttl_seconds: int = 30 * 24 * 60 * 60,
    negative_ttl_seconds: int = 24 * 60 * 60,
    force_refresh: bool = False,
) -> UnpaywallIdentifierConfig:
    return UnpaywallIdentifierConfig(
        email=email,
        cache_dir=cache_dir,
        positive_ttl_seconds=positive_ttl_seconds,
        negative_ttl_seconds=negative_ttl_seconds,
        force_refresh=force_refresh,
    )


def test_exact_title_and_year_resolves_and_preserves_oa_metadata() -> None:
    paper = _paper(title="  Digital   Forensics Ａ &amp; B ", year=2024)
    http = FakeHttp(
        [
            _fetch(
                [
                    _result(
                        "Digital Forensics A & B: Extended",
                        2024,
                        "10.9999/fuzzy",
                    ),
                    _result(
                        "DIGITAL FORENSICS A & B",
                        2025,
                        "https://doi.org/10.1234%2FAbC",
                        is_oa=True,
                        best_oa_location={
                            "url_for_landing_page": "https://repo.example/item/abc",
                            "license": "cc-by",
                            "version": "acceptedVersion",
                        },
                        oa_locations=[
                            {
                                "url_for_pdf": "https://repo.example/item/abc.pdf",
                                "url_for_landing_page": (
                                    "https://repo.example/item/abc"
                                ),
                                "license": "cc-by-nc",
                                "version": "publishedVersion",
                                "host_type": "repository",
                            }
                        ],
                    ),
                ]
            )
        ]
    )
    resolver = UnpaywallTitleResolver(_config(), http=http)

    resolution = resolver.resolve(paper)

    assert resolution.status == "resolved"
    assert resolution.reason == "exact_title_and_year_match"
    assert resolution.resolved is True
    assert resolution.doi == "10.1234/AbC"
    assert resolution.doi_original == "https://doi.org/10.1234%2FAbC"
    assert resolution.paper is not paper
    assert paper.doi is None
    assert resolution.paper.doi_source == "unpaywall-title"
    assert resolution.matched_title == "DIGITAL FORENSICS A & B"
    assert resolution.matched_year == 2025
    assert resolution.returned_items == 2
    assert resolution.exact_title_matches == 1
    assert resolution.title_year_matches == 1
    assert resolution.cache_hit is False

    location = resolution.oa_location
    assert location is not None
    assert location.url_for_pdf == "https://repo.example/item/abc.pdf"
    assert location.url_for_landing_page == "https://repo.example/item/abc"
    assert location.license == "cc-by-nc"
    assert location.version == "publishedVersion"
    assert location.host_type == "repository"
    candidate = resolution.candidate
    assert candidate is not None
    assert candidate.url == location.url_for_pdf
    assert candidate.landing_url == location.url_for_landing_page
    assert candidate.resolver == "unpaywall-title"
    assert candidate.is_open_access is True
    assert candidate.verified_doi == "10.1234/AbC"

    record = resolution.paper.identifier_resolution
    assert record is not None
    assert record["resolver"] == "unpaywall-title"
    assert record["oa_location"]["url_for_pdf"] == location.url_for_pdf

    assert len(http.calls) == 1
    url, max_bytes, headers = http.calls[0]
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.hostname == "api.unpaywall.org"
    assert parsed.path == "/v2/search"
    assert query["query"] == ["Digital   Forensics Ａ &amp; B"]
    assert query["email"] == ["researcher@example.org"]
    assert max_bytes == 8 * 1024 * 1024
    assert headers == {"Accept": "application/json"}


def test_all_unique_known_titles_are_queried_and_an_alternate_can_resolve() -> None:
    paper = _paper(
        title="디지털 포렌식 연구",
        alternate_titles=(
            "A Digital Forensics Study",
            "  A DIGITAL FORENSICS STUDY ",
            "디지털 포렌식 연구",
        ),
    )
    http = FakeHttp(
        [
            _fetch([_result("Unrelated title", 2024, "10.1234/no")]),
            _fetch(
                [
                    _result(
                        "A DIGITAL FORENSICS STUDY",
                        2025,
                        "10.1234/alternate",
                    )
                ]
            ),
        ]
    )
    resolution = UnpaywallTitleResolver(_config(), http=http).resolve(paper)

    assert resolution.status == "resolved"
    assert resolution.doi == "10.1234/alternate"
    assert resolution.matched_title == "A DIGITAL FORENSICS STUDY"
    assert resolution.matched_year == 2025
    assert resolution.returned_items == 2
    assert resolution.exact_title_matches == 1
    assert len(http.calls) == 2
    assert [
        parse_qs(urlsplit(call[0]).query)["query"][0]
        for call in http.calls
    ] == ["디지털 포렌식 연구", "A Digital Forensics Study"]


def test_different_dois_found_through_different_known_titles_are_ambiguous() -> None:
    paper = _paper(
        title="디지털 포렌식 연구",
        alternate_titles=("A Digital Forensics Study",),
    )
    resolution = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp(
            [
                _fetch([_result("디지털 포렌식 연구", 2024, "10.1234/ko")]),
                _fetch(
                    [
                        _result(
                            "A Digital Forensics Study",
                            2024,
                            "10.1234/en",
                        )
                    ]
                ),
            ]
        ),
    ).resolve(paper)

    assert resolution.status == "ambiguous"
    assert resolution.reason == "multiple_distinct_dois"
    assert resolution.doi is None
    assert resolution.title_year_matches == 2


def test_alternate_title_is_usable_when_primary_title_is_missing() -> None:
    paper = _paper(title=None, alternate_titles=("Alternate title",))
    http = FakeHttp(
        [_fetch([_result("Alternate title", 2024, "10.1234/alt")])]
    )

    resolution = UnpaywallTitleResolver(_config(), http=http).resolve(paper)

    assert resolution.status == "resolved"
    assert resolution.doi == "10.1234/alt"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ("results", "reason", "exact_count", "year_count"),
    [
        (
            [_result("A Digital Forensics Study: Extended", 2024, "10.1/fuzzy")],
            "title_mismatch",
            0,
            0,
        ),
        (
            [_result("A Digital Forensics Study", 2026, "10.1234/too-new")],
            "year_mismatch",
            1,
            0,
        ),
        (
            [_result("A Digital Forensics Study", 2024, "not-a-doi")],
            "missing_or_invalid_doi",
            1,
            1,
        ),
        (
            [_result("A Digital Forensics Study", True, "10.1234/bool-year")],
            "year_mismatch",
            1,
            0,
        ),
    ],
)
def test_fuzzy_year_and_invalid_doi_matches_are_rejected(
    results: list[object],
    reason: str,
    exact_count: int,
    year_count: int,
) -> None:
    paper = _paper()
    resolution = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp([_fetch(results)]),
    ).resolve(paper)

    assert resolution.status == "no_match"
    assert resolution.reason == reason
    assert resolution.resolved is False
    assert resolution.paper is paper
    assert resolution.doi is None
    assert resolution.exact_title_matches == exact_count
    assert resolution.title_year_matches == year_count


def test_distinct_dois_are_ambiguous_while_duplicate_doi_is_resolved() -> None:
    paper = _paper()
    ambiguous = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp(
            [
                _fetch(
                    [
                        _result(paper.title or "", 2023, "10.1234/one"),
                        _result(paper.title or "", 2024, "10.1234/two"),
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert ambiguous.status == "ambiguous"
    assert ambiguous.reason == "multiple_distinct_dois"
    assert ambiguous.doi is None
    assert ambiguous.title_year_matches == 2

    duplicate = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp(
            [
                _fetch(
                    [
                        _result(
                            paper.title or "",
                            2023,
                            "10.1234/SAME",
                            is_oa=True,
                            best_oa_location={
                                "url_for_landing_page": (
                                    "https://repo.example/same"
                                )
                            },
                        ),
                        _result(
                            paper.title or "",
                            2025,
                            "https://doi.org/10.1234/same",
                            is_oa=True,
                            best_oa_location={
                                "url_for_pdf": "https://repo.example/same.pdf"
                            },
                        ),
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert duplicate.status == "resolved"
    assert duplicate.doi == "10.1234/same"
    assert duplicate.title_year_matches == 2
    assert duplicate.candidate is not None


def test_only_first_fifty_search_results_are_considered() -> None:
    paper = _paper()
    results = [
        _result(f"Unrelated title {index}", 2024, f"10.1234/{index}")
        for index in range(UNPAYWALL_MAX_RESULTS)
    ]
    results.append(_result(paper.title or "", 2024, "10.1234/fifty-first"))
    resolution = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp([_fetch(results)]),
    ).resolve(paper)

    assert resolution.status == "no_match"
    assert resolution.reason == "title_mismatch"
    assert resolution.returned_items == UNPAYWALL_MAX_RESULTS


def test_existing_doi_and_missing_metadata_never_make_http_requests(
    tmp_path: Path,
) -> None:
    http = FakeHttp([])
    resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path),
        http=http,
    )

    existing = resolver.resolve(_paper(doi="10.1234/existing"))
    assert existing.status == "already_present"
    assert existing.resolved is True

    missing_paper = _paper(title=None, year=None, source_id="ART-MISSING")
    missing = resolver.resolve(missing_paper)
    assert missing.status == "missing_metadata"
    assert missing.reason == "missing_title_and_publication_year"
    assert missing.doi is None

    cached = resolver.resolve(missing_paper)
    assert cached.status == "missing_metadata"
    assert cached.cache_hit is True
    assert not http.calls


def test_positive_cache_is_atomic_rehydrates_oa_and_never_stores_email(
    tmp_path: Path,
) -> None:
    email = "private-contact@example.org"
    now = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
    paper = _paper(source_id="ART/CACHE")
    http = FakeHttp(
        [
            _fetch(
                [
                    _result(
                        paper.title or "",
                        2024,
                        "doi:10.1234/Cached",
                        is_oa=True,
                        best_oa_location={
                            "url_for_pdf": "https://repo.example/cached.pdf",
                            "url_for_landing_page": "https://repo.example/cached",
                            "license": "cc-by",
                        },
                    )
                ]
            )
        ]
    )
    resolver = UnpaywallTitleResolver(
        _config(
            email=email,
            cache_dir=tmp_path,
            positive_ttl_seconds=3600,
        ),
        http=http,
        clock=lambda: now,
    )

    first = resolver.resolve(paper)
    second = resolver.resolve(paper)

    assert first.status == "resolved"
    assert first.cache_hit is False
    assert second.status == "resolved"
    assert second.cache_hit is True
    assert second.doi == "10.1234/Cached"
    assert second.candidate is not None
    assert second.candidate.url == "https://repo.example/cached.pdf"
    assert second.paper.identifier_resolution is not None
    assert second.paper.identifier_resolution["cache_hit"] is True
    assert len(http.calls) == 1

    cache_path = resolver.cache_path_for(paper)
    assert cache_path is not None and cache_path.is_file()
    serialized = cache_path.read_text(encoding="utf-8")
    value = json.loads(serialized)
    assert value["schema_version"] == CACHE_SCHEMA_VERSION
    assert value["source_id"] == paper.source_id
    assert value["result"]["doi"] == "10.1234/Cached"
    assert value["result"]["oa_location"]["license"] == "cc-by"
    assert value["input"]["normalized_titles"] == [
        "a digital forensics study"
    ]
    assert "normalized_title" not in value["input"]
    assert value["expires_at"] == "2026-07-27T06:00:00Z"
    assert email not in serialized
    assert "email" not in value
    assert email not in repr(resolver.config)
    assert email not in repr(first)
    assert not list(tmp_path.glob(f".{cache_path.name}.*.part"))


def test_negative_cache_ttl_and_force_refresh_control_requests(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)]
    paper = _paper()
    http = FakeHttp(
        [
            _fetch([_result(paper.title or "", 2030, "10.1234/too-new")]),
            _fetch([_result(paper.title or "", 2024, "10.1234/found")]),
        ]
    )
    resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path, negative_ttl_seconds=60),
        http=http,
        clock=lambda: now[0],
    )

    first = resolver.resolve(paper)
    cached = resolver.resolve(paper)
    now[0] += timedelta(seconds=61)
    refreshed = resolver.resolve(paper)

    assert first.status == "no_match"
    assert first.cache_hit is False
    assert cached.status == "no_match"
    assert cached.cache_hit is True
    assert refreshed.status == "resolved"
    assert refreshed.cache_hit is False
    assert refreshed.doi == "10.1234/found"
    assert len(http.calls) == 2

    force_http = FakeHttp(
        [_fetch([_result(paper.title or "", 2024, "10.1234/forced")])]
    )
    forced = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path, force_refresh=True),
        http=force_http,
        clock=lambda: now[0],
    ).resolve(paper)
    assert forced.status == "resolved"
    assert forced.doi == "10.1234/forced"
    assert forced.cache_hit is False
    assert len(force_http.calls) == 1


def test_changed_input_fingerprint_does_not_reuse_cache(tmp_path: Path) -> None:
    first_paper = _paper(title="First title", source_id="ART-SAME")
    changed_paper = _paper(title="Corrected title", source_id="ART-SAME")
    http = FakeHttp(
        [
            _fetch([_result("Unrelated", 2024, "10.1234/no")]),
            _fetch([_result("Corrected title", 2024, "10.1234/yes")]),
        ]
    )
    resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path),
        http=http,
    )

    assert resolver.resolve(first_paper).status == "no_match"
    changed = resolver.resolve(changed_paper)

    assert changed.status == "resolved"
    assert changed.cache_hit is False
    assert changed.doi == "10.1234/yes"
    assert len(http.calls) == 2


def test_cache_fingerprint_includes_alternate_titles_and_rejects_old_schema(
    tmp_path: Path,
) -> None:
    first_paper = _paper(
        title="Primary title",
        alternate_titles=("First alternate",),
        source_id="ART-ALT-CACHE",
    )
    changed_paper = _paper(
        title="Primary title",
        alternate_titles=("Correct alternate",),
        source_id="ART-ALT-CACHE",
    )
    http = FakeHttp(
        [
            _fetch([]),
            _fetch([]),
            _fetch([]),
            _fetch(
                [_result("Correct alternate", 2024, "10.1234/alternate")]
            ),
        ]
    )
    resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path),
        http=http,
    )

    assert resolver.resolve(first_paper).status == "no_match"
    cache_path = resolver.cache_path_for(first_paper)
    assert cache_path is not None
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["input"]["normalized_titles"] == [
        "primary title",
        "first alternate",
    ]
    cached["schema_version"] = "1.0"
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    changed = resolver.resolve(changed_paper)

    assert changed.status == "resolved"
    assert changed.cache_hit is False
    assert changed.doi == "10.1234/alternate"
    assert len(http.calls) == 4


def test_transient_and_invalid_responses_are_not_cached(tmp_path: Path) -> None:
    paper = _paper()
    request_http = FakeHttp(
        [RetryableHttpError("temporary"), RetryableHttpError("temporary")]
    )
    request_resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path / "request"),
        http=request_http,
    )

    first = request_resolver.resolve(paper)
    second = request_resolver.resolve(paper)
    assert first.status == second.status == "request_error"
    assert first.reason == second.reason == "unpaywall_request_failed"
    assert first.error_code == second.error_code == "retryable_error"
    assert len(request_http.calls) == 2
    cache_path = request_resolver.cache_path_for(paper)
    assert cache_path is not None and not cache_path.exists()

    invalid_http = FakeHttp([_fetch([], body=b"{"), _fetch([], body=b"{")])
    invalid_resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path / "invalid"),
        http=invalid_http,
    )
    assert invalid_resolver.resolve(paper).status == "invalid_response"
    assert invalid_resolver.resolve(paper).status == "invalid_response"
    assert len(invalid_http.calls) == 2


def test_http_errors_and_malformed_payloads_never_fill_a_doi() -> None:
    paper = _paper()
    http_error = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp([_fetch([], status=503)]),
    ).resolve(paper)
    malformed = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp([_fetch([], body=json.dumps({"results": {}}).encode())]),
    ).resolve(paper)

    assert http_error.status == "http_error"
    assert http_error.http_status == 503
    assert http_error.doi is None
    assert malformed.status == "invalid_response"
    assert malformed.reason == "invalid_unpaywall_payload"
    assert malformed.doi is None


@pytest.mark.parametrize(
    "location",
    [
        {"url_for_pdf": "http://repo.example/paper.pdf"},
        {"url_for_pdf": "https://user:secret@repo.example/paper.pdf"},
        {"url_for_pdf": "https://127.0.0.1/paper.pdf"},
        {"url_for_pdf": "https://repo.example:8443/paper.pdf"},
    ],
)
def test_unsafe_oa_urls_are_not_preserved_but_do_not_invalidate_the_doi(
    location: dict[str, str],
) -> None:
    paper = _paper()
    resolution = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp(
            [
                _fetch(
                    [
                        _result(
                            paper.title or "",
                            2024,
                            "10.1234/safe-doi",
                            is_oa=True,
                            best_oa_location=location,
                        )
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert resolution.status == "resolved"
    assert resolution.doi == "10.1234/safe-doi"
    assert resolution.oa_location is None
    assert resolution.candidate is None


def test_location_is_not_treated_as_oa_without_explicit_is_oa_true() -> None:
    paper = _paper()
    resolution = UnpaywallTitleResolver(
        _config(),
        http=FakeHttp(
            [
                _fetch(
                    [
                        _result(
                            paper.title or "",
                            2024,
                            "10.1234/not-oa",
                            is_oa=False,
                            best_oa_location={
                                "url_for_pdf": "https://repo.example/paper.pdf"
                            },
                        )
                    ]
                )
            ]
        ),
    ).resolve(paper)

    assert resolution.status == "resolved"
    assert resolution.oa_location is None
    assert resolution.candidate is None


def test_tampered_cache_status_or_oa_url_is_ignored_safely(tmp_path: Path) -> None:
    paper = _paper()
    first_http = FakeHttp(
        [
            _fetch(
                [
                    _result(
                        paper.title or "",
                        2024,
                        "10.1234/original",
                        is_oa=True,
                        best_oa_location={
                            "url_for_pdf": "https://repo.example/original.pdf"
                        },
                    )
                ]
            )
        ]
    )
    resolver = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path),
        http=first_http,
    )
    assert resolver.resolve(paper).status == "resolved"
    path = resolver.cache_path_for(paper)
    assert path is not None
    value = json.loads(path.read_text(encoding="utf-8"))
    value["result"]["reason"] = "attacker-controlled-reason"
    value["result"]["oa_location"]["url_for_pdf"] = "https://127.0.0.1/a.pdf"
    path.write_text(json.dumps(value), encoding="utf-8")

    retry_http = FakeHttp(
        [_fetch([_result(paper.title or "", 2024, "10.1234/fresh")])]
    )
    fresh = UnpaywallTitleResolver(
        _config(cache_dir=tmp_path),
        http=retry_http,
    ).resolve(paper)

    assert fresh.status == "resolved"
    assert fresh.doi == "10.1234/fresh"
    assert fresh.cache_hit is False
    assert len(retry_http.calls) == 1


@pytest.mark.parametrize(
    "config",
    [
        UnpaywallIdentifierConfig(email=""),
        UnpaywallIdentifierConfig(email="not-an-email"),
        UnpaywallIdentifierConfig(email="a@example.org\nBcc:x@example.org"),
        UnpaywallIdentifierConfig(email="a@example.org", max_response_bytes=0),
        UnpaywallIdentifierConfig(email="a@example.org", positive_ttl_seconds=-1),
        UnpaywallIdentifierConfig(email="a@example.org", negative_ttl_seconds=-1),
    ],
)
def test_invalid_configuration_is_rejected(
    config: UnpaywallIdentifierConfig,
) -> None:
    with pytest.raises(ValueError):
        UnpaywallTitleResolver(config)
