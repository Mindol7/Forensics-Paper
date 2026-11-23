from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import pytest

import PaperPipeline.jams_browser as jams
from PaperPipeline.jams_browser import (
    DEFAULT_PROFILE_MARKER,
    AuthenticatedKdfsJamsBrowserResolver,
    JamsBrowserConfig,
    JamsBrowserControl,
    JamsBrowserOperationError,
    JamsDetailSnapshot,
    JamsSearchSnapshot,
    _ProfileLock,
    _PlaywrightJamsSession,
    _is_exact_kdfs_download_url,
    _is_exact_search_submit_control,
    _parse_download_handler,
    _read_download_without_symlink,
    _select_download_control,
    initialize_jams_profile_dir,
    parse_kdfs_detail_reference,
    reference_from_public_resolution,
    validate_jams_profile_dir,
)
from PaperPipeline.kdfs_resolver import (
    KDFS_DETAIL_PATH,
    KDFS_DOWNLOAD_PATH,
    KDFS_ORIGIN,
    KDFS_SEARCH_URL,
    KdfsResolution,
)
from PaperPipeline.records import PaperCandidate


TITLE = "얼굴 탐지 기술을 활용한 딥페이크 영상의 저작권 침해 여부 탐지"
ENGLISH_TITLE = (
    "Detection of Copyright Infringement in Deepfake Video using Face Detector"
)
DOI = "10.22798/kdfs.2022.16.1.1"
ARTICLE_ID = "SJ0000000546"
FILE_ID = "KCI_FI003362664"


