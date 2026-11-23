from __future__ import annotations

from collections import deque

import pytest

from PaperPipeline.dcs_resolver import (
    DEFAULT_MAX_HTML_BYTES,
    DcsPublicPdfResolver,
)
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.records import PaperCandidate


DOI = "10.9728/dcs.2025.26.5.1363"
DOI_URL = f"https://doi.org/{DOI}"
TITLE = "Decryption and Artifact Analysis of KakaoTalk Data"
LANDING_URL = (
    "http://journal.dcs.or.kr/_common/do.php?"
    "a=full&b=12&bidx=321&aidx=654"
)
READER_URL = (
    "http://journal.dcs.or.kr/_PR/view/index.php?"
    "bidx=321&aidx=654"
)
PDF_URL = "http://journal.dcs.or.kr/xml/654/654.pdf"


class FakeHttp:
    def __init__(self, responses: dict[str, list[HttpFetch]]) -> None:
        self.responses = {
            url: deque(values)
            for url, values in responses.items()
        }
        self.calls: list[tuple[str, int, int | None]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del headers
        self.calls.append((url, max_bytes, max_html_bytes))
        return self.responses[url].popleft()


def _fetch(
    requested_url: str,
    body: str,
    *,
    final_url: str | None = None,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> HttpFetch:
    return HttpFetch(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body.encode(),
        redirects=(),
    )


def _paper(
    *,
    doi: str | None = DOI,
    title: str | None = TITLE,
    alternate_titles: tuple[str, ...] = (),
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART003000001",
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=2025,
        first_page=None,
        final_page=None,
        alternate_titles=alternate_titles,
    )


def _identity_metadata(*, doi: str = DOI, title: str = TITLE) -> str:
    return (
        f'<meta name="dc.identifier" content="{doi}">'
        f'<meta name="dc.title" content="{title}">'
    )


def _success_responses(
    *,
    landing_url: str = LANDING_URL,
    reader_href: str = READER_URL,
    reader_final_url: str = READER_URL,
    landing_doi: str = DOI,
    landing_title: str = TITLE,
    reader_doi: str = DOI,
    reader_title: str = TITLE,
    pdf_href: str = PDF_URL,
) -> dict[str, list[HttpFetch]]:
    landing_html = (
        _identity_metadata(doi=landing_doi, title=landing_title)
        + f'<a href="{reader_href}">Public reader</a>'
    )
    reader_html = (
        _identity_metadata(doi=reader_doi, title=reader_title)
        + f'<a href="{pdf_href}">PDF</a>'
    )
    return {
        DOI_URL: [
            _fetch(DOI_URL, landing_html, final_url=landing_url),
        ],
        reader_href: [
            _fetch(reader_href, reader_html, final_url=reader_final_url),
        ],
    }


def test_resolves_only_explicit_reader_and_pdf_anchors() -> None:
    fake = FakeHttp(_success_responses())

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.resolved
    assert resolution.status == "resolved"
    assert resolution.reason == "official_reader_exact_identity"
    assert resolution.candidate is not None
    assert resolution.candidate.url == PDF_URL
    assert resolution.candidate.landing_url == LANDING_URL
    assert resolution.candidate.resolver == "dcs-public-reader"
    assert resolution.candidate.verified_doi == DOI
    assert resolution.candidate.is_open_access is True
    assert [attempt.stage for attempt in resolution.attempts] == [
        "doi-landing",
        "dcs-reader",
    ]
    assert fake.calls == [
        (DOI_URL, DEFAULT_MAX_HTML_BYTES, DEFAULT_MAX_HTML_BYTES),
        (READER_URL, DEFAULT_MAX_HTML_BYTES, DEFAULT_MAX_HTML_BYTES),
    ]


def test_accepts_live_page_title_nodes_and_reader_doi_anchor() -> None:
    landing_html = (
        f'<meta name="dc.identifier" content="{DOI}">'
        f'<span class="tl-document">{TITLE}</span>'
        f'<a href="{READER_URL}">Public reader</a>'
    )
    reader_html = (
        "<title>Journal article</title>"
        f'<div><span class="tl-document">{TITLE}</span></div>'
        f'<a href="https://doi.org/{DOI}">DOI</a>'
        f'<a href="{PDF_URL}">PDF</a>'
    )
    fake = FakeHttp(
        {
            DOI_URL: [_fetch(DOI_URL, landing_html, final_url=LANDING_URL)],
            READER_URL: [_fetch(READER_URL, reader_html)],
        }
    )

    resolution = DcsPublicPdfResolver(fake, allow_http=True).resolve(_paper())

    assert resolution.resolved
    assert resolution.candidate is not None
    assert resolution.candidate.url == PDF_URL


def test_reader_doi_anchor_must_include_the_expected_doi() -> None:
    landing_html = (
        f'<meta name="dc.identifier" content="{DOI}">'
        f'<span class="tl-document">{TITLE}</span>'
        f'<a href="{READER_URL}">Public reader</a>'
    )
    reader_html = (
        f'<span class="tl-document">{TITLE}</span>'
        '<a href="https://doi.org/10.9728/dcs.2025.26.5.9999">DOI</a>'
        f'<a href="{PDF_URL}">PDF</a>'
    )
    fake = FakeHttp(
        {
            DOI_URL: [_fetch(DOI_URL, landing_html, final_url=LANDING_URL)],
            READER_URL: [_fetch(READER_URL, reader_html)],
        }
    )

    resolution = DcsPublicPdfResolver(fake, allow_http=True).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == "reader_missing_dc_identifier_doi"


def test_plain_http_workflow_is_disabled_by_default() -> None:
    fake = FakeHttp({})

    resolution = DcsPublicPdfResolver(fake).resolve(_paper())

    assert resolution.status == "skipped"
    assert resolution.reason == "http_not_explicitly_allowed"
    assert not resolution.attempts
    assert not fake.calls


def test_rejects_non_dcs_doi_without_network() -> None:
    fake = FakeHttp({})

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper(doi="10.9728/not-dcs.2025.1"))

    assert resolution.status == "unsupported"
    assert resolution.reason == "unsupported_doi_prefix"
    assert not fake.calls


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"landing_doi": "10.9728/dcs.2025.26.5.9999"}, "landing_doi_mismatch"),
        ({"landing_title": "A different article"}, "landing_title_mismatch"),
        ({"reader_doi": "10.9728/dcs.2025.26.5.9999"}, "reader_doi_mismatch"),
        ({"reader_title": "A different article"}, "reader_title_mismatch"),
    ],
)
def test_rejects_doi_or_title_identity_mismatch(
    changes: dict[str, str],
    reason: str,
) -> None:
    fake = FakeHttp(_success_responses(**changes))

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == reason
    assert resolution.candidate is None


