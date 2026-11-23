from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
)
from PaperPipeline.kci_openapi_identifier import (
    DEFAULT_KCI_IDENTIFIER_CACHE_DIR,
    KCI_IDENTIFIER_CACHE_SCHEMA_VERSION,
    KCI_OPENAPI_API_CODE,
    KCI_OPENAPI_DETAIL_URL,
    KciOpenApiHttpClient,
    KciOpenApiIdentifierConfig,
    KciOpenApiIdentifierResolver,
)
from PaperPipeline.records import PaperCandidate


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


def _paper(
    *,
    source_id: str = "ART000000001",
    title: str | None = "디지털 포렌식 연구",
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


def _xml(
    *,
    article_id: str = "ART000000001",
    titles: tuple[str, ...] = ("디지털 포렌식 연구",),
    year: str = "2024",
    dois: tuple[str, ...] = ("https://doi.org/10.1234%2FKCI.Test",),
    authors: tuple[str, ...] = (),
    namespace: bool = False,
) -> bytes:
    namespace_value = ' xmlns="urn:kci:test"' if namespace else ""
    title_xml = "".join(
        f'<article-title lang="title-{index}">{title}</article-title>'
        for index, title in enumerate(titles)
    )
    doi_xml = "".join(f"<doi>{doi}</doi>" for doi in dois)
    author_xml = (
        "<author-group>"
        + "".join(
            f"<author><name>{author}</name></author>" for author in authors
        )
        + "</author-group>"
        if authors
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaData{namespace_value}>
  <outputData>
    <result>
      <total>1</total>
      <resultCode>0</resultCode>
      <resultMsg>정상 처리되었습니다.</resultMsg>
    </result>
    <record>
      <journalInfo>
        <journal-name>Example Journal</journal-name>
        <pub-year>{year}</pub-year>
      </journalInfo>
      <articleInfo article-id="{article_id}">
        <title-group>{title_xml}</title-group>
        {author_xml}
        {doi_xml}
      </articleInfo>
    </record>
  </outputData>
</MetaData>
""".encode()


def _fetch(
    body: bytes | None = None,
    *,
    status: int = 200,
    requested_url: str = KCI_OPENAPI_DETAIL_URL,
    final_url: str = KCI_OPENAPI_DETAIL_URL,
    redirects: tuple[str, ...] = (),
    content_type: str = "application/xml; charset=UTF-8",
) -> HttpFetch:
    return HttpFetch(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=_xml() if body is None else body,
        redirects=redirects,
    )


def _resolver(
    http: FakeHttp,
    *,
    api_key: str | None = "private-kci-key",
    cache_dir: Path | None = None,
    **changes: object,
) -> KciOpenApiIdentifierResolver:
    values: dict[str, object] = {
        "api_key": api_key,
        "cache_dir": cache_dir,
    }
    values.update(changes)
    config = KciOpenApiIdentifierConfig(**values)  # type: ignore[arg-type]
    return KciOpenApiIdentifierResolver(config, http=http)


def test_exact_art_id_all_known_titles_and_year_resolve_without_key_leak() -> None:
    secret = "not-for-logs-6d91"
    paper = _paper(
        title="  디지털   포렌식 연구 ",
        alternate_titles=("Digital Forensics Ａ &amp; B",),
    )
    http = FakeHttp(
        [
            _fetch(
                _xml(
                    titles=(
                        "DIGITAL FORENSICS A &amp; B",
                        "디지털 포렌식 연구",
                    ),
                    namespace=True,
                )
            )
        ]
    )
    config = KciOpenApiIdentifierConfig(api_key=secret, cache_dir=None)
    result = KciOpenApiIdentifierResolver(config, http=http).resolve(paper)

    assert result.status == "resolved"
    assert result.reason == "exact_article_id_title_and_year_match"
    assert result.resolver == "kci-openapi-detail"
    assert result.doi == "10.1234/KCI.Test"
    assert result.doi_original == "https://doi.org/10.1234%2FKCI.Test"
    assert result.paper is not paper
    assert paper.doi is None
    assert result.paper.doi_source == "kci-openapi-detail"
    assert result.paper.identifier_resolution == {
        "resolver": "kci-openapi-detail",
        "status": "resolved",
        "reason": "exact_article_id_title_and_year_match",
        "matched_title": "DIGITAL FORENSICS A & B",
        "matched_year": 2024,
        "cache_hit": False,
    }
    assert result.returned_items == 1
    assert result.exact_title_matches == 1
    assert result.title_year_matches == 1

    assert len(http.calls) == 1
    url, params, max_bytes, headers = http.calls[0]
    assert url == KCI_OPENAPI_DETAIL_URL
    assert "?" not in url
    assert dict(params) == {
        "apiCode": KCI_OPENAPI_API_CODE,
        "key": secret,
        "id": paper.source_id,
    }
    assert max_bytes == 2 * 1024 * 1024
    assert headers == {"Accept": "application/xml, text/xml"}
    assert secret not in repr(config)
    assert secret not in repr(params)
    assert secret not in repr(result)
    assert secret not in json.dumps(
        result.paper.identifier_resolution,
        ensure_ascii=False,
    )


def test_one_exact_known_title_allows_missing_or_extra_language_titles() -> None:
    paper = _paper(
        title="한국어 제목",
        alternate_titles=("English title",),
    )
    missing_alternate = _resolver(
        FakeHttp([_fetch(_xml(titles=("한국어 제목",)))]),
    ).resolve(paper)
    extra_title = _resolver(
        FakeHttp(
            [
                _fetch(
                    _xml(
                        titles=("한국어 제목", "English title", "Unexpected"),
                    )
                )
            ]
        ),
    ).resolve(paper)

    assert missing_alternate.status == extra_title.status == "resolved"
    assert missing_alternate.doi == extra_title.doi == "10.1234/KCI.Test"


def test_exact_kci_record_enriches_missing_authors_even_without_doi() -> None:
    paper = _paper()
    result = _resolver(
        FakeHttp(
            [
                _fetch(
                    _xml(
                        dois=(),
                        authors=("정재", "최석환"),
                    )
                )
            ]
        ),
    ).resolve(paper)

    assert result.status == "no_match"
    assert result.reason == "missing_doi"
    assert result.matched_authors == ("정재", "최석환")
    assert paper.authors == ()
    assert result.paper.authors == ("정재", "최석환")


def test_title_requires_exact_overlap_except_terminal_subtitle_marker() -> None:
    trailing_marker = _resolver(
        FakeHttp(
            [
                _fetch(
                    _xml(
                        titles=("완전한 논문 제목 -",),
                    )
                )
            ]
        ),
    ).resolve(_paper(title="완전한 논문 제목"))
    unrelated = _resolver(
        FakeHttp([_fetch(_xml(titles=("전혀 다른 논문 제목",)))]),
    ).resolve(_paper(title="완전한 논문 제목"))

    assert trailing_marker.status == "resolved"
    assert unrelated.status == "identity_mismatch"
    assert unrelated.reason == "title_mismatch"
    assert unrelated.doi is None


@pytest.mark.parametrize(
    ("body", "reason", "exact_title_matches"),
    [
        (
            _xml(article_id="ART000000002"),
            "article_id_mismatch",
            0,
        ),
        (
            _xml(year="2023"),
            "publication_year_mismatch",
            1,
        ),
    ],
)
def test_article_id_and_publication_year_must_match_exactly(
    body: bytes,
    reason: str,
    exact_title_matches: int,
) -> None:
    result = _resolver(FakeHttp([_fetch(body)])).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert result.doi is None
    assert result.exact_title_matches == exact_title_matches


@pytest.mark.parametrize(
    ("dois", "status", "reason"),
    [
        ((), "no_match", "missing_doi"),
        (("not-a-doi",), "identity_mismatch", "invalid_doi"),
        (
            ("10.1234/one", "10.1234/two"),
            "ambiguous",
            "multiple_distinct_dois",
        ),
    ],
)
def test_missing_invalid_or_conflicting_doi_remain_distinct(
    dois: tuple[str, ...],
    status: str,
    reason: str,
) -> None:
    result = _resolver(
        FakeHttp([_fetch(_xml(dois=dois))]),
    ).resolve(_paper())

    assert result.status == status
    assert result.reason == reason
    assert result.doi is None
    assert result.title_year_matches == 1


def test_multiple_article_records_are_ambiguous() -> None:
    xml = _xml().decode()
    record = xml.split("<record>", 1)[1].split("</record>", 1)[0]
    body = (
        xml.replace("<total>1</total>", "<total>2</total>")
        .replace("</record>", f"</record><record>{record}</record>", 1)
        .encode()
    )

    result = _resolver(FakeHttp([_fetch(body)])).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_kci_article_records"
    assert result.error_code == "ambiguous_response"
    assert result.returned_items == 2
    assert result.doi is None


@pytest.mark.parametrize(
    "source_id",
    [
        "ART1",
        "art000000001",
        "ART000000001?key=x",
        "ART000000001/extra",
        "ART１２３４５６７８９",
        "../ART000000001",
    ],
)
def test_invalid_art_id_is_rejected_before_http(
    source_id: str,
) -> None:
    http = FakeHttp([])
    result = _resolver(http).resolve(_paper(source_id=source_id))

    assert result.status == "invalid_input"
    assert result.reason == "invalid_kci_article_id"
    assert result.error_code == "invalid_source_id"
    assert not http.calls


def test_existing_doi_missing_metadata_and_missing_key_make_no_request() -> None:
    http = FakeHttp([])
    resolver = _resolver(http)

    existing = resolver.resolve(_paper(doi="10.1234/existing"))
    missing = resolver.resolve(_paper(title=None, year=None))
    missing_key = _resolver(http, api_key=None).resolve(_paper())

    assert existing.status == "already_present"
    assert existing.resolved is True
    assert missing.status == "missing_metadata"
    assert missing.reason == "missing_title_and_publication_year"
    assert missing_key.status == "configuration_error"
    assert missing_key.reason == "missing_kci_openapi_key"
    assert not http.calls


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<MetaData>", "invalid_kci_openapi_xml"),
        (
            b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
            b"<x>&secret;</x>",
            "unsafe_kci_openapi_xml",
        ),
        (
            b"<?xml version='1.0'?><foo><bar /></foo>",
            "invalid_kci_openapi_envelope",
        ),
    ],
)
def test_malformed_xxe_and_non_detail_xml_are_rejected(
    body: bytes,
    reason: str,
) -> None:
    result = _resolver(FakeHttp([_fetch(body)])).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == reason
    assert result.doi is None


def test_forged_nested_record_and_nonzero_result_code_are_rejected() -> None:
    record = _xml().decode().split("<record>", 1)[1].split(
        "</record>", 1
    )[0]
    forged = (
        f"<evil><record>{record}</record></evil>".encode()
    )
    reported_error = _xml().replace(
        b"<resultCode>0</resultCode>",
        b"<resultCode>99</resultCode>",
    )

    forged_result = _resolver(FakeHttp([_fetch(forged)])).resolve(_paper())
    error_result = _resolver(
        FakeHttp([_fetch(reported_error)])
    ).resolve(_paper())

    assert forged_result.status == "invalid_response"
    assert forged_result.reason == "invalid_kci_openapi_envelope"
    assert error_result.status == "invalid_response"
    assert error_result.reason == "kci_openapi_reported_error"


def test_xml_element_depth_and_response_size_are_bounded() -> None:
    deep_xml = ("<x>" * 8 + "</x>" * 8).encode()
    deep = _resolver(
        FakeHttp([_fetch(deep_xml)]),
        max_xml_depth=4,
    ).resolve(_paper())
    too_large = _resolver(
        FakeHttp([_fetch(_xml() + b"padding")]),
        max_response_bytes=len(_xml()),
    ).resolve(_paper())
    transport_too_large = _resolver(
        FakeHttp([ResponseTooLargeError("large")]),
    ).resolve(_paper())

    assert deep.status == "invalid_response"
    assert deep.reason == "complex_kci_openapi_xml"
    assert too_large.status == "invalid_response"
    assert too_large.reason == "kci_openapi_response_too_large"
    assert transport_too_large.status == "invalid_response"
    assert transport_too_large.error_code == "too_large"


def test_redirects_and_non_xml_content_are_rejected() -> None:
    redirected = _resolver(
        FakeHttp(
            [
                _fetch(
                    final_url="https://evil.example/detail",
                    redirects=("https://evil.example/detail?key=redacted",),
                )
            ]
        ),
    ).resolve(_paper())
    non_xml = _resolver(
        FakeHttp([_fetch(content_type="text/html")]),
    ).resolve(_paper())
    redirect_status = _resolver(
        FakeHttp([_fetch(status=302)]),
    ).resolve(_paper())

    assert redirected.status == "invalid_response"
    assert redirected.reason == "kci_openapi_redirect_rejected"
    assert redirected.error_code == "redirect_not_allowed"
    assert non_xml.status == "invalid_response"
    assert non_xml.reason == "invalid_kci_openapi_content_type"
    assert redirect_status.status == "invalid_response"
    assert redirect_status.reason == "kci_openapi_redirect_rejected"


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_http_status_is_distinct_and_never_resolves(status: int) -> None:
    result = _resolver(
        FakeHttp([_fetch(status=status)]),
    ).resolve(_paper())

    assert result.status == "retryable_error"
    assert result.error_code == "retryable_error"
    assert result.http_status == status
    assert result.doi is None


def test_request_access_http_and_not_found_statuses_remain_distinct() -> None:
    retryable = _resolver(
        FakeHttp([RetryableHttpError("temporary")]),
    ).resolve(_paper())
    request_error = _resolver(
        FakeHttp([HttpClientError("failed")]),
    ).resolve(_paper())
    denied = _resolver(FakeHttp([_fetch(status=403)])).resolve(_paper())
    bad_request = _resolver(FakeHttp([_fetch(status=400)])).resolve(_paper())
    not_found = _resolver(FakeHttp([_fetch(status=404)])).resolve(_paper())

    assert retryable.status == "retryable_error"
    assert request_error.status == "request_error"
    assert denied.status == "configuration_error"
    assert denied.error_code == "access_denied"
    assert bad_request.status == "http_error"
    assert not_found.status == "no_match"
    assert not_found.reason == "kci_article_not_found"


def test_authoritative_empty_response_is_no_match() -> None:
    body = b"""<MetaData>
      <outputData>
        <result><resultMsg>No Data</resultMsg><total>0</total></result>
      </outputData>
    </MetaData>"""
    result = _resolver(FakeHttp([_fetch(body)])).resolve(_paper())

    assert result.status == "no_match"
    assert result.reason == "kci_article_not_found"


def test_positive_cache_is_atomic_rehydrates_and_is_entirely_secret_free(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc)
    secret = "cache-must-not-contain-this-99"
    http = FakeHttp([_fetch()])
    resolver = KciOpenApiIdentifierResolver(
        KciOpenApiIdentifierConfig(
            api_key=secret,
            cache_dir=tmp_path,
            positive_ttl_seconds=3600,
        ),
        http=http,
        clock=lambda: now,
    )
    paper = _paper()

    first = resolver.resolve(paper)
    cached_without_key = KciOpenApiIdentifierResolver(
        KciOpenApiIdentifierConfig(api_key=None, cache_dir=tmp_path),
        http=FakeHttp([]),
        clock=lambda: now,
    ).resolve(paper)

    assert first.status == cached_without_key.status == "resolved"
    assert cached_without_key.cache_hit is True
    assert cached_without_key.doi == "10.1234/KCI.Test"
    assert cached_without_key.paper.identifier_resolution is not None
    assert cached_without_key.paper.identifier_resolution["cache_hit"] is True
    assert len(http.calls) == 1

    path = resolver.cache_path_for(paper)
    assert path == tmp_path / f"{paper.source_id}.json"
    assert path is not None and path.is_file()
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schema_version"] == KCI_IDENTIFIER_CACHE_SCHEMA_VERSION
    assert value["input"] == {
        "source_id": paper.source_id,
        "normalized_titles": ["디지털 포렌식 연구"],
        "publication_year": 2024,
    }
    assert value["result"]["doi"] == "10.1234/KCI.Test"
    assert value["expires_at"] == "2026-07-28T02:02:03Z"
    assert secret not in raw
    assert "apiCode" not in raw
    assert "request_url" not in raw
    assert KCI_OPENAPI_DETAIL_URL not in raw
    assert not list(tmp_path.glob(f".{path.name}.*.part"))


def test_authoritative_no_match_is_cached_but_retryable_is_not(
    tmp_path: Path,
) -> None:
    no_match_http = FakeHttp([_fetch(_xml(dois=()))])
    no_match_resolver = _resolver(no_match_http, cache_dir=tmp_path / "negative")
    paper = _paper()

    first = no_match_resolver.resolve(paper)
    second = no_match_resolver.resolve(paper)

    assert first.status == second.status == "no_match"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(no_match_http.calls) == 1

    retry_http = FakeHttp(
        [RetryableHttpError("temporary"), RetryableHttpError("temporary")]
    )
    retry_resolver = _resolver(retry_http, cache_dir=tmp_path / "retry")
    retry_first = retry_resolver.resolve(paper)
    retry_second = retry_resolver.resolve(paper)

    assert retry_first.status == retry_second.status == "retryable_error"
    assert len(retry_http.calls) == 2
    retry_path = retry_resolver.cache_path_for(paper)
    assert retry_path is not None and not retry_path.exists()


def test_cache_fingerprint_and_expiry_force_a_new_request(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 28, tzinfo=timezone.utc)]
    first = _paper(title="First title")
    changed = _paper(title="Changed title")
    http = FakeHttp(
        [
            _fetch(_xml(titles=("First title",), dois=())),
            _fetch(_xml(titles=("Changed title",), dois=("10.1234/changed",))),
            _fetch(_xml(titles=("Changed title",), dois=("10.1234/refreshed",))),
        ]
    )
    resolver = KciOpenApiIdentifierResolver(
        KciOpenApiIdentifierConfig(
            api_key="key",
            cache_dir=tmp_path,
            positive_ttl_seconds=60,
            negative_ttl_seconds=60,
        ),
        http=http,
        clock=lambda: now[0],
    )

    assert resolver.resolve(first).status == "no_match"
    changed_result = resolver.resolve(changed)
    now[0] += timedelta(seconds=61)
    refreshed = resolver.resolve(changed)

    assert changed_result.status == "resolved"
    assert changed_result.doi == "10.1234/changed"
    assert refreshed.status == "resolved"
    assert refreshed.doi == "10.1234/refreshed"
    assert len(http.calls) == 3


def test_default_cache_location_and_invalid_configuration() -> None:
    assert (
        KciOpenApiIdentifierConfig().cache_dir
        == DEFAULT_KCI_IDENTIFIER_CACHE_DIR
    )
    with pytest.raises(ValueError):
        KciOpenApiIdentifierResolver(
            KciOpenApiIdentifierConfig(api_key="has whitespace")
        )
    with pytest.raises(ValueError):
        KciOpenApiIdentifierResolver(
            KciOpenApiIdentifierConfig(max_response_bytes=0)
        )
    with pytest.raises(ValueError):
        KciOpenApiIdentifierResolver(
            KciOpenApiIdentifierConfig(negative_ttl_seconds=-1)
        )


def test_default_transport_gate_receives_only_the_query_free_endpoint() -> None:
    calls: list[str] = []

    class GateStopped(RuntimeError):
        pass

    def gate(url: str) -> None:
        calls.append(url)
        raise GateStopped

    client = KciOpenApiHttpClient(request_gate=gate)
    with pytest.raises(GateStopped):
        client.get_bytes(
            KCI_OPENAPI_DETAIL_URL,
            params={
                "apiCode": KCI_OPENAPI_API_CODE,
                "key": "private-key",
                "id": "ART000000001",
            },
            max_bytes=1024,
        )

    assert calls == [KCI_OPENAPI_DETAIL_URL]
    assert "?" not in calls[0]