def _paper(**changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 1,
        "source": "kci",
        "source_id": "ART002830207",
        "title": TITLE,
        "doi_original": DOI,
        "doi": DOI,
        "landing_url": "https://www.kci.go.kr/example",
        "abstract": None,
        "publication_year": 2022,
        "first_page": "1",
        "final_page": "12",
        "alternate_titles": (ENGLISH_TITLE,),
        "journal_id": "디지털포렌식연구",
        "institution_id": "한국디지털포렌식학회",
        "issue_id": "16_1",
        "authors": ("김동엽 (고려대학교)", "이상진 (고려대학교)"),
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _detail_url(
    *, article_id: str = ARTICLE_ID, count: str = "1"
) -> str:
    return (
        f"{KDFS_ORIGIN}{KDFS_DETAIL_PATH}?"
        + urlencode(
            {
                "soceId": "INS000009412",
                "artiId": article_id,
                "sereId": "SER000000001",
                "submCnt": count,
            }
        )
    )


def _download_url(file_id: str = FILE_ID) -> str:
    return (
        f"{KDFS_ORIGIN}{KDFS_DOWNLOAD_PATH}?"
        + urlencode({"orteFileId": file_id})
    )


def _detail_document(
    *,
    title: str = TITLE,
    journal: str = "디지털포렌식연구",
    year: int | None = 2022,
    volume: int = 16,
    issue: int = 1,
    first_page: int = 1,
    final_page: int = 12,
    author: str = "김동엽 (고려대학교)",
    doi: str | None = None,
) -> str:
    doi_html = f"<p>DOI: {doi}</p>" if doi else ""
    year_text = f"{year}.03 " if year is not None else ""
    return f"""
      <!doctype html><html><body>
        <div class="infos">
          <p class="txt1">{title}</p>
          <p><em>학술지명:</em> {journal}</p>
          <p><em>발행정보:</em> {year_text}제{volume}권 제{issue}호
             pp.{first_page}~{final_page}</p>
          <p><em>저자:</em> {author}</p>
          {doi_html}
        </div>
      </body></html>
    """


def _search_document(
    *,
    title: str = TITLE,
    article_id: str = ARTICLE_ID,
    second_article_id: str | None = None,
    author: str = "김동엽 (고려대학교)",
) -> str:
    def row(current_id: str) -> str:
        return f"""
          <div class="col-12">
            <a href="javascript:fncGoList(
              'INS000009412','{current_id}','SER000000001','1');">
              1. <em>{title}</em>
            </a>
            <div>
              저자 {author}
              학술지 디지털포렌식연구 제16권 제1호 pp.1~12
              발행년월 2022.03
            </div>
          </div>
        """

    rows = row(article_id)
    if second_article_id is not None:
        rows += row(second_article_id)
    return f"<!doctype html><html><body>{rows}</body></html>"


def _control(
    *,
    dom_index: int = 3,
    href: str | None = "#",
    onclick: str | None = f"fncFileIframe('{FILE_ID}')",
    label: str = "원문 PDF 다운로드",
    visible: bool = True,
    enabled: bool = True,
) -> JamsBrowserControl:
    return JamsBrowserControl(
        dom_index=dom_index,
        tag="a",
        href=href,
        onclick=onclick,
        label=label,
        visible=visible,
        enabled=enabled,
    )


def _public_resolution(
    paper: PaperCandidate | None = None,
    **changes: object,
) -> KdfsResolution:
    value = paper or _paper()
    fields: dict[str, object] = {
        "status": "permission_required",
        "reason": "kdfs_anonymous_download_not_advertised",
        "error_code": "permission_required",
        "paper": value,
        "matched_title": TITLE,
        "jams_article_id": ARTICLE_ID,
        "detail_url": _detail_url(),
        "expected_doi": DOI,
        "expected_year": 2022,
        "expected_volume": 16,
        "expected_issue": 1,
        "expected_first_page": 1,
        "expected_final_page": 12,
    }
    fields.update(changes)
    return KdfsResolution(**fields)  # type: ignore[arg-type]


@dataclass
class FakeSession:
    details: list[JamsDetailSnapshot] = field(default_factory=list)
    searches: list[JamsSearchSnapshot] = field(default_factory=list)
    content: bytes | BaseException = b"%PDF-1.7\nfixture\n%%EOF"
    search_calls: list[tuple[str, int, int]] = field(default_factory=list)
    detail_calls: list[tuple[str, int, int]] = field(default_factory=list)
    download_calls: list[tuple[JamsBrowserControl, int, int]] = field(
        default_factory=list
    )
    close_calls: int = 0

    def search_title(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsSearchSnapshot:
        self.search_calls.append((title, timeout_ms, max_html_bytes))
        value = self.searches[len(self.search_calls) - 1]
        if len(value.document.encode()) > max_html_bytes:
            raise JamsBrowserOperationError(
                "invalid_response", "jams_page_too_large", "too_large"
            )
        return value

    def open_detail(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsDetailSnapshot:
        self.detail_calls.append((url, timeout_ms, max_html_bytes))
        value = self.details[len(self.detail_calls) - 1]
        if len(value.document.encode()) > max_html_bytes:
            raise JamsBrowserOperationError(
                "invalid_response", "jams_page_too_large", "too_large"
            )
        return value

    def download_control(
        self,
        control: JamsBrowserControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self.download_calls.append((control, timeout_ms, max_pdf_bytes))
        if isinstance(self.content, BaseException):
            raise self.content
        if len(self.content) > max_pdf_bytes:
            raise JamsBrowserOperationError(
                "too_large", "jams_pdf_exceeds_size_limit", "too_large"
            )
        return self.content

    def close(self) -> None:
        self.close_calls += 1


def _snapshot(
    *,
    final_url: str | None = None,
    document: str | None = None,
    controls: tuple[JamsBrowserControl, ...] | None = None,
    challenge: str | None = None,
    status: int | None = 200,
) -> JamsDetailSnapshot:
    return JamsDetailSnapshot(
        final_url=final_url or _detail_url(),
        document=document or _detail_document(),
        controls=controls if controls is not None else (_control(),),
        challenge=challenge,
        http_status=status,
    )


def _resolver(session: FakeSession, **changes: object) -> AuthenticatedKdfsJamsBrowserResolver:
    return AuthenticatedKdfsJamsBrowserResolver(
        JamsBrowserConfig(
            permission_granted=True,
            profile_dir=Path("/unused/fake-kdfs-jams-profile"),
            min_interval_seconds=1,
            **changes,
        ),
        session=session,
        clock=lambda: 100.0,
        sleeper=lambda _seconds: None,
    )


def _accept_fixture_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jams, "_validate_pdf_identity", lambda *args, **kwargs: None)


def test_permission_guard_makes_no_browser_call() -> None:
    session = FakeSession(details=[_snapshot()])
    resolver = AuthenticatedKdfsJamsBrowserResolver(
        JamsBrowserConfig(permission_granted=False), session=session
    )

    result = resolver.resolve(_paper(), public_resolution=_public_resolution())

    assert result.status == "permission_required"
    assert result.reason == "jams_browser_collection_disabled"
    assert session.detail_calls == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"institution_id": "다른 학회"}, "unsupported_kdfs_institution"),
        ({"journal_id": "다른 학술지"}, "unsupported_kdfs_journal"),
        ({"source_id": "BAD"}, "invalid_kci_source_id"),
    ],
)
def test_exact_kdfs_applicability_precedes_browser(
    changes: dict[str, object], reason: str
) -> None:
    session = FakeSession()
    result = _resolver(session).resolve(_paper(**changes))

    assert result.reason == reason
    assert session.search_calls == []
    assert session.detail_calls == []


