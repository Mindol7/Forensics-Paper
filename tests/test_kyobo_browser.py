from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from PaperPipeline.kyobo_browser import (
    DEFAULT_PROFILE_MARKER,
    AuthenticatedKyoboBrowserResolver,
    KyoboBrowserConfig,
    KyoboBrowserOperationError,
    KyoboBrowserSearchResult,
    KyoboDownloadControl,
    KyoboLandingIdentityError,
    KyoboPageSnapshot,
    _KyoboProfileLock,
    _is_allowed_native_download_url,
    _is_allowed_kyobo_page_url,
    _read_download_without_symlink,
    initialize_kyobo_profile_dir,
    is_exact_kyobo_article_url,
    normalize_kyobo_article_url,
    validate_kyobo_landing_document,
    validate_kyobo_profile_dir,
)
from PaperPipeline.records import PaperCandidate


TITLE = "전자정보 압수 방식과 무관정보 처리에 관한 법적 고찰"
DOI = "10.38133/cnulawreview.2025.45.3.151"
ARTICLE_URL = "https://scholar.kyobobook.co.kr/article/detail/4010071506079"
PDF = b"%PDF-1.7\nkyobo-native-download\n%%EOF"


def _paper(**changes: object) -> PaperCandidate:
    paper = PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART004000001",
        title=TITLE,
        doi_original=DOI,
        doi=DOI,
        landing_url=None,
        abstract=None,
        publication_year=2025,
        first_page="151",
        final_page="174",
        journal_id="법학논총",
        institution_id="전남대학교 법학연구소",
        issue_id="45_3",
        authors=("이소연",),
    )
    return replace(paper, **changes)


def _document(
    *,
    title: str = TITLE,
    doi: str | None = DOI,
    year: int = 2025,
    journal: str = "법학논총",
    publisher: str = "전남대학교 법학연구소",
    volume: str = "45",
    issue: str = "3",
    first_page: str = "151",
    last_page: str = "174",
    authors: tuple[str, ...] = ("이소연",),
    canonical: str = ARTICLE_URL,
) -> str:
    metas = [
        f'<meta name="citation_title" content="{title}">',
        f'<meta name="citation_publication_date" content="{year}/08">',
        f'<meta name="citation_journal_title" content="{journal}">',
        f'<meta name="citation_volume" content="{volume}">',
        f'<meta name="citation_issue" content="{issue}">',
        f'<meta name="citation_firstpage" content="{first_page}">',
        f'<meta name="citation_lastpage" content="{last_page}">',
    ]
    if doi is not None:
        metas.append(f'<meta name="citation_doi" content="{doi}">')
    metas.extend(
        f'<meta name="citation_author" content="{author}">'
        for author in authors
    )
    return (
        "<!doctype html><html><head>"
        + "".join(metas)
        + f'<link rel="canonical" href="{canonical}">'
        + "</head><body>"
        + f'<span id="academyTitle">{publisher}</span>'
        + f'<span id="journalTitle">{journal}</span>'
        + f'<span id="pubTitle">{title}</span>'
        + "</body></html>"
    )


def _control(
    *,
    index: int = 0,
    label: str = "원문저장",
    visible: bool = True,
    enabled: bool = True,
    classes: tuple[str, ...] = ("btn_type01", "down"),
    data_lang: str | None = "article_btn_save",
) -> KyoboDownloadControl:
    return KyoboDownloadControl(
        dom_index=index,
        class_tokens=classes,
        label=label,
        data_lang=data_lang,
        visible=visible,
        enabled=enabled,
    )


def _snapshot(
    *,
    document: str | None = None,
    controls: tuple[KyoboDownloadControl, ...] | None = None,
    final_url: str = ARTICLE_URL,
    http_status: int = 200,
) -> KyoboPageSnapshot:
    return KyoboPageSnapshot(
        final_url=final_url,
        document=document if document is not None else _document(),
        controls=controls if controls is not None else (_control(),),
        http_status=http_status,
    )


