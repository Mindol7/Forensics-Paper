from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.identifier_resolver import (
    CACHE_SCHEMA_VERSION,
    CROSSREF_MAX_RESULTS,
    CrossrefTitleResolver,
    IdentifierResolverConfig,
    normalize_title,
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


def _item(
    title: str,
    year: int,
    doi: str | None,
    *,
    date_field: str = "published",
) -> dict:
    value = {
        "title": [title],
        date_field: {"date-parts": [[year, 1, 1]]},
    }
    if doi is not None:
        value["DOI"] = doi
    return value


def _fetch(
    items: list[object],
    *,
    status: int = 200,
    body: bytes | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url="https://api.crossref.org/works",
        final_url="https://api.crossref.org/works",
        status_code=status,
        headers={"Content-Type": "application/json"},
        body=body
        if body is not None
        else json.dumps({"status": "ok", "message": {"items": items}}).encode(),
        redirects=(),
    )


def test_exact_normalized_title_and_year_window_populates_a_new_paper() -> None:
    paper = _paper(title="  Digital   Forensics Ａ &amp; B ", year=2024)
    http = FakeHttp(
        [
            _fetch(
                [
                    _item(
                        "Digital Forensics A & B: Extended",
                        2024,
                        "10.9999/fuzzy",
                    ),
                    _item(
                        "DIGITAL FORENSICS A & B",
                        2025,
                        "https://doi.org/10.1234%2FAbC",
                        date_field="published-online",
                    ),
                ]
            )
        ]
    )
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(contact_email="researcher@example.org"),
        http=http,  # type: ignore[arg-type]
    )

    result = resolver.resolve(paper)

    assert result.status == "resolved"
    assert result.resolved is True
    assert result.reason == "exact_title_and_year_match"
    assert result.doi == "10.1234/AbC"
    assert result.doi_original == "https://doi.org/10.1234%2FAbC"
    assert result.paper is not paper
    assert paper.doi is None
    assert result.paper.doi == "10.1234/AbC"
    assert result.paper.doi_original == "https://doi.org/10.1234%2FAbC"
    assert result.matched_title == "DIGITAL FORENSICS A & B"
    assert result.matched_year == 2025
    assert result.returned_items == 2
    assert result.exact_title_matches == 1
    assert result.title_year_matches == 1
    assert result.cache_hit is False

    assert len(http.calls) == 1
    url, max_bytes, headers = http.calls[0]
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).hostname == "api.crossref.org"
    assert urlsplit(url).path == "/works"
    assert query["query.title"] == ["Digital   Forensics Ａ &amp; B"]
    assert query["rows"] == [str(CROSSREF_MAX_RESULTS)]
    assert query["mailto"] == ["researcher@example.org"]
    assert max_bytes == 2 * 1024 * 1024
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
            _fetch([_item("Unrelated title", 2024, "10.1234/no")]),
            _fetch(
                [
                    _item(
                        "A DIGITAL FORENSICS STUDY",
                        2025,
                        "10.1234/alternate",
                    )
                ]
            ),
        ]
    )
    result = CrossrefTitleResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.doi == "10.1234/alternate"
    assert result.matched_title == "A DIGITAL FORENSICS STUDY"
    assert result.matched_year == 2025
    assert result.returned_items == 2
    assert result.exact_title_matches == 1
    assert len(http.calls) == 2
    assert [
        parse_qs(urlsplit(call[0]).query)["query.title"][0]
        for call in http.calls
    ] == ["디지털 포렌식 연구", "A Digital Forensics Study"]


def test_different_dois_found_through_different_known_titles_are_ambiguous() -> None:
    paper = _paper(
        title="디지털 포렌식 연구",
        alternate_titles=("A Digital Forensics Study",),
    )
    result = CrossrefTitleResolver(
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch([_item("디지털 포렌식 연구", 2024, "10.1234/ko")]),
                _fetch(
                    [
                        _item(
                            "A Digital Forensics Study",
                            2024,
                            "10.1234/en",
                        )
                    ]
                ),
            ]
        ),
    ).resolve(paper)

    assert result.status == "ambiguous"
    assert result.reason == "multiple_distinct_dois"
    assert result.doi is None
    assert result.title_year_matches == 2