def test_exact_public_reference_detail_and_pdf_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(details=[_snapshot()])

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.resolved
    assert result.reason == "authenticated_kdfs_jams_exact_pdf_identity"
    assert result.content == b"%PDF-1.7\nfixture\n%%EOF"
    assert result.content_type == "application/pdf"
    assert result.matched_title == TITLE
    assert result.matched_doi == DOI
    assert len(session.detail_calls) == 1
    assert len(session.download_calls) == 1
    assert all("http" not in repr(attempt).casefold() for attempt in result.attempts)
    assert "kdfs.jams.or.kr" not in repr(result)
    assert FILE_ID not in repr(result)


def test_rendered_exact_search_is_used_when_public_result_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(),
                http_status=200,
            )
        ],
        details=[_snapshot()],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert session.search_calls[0][0] == TITLE
    assert session.detail_calls[0][0] == _detail_url()
    assert "jams_detail_reference_rendered-search-exact" in result.identity_provenance


def test_rendered_search_may_defer_only_an_explicitly_blank_author_to_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(author=""),
                http_status=200,
            )
        ],
        details=[_snapshot()],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert (
        "jams_detail_reference_rendered-search-exact-detail-author"
        in result.identity_provenance
    )
    assert "jams_detail_author_exact" in result.identity_provenance


def test_explicitly_blank_search_and_detail_author_is_deferred_to_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(author=""),
                http_status=200,
            )
        ],
        details=[_snapshot(document=_detail_document(author=""))],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert "jams_detail_author_deferred_to_pdf" in result.identity_provenance
    assert "kdfs_pdf_identity_exact" in result.identity_provenance


def test_deferred_blank_author_is_still_rejected_by_strict_pdf_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_text = (
        f"디지털포렌식연구 2022 제16권 제1호 pp.1~12 {TITLE} "
        f"다른저자 DOI: {DOI}"
    )
    monkeypatch.setattr(
        jams.public_kdfs,
        "_read_pdf_document",
        lambda *args, **kwargs: jams.public_kdfs._PdfDocument(
            page_count=12,
            first_pages=(pdf_text, "둘째 페이지", "셋째 페이지"),
            last_page="마지막 페이지 12",
            metadata_text=TITLE,
        ),
    )
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(author=""),
                http_status=200,
            )
        ],
        details=[_snapshot(document=_detail_document(author=""))],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "kdfs_pdf_author_mismatch"


def test_rendered_search_never_defers_a_conflicting_nonempty_author() -> None:
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(author="다른 저자"),
                http_status=200,
            )
        ]
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "kdfs_search_bibliographic_identity_mismatch"
    assert session.detail_calls == []


def test_exact_adjacent_duplicate_title_repair_is_scoped_and_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    repaired = "디지털포렌식 숙련도시험운영기관 발전방안"
    duplicated = repaired + repaired
    paper = _paper(title=duplicated, alternate_titles=())
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(title="검색 결과 없음"),
                http_status=200,
            ),
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(title=repaired),
                http_status=200,
            ),
        ],
        details=[_snapshot(document=_detail_document(title=repaired))],
    )

    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert [call[0] for call in session.search_calls] == [duplicated, repaired]
    assert result.matched_title == repaired


