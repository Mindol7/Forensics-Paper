from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.kci_journal import (
    ROBOTS_URL,
    SITEMAP_INDEX_URL,
    InvalidKciSourceIdError,
    KciJournalCollisionError,
    KciJournalIndexLoader,
    KciJournalRobotsError,
    KciJournalUrlError,
)


class FakeHttp:
    user_agent = "Forensics-Paper/1.0 (test)"

    def __init__(
        self,
        responses: dict[str, list[HttpFetch | BaseException]],
    ) -> None:
        self.responses = {
            url: deque(items)
            for url, items in responses.items()
        }
        self.calls: list[tuple[str, int]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del max_html_bytes, headers
        self.calls.append((url, max_bytes))
        value = self.responses[url].popleft()
        if isinstance(value, BaseException):
            raise value
        return value


def _fetch(
    url: str,
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/xml",
    final_url: str | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url=url,
        final_url=final_url or url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body,
        redirects=(),
    )


def _robots(*rules: str) -> bytes:
    values = [
        "User-agent: *",
        *rules,
        f"Sitemap: {SITEMAP_INDEX_URL}",
    ]
    return ("\n".join(values) + "\n").encode()


def _index_xml(*urls: str) -> bytes:
    entries = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return (
        '<?xml version="1.0"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</sitemapindex>"
    ).encode()


def _journal_xml(*urls: str) -> bytes:
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    ).encode()


def _article_url(slug: str, source_id: str) -> str:
    return (
        f"https://journal.kci.go.kr/{slug}/archive/articleView?"
        f"artiId={source_id}"
    )


def _base_responses(
    sitemap_urls: list[str],
) -> dict[str, list[HttpFetch | BaseException]]:
    return {
        ROBOTS_URL: [
            _fetch(
                ROBOTS_URL,
                _robots(
                    "Disallow: *poDownload.kci*",
                    "Allow: /",
                ),
                content_type="text/plain",
            )
        ],
        SITEMAP_INDEX_URL: [
            _fetch(SITEMAP_INDEX_URL, _index_xml(*sitemap_urls))
        ],
    }


def test_builds_index_from_allowlisted_journal_sitemaps() -> None:
    first_sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    second_sitemap = "https://journal.kci.go.kr/MSES/sitemap"
    first_article = _article_url("alpha", "ART000000001")
    second_article = _article_url("MSES", "ART000000002")
    responses = _base_responses([first_sitemap, second_sitemap])
    responses[first_sitemap] = [
        _fetch(
            first_sitemap,
            _journal_xml(
                "https://journal.kci.go.kr/alpha/info/about",
                first_article,
                first_article,
            ),
        )
    ]
    responses[second_sitemap] = [
        _fetch(second_sitemap, _journal_xml(second_article))
    ]
    fake = FakeHttp(responses)

    index = KciJournalIndexLoader(fake).load()  # type: ignore[arg-type]

    assert index.advertised_sitemaps == 2
    assert index.loaded_sitemaps == 2
    assert not index.failures
    assert list(index.articles) == ["ART000000001", "ART000000002"]
    assert index.resolve("ART000000002").slug == "MSES"  # type: ignore[union-attr]
    assert index.pdf_url_for("ART000000001") == (
        "https://journal.kci.go.kr/alpha/archive/articlePdf?"
        "artiId=ART000000001"
    )


def test_special_site_sitemap_ignores_bare_host_pages() -> None:
    site_sitemap = "https://journal.kci.go.kr/kjcSitemap"
    responses = _base_responses([site_sitemap])
    responses[site_sitemap] = [
        _fetch(
            site_sitemap,
            _journal_xml(
                "https://journal.kci.go.kr",
                "https://journal.kci.go.kr/about",
            ),
        )
    ]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert index.loaded_sitemaps == 1
    assert not index.failures
    assert not index.articles