def test_alternate_title_is_usable_when_primary_title_is_missing() -> None:
    paper = _paper(title=None, alternate_titles=("Alternate title",))
    http = FakeHttp([_fetch([_item("Alternate title", 2024, "10.1234/alt")])])

    result = CrossrefTitleResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(paper)

    assert result.status == "resolved"
    assert result.doi == "10.1234/alt"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ("items", "reason", "exact_count", "year_count"),
    [
        (
            [_item("A Digital Forensics Study: Extended", 2024, "10.1234/one")],
            "title_mismatch",
            0,
            0,
        ),
        (
            [_item("A Digital Forensics Study", 2026, "10.1234/one")],
            "year_mismatch",
            1,
            0,
        ),
        (
            [_item("A Digital Forensics Study", 2024, "not-a-doi")],
            "missing_or_invalid_doi",
            1,
            1,
        ),
    ],
)
def test_title_year_and_doi_mismatches_are_rejected(
    items: list[object],
    reason: str,
    exact_count: int,
    year_count: int,
) -> None:
    paper = _paper()
    resolver = CrossrefTitleResolver(
        http=FakeHttp([_fetch(items)]),  # type: ignore[arg-type]
    )

    result = resolver.resolve(paper)

    assert result.status == "no_match"
    assert result.reason == reason
    assert result.resolved is False
    assert result.paper is paper
    assert result.doi is None
    assert result.exact_title_matches == exact_count
    assert result.title_year_matches == year_count


def test_distinct_matching_dois_are_ambiguous_but_duplicate_doi_is_not() -> None:
    paper = _paper()
    ambiguous = CrossrefTitleResolver(
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch(
                    [
                        _item(paper.title or "", 2023, "10.1234/one"),
                        _item(paper.title or "", 2024, "10.1234/two"),
                    ]
                )
            ]
        )
    ).resolve(paper)

    assert ambiguous.status == "ambiguous"
    assert ambiguous.reason == "multiple_distinct_dois"
    assert ambiguous.doi is None
    assert ambiguous.title_year_matches == 2

    duplicate = CrossrefTitleResolver(
        http=FakeHttp(  # type: ignore[arg-type]
            [
                _fetch(
                    [
                        _item(paper.title or "", 2023, "10.1234/SAME"),
                        _item(
                            paper.title or "",
                            2025,
                            "https://doi.org/10.1234/same",
                        ),
                    ]
                )
            ]
        )
    ).resolve(paper)

    assert duplicate.status == "resolved"
    assert duplicate.doi == "10.1234/SAME"
    assert duplicate.title_year_matches == 2


def test_only_first_five_crossref_results_are_considered() -> None:
    paper = _paper()
    items = [
        _item(f"Unrelated title {index}", 2024, f"10.1234/{index}")
        for index in range(CROSSREF_MAX_RESULTS)
    ]
    items.append(_item(paper.title or "", 2024, "10.1234/sixth"))
    resolver = CrossrefTitleResolver(
        http=FakeHttp([_fetch(items)]),  # type: ignore[arg-type]
    )

    result = resolver.resolve(paper)

    assert result.status == "no_match"
    assert result.reason == "title_mismatch"
    assert result.returned_items == CROSSREF_MAX_RESULTS


def test_existing_doi_and_missing_metadata_do_not_make_http_requests(
    tmp_path: Path,
) -> None:
    http = FakeHttp([])
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    existing = resolver.resolve(_paper(doi="10.1234/existing"))
    assert existing.status == "already_present"
    assert existing.resolved is True

    missing_paper = _paper(title=None, year=None, source_id="ART-MISSING")
    missing = resolver.resolve(missing_paper)
    assert missing.status == "missing_metadata"
    assert missing.reason == "missing_title_and_publication_year"
    assert missing.doi is None
    assert not http.calls

    cached = resolver.resolve(missing_paper)
    assert cached.status == "missing_metadata"
    assert cached.cache_hit is True
    assert not http.calls


def test_positive_cache_is_atomic_and_rehydrates_the_resolved_paper(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
    paper = _paper(source_id="ART/CACHE")
    http = FakeHttp(
        [_fetch([_item(paper.title or "", 2024, "doi:10.1234/Cached")])]
    )
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(
            cache_dir=tmp_path,
            positive_ttl_seconds=3600,
        ),
        http=http,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    first = resolver.resolve(paper)
    second = resolver.resolve(paper)

    assert first.status == "resolved"
    assert first.cache_hit is False
    assert second.status == "resolved"
    assert second.cache_hit is True
    assert second.doi == "10.1234/Cached"
    assert len(http.calls) == 1

    cache_path = resolver.cache_path_for(paper)
    assert cache_path is not None and cache_path.is_file()
    value = json.loads(cache_path.read_text(encoding="utf-8"))
    assert value["schema_version"] == CACHE_SCHEMA_VERSION
    assert value["source_id"] == paper.source_id
    assert value["result"]["status"] == "resolved"
    assert value["result"]["doi"] == "10.1234/Cached"
    assert value["input"]["normalized_titles"] == [
        "a digital forensics study"
    ]
    assert "normalized_title" not in value["input"]
    assert value["expires_at"] == "2026-07-27T06:00:00Z"
    assert not list(tmp_path.glob(f".{cache_path.name}.*.part"))


def test_negative_cache_expires_and_changed_crossref_data_is_retried(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)]
    paper = _paper()
    http = FakeHttp(
        [
            _fetch([_item(paper.title or "", 2030, "10.1234/too-new")]),
            _fetch([_item(paper.title or "", 2024, "10.1234/found-later")]),
        ]
    )
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(
            cache_dir=tmp_path,
            negative_ttl_seconds=60,
        ),
        http=http,  # type: ignore[arg-type]
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
    assert refreshed.doi == "10.1234/found-later"
    assert len(http.calls) == 2