def test_rendered_search_ambiguity_fails_closed_and_opens_circuit() -> None:
    session = FakeSession(
        searches=[
            JamsSearchSnapshot(
                final_url=KDFS_SEARCH_URL,
                document=_search_document(second_article_id="SJ0000000999"),
                http_status=200,
            )
        ]
    )
    resolver = _resolver(session)

    first = resolver.resolve(_paper())
    second = resolver.resolve(_paper())

    assert first.status == "ambiguous"
    assert first.reason == "multiple_exact_kdfs_article_matches"
    assert second.attempts[-1].stage == "jams-browser-circuit"
    assert len(session.search_calls) == 1


def test_multiple_visible_download_controls_fail_closed() -> None:
    session = FakeSession(
        details=[_snapshot(controls=(_control(dom_index=1), _control(dom_index=2)))]
    )

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "ambiguous"
    assert result.reason == "multiple_jams_download_controls"
    assert session.download_calls == []


@pytest.mark.parametrize("challenge", ["authentication_required", "captcha_required"])
def test_authentication_and_captcha_stop_the_run_circuit(challenge: str) -> None:
    session = FakeSession(details=[_snapshot(challenge=challenge)])
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), public_resolution=_public_resolution())
    second = resolver.resolve(_paper(), public_resolution=_public_resolution())

    assert first.error_code == challenge
    assert second.attempts[-1].stage == "jams-browser-circuit"
    assert len(session.detail_calls) == 1


def test_unsafe_public_detail_reference_is_not_navigated() -> None:
    session = FakeSession()
    resolution = _public_resolution(
        detail_url=(
            "https://evil.example/po/volisse/sjPubsArtiPopView.kci?"
            "soceId=INS000009412&artiId=SJ0000000546&"
            "sereId=SER000000001&submCnt=1"
        )
    )

    result = _resolver(session).resolve(_paper(), public_resolution=resolution)

    assert result.status == "invalid_response"
    assert result.reason == "invalid_public_kdfs_detail_reference"
    assert session.detail_calls == []


def test_detail_redirect_to_other_article_is_rejected() -> None:
    session = FakeSession(
        details=[_snapshot(final_url=_detail_url(article_id="SJ0000000999"))]
    )

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "identity_mismatch"
    assert result.reason == "jams_detail_redirect_identity_mismatch"
    assert session.download_calls == []


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (_detail_document(title="전혀 다른 제목"), "jams_detail_title_mismatch"),
        (_detail_document(journal="다른 학술지"), "jams_detail_journal_mismatch"),
        (_detail_document(year=2021), "jams_detail_year_mismatch"),
        (_detail_document(final_page=13), "jams_detail_page_range_mismatch"),
        (_detail_document(author="다른 저자"), "jams_detail_author_mismatch"),
    ],
)
def test_strict_detail_bibliography_is_identity_bound(
    document: str, reason: str
) -> None:
    session = FakeSession(details=[_snapshot(document=document)])

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert session.download_calls == []


def test_detail_may_omit_year_when_exact_search_already_bound_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(
        details=[_snapshot(document=_detail_document(year=None))]
    )

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.resolved
    assert "jams_detail_year_from_exact_search" in result.identity_provenance


def test_exact_title_and_doi_can_bind_detail_without_bibliographic_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    document = _detail_document(
        journal="다른 표시",
        year=1999,
        volume=1,
        issue=9,
        first_page=99,
        final_page=100,
        author="다른 저자",
        doi=DOI,
    )
    session = FakeSession(details=[_snapshot(document=document)])

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.resolved
    assert "jams_detail_doi_exact" in result.identity_provenance


def test_oversize_download_is_bounded() -> None:
    session = FakeSession(
        details=[_snapshot()],
        content=b"%PDF-" + b"x" * 30,
    )

    result = _resolver(session, max_pdf_bytes=10).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "too_large"
    assert result.reason == "jams_pdf_exceeds_size_limit"


