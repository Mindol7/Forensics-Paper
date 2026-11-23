from __future__ import annotations

from collections import deque
from typing import Mapping

import pytest

from PaperPipeline.dbpia_openapi import (
    DBPIA_OPENAPI_URL,
    DbpiaOpenApiConfig,
    DbpiaOpenApiHttpClient,
    DbpiaOpenApiResolver,
)
from PaperPipeline.http_client import HttpClientError, HttpFetch, ResponseTooLargeError
from PaperPipeline.records import PaperCandidate


TITLE = "안드로이드 모바일 기기 암호화폐 개인 지갑 앱아티팩트 분석연구"
API_TITLE = "안드로이드 모바일 기기 암호화폐 개인 지갑 앱 아티팩트 분석연구"
NODE = "NODE11750870"
LANDING = f"https://www.dbpia.co.kr/Journal/ArticleDetail/{NODE}"
LEGACY_ID = "3209215"
LEGACY_LANDING = (
    f"https://www.dbpia.co.kr/Journal/ArticleDetail/{LEGACY_ID}"
)
OTHER_JOURNAL = "정보보호학회논문지"
OTHER_PUBLISHER = "한국정보보호학회"


class FakeHttp:
    def __init__(self, responses: list[HttpFetch | BaseException]) -> None:
        self.responses = deque(responses)
        self.calls: list[
            tuple[str, Mapping[str, str], int, Mapping[str, str] | None]
        ] = []

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append((url, params, max_bytes, headers))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _paper(source_id: str = "ART003071074", **changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 150,
        "source": "kci",
        "source_id": source_id,
        "title": TITLE,
        "doi_original": "http://dx.doi.org/10.22798/KDFS.2024.18.1.11",
        "doi": "10.22798/KDFS.2024.18.1.11",
        "landing_url": None,
        "abstract": None,
        "publication_year": 2024,
        "first_page": "11",
        "final_page": "23",
        "alternate_titles": (),
        "journal_id": "디지털포렌식연구",
        "institution_id": "한국디지털포렌식학회",
        "issue_id": "18_1",
        "authors": ("심대성", "이상진", "윤지원"),
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _item(
    *,
    node: str = NODE,
    title: str = API_TITLE,
    year: str | None = "202403",
    pages: str = "11-23",
    journal: str = "디지털포렌식연구",
    publisher: str = "한국디지털포렌식학회",
    volume: str = "18",
    issue: str = "1",
    authors: tuple[str, ...] = ("심대성", "이상진", "윤지원"),
    link_node: str | None = None,
    link_query: str | None = None,
    link_url: str | None = None,
    ctype: str = "article",
) -> str:
    linked = link_node or node
    author_xml = "".join(
        f"<author><name>{author}</name></author>" for author in authors
    )
    yymm_xml = f"<yymm>{year}</yymm>" if year is not None else ""
    query_xml = f"?{link_query}" if link_query else ""
    raw_link = link_url or (
        f"https://www.dbpia.co.kr/Journal/ArticleDetail/{linked}{query_xml}"
    )
    escaped_link = raw_link.replace("&", "&amp;")
    return f"""
      <item id="{node}">
        <ctype>{ctype}</ctype>
        <title>{title}</title>
        <authors>{author_xml}</authors>
        <publisher><name>{publisher}</name></publisher>
        <publication><name>{journal}</name></publication>
        <issue>
          {yymm_xml}<name>제{volume}권 제{issue}호</name>
          <num>{volume}({issue})</num>
        </issue>
        <pages>{pages} (13 pages)</pages>
        <link_url>{escaped_link}</link_url>
      </item>
    """


def _xml(*items: str, total: int | None = None) -> bytes:
    count = len(items) if total is None else total
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:dbpia:test">
  <paramdata><totalcount>{count}</totalcount></paramdata>
  <result><items>{''.join(items)}</items></result>
</root>
""".encode()


def _fetch(
    body: bytes | None = None,
    *,
    status: int = 200,
    requested_url: str = DBPIA_OPENAPI_URL,
    final_url: str = DBPIA_OPENAPI_URL,
    redirects: tuple[str, ...] = (),
    content_type: str = "application/xml; charset=UTF-8",
) -> HttpFetch:
    return HttpFetch(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=_xml(_item()) if body is None else body,
        redirects=redirects,
    )


def _resolver(
    http: FakeHttp,
    *,
    api_key: str | None = "private-dbpia-key",
    **changes: object,
) -> DbpiaOpenApiResolver:
    values: dict[str, object] = {"api_key": api_key}
    values.update(changes)
    return DbpiaOpenApiResolver(
        DbpiaOpenApiConfig(**values),  # type: ignore[arg-type]
        http=http,
    )


def test_exact_catalog_match_is_canonical_and_key_is_redacted() -> None:
    secret = "dbpia-secret-6d91"
    http = FakeHttp([_fetch()])
    resolver = _resolver(http, api_key=secret)

    batch = resolver.resolve([_paper()])
    result = batch.resolutions["ART003071074"]

    assert result.resolved
    assert result.reason == "exact_dbpia_bibliographic_match"
    assert result.node_id == NODE
    assert result.landing_url == LANDING
    assert result.matched_title == API_TITLE
    assert batch.pages_fetched == 1
    assert batch.returned_items == 1
    assert len(http.calls) == 1
    url, params, max_bytes, headers = http.calls[0]
    assert url == DBPIA_OPENAPI_URL
    assert "?" not in url
    assert dict(params) == {
        "key": secret,
        "target": "se_adv",
        "searchbook": "디지털포렌식연구",
        "searchpublisher": "한국디지털포렌식학회",
        "itype": "1",
        "pyear": "3",
        "pyear_start": "2024",
        "pyear_end": "2024",
        "pagecount": "20",
        "pagenumber": "1",
    }
    assert max_bytes == 4 * 1024 * 1024
    assert headers == {"Accept": "application/xml, text/xml"}
    assert secret not in repr(params)
    assert secret not in repr(resolver.config)
    assert secret not in repr(batch)


def test_explicit_non_kdfs_scope_keeps_exact_catalog_matching() -> None:
    paper = _paper(
        journal_id=OTHER_JOURNAL,
        institution_id=OTHER_PUBLISHER,
    )
    http = FakeHttp(
        [
            _fetch(
                _xml(
                    _item(
                        journal=OTHER_JOURNAL,
                        publisher=OTHER_PUBLISHER,
                    )
                )
            )
        ]
    )

    batch = _resolver(
        http,
        journal=OTHER_JOURNAL,
        publisher=OTHER_PUBLISHER,
    ).resolve([paper])
    resolution = batch.resolutions[paper.source_id]

    assert resolution.resolved
    assert resolution.node_id == NODE
    assert http.calls[0][1]["searchbook"] == OTHER_JOURNAL
    assert http.calls[0][1]["searchpublisher"] == OTHER_PUBLISHER
    assert NODE not in repr(resolution)
    assert NODE not in repr(batch)


def test_non_kdfs_opaque_journal_identifier_is_not_inferred() -> None:
    http = FakeHttp([])
    paper = _paper(
        journal_id="SER000009999",
        institution_id=OTHER_PUBLISHER,
    )

    resolution = _resolver(
        http,
        journal=OTHER_JOURNAL,
        publisher=OTHER_PUBLISHER,
    ).resolve([paper]).resolutions[paper.source_id]

    assert resolution.status == "missing_metadata"
    assert resolution.reason == "missing_or_mismatched_dbpia_journal"
    assert not http.calls


@pytest.mark.parametrize(
    "item_xml",
    [
        _item(journal="다른 학술지", publisher=OTHER_PUBLISHER),
        _item(journal=OTHER_JOURNAL, publisher="다른 발행기관"),
    ],
)
def test_explicit_non_kdfs_scope_rejects_cross_scope_items(
    item_xml: str,
) -> None:
    paper = _paper(
        journal_id=OTHER_JOURNAL,
        institution_id=OTHER_PUBLISHER,
    )

    resolution = _resolver(
        FakeHttp([_fetch(_xml(item_xml))]),
        journal=OTHER_JOURNAL,
        publisher=OTHER_PUBLISHER,
    ).resolve([paper]).resolutions[paper.source_id]

    assert resolution.status == "not_found"
    assert resolution.reason == "no_exact_dbpia_article"


def test_selected_papers_share_one_paginated_year_catalog() -> None:
    second_node = "NODE11750871"
    second_title = "두 번째 디지털포렌식 논문"
    first_page = _fetch(_xml(_item(), total=2))
    second_page = _fetch(
        _xml(
            _item(
                node=second_node,
                title=second_title,
                pages="24-35",
                link_node=second_node,
            ),
            total=2,
        )
    )
    http = FakeHttp([first_page, second_page])
    papers = [
        _paper(),
        _paper(
            "ART003071075",
            database_id=151,
            title=second_title,
            first_page="24",
            final_page="35",
        ),
    ]

    batch = _resolver(http, page_size=1).resolve(papers)

    assert batch.pages_fetched == 2
    assert batch.returned_items == 2
    assert batch.resolutions[papers[0].source_id].node_id == NODE
    assert batch.resolutions[papers[1].source_id].node_id == second_node
    assert [call[1]["pagenumber"] for call in http.calls] == ["1", "2"]


def test_catalog_is_batched_once_per_year_and_year_filter_binds_missing_yymm() -> None:
    title_2023 = "2023년 디지털포렌식 논문"
    node_2023 = "NODE11750871"
    http = FakeHttp(
        [
            _fetch(
                _xml(
                    _item(
                        node=node_2023,
                        link_node=node_2023,
                        title=title_2023,
                        year=None,
                    )
                )
            ),
            _fetch(_xml(_item(year=None))),
        ]
    )
    papers = [
        _paper(
            "ART003071073",
            database_id=149,
            title=title_2023,
            publication_year=2023,
        ),
        _paper(),
    ]

    batch = _resolver(http).resolve(papers)

    assert batch.resolutions[papers[0].source_id].matched_year == 2023
    assert batch.resolutions[papers[1].source_id].matched_year == 2024
    assert [call[1]["pyear_start"] for call in http.calls] == ["2023", "2024"]
    assert [call[1]["pyear_end"] for call in http.calls] == ["2023", "2024"]


def test_exact_legacy_kci_journal_identifier_and_author_affiliations_match() -> None:
    paper = _paper(
        journal_id="SER000003109",
        authors=("심대성(고려대학교)", "이상진(고려대학교)", "윤지원(고려대학교)"),
    )

    resolution = _resolver(FakeHttp([_fetch()])).resolve([paper]).resolutions[
        paper.source_id
    ]

    assert resolution.resolved
    assert resolution.node_id == NODE


@pytest.mark.parametrize(
    "item_xml",
    [
        _item(title="전혀 다른 논문 제목"),
        _item(year="202303"),
        _item(pages="12-23"),
        _item(journal="다른 학술지"),
        _item(publisher="다른 학회"),
        _item(volume="17"),
        _item(issue="2"),
        _item(authors=("심대성", "다른 저자")),
    ],
)
def test_every_bibliographic_mismatch_is_rejected(item_xml: str) -> None:
    result = _resolver(FakeHttp([_fetch(_xml(item_xml))])).resolve([_paper()])
    resolution = result.resolutions["ART003071074"]

    assert resolution.status == "not_found"
    assert resolution.exact_matches == 0


def test_multiple_exact_nodes_are_ambiguous() -> None:
    other_node = "NODE11750871"
    batch = _resolver(
        FakeHttp(
            [
                _fetch(
                    _xml(
                        _item(),
                        _item(node=other_node, link_node=other_node),
                    )
                )
            ]
        )
    ).resolve([_paper()])

    resolution = batch.resolutions["ART003071074"]
    assert resolution.status == "ambiguous"
    assert resolution.exact_matches == 2
    assert resolution.landing_url is None


def test_node_attribute_and_link_must_identify_the_same_article() -> None:
    batch = _resolver(
        FakeHttp([_fetch(_xml(_item(link_node="NODE11750871")))])
    ).resolve([_paper()])

    resolution = batch.resolutions["ART003071074"]
    assert resolution.status == "invalid_response"
    assert resolution.reason == "invalid_dbpia_article_identity"
    assert resolution.error_code == "invalid_xml"


def test_documented_link_is_sufficient_when_undocumented_item_id_is_absent() -> None:
    item_without_id = _item().replace(f' id="{NODE}"', "", 1)

    resolution = _resolver(
        FakeHttp([_fetch(_xml(item_without_id))])
    ).resolve([_paper()]).resolutions["ART003071074"]

    assert resolution.resolved
    assert resolution.node_id == NODE


def test_official_numeric_link_is_canonicalized_without_guessing_a_node() -> None:
    search_context = (
        "Collection=0&Multimedia=0&Page=0&PageSize=20&"
        "SearchAll=%EB%94%94%EC%A7%80%ED%84%B8&Seq=37&Sort=1&"
        "SortType=desc&isIdentifyAuthor=0&q=forensics&"
        "searchWord=transient-search-term"
    )
    resolution = _resolver(
        FakeHttp(
            [
                _fetch(
                    _xml(
                        _item(
                            node=LEGACY_ID,
                            link_node=LEGACY_ID,
                            link_query=search_context,
                        )
                    )
                )
            ]
        )
    ).resolve([_paper()]).resolutions["ART003071074"]

    assert resolution.resolved
    assert resolution.node_id is None
    assert resolution.landing_url == f"{LEGACY_LANDING}?{search_context}"
    assert "transient-search-term" not in repr(resolution)
    assert "SearchAll" not in repr(resolution)


@pytest.mark.parametrize(
    "link_query",
    [
        "redirect=https%3A%2F%2Fevil.example",
        "nodeId=NODE99999999",
        "download=true",
        "Seq=1&Seq=2",
        "Seq=",
        "seq=1",
    ],
)
def test_unknown_duplicate_or_blank_link_context_is_rejected(
    link_query: str,
) -> None:
    resolution = _resolver(
        FakeHttp([_fetch(_xml(_item(link_query=link_query)))])
    ).resolve([_paper()]).resolutions["ART003071074"]

    assert resolution.status == "invalid_response"
    assert resolution.reason == "invalid_dbpia_article_identity"


@pytest.mark.parametrize(
    "link_url",
    [
        f"http://www.dbpia.co.kr/Journal/ArticleDetail/{LEGACY_ID}",
        f"https://evil.example/Journal/ArticleDetail/{LEGACY_ID}",
        f"https://user@www.dbpia.co.kr/Journal/ArticleDetail/{LEGACY_ID}",
        f"https://www.dbpia.co.kr:444/Journal/ArticleDetail/{LEGACY_ID}",
        f"https://www.dbpia.co.kr/Journal/ArticleDetail/{LEGACY_ID}#fragment",
        "https://www.dbpia.co.kr/Journal/ArticleDetail/not-an-id",
    ],
)
def test_unsafe_or_unrecognized_catalog_link_is_rejected(link_url: str) -> None:
    resolution = _resolver(
        FakeHttp([_fetch(_xml(_item(link_url=link_url)))])
    ).resolve([_paper()]).resolutions["ART003071074"]

    assert resolution.status == "invalid_response"
    assert resolution.reason == "invalid_dbpia_article_identity"


def test_unrecognized_item_attribute_is_ignored_in_favor_of_official_link() -> None:
    opaque_id = "undocumented-item-token"
    item = _item().replace(f'id="{NODE}"', f'id="{opaque_id}"', 1)

    resolution = _resolver(FakeHttp([_fetch(_xml(item))])).resolve(
        [_paper()]
    ).resolutions["ART003071074"]

    assert resolution.resolved
    assert resolution.node_id == NODE


def test_item_attribute_in_an_undocumented_id_namespace_is_not_cross_compared() -> None:
    item = _item().replace(f'id="{NODE}"', f'id="{LEGACY_ID}"', 1)

    resolution = _resolver(FakeHttp([_fetch(_xml(item))])).resolve(
        [_paper()]
    ).resolutions["ART003071074"]

    assert resolution.resolved
    assert resolution.node_id == NODE


def test_non_article_catalog_items_are_counted_for_pagination_but_not_matched() -> None:
    book_node = "NODE11750871"
    body = _xml(
        _item(
            node=book_node,
            link_node=book_node,
            title="간행물 레코드",
            ctype="book",
        ),
        _item(),
        total=2,
    )

    batch = _resolver(FakeHttp([_fetch(body)])).resolve([_paper()])

    assert batch.resolutions["ART003071074"].resolved
    assert batch.returned_items == 1


def test_overlapping_pages_fail_instead_of_hiding_a_missing_unique_node() -> None:
    http = FakeHttp(
        [
            _fetch(_xml(_item(), total=2)),
            _fetch(_xml(_item(), total=2)),
        ]
    )

    resolution = _resolver(http, page_size=1).resolve([_paper()]).resolutions[
        "ART003071074"
    ]

    assert resolution.status == "invalid_response"
    assert resolution.reason == "duplicate_dbpia_article_across_pages"
    assert len(http.calls) == 2


def test_repeated_nonarticle_cannot_hide_an_unseen_later_article() -> None:
    book = _item(
        node="NODE11750871",
        link_node="NODE11750871",
        title="반복된 간행물 레코드",
        ctype="book",
    )
    http = FakeHttp(
        [
            _fetch(_xml(book, _item(), total=3)),
            _fetch(_xml(book, total=3)),
        ]
    )

    resolution = _resolver(http, page_size=2).resolve([_paper()]).resolutions[
        "ART003071074"
    ]

    assert resolution.status == "invalid_response"
    assert resolution.reason == "duplicate_dbpia_nonarticle_catalog_item"
    assert len(http.calls) == 2


def test_idless_nonarticle_overlap_ignores_varying_search_context() -> None:
    first_book = _item(
        node="NODE11750871",
        link_node="NODE11750871",
        link_query="Page=1&Seq=1",
        title="ID 없는 반복 간행물",
        ctype="book",
    ).replace(' id="NODE11750871"', "", 1)
    second_book = _item(
        node="NODE11750871",
        link_node="NODE11750871",
        link_query="Page=2&Seq=1",
        title="ID 없는 반복 간행물",
        ctype="book",
    ).replace(' id="NODE11750871"', "", 1)
    http = FakeHttp(
        [
            _fetch(_xml(first_book, _item(), total=3)),
            _fetch(_xml(second_book, total=3)),
        ]
    )

    resolution = _resolver(http, page_size=2).resolve([_paper()]).resolutions[
        "ART003071074"
    ]

    assert resolution.status == "invalid_response"
    assert resolution.reason == "duplicate_dbpia_nonarticle_catalog_item"


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("E0016", "not_found", "no_exact_dbpia_article"),
        ("E0017", "retryable_error", "dbpia_openapi_rate_limited"),
        ("E0001", "configuration_error", "dbpia_openapi_error_response"),
    ],
)
def test_documented_api_error_codes_are_classified(
    code: str,
    status: str,
    reason: str,
) -> None:
    body = (
        f'<?xml version="1.0"?><root><error><code>{code}</code>'
        "<message>documented API error</message></error></root>"
    ).encode()

    resolution = _resolver(FakeHttp([_fetch(body)])).resolve([_paper()]).resolutions[
        "ART003071074"
    ]

    assert resolution.status == status
    assert resolution.reason == reason


@pytest.mark.parametrize("code", ["E0001", "E0017"])
def test_global_api_failure_stops_before_requesting_later_years(code: str) -> None:
    body = (
        f'<?xml version="1.0"?><root><error><code>{code}</code>'
        "<message>global error</message></error></root>"
    ).encode()
    http = FakeHttp([_fetch(body)])
    papers = [
        _paper("ART003071073", database_id=149, publication_year=2023),
        _paper(),
    ]

    batch = _resolver(http).resolve(papers)

    assert len(http.calls) == 1
    assert batch.stop_provider
    assert batch.resolutions[papers[0].source_id].status in {
        "configuration_error",
        "retryable_error",
    }
    assert batch.resolutions[papers[1].source_id].status == (
        batch.resolutions[papers[0].source_id].status
    )


def test_year_local_malformed_item_does_not_suppress_later_year() -> None:
    bad_2023 = _item(
        title="2023년 malformed 논문",
        year="202303",
        link_url="https://evil.example/Journal/ArticleDetail/3209215",
    )
    http = FakeHttp(
        [
            _fetch(_xml(bad_2023)),
            _fetch(_xml(_item())),
        ]
    )
    paper_2023 = _paper(
        "ART003071073",
        database_id=149,
        title="2023년 malformed 논문",
        publication_year=2023,
    )

    batch = _resolver(http).resolve([paper_2023, _paper()])

    assert batch.resolutions[paper_2023.source_id].status == "invalid_response"
    assert batch.resolutions["ART003071074"].resolved
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    ("fetch", "status", "reason"),
    [
        (
            _fetch(
                final_url="https://evil.example/search.xml",
                redirects=("https://evil.example/search.xml",),
            ),
            "invalid_response",
            "dbpia_openapi_redirect_rejected",
        ),
        (_fetch(status=429), "retryable_error", "dbpia_openapi_rate_limited"),
        (_fetch(status=403), "configuration_error", "dbpia_openapi_access_denied"),
        (
            _fetch(content_type="text/html"),
            "invalid_response",
            "invalid_dbpia_openapi_content_type",
        ),
    ],
)
def test_transport_and_response_failures_are_typed(
    fetch: HttpFetch,
    status: str,
    reason: str,
) -> None:
    batch = _resolver(FakeHttp([fetch])).resolve([_paper()])
    resolution = batch.resolutions["ART003071074"]

    assert resolution.status == status
    assert resolution.reason == reason
    assert batch.stop_provider


def test_unsafe_or_complex_xml_is_rejected() -> None:
    unsafe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY y "z">]><root/>'
    deep = b"<root><totalcount>0</totalcount><a><b><c/></b></a></root>"
    unsafe_result = _resolver(FakeHttp([_fetch(unsafe)])).resolve([_paper()])
    deep_result = _resolver(
        FakeHttp([_fetch(deep)]), max_xml_depth=3
    ).resolve([_paper()])

    assert unsafe_result.resolutions["ART003071074"].reason == (
        "unsafe_dbpia_openapi_xml"
    )
    assert deep_result.resolutions["ART003071074"].reason == (
        "complex_dbpia_openapi_xml"
    )


def test_missing_key_or_out_of_scope_year_makes_no_request() -> None:
    no_key_http = FakeHttp([])
    no_key = _resolver(no_key_http, api_key=None).resolve([_paper()])
    old_http = FakeHttp([])
    old = _resolver(old_http).resolve([_paper(publication_year=2019)])

    assert no_key.resolutions["ART003071074"].status == "configuration_error"
    assert no_key.stop_provider
    assert old.resolutions["ART003071074"].status == "unsupported"
    assert not no_key_http.calls and not old_http.calls


def test_response_too_large_exception_is_secret_free() -> None:
    secret = "never-print-this-key"
    result = _resolver(
        FakeHttp([ResponseTooLargeError("response too large")]),
        api_key=secret,
    ).resolve([_paper()])

    resolution = result.resolutions["ART003071074"]
    assert resolution.status == "invalid_response"
    assert resolution.reason == "dbpia_openapi_response_too_large"
    assert secret not in repr(result)


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/xml", "Content-Length": "4"}

    def __init__(self) -> None:
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield b"<x/>"

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self) -> None:
        self.trust_env = True
        self.calls: list[tuple[str, Mapping[str, str], bool]] = []
        self.response = _FakeResponse()

    def get(self, url: str, *, params, allow_redirects: bool, **_kwargs):
        self.calls.append((url, params, allow_redirects))
        return self.response


def test_http_client_keeps_secret_out_of_fetch_urls_and_rejects_other_hosts() -> None:
    session = _FakeSession()
    client = DbpiaOpenApiHttpClient(session=session, retries=0)  # type: ignore[arg-type]
    params = {
        "key": "secret-value",
        "target": "se_adv",
        "searchbook": "디지털포렌식연구",
        "searchpublisher": "한국디지털포렌식학회",
        "itype": "1",
        "pyear": "3",
        "pyear_start": "2024",
        "pyear_end": "2024",
        "pagecount": "100",
        "pagenumber": "1",
    }

    fetch = client.get_bytes(DBPIA_OPENAPI_URL, params=params, max_bytes=100)

    assert fetch.requested_url == fetch.final_url == DBPIA_OPENAPI_URL
    assert "secret-value" not in repr(fetch)
    assert session.trust_env is False
    assert session.calls[0][2] is False
    with pytest.raises(HttpClientError):
        client.get_bytes(
            "https://evil.example/v2/search/search.xml",
            params=params,
            max_bytes=100,
        )


def test_http_client_enforces_its_explicit_search_scope_before_network() -> None:
    session = _FakeSession()
    client = DbpiaOpenApiHttpClient(
        session=session,  # type: ignore[arg-type]
        retries=0,
        search_journal=OTHER_JOURNAL,
        search_publisher=OTHER_PUBLISHER,
    )
    params = {
        "key": "secret-value",
        "target": "se_adv",
        "searchbook": OTHER_JOURNAL,
        "searchpublisher": OTHER_PUBLISHER,
        "itype": "1",
        "pyear": "3",
        "pyear_start": "2024",
        "pyear_end": "2024",
        "pagecount": "20",
        "pagenumber": "1",
    }

    client.get_bytes(DBPIA_OPENAPI_URL, params=params, max_bytes=100)
    wrong_journal = {**params, "searchbook": "다른 학술지"}
    wrong_publisher = {**params, "searchpublisher": "다른 발행기관"}
    with pytest.raises(HttpClientError, match="search scope"):
        client.get_bytes(
            DBPIA_OPENAPI_URL,
            params=wrong_journal,
            max_bytes=100,
        )
    with pytest.raises(HttpClientError, match="search scope"):
        client.get_bytes(
            DBPIA_OPENAPI_URL,
            params=wrong_publisher,
            max_bytes=100,
        )

    assert len(session.calls) == 1


def test_concrete_http_client_scope_must_match_resolver_config() -> None:
    client = DbpiaOpenApiHttpClient(
        session=_FakeSession(),  # type: ignore[arg-type]
        retries=0,
        search_journal=OTHER_JOURNAL,
        search_publisher=OTHER_PUBLISHER,
    )

    with pytest.raises(ValueError, match="scope must match"):
        DbpiaOpenApiResolver(
            DbpiaOpenApiConfig(api_key="secret-value"),
            http=client,
        )


@pytest.mark.parametrize(
    "invalid",
    ["", " 앞 공백", "뒤 공백 ", "제어\n문자", "x" * 256],
)
def test_config_and_http_client_reject_invalid_explicit_scope(invalid: str) -> None:
    with pytest.raises(ValueError, match="search journal"):
        DbpiaOpenApiConfig(
            api_key="secret-value",
            journal=invalid,
            publisher=OTHER_PUBLISHER,
        )
    with pytest.raises(ValueError, match="search publisher"):
        DbpiaOpenApiHttpClient(
            session=_FakeSession(),  # type: ignore[arg-type]
            retries=0,
            search_journal=OTHER_JOURNAL,
            search_publisher=invalid,
        )