def test_cache_input_fingerprint_prevents_reuse_after_title_change(
    tmp_path: Path,
) -> None:
    first_paper = _paper(title="First title", source_id="ART-SAME")
    changed_paper = _paper(title="Corrected title", source_id="ART-SAME")
    http = FakeHttp(
        [
            _fetch([_item("Unrelated", 2024, "10.1234/no")]),
            _fetch([_item("Corrected title", 2024, "10.1234/yes")]),
        ]
    )
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
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
            _fetch([_item("Correct alternate", 2024, "10.1234/alternate")]),
        ]
    )
    resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(cache_dir=tmp_path),
        http=http,  # type: ignore[arg-type]
    )

    assert resolver.resolve(first_paper).status == "no_match"
    cache_path = resolver.cache_path_for(first_paper)
    assert cache_path is not None
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["input"]["normalized_titles"] == [
        "primary title",
        "first alternate",
    ]
    cached["schema_version"] = "1.1"
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    changed = resolver.resolve(changed_paper)

    assert changed.status == "resolved"
    assert changed.cache_hit is False
    assert changed.doi == "10.1234/alternate"
    assert len(http.calls) == 4


def test_transient_request_and_response_errors_are_not_negative_cached(
    tmp_path: Path,
) -> None:
    paper = _paper()
    request_http = FakeHttp(
        [RetryableHttpError("temporary"), RetryableHttpError("temporary")]
    )
    request_resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(cache_dir=tmp_path / "request"),
        http=request_http,  # type: ignore[arg-type]
    )

    first = request_resolver.resolve(paper)
    second = request_resolver.resolve(paper)

    assert first.status == second.status == "request_error"
    assert first.error_code == second.error_code == "retryable_error"
    assert len(request_http.calls) == 2
    request_path = request_resolver.cache_path_for(paper)
    assert request_path is not None and not request_path.exists()

    invalid_http = FakeHttp(
        [
            _fetch([], body=b"{"),
            _fetch([], body=b"{"),
        ]
    )
    invalid_resolver = CrossrefTitleResolver(
        IdentifierResolverConfig(cache_dir=tmp_path / "invalid"),
        http=invalid_http,  # type: ignore[arg-type]
    )
    assert invalid_resolver.resolve(paper).status == "invalid_response"
    assert invalid_resolver.resolve(paper).status == "invalid_response"
    assert len(invalid_http.calls) == 2


def test_http_error_and_malformed_payload_never_fill_a_doi() -> None:
    paper = _paper()
    http_error = CrossrefTitleResolver(
        http=FakeHttp([_fetch([], status=503)]),  # type: ignore[arg-type]
    ).resolve(paper)
    malformed = CrossrefTitleResolver(
        http=FakeHttp(  # type: ignore[arg-type]
            [_fetch([], body=json.dumps({"message": []}).encode())]
        )
    ).resolve(paper)

    assert http_error.status == "http_error"
    assert http_error.http_status == 503
    assert http_error.doi is None
    assert malformed.status == "invalid_response"
    assert malformed.reason == "invalid_crossref_payload"
    assert malformed.doi is None


def test_title_normalization_is_unicode_case_and_whitespace_only_not_fuzzy() -> None:
    assert normalize_title("  Ａ &amp;   B\nStudy  ") == "a & b study"
    assert normalize_title("A-B") != normalize_title("A B")
    assert normalize_title(None) is None


@pytest.mark.parametrize(
    "config",
    [
        IdentifierResolverConfig(max_response_bytes=0),
        IdentifierResolverConfig(positive_ttl_seconds=-1),
        IdentifierResolverConfig(negative_ttl_seconds=-1),
    ],
)
def test_rejects_invalid_configuration(config: IdentifierResolverConfig) -> None:
    with pytest.raises(ValueError):
        CrossrefTitleResolver(config)