def test_non_pdf_download_is_rejected_before_parser() -> None:
    session = FakeSession(details=[_snapshot()], content=b"<!doctype html>login")

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "invalid_pdf"
    assert result.reason == "jams_download_is_not_pdf"


def test_missing_authenticated_control_is_an_auth_gate() -> None:
    session = FakeSession(details=[_snapshot(controls=())])

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "authentication_required"
    assert result.reason == "jams_authenticated_download_control_unavailable"


def test_unsafe_handler_stops_on_dom_change() -> None:
    session = FakeSession(
        details=[
            _snapshot(
                controls=(
                    _control(onclick="fncFileIframe(userInput + token)"),
                )
            )
        ]
    )

    result = _resolver(session).resolve(
        _paper(), public_resolution=_public_resolution()
    )

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_jams_download_control"
    assert result.error_code == "dom_changed"


def test_file_check_handler_requires_only_safe_literal_arguments() -> None:
    safe = "fncFileIdChk('KCI_FI003362664','SJ0000000546',1);"
    unsafe = "fncFileIdChk(getToken(), 'SJ0000000546')"

    assert _parse_download_handler(safe) is not None
    assert _parse_download_handler(unsafe) is None
    selected = _select_download_control(
        (
            _control(onclick=safe, label="", href="#"),
        )
    )
    assert selected.onclick == safe


@pytest.mark.parametrize(
    ("tag", "label", "onclick", "expected"),
    [
        ("button", "검색", "goSearch();", True),
        ("button", "검색 검색", "javascript:goSearch()", True),
        ("button", "검색", "javascript:goSearch(); return false;", True),
        ("button", "검색", None, False),
        ("button", "검색", "subGoSearch();", False),
        ("button", "검색", "goSearch(); stealCookie()", False),
        ("a", "다음", "goSearch();", False),
    ],
)
def test_only_exact_form_scoped_jams_search_action_is_clickable(
    tag: str,
    label: str,
    onclick: str | None,
    expected: bool,
) -> None:
    assert (
        _is_exact_search_submit_control(
            tag=tag,
            label=label,
            onclick=onclick,
        )
        is expected
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://kdfs.jams.or.kr/co/download/popup/poDownload.kci?orteFileId=KCI_FI003362664",
        "https://evil.example/co/download/popup/poDownload.kci?orteFileId=KCI_FI003362664",
        "https://kdfs.jams.or.kr/co/download/popup/poDownload.kci?orteFileId=../../secret",
        "https://kdfs.jams.or.kr/co/download/popup/poDownload.kci?orteFileId=KCI_FI003362664&x=1",
    ],
)
def test_only_exact_official_download_urls_are_allowed(url: str) -> None:
    assert not _is_exact_kdfs_download_url(url)


def test_exact_public_download_url_is_allowed() -> None:
    assert _is_exact_kdfs_download_url(_download_url())
    assert _is_exact_kdfs_download_url(
        "/co/download/popup/poDownload.kci?orteFileId=KCI_FI003362664"
    )


def test_handler_may_be_advertised_by_exact_href() -> None:
    selected = _select_download_control(
        (
            _control(
                href=f"javascript:fncFileIframe('{FILE_ID}')",
                onclick=None,
                label="",
            ),
        )
    )

    assert selected.href is not None


def test_public_resolution_reference_checks_all_identity_fields() -> None:
    with pytest.raises(ValueError, match="identity-bound"):
        reference_from_public_resolution(
            _paper(), _public_resolution(expected_final_page=13)
        )


def test_detail_reference_rejects_secrets_duplicates_and_fragments() -> None:
    valid = _detail_url()
    assert parse_kdfs_detail_reference(valid, provenance="test").article_id == ARTICLE_ID
    for unsafe in (
        valid + "&token=secret",
        valid + "#fragment",
        valid.replace("https://", "https://user:pass@"),
        valid.replace("submCnt=1", "submCnt=1&submCnt=1"),
    ):
        with pytest.raises(ValueError, match="invalid KDFS"):
            parse_kdfs_detail_reference(unsafe, provenance="test")