@pytest.mark.parametrize(
    ("reader_href", "reason"),
    [
        (
            "http://journal.dcs.or.kr/_PR/view/index.php?"
            "bidx=321&aidx=999",
            "reader_id_mismatch",
        ),
        (
            "http://evil.example/_PR/view/index.php?"
            "bidx=321&aidx=654",
            "reader_host_mismatch",
        ),
        (
            "https://journal.dcs.or.kr/_PR/view/index.php?"
            "bidx=321&aidx=654",
            "reader_scheme_mismatch",
        ),
    ],
)
def test_rejects_reader_id_host_or_scheme_mismatch(
    reader_href: str,
    reason: str,
) -> None:
    fake = FakeHttp(
        {
            DOI_URL: [
                _fetch(
                    DOI_URL,
                    _identity_metadata()
                    + f'<a href="{reader_href}">Reader</a>',
                    final_url=LANDING_URL,
                )
            ]
        }
    )

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == reason
    assert resolution.candidate is None
    assert [call[0] for call in fake.calls] == [DOI_URL]


def test_rejects_reader_final_url_with_different_article_ids() -> None:
    final_url = (
        "http://journal.dcs.or.kr/_PR/view/index.php?"
        "bidx=321&aidx=999"
    )
    fake = FakeHttp(_success_responses(reader_final_url=final_url))

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == "reader_final_id_mismatch"
    assert resolution.candidate is None


@pytest.mark.parametrize(
    ("pdf_href", "reason"),
    [
        ("http://journal.dcs.or.kr/xml/655/655.pdf", "pdf_id_mismatch"),
        ("http://evil.example/xml/654/654.pdf", "pdf_host_mismatch"),
        ("https://journal.dcs.or.kr/xml/654/654.pdf", "pdf_scheme_mismatch"),
    ],
)
def test_rejects_pdf_id_host_or_scheme_mismatch(
    pdf_href: str,
    reason: str,
) -> None:
    fake = FakeHttp(_success_responses(pdf_href=pdf_href))

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == reason
    assert resolution.candidate is None


def test_accepts_exact_alternate_title_on_both_pages() -> None:
    korean_title = "카카오톡 데이터 복호화 및 아티팩트 분석"
    responses = _success_responses(
        landing_title=TITLE,
        reader_title=TITLE,
    )
    fake = FakeHttp(responses)

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(
        _paper(
            title=korean_title,
            alternate_titles=(TITLE,),
        )
    )

    assert resolution.resolved


def test_never_synthesizes_pdf_from_a_mismatched_explicit_path() -> None:
    mismatched = "http://journal.dcs.or.kr/xml/654/999.pdf"
    fake = FakeHttp(_success_responses(pdf_href=mismatched))

    resolution = DcsPublicPdfResolver(
        fake,
        allow_http=True,
    ).resolve(_paper())

    assert resolution.status == "identity_mismatch"
    assert resolution.reason == "pdf_id_mismatch"
    assert resolution.candidate is None