@dataclass
class FakeSession:
    doi_results: list[str | None | BaseException] = field(default_factory=list)
    search_batches: list[
        tuple[KyoboBrowserSearchResult, ...] | BaseException
    ] = field(default_factory=list)
    snapshots: list[KyoboPageSnapshot | BaseException] = field(default_factory=list)
    downloads: list[bytes | BaseException] = field(default_factory=lambda: [PDF])
    doi_calls: list[tuple[str, int]] = field(default_factory=list)
    search_calls: list[tuple[str, int, int]] = field(default_factory=list)
    open_calls: list[tuple[str, int]] = field(default_factory=list)
    download_calls: list[tuple[KyoboDownloadControl, int, int]] = field(
        default_factory=list
    )
    close_calls: int = 0

    def discover_by_doi(self, doi: str, *, timeout_ms: int) -> str | None:
        self.doi_calls.append((doi, timeout_ms))
        value = self.doi_results.pop(0) if self.doi_results else None
        if isinstance(value, BaseException):
            raise value
        return value

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[KyoboBrowserSearchResult, ...]:
        self.search_calls.append((title, timeout_ms, max_results))
        value = self.search_batches.pop(0) if self.search_batches else ()
        if isinstance(value, BaseException):
            raise value
        return value

    def open_article(self, url: str, *, timeout_ms: int) -> KyoboPageSnapshot:
        self.open_calls.append((url, timeout_ms))
        value = self.snapshots.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def download_control(
        self,
        control: KyoboDownloadControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self.download_calls.append((control, timeout_ms, max_pdf_bytes))
        value = self.downloads.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        self.close_calls += 1


def _resolver(
    session: FakeSession,
    *,
    max_pdf_bytes: int = 1_000,
    max_attempts: int = 25,
    sleeper=lambda _seconds: None,
) -> AuthenticatedKyoboBrowserResolver:
    return AuthenticatedKyoboBrowserResolver(
        KyoboBrowserConfig(
            permission_granted=True,
            profile_dir=Path("/unused/fake-kyobo-profile"),
            navigation_timeout_seconds=2,
            download_timeout_seconds=3,
            min_interval_seconds=1,
            max_attempts_per_run=max_attempts,
            max_pdf_bytes=max_pdf_bytes,
        ),
        session=session,
        clock=lambda: 100.0,
        sleeper=sleeper,
    )


def test_permission_is_explicit_and_performs_no_browser_work() -> None:
    session = FakeSession()
    resolver = AuthenticatedKyoboBrowserResolver(
        KyoboBrowserConfig(permission_granted=False),
        session=session,
    )

    result = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.status == "permission_required"
    assert result.reason == "kyobo_browser_collection_disabled"
    assert not session.open_calls


def test_enabled_resolver_requires_dedicated_profile_path() -> None:
    with pytest.raises(ValueError, match="profile_dir"):
        AuthenticatedKyoboBrowserResolver(
            KyoboBrowserConfig(permission_granted=True),
            session=FakeSession(),
        )


def test_exact_supplied_article_downloads_native_pdf() -> None:
    session = FakeSession(snapshots=[_snapshot()])

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.resolved
    assert result.content == PDF
    assert result.matched_title == TITLE
    assert result.matched_doi == DOI
    assert session.open_calls == [(ARTICLE_URL, 2_000)]
    assert session.download_calls[0][1:] == (3_000, 1_000)
    assert "kyobo_visible_down_control_exact" in result.identity_provenance
    assert ARTICLE_URL not in repr(result)
    assert DOI not in repr(result.attempts)


def test_doi_redirect_discovers_exact_article_before_landing() -> None:
    session = FakeSession(
        doi_results=[ARTICLE_URL],
        snapshots=[_snapshot()],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert session.doi_calls == [(DOI, 2_000)]
    assert not session.search_calls
    assert "kyobo_doi_redirect_article_exact" in result.identity_provenance


def test_non_kyobo_doi_redirect_falls_back_to_exact_rendered_search() -> None:
    session = FakeSession(
        doi_results=[None],
        search_batches=[
            (KyoboBrowserSearchResult(TITLE, ARTICLE_URL),),
        ],
        snapshots=[_snapshot()],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert session.search_calls[0][0] == TITLE
    assert result.attempts[0].stage == "kyobo-browser-doi"
    assert result.attempts[0].status == "not_found"
    assert "kyobo_web_title_search_exact" in result.identity_provenance


def test_exact_title_search_rejects_two_distinct_articles() -> None:
    second = "https://scholar.kyobobook.co.kr/article/detail/4010071506080"
    session = FakeSession(
        doi_results=[None],
        search_batches=[
            (
                KyoboBrowserSearchResult(TITLE, ARTICLE_URL),
                KyoboBrowserSearchResult(TITLE, second),
            )
        ],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "ambiguous_exact_kyobo_search_results"
    assert result.error_code == "ambiguous"
    assert not session.open_calls


def test_search_rejects_unsafe_result_even_when_title_does_not_match() -> None:
    session = FakeSession(
        doi_results=[None],
        search_batches=[
            (KyoboBrowserSearchResult("unrelated", "https://evil.example/a"),)
        ],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.error_code == "unsafe_url"
    assert not session.open_calls


@pytest.mark.parametrize(
    "url",
    [
        "http://scholar.kyobobook.co.kr/article/detail/4010071506079",
        "https://scholar.kyobobook.co.kr/article/detail/4010071506079?token=x",
        "https://scholar.kyobobook.co.kr/file/downs?id=4010071506079",
        "https://evil.example/article/detail/4010071506079",
        "https://user:pass@scholar.kyobobook.co.kr/article/detail/4010071506079",
    ],
)
def test_unsafe_supplied_article_urls_fail_before_browser(url: str) -> None:
    session = FakeSession()
    result = _resolver(session).resolve(_paper(), landing_url=url)
    assert result.status == "invalid_response"
    assert result.error_code == "unsafe_url"
    assert not session.open_calls


def test_authentication_failure_opens_same_run_circuit() -> None:
    error = KyoboBrowserOperationError(
        "authentication_required",
        "kyobo_authentication_requires_manual_refresh",
        "authentication_required",
    )
    session = FakeSession(snapshots=[error])
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    second = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert first.status == "authentication_required"
    assert second.attempts[0].stage == "browser-circuit"
    assert len(session.open_calls) == 1


def test_captcha_failure_opens_same_run_circuit() -> None:
    error = KyoboBrowserOperationError(
        "captcha_required",
        "kyobo_captcha_requires_manual_action",
        "captcha_required",
    )
    session = FakeSession(snapshots=[error])
    resolver = _resolver(session)

    assert resolver.resolve(_paper(), landing_url=ARTICLE_URL).status == "captcha_required"
    assert resolver.resolve(_paper(), landing_url=ARTICLE_URL).attempts[0].stage == "browser-circuit"


@pytest.mark.parametrize(
    ("content", "status", "reason"),
    [
        (b"<html>login</html>", "invalid_response", "kyobo_download_is_not_pdf"),
        (b"%PDF-" + b"x" * 100, "too_large", "kyobo_pdf_exceeds_size_limit"),
    ],
)
def test_download_bytes_are_bounded_and_pdf_magic_checked(
    content: bytes,
    status: str,
    reason: str,
) -> None:
    session = FakeSession(snapshots=[_snapshot()], downloads=[content])

    result = _resolver(session, max_pdf_bytes=20).resolve(
        _paper(),
        landing_url=ARTICLE_URL,
    )

    assert result.status == status
    assert result.reason == reason
    assert result.content is None


def test_hidden_or_ambiguous_download_controls_fail_closed() -> None:
    hidden = FakeSession(snapshots=[_snapshot(controls=(_control(visible=False),))])
    first = _resolver(hidden).resolve(_paper(), landing_url=ARTICLE_URL)
    assert first.status == "access_denied"
    assert first.reason == "kyobo_download_control_unavailable"

    ambiguous = FakeSession(
        snapshots=[_snapshot(controls=(_control(index=0), _control(index=1)))]
    )
    second = _resolver(ambiguous).resolve(_paper(), landing_url=ARTICLE_URL)
    assert second.status == "identity_mismatch"
    assert second.error_code == "ambiguous"


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (_document(title="다른 논문"), "kyobo_landing_title_mismatch"),
        (_document(doi="10.9999/wrong"), "kyobo_landing_doi_mismatch"),
        (_document(year=2024), "kyobo_landing_year_mismatch"),
        (_document(journal="다른 학술지"), "kyobo_landing_journal_mismatch"),
        (_document(canonical="https://scholar.kyobobook.co.kr/article/detail/4010071506080"), "kyobo_landing_canonical_mismatch"),
    ],
)
def test_landing_identity_mismatches_never_click_download(
    document: str,
    reason: str,
) -> None:
    session = FakeSession(snapshots=[_snapshot(document=document)])

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert not session.download_calls


def test_kci_author_affiliations_are_removed_before_exact_name_set_match() -> None:
    paper = _paper(
        authors=(
            "김민동(성균관대학교 대학원 과학수사학과)",
            "이경열(성균관대학교 법학전문대학원)",
        )
    )
    document = _document(authors=("김민동", "이경열"))

    identity = validate_kyobo_landing_document(
        document,
        paper,
        article_url=ARTICLE_URL,
    )

    assert "kyobo_landing_authors_exact" in identity.provenance

    nested = validate_kyobo_landing_document(
        _document(authors=("이승주",)),
        _paper(authors=("이승주(대검찰청(서울남부지검))",)),
        article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_authors_exact" in nested.provenance


def test_generic_kci_law_institute_is_qualified_only_with_exact_doi() -> None:
    paper = _paper(institution_id="법학연구소")
    document = _document(publisher="전남대학교 법학연구소")

    identity = validate_kyobo_landing_document(
        document,
        paper,
        article_url=ARTICLE_URL,
    )

    assert (
        "kyobo_landing_publisher_qualified_generic_exact"
        in identity.provenance
    )

    with pytest.raises(KyoboLandingIdentityError) as doi_less:
        validate_kyobo_landing_document(
            _document(doi=None, publisher="전남대학교 법학연구소"),
            replace(paper, doi=None, doi_original=None),
            article_url=ARTICLE_URL,
        )
    assert getattr(doi_less.value, "reason", None) == "kyobo_landing_publisher_mismatch"

    with pytest.raises(KyoboLandingIdentityError) as missing_year_chain:
        validate_kyobo_landing_document(
            document,
            replace(paper, publication_year=None),
            article_url=ARTICLE_URL,
        )
    assert missing_year_chain.value.reason == "kyobo_landing_publisher_mismatch"


@pytest.mark.parametrize(
    ("candidate_title", "landing_title", "provenance"),
    [
        (
            "클라우드컴퓨팅서비스의 이용자 정보 압수수색에 관한 소고: "
            "협업플랫폼 사업자에 대한 압수수색을 중심으로",
            "클라우드컴퓨팅서비스의 이용자 정보 압수수색에 관한 소고",
            "kyobo_landing_title_doi_bibliographic_equivalent",
        ),
        (
            "정보저장매체 임의제출 후 집행과정의 참여권 보장과 증거능력 "
            "- 대법원 판결을 중심으로 -",
            "정보저장매체 임의제출 후 집행과정의 참여권 보장과 증거능력",
            "kyobo_landing_title_doi_bibliographic_equivalent",
        ),
        (
            "임의제출된 정보저장매체 압수·수색절차에서의 참여권",
            "임의제출된 정보저장매체 압수·수색절차의 참여권",
            "kyobo_landing_title_doi_bibliographic_equivalent",
        ),
        (
            "디지털 증거 관련 절차 규범의 문제점에 대한 소고 ― "
            "임의제출물 및 유류물 내 디지털 증거를 중심으로 ―",
            "디지털 증거 관련 절차 규범의 문제점에 대한 소고 - "
            "임의제출물 및 유류물 내 디지털 증거를 중심으로",
            "kyobo_landing_title_compact_exact",
        ),
    ],
)
def test_exact_doi_and_complete_bibliography_allow_audited_title_variants(
    candidate_title: str,
    landing_title: str,
    provenance: str,
) -> None:
    identity = validate_kyobo_landing_document(
        _document(title=landing_title),
        _paper(title=candidate_title),
        article_url=ARTICLE_URL,
    )

    assert provenance in identity.provenance


def test_unrelated_long_title_still_fails_with_exact_doi_and_bibliography() -> None:
    with pytest.raises(KyoboLandingIdentityError) as mismatch:
        validate_kyobo_landing_document(
            _document(title="전혀 별개의 인공지능 저작권 책임에 관한 장문의 연구"),
            _paper(),
            article_url=ARTICLE_URL,
        )
    assert getattr(mismatch.value, "reason", None) == "kyobo_landing_title_mismatch"


def test_exact_doi_allows_only_one_page_terminal_disagreement() -> None:
    identity = validate_kyobo_landing_document(
        _document(last_page="173"),
        _paper(),
        article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_final_page_off_by_one" in identity.provenance

    with pytest.raises(KyoboLandingIdentityError) as mismatch:
        validate_kyobo_landing_document(
            _document(last_page="172"),
            _paper(),
            article_url=ARTICLE_URL,
        )
    assert getattr(mismatch.value, "reason", None) == "kyobo_landing_page_range_mismatch"

    with pytest.raises(KyoboLandingIdentityError) as incomplete_chain:
        validate_kyobo_landing_document(
            _document(last_page="173", authors=()),
            replace(_paper(), authors=()),
            article_url=ARTICLE_URL,
        )
    assert incomplete_chain.value.reason == "kyobo_landing_page_range_mismatch"


def test_affiliation_normalization_requires_exact_doi_or_doi_less_full_chain() -> None:
    paper = _paper(authors=("이소연(법무법인 율맥)",))
    identity = validate_kyobo_landing_document(
        _document(authors=("이소연",)),
        paper,
        article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_authors_exact" in identity.provenance

    doi_less = validate_kyobo_landing_document(
        _document(doi=None, authors=("이소연",)),
        replace(paper, doi=None, doi_original=None),
        article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_authors_exact" in doi_less.provenance

    with pytest.raises(KyoboLandingIdentityError) as incomplete:
        validate_kyobo_landing_document(
            _document(doi=None, authors=("이소연",), last_page="173"),
            replace(paper, doi=None, doi_original=None),
            article_url=ARTICLE_URL,
        )
    assert incomplete.value.reason == "kyobo_landing_page_range_mismatch"


def test_rendered_title_search_path_is_narrowly_first_party_allowed() -> None:
    assert _is_allowed_kyobo_page_url(
        "https://scholar.kyobobook.co.kr/search/article/productNm?keyword=test"
    )
    assert not _is_allowed_kyobo_page_url(
        "https://scholar.kyobobook.co.kr/search/article/authorNm?keyword=test"
    )
    assert not _is_allowed_kyobo_page_url(
        "https://evil.example/search/article/productNm?keyword=test"
    )


def test_search_uses_main_title_variant_but_landing_keeps_doi_less_fail_closed() -> None:
    full = "대법원의 참여권 법리에 대한 비판적 검토 -대법원 판결을 중심으로-"
    main = "대법원의 참여권 법리에 대한 비판적 검토"
    paper = _paper(title=full, doi=None, doi_original=None)
    session = FakeSession(
        search_batches=[(), (KyoboBrowserSearchResult(main, ARTICLE_URL),)],
        snapshots=[_snapshot(document=_document(title=main, doi=None))],
    )

    result = _resolver(session).resolve(paper)

    assert [call[0] for call in session.search_calls] == [full, main]
    assert result.reason == "kyobo_landing_title_mismatch"
    assert not session.download_calls


def test_long_generated_main_title_is_shortlisted_then_bound_by_provider_doi() -> None:
    full = (
        "대법원의 참여권 법리에 대한 비판적 검토 -대법원 2021. 11. 18. "
        "선고 2016도348 전원합의체 판결을 중심으로-"
    )
    main = "대법원의 참여권 법리에 대한 비판적 검토"
    paper = _paper(title=full, doi=None, doi_original=None)
    session = FakeSession(
        search_batches=[(), (KyoboBrowserSearchResult(main, ARTICLE_URL),)],
        snapshots=[
            _snapshot(
                document=_document(
                    title=main,
                    doi="10.34222/kdps.2022.14.1.1",
                )
            )
        ],
    )

    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert result.matched_doi == "10.34222/kdps.2022.14.1.1"
    assert "kyobo_landing_title_main_exact" in result.identity_provenance
    assert (
        "kyobo_landing_provider_doi_bibliographic_identity"
        in result.identity_provenance
    )
    assert result.landing_page_count == 24


def test_unique_provider_doi_allows_only_bounded_page_or_publisher_variance() -> None:
    doi_less = replace(_paper(), doi=None, doi_original=None)
    page_identity = validate_kyobo_landing_document(
        _document(doi="10.13067/jkiecs.2022.17.2.151", last_page="173"),
        doi_less,
        article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_final_page_off_by_one" in page_identity.provenance
    assert page_identity.page_count == 23

    generic_institute = replace(
        doi_less,
        institution_id="법학연구소",
    )
    publisher_identity = validate_kyobo_landing_document(
        _document(
            doi="10.34267/cbstl.2025.16.2.151",
            publisher="충북대학교 법학연구소",
        ),
        generic_institute,
        article_url=ARTICLE_URL,
    )
    assert (
        "kyobo_landing_publisher_qualified_generic_exact"
        in publisher_identity.provenance
    )

    with pytest.raises(KyoboLandingIdentityError) as unbounded:
        validate_kyobo_landing_document(
            _document(
                doi="10.13067/jkiecs.2022.17.2.151",
                last_page="172",
            ),
            doi_less,
            article_url=ARTICLE_URL,
        )
    assert unbounded.value.reason == "kyobo_landing_page_range_mismatch"

    with pytest.raises(KyoboLandingIdentityError) as combined_variance:
        validate_kyobo_landing_document(
            _document(
                doi="10.34267/cbstl.2025.16.2.151",
                publisher="충북대학교 법학연구소",
                last_page="173",
            ),
            generic_institute,
            article_url=ARTICLE_URL,
        )
    assert (
        combined_variance.value.reason
        == "kyobo_landing_multiple_bibliographic_variances"
    )


def test_doi_less_compact_exact_title_is_bound_by_complete_exact_bibliography() -> None:
    candidate = "생성적 적대 신경망을 활용한부분 위변조 이미지 생성에 관한 연구"
    landing = "생성적 적대 신경망을 활용한 부분 위변조 이미지 생성에 관한 연구"
    paper = _paper(title=candidate, doi=None, doi_original=None)
    session = FakeSession(
        search_batches=[(KyoboBrowserSearchResult(landing, ARTICLE_URL),)],
        snapshots=[_snapshot(document=_document(title=landing, doi=None))],
    )

    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert "kyobo_landing_title_compact_exact" in result.identity_provenance


def test_malformed_publisher_doi_is_never_repaired_and_only_doi_less_chain_ignores_it() -> None:
    malformed = _document(doi="0.34222/kdps.2022.14.4.65")
    identity = validate_kyobo_landing_document(
        malformed,
        _paper(doi=None, doi_original=None),
        article_url=ARTICLE_URL,
    )
    assert identity.doi is None
    assert "kyobo_landing_malformed_doi_ignored" in identity.provenance

    with pytest.raises(KyoboLandingIdentityError) as expected_doi:
        validate_kyobo_landing_document(
            malformed,
            _paper(),
            article_url=ARTICLE_URL,
        )
    assert expected_doi.value.reason == "invalid_kyobo_landing_doi"


def test_multiple_landing_doi_values_remain_hard_ambiguous_failure() -> None:
    document = _document().replace(
        '<meta name="citation_doi" content="10.38133/cnulawreview.2025.45.3.151">',
        '<meta name="citation_doi" content="10.38133/cnulawreview.2025.45.3.151">'
        '<meta name="citation_doi" content="10.9999/different">',
    )
    with pytest.raises(KyoboLandingIdentityError) as ambiguous:
        validate_kyobo_landing_document(
            document,
            _paper(),
            article_url=ARTICLE_URL,
        )
    assert ambiguous.value.reason == "ambiguous_kyobo_landing_doi"


def test_doi_less_article_requires_complete_bibliography() -> None:
    paper = _paper(doi=None, doi_original=None)
    document = _document(doi=None)

    identity = validate_kyobo_landing_document(
        document,
        paper,
        article_url=ARTICLE_URL,
    )

    assert identity.doi is None
    assert "kyobo_landing_authors_exact" in identity.provenance

    incomplete = replace(paper, institution_id=None)
    session = FakeSession(snapshots=[_snapshot(document=document)])
    result = _resolver(session).resolve(incomplete, landing_url=ARTICLE_URL)
    assert result.status == "missing_metadata"
    assert result.reason == "missing_kyobo_doi_less_bibliographic_identity"


def test_rate_limit_applies_between_landing_and_download() -> None:
    sleeps: list[float] = []
    session = FakeSession(snapshots=[_snapshot()])

    result = _resolver(session, sleeper=sleeps.append).resolve(
        _paper(),
        landing_url=ARTICLE_URL,
    )

    assert result.resolved
    assert sleeps == [1.0]


def test_attempt_budget_defers_without_second_navigation() -> None:
    session = FakeSession(snapshots=[_snapshot()])
    resolver = _resolver(session, max_attempts=1)

    assert resolver.resolve(_paper(), landing_url=ARTICLE_URL).resolved
    deferred = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert deferred.reason == "kyobo_browser_attempt_budget_exhausted"
    assert deferred.attempts[0].stage == "browser-budget"
    assert len(session.open_calls) == 1


def test_three_transient_failures_open_circuit() -> None:
    error = KyoboBrowserOperationError(
        "retryable_error",
        "kyobo_landing_navigation_failed",
        "browser_error",
    )
    session = FakeSession(snapshots=[error, error, error])
    resolver = _resolver(session)

    for _ in range(3):
        assert resolver.resolve(_paper(), landing_url=ARTICLE_URL).status == "retryable_error"
    circuit = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert circuit.reason == "kyobo_browser_transient_circuit_open"
    assert circuit.attempts[0].stage == "browser-circuit"
    assert len(session.open_calls) == 3


def test_close_releases_injected_session_without_transport_details() -> None:
    session = FakeSession()
    resolver = _resolver(session)
    resolver.close()
    assert session.close_calls == 1


def test_article_url_and_native_download_origin_validation() -> None:
    assert is_exact_kyobo_article_url(ARTICLE_URL)
    assert normalize_kyobo_article_url("/article/detail/4010071506079") == ARTICLE_URL
    assert _is_allowed_native_download_url(
        "blob:https://scholar.kyobobook.co.kr/87d07c31-9d1a"
    )
    assert _is_allowed_native_download_url(
        "https://scholar.kyobobook.co.kr/file/downs"
    )
    assert not _is_allowed_native_download_url("blob:https://evil.example/id")
    assert not _is_allowed_native_download_url("https://evil.example/file.pdf")


def test_profile_initialization_is_private_marked_and_secret_free(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    outside = tmp_path / "profiles" / "kyobo"
    repository.mkdir()

    profile = initialize_kyobo_profile_dir(outside, repository_root=repository)
    marker = profile / DEFAULT_PROFILE_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert set(payload) == {"schema_version", "purpose"}
    assert "cookie" not in marker.read_text(encoding="utf-8").casefold()
    assert validate_kyobo_profile_dir(profile, repository_root=repository) == profile


def test_profile_rejects_repository_default_profile_and_broad_mode(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    with pytest.raises(ValueError, match="outside"):
        initialize_kyobo_profile_dir(
            repository / "profile",
            repository_root=repository,
        )

    default = tmp_path / ".config" / "google-chrome"
    with pytest.raises(ValueError, match="default personal"):
        initialize_kyobo_profile_dir(default, repository_root=repository)

    outside = initialize_kyobo_profile_dir(
        tmp_path / "outside",
        repository_root=repository,
    )
    os.chmod(outside, 0o755)
    with pytest.raises(ValueError, match="permissions"):
        validate_kyobo_profile_dir(outside, repository_root=repository)


def test_profile_lock_fails_closed_when_already_in_use(tmp_path: Path) -> None:
    profile = initialize_kyobo_profile_dir(
        tmp_path / "profile",
        repository_root=tmp_path / "repo",
    )
    first = _KyoboProfileLock(profile)
    second = _KyoboProfileLock(profile)
    first.acquire()
    try:
        with pytest.raises(KyoboBrowserOperationError) as raised:
            second.acquire()
        assert raised.value.error_code == "profile_in_use"
    finally:
        first.release()


def test_download_reader_rejects_oversize_and_symlink(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(PDF)
    with pytest.raises(KyoboBrowserOperationError) as oversized:
        _read_download_without_symlink(pdf, 5)
    assert oversized.value.error_code == "too_large"

    link = tmp_path / "link.pdf"
    link.symlink_to(pdf)
    with pytest.raises(KyoboBrowserOperationError) as unsafe:
        _read_download_without_symlink(link, 1_000)
    assert unsafe.value.error_code == "invalid_pdf"


@pytest.mark.parametrize("value", [0, 1001])
def test_attempt_budget_config_bounds(value: int) -> None:
    with pytest.raises(ValueError, match="max_attempts_per_run"):
        AuthenticatedKyoboBrowserResolver(
            KyoboBrowserConfig(
                permission_granted=True,
                profile_dir=Path("/unused/profile"),
                max_attempts_per_run=value,
            ),
            session=FakeSession(),
        )


def test_legal_form_prefix_is_the_only_publisher_difference_accepted_with_a_doi() -> None:
    paper = _paper(institution_id="사단법인 한국국가법학회", journal_id="국가법연구")
    identity = validate_kyobo_landing_document(
        _document(publisher="한국국가법학회", journal="국가법연구"), paper, article_url=ARTICLE_URL
    )
    assert "kyobo_landing_publisher_legal_form_exact" in identity.provenance
    # KCI's affiliation-annotated author form is accepted through the same complete chain.
    annotated = validate_kyobo_landing_document(
        _document(publisher="한국국가법학회", journal="국가법연구"),
        replace(paper, authors=("이소연(조선대학교 법사회대학)",)), article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_authors_exact" in annotated.provenance
    # The organisation name itself must still match exactly ...
    with pytest.raises(KyoboLandingIdentityError) as mismatch:
        validate_kyobo_landing_document(
            _document(publisher="한국헌법학회", journal="국가법연구"), paper, article_url=ARTICLE_URL
        )
    assert mismatch.value.reason == "kyobo_landing_publisher_mismatch"
    # ... and a DOI-less landing never gets this allowance.
    with pytest.raises(KyoboLandingIdentityError) as doi_less:
        validate_kyobo_landing_document(
            _document(publisher="한국국가법학회", journal="국가법연구", doi=None),
            replace(paper, doi=None, doi_original=None), article_url=ARTICLE_URL,
        )
    assert doi_less.value.reason == "kyobo_landing_publisher_mismatch"


def _without_volume_metadata(document: str) -> str:
    import re as _re

    return _re.sub(r'<meta name="citation_(?:volume|issue)" content="[^"]*">', "", document)


def test_landing_without_volume_metadata_needs_an_exact_page_range() -> None:
    paper = _paper(issue_id="34")
    identity = validate_kyobo_landing_document(_without_volume_metadata(_document()), paper, article_url=ARTICLE_URL)
    assert "kyobo_landing_volume_issue_absent_page_range_exact" in identity.provenance
    assert "kyobo_landing_volume_issue_exact" not in identity.provenance
    # A DOI-less row with KCI's affiliation-annotated author is bound by the exact
    # title, year, journal, publisher, author and page range.
    doi_less = validate_kyobo_landing_document(
        _without_volume_metadata(_document(doi=None)),
        replace(paper, doi=None, doi_original=None, authors=("이소연(이화여자대학교)",)), article_url=ARTICLE_URL,
    )
    assert "kyobo_landing_volume_issue_absent_page_range_exact" in doi_less.provenance

    # Missing volume metadata plus an inexact page range is not enough.
    with pytest.raises(KyoboLandingIdentityError) as off_by_one:
        validate_kyobo_landing_document(
            _without_volume_metadata(_document(last_page="173")), paper, article_url=ARTICLE_URL
        )
    assert off_by_one.value.reason in {"kyobo_landing_volume_issue_mismatch", "kyobo_landing_page_range_mismatch"}
    # Contradicting volume metadata is still a mismatch.
    with pytest.raises(KyoboLandingIdentityError) as contradiction:
        validate_kyobo_landing_document(_document(volume="33", issue=""), paper, article_url=ARTICLE_URL)
    assert contradiction.value.reason == "kyobo_landing_volume_issue_mismatch"