def test_denied_robots_fails_closed_before_sitemap_fetch() -> None:
    fake = FakeHttp(
        {
            ROBOTS_URL: [
                _fetch(
                    ROBOTS_URL,
                    _robots("Disallow: /"),
                    content_type="text/plain",
                )
            ]
        }
    )

    with pytest.raises(KciJournalRobotsError, match="disallows"):
        KciJournalIndexLoader(fake).load()  # type: ignore[arg-type]

    assert [url for url, _limit in fake.calls] == [ROBOTS_URL]


def test_robots_wildcard_denial_blocks_derived_pdf_path() -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    article = _article_url("alpha", "ART000000001")
    responses = _base_responses([sitemap])
    responses[ROBOTS_URL] = [
        _fetch(
            ROBOTS_URL,
            _robots(
                "Disallow: */archive/articlePdf*",
                "Allow: /",
            ),
            content_type="text/plain",
        )
    ]
    responses[sitemap] = [_fetch(sitemap, _journal_xml(article))]

    with pytest.raises(KciJournalRobotsError, match="articlePdf"):
        KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "advertised",
    [
        "https://evil.example/alpha/sitemap",
        "http://journal.kci.go.kr/alpha/sitemap",
        "https://journal.kci.go.kr/alpha/not-sitemap",
        "https://journal.kci.go.kr/alpha/sitemap?next=evil",
        "https://journal.kci.go.kr/a/b/sitemap",
    ],
)
def test_sitemap_index_rejects_unapproved_host_or_path(advertised: str) -> None:
    fake = FakeHttp(_base_responses([advertised]))

    with pytest.raises(KciJournalUrlError):
        KciJournalIndexLoader(fake).load()  # type: ignore[arg-type]

    assert all(url != advertised for url, _limit in fake.calls)


def test_bad_journal_xml_is_reported_without_losing_good_journal() -> None:
    bad_sitemap = "https://journal.kci.go.kr/bad/sitemap"
    good_sitemap = "https://journal.kci.go.kr/good/sitemap"
    responses = _base_responses([bad_sitemap, good_sitemap])
    responses[bad_sitemap] = [_fetch(bad_sitemap, b"<urlset><url>")]
    responses[good_sitemap] = [
        _fetch(
            good_sitemap,
            _journal_xml(_article_url("good", "ART000000009")),
        )
    ]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert index.loaded_sitemaps == 1
    assert index.pdf_url_for("ART000000009") is not None
    assert len(index.failures) == 1
    assert index.failures[0].url == bad_sitemap
    assert index.failures[0].code == "invalid_sitemap"


def test_network_failure_of_one_sitemap_is_partial() -> None:
    failed_sitemap = "https://journal.kci.go.kr/down/sitemap"
    good_sitemap = "https://journal.kci.go.kr/good/sitemap"
    responses = _base_responses([failed_sitemap, good_sitemap])
    responses[failed_sitemap] = [RetryableHttpError("temporary outage")]
    responses[good_sitemap] = [
        _fetch(
            good_sitemap,
            _journal_xml(_article_url("good", "ART000000010")),
        )
    ]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert index.loaded_sitemaps == 1
    assert index.failures[0].code == "retryable_error"
    assert "ART000000010" in index.articles


@pytest.mark.parametrize(
    "article_url",
    [
        _article_url("alpha", "art000000001"),
        _article_url("alpha", "ART123"),
        _article_url("alpha", "ART00000000X"),
        (
            "https://journal.kci.go.kr/alpha/archive/articleView?"
            "artiId=ART000000001&extra=1"
        ),
    ],
)
def test_invalid_source_id_or_query_rejects_that_sitemap(
    article_url: str,
) -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    responses = _base_responses([sitemap])
    responses[sitemap] = [_fetch(sitemap, _journal_xml(article_url))]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert not index.articles
    assert index.loaded_sitemaps == 0
    assert len(index.failures) == 1
    assert index.failures[0].code == "invalid_sitemap"


def test_conflicting_duplicate_source_id_is_fatal() -> None:
    first_sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    second_sitemap = "https://journal.kci.go.kr/beta/sitemap"
    source_id = "ART000000001"
    responses = _base_responses([first_sitemap, second_sitemap])
    responses[first_sitemap] = [
        _fetch(first_sitemap, _journal_xml(_article_url("alpha", source_id)))
    ]
    responses[second_sitemap] = [
        _fetch(second_sitemap, _journal_xml(_article_url("beta", source_id)))
    ]

    with pytest.raises(KciJournalCollisionError, match=source_id):
        KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]


