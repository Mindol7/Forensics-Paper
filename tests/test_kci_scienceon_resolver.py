from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass

from PaperPipeline.http_client import HttpFetch
from PaperPipeline.kci_scienceon_resolver import (
    KCI_SCIENCEON_LOOKUP_URL,
    KCI_SCIENCEON_RESOLVER_NAME,
    KciScienceOnConfig,
    KciScienceOnResolver,
)
from PaperPipeline.records import PaperCandidate


TITLE = "안티 포렌식 동향 분석 및 대응 방안 연구"
SOURCE_ID = "ART002950194"
CN = "JAKO202317151736307"
VIEWER_URL = (
    "https://scienceon.kisti.re.kr/commons/util/originalView.do"
    f"?cn={CN}&dbt=JAKO"
)


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"


class FakeHttp:
    def __init__(self, *, posts: list[Reply], gets: list[Reply]) -> None:
        self.posts = deque(posts)
        self.gets = deque(gets)
        self.post_calls: list[tuple[str, dict[str, str], int]] = []
        self.get_calls: list[tuple[str, int]] = []

    def post_form_bytes(
        self,
        url: str,
        *,
        data: dict[str, str],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del headers
        self.post_calls.append((url, data, max_bytes))
        reply = self.posts.popleft()
        return _fetch(url, reply)

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del headers
        self.get_calls.append((url, max_bytes))
        reply = self.gets.popleft()
        return _fetch(url, reply)


def _fetch(url: str, reply: Reply) -> HttpFetch:
    return HttpFetch(
        requested_url=url,
        final_url=url,
        status_code=reply.status,
        headers={"Content-Type": reply.content_type},
        body=reply.body,
        redirects=(),
    )


def _paper(
    *,
    source_id: str = SOURCE_ID,
    title: str | None = TITLE,
    year: int | None = 2023,
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=source_id,
        title=title,
        alternate_titles=(
            "Analysis of anti-forensic trends and research on countermeasures",
        ),
        doi_original="10.33778/kcsa.2023.23.1.097",
        doi="10.33778/kcsa.2023.23.1.097",
        landing_url=None,
        abstract=None,
        publication_year=year,
        first_page="97",
        final_page="107",
    )


def _lookup(
    *,
    title: str = TITLE,
    year: str = "2023",
    urls: list[str] | None = None,
) -> bytes:
    if urls is None:
        urls = [
            "10.33778/kcsa.2023.23.1.097",
            (
                "http://click.ndsl.kr/servlet/OpenAPIFullTextView"
                f"?cn={CN}&dbt=JAKO&keyValue=01866548"
            ),
        ]
    return json.dumps(
        [
            {
                "articleTitle": {
                    "articleTitle": title,
                    "articleTitle2": "English title",
                },
                "year": year,
                "urlInfo": urls,
            }
        ]
    ).encode()


def _viewer(*, docurl: str | None = None) -> bytes:
    if docurl is None:
        docurl = (
            "https://scienceon.kisti.re.kr/commons/util/orgDocDown.do"
            "?url=/soc_img/society/kias/SOBTCQ/2023/v23n1/"
            "SOBTCQ_2023_v23n1_97.pdf"
            "&amp;ex=pdf"
            "&amp;filename=안티포렌식동향분석및대응방안연구"
            f"&amp;cn={CN}"
        )
    return (
        "<html><script>"
        f"var docurl = '{docurl}';"
        "</script></html>"
    ).encode()


def test_resolves_exact_kci_record_to_safe_scienceon_pdf() -> None:
    http = FakeHttp(
        posts=[Reply(_lookup(), content_type="application/json")],
        gets=[Reply(_viewer())],
    )
    resolver = KciScienceOnResolver(
        KciScienceOnConfig(max_lookup_bytes=111, max_viewer_bytes=222),
        http=http,  # type: ignore[arg-type]
    )

    result = resolver.resolve(_paper())

    assert result.resolved is True
    assert result.reason == "exact_kci_record_and_explicit_scienceon_pdf"
    assert result.candidate is not None
    assert result.candidate.resolver == KCI_SCIENCEON_RESOLVER_NAME
    assert result.candidate.verified_source_id == SOURCE_ID
    assert result.candidate.landing_url == VIEWER_URL
    assert result.candidate.url.startswith(
        "https://scienceon.kisti.re.kr/commons/util/orgDocDown.do?"
    )
    assert "SOBTCQ_2023_v23n1_97.pdf" in result.candidate.url
    assert http.post_calls == [
        (
            KCI_SCIENCEON_LOOKUP_URL,
            {"sereArticleSearchBean.artiId": SOURCE_ID},
            111,
        )
    ]
    assert http.get_calls == [(VIEWER_URL, 222)]


def test_rejects_title_or_year_mismatch_without_opening_viewer() -> None:
    for lookup in (
        _lookup(title="다른 논문"),
        _lookup(year="2022"),
    ):
        http = FakeHttp(
            posts=[Reply(lookup, content_type="application/json")],
            gets=[],
        )
        result = KciScienceOnResolver(http=http).resolve(  # type: ignore[arg-type]
            _paper()
        )
        assert result.status == "no_match"
        assert result.candidate is None
        assert http.get_calls == []


def test_rejects_non_fulltext_or_unsafe_download_urls() -> None:
    cases = [
        (
            _lookup(urls=["10.33778/kcsa.2023.23.1.097"]),
            None,
            "not_found",
        ),
        (
            _lookup(
                urls=[
                    (
                        "https://evil.example/servlet/OpenAPIFullTextView"
                        f"?cn={CN}&dbt=JAKO"
                    )
                ]
            ),
            None,
            "not_found",
        ),
        (
            _lookup(),
            _viewer(
                docurl=(
                    "https://evil.example/file.pdf"
                    f"?url=/soc_img/a.pdf&amp;ex=pdf&amp;filename=x&amp;cn={CN}"
                )
            ),
            "not_found",
        ),
        (
            _lookup(),
            _viewer(
                docurl=(
                    "https://scienceon.kisti.re.kr/commons/util/orgDocDown.do"
                    f"?url=/soc_img/../secret.pdf&amp;ex=pdf&amp;filename=x&amp;cn={CN}"
                )
            ),
            "not_found",
        ),
    ]
    for lookup, viewer, expected in cases:
        http = FakeHttp(
            posts=[Reply(lookup, content_type="application/json")],
            gets=[] if viewer is None else [Reply(viewer)],
        )
        result = KciScienceOnResolver(http=http).resolve(  # type: ignore[arg-type]
            _paper()
        )
        assert result.status == expected
        assert result.candidate is None


def test_rejects_invalid_envelope_and_non_kci_id() -> None:
    http = FakeHttp(
        posts=[Reply(b"{}", content_type="application/json")],
        gets=[],
    )
    result = KciScienceOnResolver(http=http).resolve(_paper())
    assert result.status == "invalid_response"

    no_calls = FakeHttp(posts=[], gets=[])
    result = KciScienceOnResolver(http=no_calls).resolve(  # type: ignore[arg-type]
        _paper(source_id="NOT-KCI")
    )
    assert result.status == "not_applicable"
    assert no_calls.post_calls == []