def test_attempt_budget_defers_without_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accept_fixture_pdf(monkeypatch)
    session = FakeSession(details=[_snapshot()])
    resolver = _resolver(session, max_attempts_per_run=1)

    first = resolver.resolve(_paper(), public_resolution=_public_resolution())
    deferred = resolver.resolve(_paper(), public_resolution=_public_resolution())

    assert first.resolved
    assert deferred.reason == "jams_browser_attempt_budget_exhausted"
    assert deferred.attempts[-1].stage == "jams-browser-budget"
    assert len(session.detail_calls) == 1


def test_close_releases_injected_session() -> None:
    session = FakeSession()
    resolver = _resolver(session)

    resolver.close()

    assert session.close_calls == 1


def test_profile_is_private_marked_and_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    profile = tmp_path / "private" / "jams-profile"

    initialized = initialize_jams_profile_dir(
        profile, repository_root=repository
    )

    marker = initialized / DEFAULT_PROFILE_MARKER
    assert validate_jams_profile_dir(
        initialized, repository_root=repository
    ) == initialized
    assert stat.S_IMODE(initialized.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert "password" not in marker.read_text(encoding="utf-8").casefold()
    assert "cookie" not in marker.read_text(encoding="utf-8").casefold()


def test_profile_inside_repository_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="outside the repository"):
        initialize_jams_profile_dir(
            repository / "browser-profile",
            repository_root=repository,
        )


def test_default_personal_browser_profile_child_is_rejected(tmp_path: Path) -> None:
    profile = tmp_path / ".config" / "google-chrome" / "Default"

    with pytest.raises(ValueError, match="default personal"):
        initialize_jams_profile_dir(profile, repository_root=None)


def test_nonempty_unmarked_profile_is_not_reused(tmp_path: Path) -> None:
    profile = tmp_path / "existing"
    profile.mkdir(mode=0o700)
    (profile / "Cookies").write_text("browser state", encoding="utf-8")

    with pytest.raises(ValueError, match="unmarked"):
        initialize_jams_profile_dir(profile, repository_root=None)


def test_profile_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        initialize_jams_profile_dir(profile, repository_root=None)


def test_profile_permissions_must_remain_private(tmp_path: Path) -> None:
    profile = initialize_jams_profile_dir(
        tmp_path / "profile", repository_root=None
    )
    os.chmod(profile, 0o755)

    with pytest.raises(ValueError, match="too broad"):
        validate_jams_profile_dir(profile, repository_root=None)


def test_profile_lock_rejects_a_second_process_handle(tmp_path: Path) -> None:
    profile = initialize_jams_profile_dir(
        tmp_path / "profile", repository_root=None
    )
    first = _ProfileLock(profile)
    second = _ProfileLock(profile)
    first.acquire()
    try:
        with pytest.raises(JamsBrowserOperationError) as exc_info:
            second.acquire()
        assert exc_info.value.error_code == "profile_in_use"
    finally:
        first.release()


def test_config_repr_never_exposes_profile_path() -> None:
    secret_profile = Path("/private/auth/profile-do-not-log")
    config = JamsBrowserConfig(
        permission_granted=True,
        profile_dir=secret_profile,
    )

    assert str(secret_profile) not in repr(config)
    assert not hasattr(config, "password")
    assert not hasattr(config, "cookie")
    assert not hasattr(config, "token")