def test_redirected_journal_sitemap_is_a_partial_failure() -> None:
    redirected = "https://journal.kci.go.kr/alpha/sitemap"
    good = "https://journal.kci.go.kr/good/sitemap"
    responses = _base_responses([redirected, good])
    responses[redirected] = [
        _fetch(
            redirected,
            _journal_xml(_article_url("alpha", "ART000000001")),
            final_url="https://journal.kci.go.kr/beta/sitemap",
        )
    ]
    responses[good] = [
        _fetch(
            good,
            _journal_xml(_article_url("good", "ART000000002")),
        )
    ]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert list(index.articles) == ["ART000000002"]
    assert index.failures[0].url == redirected


def test_dtd_or_entity_declaration_is_not_parsed() -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    malicious = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<urlset><url><loc>&xxe;</loc></url></urlset>"
    )
    responses = _base_responses([sitemap])
    responses[sitemap] = [_fetch(sitemap, malicious)]

    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    assert not index.articles
    assert "DTD/entity" in index.failures[0].message


def test_fresh_cache_avoids_sitemap_requests_but_rechecks_robots(
    tmp_path: Path,
) -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    article = _article_url("alpha", "ART000000001")
    cache_path = tmp_path / "kjc-index.json"
    responses = _base_responses([sitemap])
    responses[sitemap] = [_fetch(sitemap, _journal_xml(article))]
    first_http = FakeHttp(responses)
    clock = lambda: 10_000.0
    first = KciJournalIndexLoader(
        first_http,  # type: ignore[arg-type]
        cache_path=cache_path,
        cache_ttl_seconds=3600,
        clock=clock,
    ).load()
    assert not first.from_cache
    cache_path.touch()

    second_http = FakeHttp(
        {
            ROBOTS_URL: [
                _fetch(
                    ROBOTS_URL,
                    _robots("Allow: /"),
                    content_type="text/plain",
                )
            ]
        }
    )
    second = KciJournalIndexLoader(
        second_http,  # type: ignore[arg-type]
        cache_path=cache_path,
        cache_ttl_seconds=3600,
        clock=lambda: cache_path.stat().st_mtime + 30,
    ).load()

    assert second.from_cache
    assert second.pdf_url_for("ART000000001") == first.pdf_url_for(
        "ART000000001"
    )
    assert [url for url, _limit in second_http.calls] == [ROBOTS_URL]


def test_cache_is_not_used_when_new_robots_policy_denies_pdf(
    tmp_path: Path,
) -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    cache_path = tmp_path / "kjc-index.json"
    responses = _base_responses([sitemap])
    responses[sitemap] = [
        _fetch(
            sitemap,
            _journal_xml(_article_url("alpha", "ART000000001")),
        )
    ]
    KciJournalIndexLoader(
        FakeHttp(responses),  # type: ignore[arg-type]
        cache_path=cache_path,
    ).load()

    denied = FakeHttp(
        {
            ROBOTS_URL: [
                _fetch(
                    ROBOTS_URL,
                    _robots(
                        "Disallow: */archive/articlePdf*",
                        "Allow: /",
                    ),
                    content_type="text/plain",
                )
            ]
        }
    )
    with pytest.raises(KciJournalRobotsError):
        KciJournalIndexLoader(
            denied,  # type: ignore[arg-type]
            cache_path=cache_path,
        ).load()


def test_resolve_rejects_noncanonical_source_id() -> None:
    sitemap = "https://journal.kci.go.kr/alpha/sitemap"
    responses = _base_responses([sitemap])
    responses[sitemap] = [_fetch(sitemap, _journal_xml())]
    index = KciJournalIndexLoader(FakeHttp(responses)).load()  # type: ignore[arg-type]

    with pytest.raises(InvalidKciSourceIdError):
        index.resolve("../ART000000001")