def test_interactive_login_launches_headed_and_prompts_once_in_same_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    profile = initialize_jams_profile_dir(
        tmp_path / "profile",
        repository_root=repository,
    )
    prompts: list[str] = []
    launches: list[dict[str, object]] = []

    class FakeResponse:
        status = 200

    class FakePage:
        url = jams.KDFS_RENDERED_SEARCH_URL

        def __init__(self) -> None:
            self.goto_calls = 0
            self.events: list[tuple[str, object]] = []

        def goto(self, _url: str, **_kwargs: object) -> FakeResponse:
            self.goto_calls += 1
            return FakeResponse()

        def on(self, name: str, callback: object) -> None:
            self.events.append((name, callback))

    page = FakePage()

    class FakeContext:
        pages = [page]

        def __init__(self) -> None:
            self.closed = False
            self.routes: list[tuple[str, object]] = []

        def route(self, pattern: str, callback: object) -> None:
            self.routes.append((pattern, callback))

        def close(self) -> None:
            self.closed = True

    context = FakeContext()

    class FakeChromium:
        def launch_persistent_context(self, **kwargs: object) -> FakeContext:
            launches.append(kwargs)
            return context

    class FakePlaywright:
        chromium = FakeChromium()

        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    playwright = FakePlaywright()

    class FakeStarter:
        @staticmethod
        def start() -> FakePlaywright:
            return playwright

    monkeypatch.setattr(jams, "_load_sync_playwright", lambda: FakeStarter)
    session = _PlaywrightJamsSession(
        JamsBrowserConfig(
            permission_granted=True,
            profile_dir=profile,
            repository_root=repository,
            headless=True,
            interactive_login=True,
            interactive_input_fn=lambda prompt: prompts.append(prompt) or "",
        ),
        profile,
    )

    session._ensure_started()
    first_context = session._context
    first_page = session._page
    session._ensure_started()

    assert launches[0]["headless"] is False
    assert launches[0]["args"] == ["--restore-last-session"]
    assert session._context is first_context is context
    assert session._page is first_page is page
    assert page.goto_calls == 1
    assert len(prompts) == 1
    assert len(context.routes) == 1
    assert context.routes[0][0] == "**/*"
    assert "브라우저에서만" in prompts[0]
    assert "Enter만" in prompts[0]
    session.close()
    assert context.closed
    assert playwright.stopped


def test_interactive_login_rejects_nonempty_terminal_input_without_leaking_it() -> None:
    supplied = "must-never-be-logged-or-persisted"

    class FakeResponse:
        status = 200

    class FakePage:
        url = jams.KDFS_RENDERED_SEARCH_URL

        @staticmethod
        def goto(_url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    config = JamsBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/profile"),
        interactive_login=True,
        interactive_input_fn=lambda _prompt: supplied,
    )
    session = _PlaywrightJamsSession(config, Path("/unused/profile"))
    session._page = FakePage()

    with pytest.raises(JamsBrowserOperationError) as exc_info:
        session._perform_interactive_login_once()

    assert exc_info.value.error_code == "interactive_login_acknowledgement_rejected"
    assert supplied not in str(exc_info.value)
    assert supplied not in repr(config)
    assert not session._interactive_login_completed


@pytest.mark.parametrize("value", [0, 1001, True])
def test_attempt_budget_bounds(value: int) -> None:
    with pytest.raises(ValueError, match="max_attempts_per_run"):
        AuthenticatedKdfsJamsBrowserResolver(
            JamsBrowserConfig(
                permission_granted=True,
                profile_dir=Path("/unused/profile"),
                max_attempts_per_run=value,
            ),
            session=FakeSession(),
        )


def test_enabled_config_requires_dedicated_profile() -> None:
    with pytest.raises(ValueError, match="profile_dir"):
        AuthenticatedKdfsJamsBrowserResolver(
            JamsBrowserConfig(permission_granted=True),
            session=FakeSession(),
        )


def test_resolution_is_frozen_and_cannot_receive_transport_urls() -> None:
    result = AuthenticatedKdfsJamsBrowserResolver(
        JamsBrowserConfig(permission_granted=False), session=FakeSession()
    ).resolve(_paper())

    assert "detail_url" not in result.__dataclass_fields__
    assert "download_url" not in result.__dataclass_fields__
    with pytest.raises((AttributeError, TypeError)):
        result.status = "resolved"  # type: ignore[misc]


def test_download_reader_rejects_oversize_and_non_regular_files(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "large.pdf"
    oversized.write_bytes(b"%PDF-" + b"x" * 20)
    with pytest.raises(JamsBrowserOperationError) as exc_info:
        _read_download_without_symlink(oversized, 10)
    assert exc_info.value.error_code == "too_large"

    if hasattr(os, "symlink"):
        link = tmp_path / "link.pdf"
        link.symlink_to(oversized)
        with pytest.raises(JamsBrowserOperationError) as exc_info:
            _read_download_without_symlink(link, 100)
        assert exc_info.value.error_code == "invalid_pdf"
