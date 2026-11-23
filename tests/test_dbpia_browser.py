from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import PaperPipeline.dbpia_browser as dbpia_browser
from PaperPipeline.dbpia_browser import (
    DEFAULT_PROFILE_MARKER,
    AuthenticatedDbpiaBrowserResolver,
    DbpiaBrowserButton,
    DbpiaBrowserConfig,
    DbpiaBrowserResolution,
    DbpiaBrowserSearchResult,
    DbpiaPageSnapshot,
    DbpiaBrowserOperationError,
    _PlaywrightDbpiaSession,
    _is_allowed_download_url,
    _page_challenge,
    _read_download_without_symlink,
    initialize_dbpia_profile_dir,
    is_exact_dbpia_article_url,
    normalize_dbpia_article_url,
    parse_dbpia_article_reference,
    validate_dbpia_profile_dir,
)
from PaperPipeline.records import PaperCandidate


TITLE = "디지털 포렌식 증거수집 기법 연구"
DOI = "10.1234/forensics.2025.1"
NODE = "NODE12345678"
ARTICLE_URL = (
    "https://www.dbpia.co.kr/journal/articleDetail?"
    f"nodeId={NODE}"
)


def _paper(*, title: str = TITLE, landing_url: str | None = None) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART003999999",
        title=title,
        doi_original=DOI,
        doi=DOI,
        landing_url=landing_url,
        abstract=None,
        publication_year=2025,
        first_page="1",
        final_page="10",
    )


def _landing_document(
    *,
    title: str = TITLE,
    body: str = "",
    include_doi: bool = True,
    include_year: bool = True,
) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="citation_title" content="{title}">'
        + (f'<meta name="citation_doi" content="{DOI}">' if include_doi else "")
        + (
            '<meta name="citation_publication_date" content="2025/07">'
            if include_year
            else ""
        )
        + f"</head><body>{body}</body></html>"
    )


def _button(
    node_id: str = NODE,
    *,
    dom_index: int = 3,
    visible: bool = True,
    enabled: bool = True,
) -> DbpiaBrowserButton:
    return DbpiaBrowserButton(
        dom_index=dom_index,
        node_ids=(node_id,),
        visible=visible,
        enabled=enabled,
    )


@dataclass
class FakeSession:
    snapshots: list[DbpiaPageSnapshot]
    content: bytes = b"%PDF-1.7\nsecret-pdf-payload\n%%EOF"
    search_batches: list[tuple[DbpiaBrowserSearchResult, ...]] = field(
        default_factory=list
    )
    open_calls: list[tuple[str, int]] = field(default_factory=list)
    search_calls: list[tuple[str, int, int]] = field(default_factory=list)
    download_calls: list[tuple[DbpiaBrowserButton, str, int, int]] = field(
        default_factory=list
    )
    close_calls: int = 0

    def open_article(self, url: str, *, timeout_ms: int) -> DbpiaPageSnapshot:
        self.open_calls.append((url, timeout_ms))
        return self.snapshots[len(self.open_calls) - 1]

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[DbpiaBrowserSearchResult, ...]:
        self.search_calls.append((title, timeout_ms, max_results))
        return self.search_batches[len(self.search_calls) - 1]

    def download_button(
        self,
        button: DbpiaBrowserButton,
        *,
        expected_node_id: str,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self.download_calls.append(
            (button, expected_node_id, timeout_ms, max_pdf_bytes)
        )
        return self.content

    def close(self) -> None:
        self.close_calls += 1


def _resolver(session: FakeSession) -> AuthenticatedDbpiaBrowserResolver:
    return AuthenticatedDbpiaBrowserResolver(
        DbpiaBrowserConfig(
            permission_granted=True,
            # FakeSession keeps this path entirely unused; the real adapter
            # validates and locks the initialized dedicated profile.
            profile_dir=Path("/unused/fake-dbpia-profile"),
            navigation_timeout_seconds=2,
            download_timeout_seconds=3,
            min_interval_seconds=1,
            max_pdf_bytes=123_456,
        ),
        session=session,
        clock=lambda: 100.0,
        sleeper=lambda _seconds: None,
    )


def test_per_run_attempt_budget_defers_without_navigation() -> None:
    snapshot = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
        http_status=200,
    )
    session = FakeSession(snapshots=[snapshot])
    resolver = AuthenticatedDbpiaBrowserResolver(
        DbpiaBrowserConfig(
            permission_granted=True,
            profile_dir=Path("/unused/fake-dbpia-profile"),
            min_interval_seconds=1,
            max_attempts_per_run=1,
        ),
        session=session,
        clock=lambda: 100.0,
        sleeper=lambda _seconds: None,
    )

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    deferred = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert first.resolved
    assert deferred.status == "retryable_error"
    assert deferred.reason == "dbpia_browser_attempt_budget_exhausted"
    assert deferred.error_code == "browser_attempt_limit_reached"
    assert deferred.attempts[0].stage == "browser-budget"
    assert len(session.open_calls) == 1


@pytest.mark.parametrize("value", [0, 1001])
def test_attempt_budget_bounds_are_validated(value: int) -> None:
    with pytest.raises(ValueError, match="max_attempts_per_run"):
        AuthenticatedDbpiaBrowserResolver(
            DbpiaBrowserConfig(
                permission_granted=True,
                profile_dir=Path("/unused/fake-dbpia-profile"),
                max_attempts_per_run=value,
            ),
            session=FakeSession(snapshots=[]),
        )


@pytest.mark.parametrize("value", [0, 101, True])
def test_search_result_limit_bounds_are_validated(value: int) -> None:
    with pytest.raises(ValueError, match="max_search_results"):
        AuthenticatedDbpiaBrowserResolver(
            DbpiaBrowserConfig(
                permission_granted=True,
                profile_dir=Path("/unused/fake-dbpia-profile"),
                max_search_results=value,
            ),
            session=FakeSession(snapshots=[]),
        )


def test_credentials_are_both_or_neither_and_never_appear_in_repr() -> None:
    with pytest.raises(ValueError, match="provided together"):
        AuthenticatedDbpiaBrowserResolver(
            DbpiaBrowserConfig(
                permission_granted=True,
                profile_dir=Path("/unused/fake-dbpia-profile"),
                login_id="researcher-id",
            ),
            session=FakeSession(snapshots=[]),
        )

    config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
        login_id="unique-researcher-id",
        login_password="unique-secret-password",
        required_institution_name="고려대학교",
    )
    rendered = repr(config)

    assert "unique-researcher-id" not in rendered
    assert "unique-secret-password" not in rendered
    assert "고려대학교" not in rendered


@pytest.mark.parametrize(
    ("url", "node_id"),
    [
        (ARTICLE_URL, NODE),
        (
            "https://www.dbpia.co.kr/Journal/ArticleDetail/NODE87654321",
            "NODE87654321",
        ),
        (
            "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215",
            None,
        ),
        (
            "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215?"
            "Collection=0&Page=1&Seq=1&q=forensics",
            None,
        ),
        (
            "https://www.dbpia.co.kr/Journal/ArticleDetail/NODE87654321?"
            "Collection=0&Page=1",
            "NODE87654321",
        ),
    ],
)
def test_parse_dbpia_article_reference_accepts_only_article_shapes(
    url: str,
    node_id: str | None,
) -> None:
    reference = parse_dbpia_article_reference(url)

    assert reference.node_id == node_id
    assert is_exact_dbpia_article_url(url)
    # Even this small in-memory value keeps the DBpia identifier out of logs.
    assert node_id is None or node_id not in repr(reference)


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.dbpia.co.kr/journal/articleDetail?nodeId={NODE}",
        f"https://dbpia.co.kr/journal/articleDetail?nodeId={NODE}",
        f"https://user@www.dbpia.co.kr/journal/articleDetail?nodeId={NODE}",
        f"https://www.dbpia.co.kr:444/journal/articleDetail?nodeId={NODE}",
        f"{ARTICLE_URL}#download",
        f"{ARTICLE_URL}&width=1920",
        f"{ARTICLE_URL}&nodeId={NODE}",
        f"https://www.dbpia.co.kr/pdf/pdfView.do?nodeId={NODE}&width=1920",
        "https://download.dbpia.co.kr/File/Download?key=ephemeral-key",
        (
            "https://downloadcf.dbpia.co.kr/1/2/3/1234567.pdf?"
            "jwt=ephemeral-jwt"
        ),
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215?foo=bar",
    ],
)
def test_parse_dbpia_article_reference_rejects_non_article_or_unsafe_urls(
    url: str,
) -> None:
    with pytest.raises(ValueError, match="invalid DBpia article URL"):
        parse_dbpia_article_reference(url)

    assert not is_exact_dbpia_article_url(url)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (
            f"/journal/articleDetail?nodeId={NODE}",
            ARTICLE_URL,
        ),
        (
            f"https://www.dbpia.co.kr/Journal/ArticleDetail/{NODE}",
            ARTICLE_URL,
        ),
        (
            "/Journal/ArticleDetail/3209215",
            "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215",
        ),
        ("https://evil.example/article", None),
        (f"//evil.example/journal/articleDetail?nodeId={NODE}", None),
        (f"/pdf/pdfAiChatView.do?nodeId={NODE}", None),
    ],
)
def test_search_card_href_is_normalized_only_for_exact_articles(
    href: str,
    expected: str | None,
) -> None:
    assert normalize_dbpia_article_url(href) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://download.dbpia.co.kr/File/Download?key=123",
        (
            "https://downloadcf.dbpia.co.kr/1/22/333/4444444.pdf?"
            "jwt=header.payload.signature&filename=paper.pdf"
        ),
    ],
)
def test_native_download_url_allowlist_accepts_only_expected_shapes(
    url: str,
) -> None:
    assert _is_allowed_download_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://download.dbpia.co.kr/File/Download?key=123",
        "https://user@download.dbpia.co.kr/File/Download?key=123",
        "https://download.dbpia.co.kr/File/Download?key=123&key=456",
        "https://download.dbpia.co.kr/File/Download?jwt=value",
        "https://downloadcf.dbpia.co.kr/1/2/3/1234.pdf?filename=paper.pdf",
        "https://downloadcf.dbpia.co.kr/1/2/3/1234.pdf?jwt=x&extra=y",
        "https://evil.example/1/2/3/1234.pdf?jwt=value",
        "blob:https://www.dbpia.co.kr/ephemeral",
    ],
)
def test_native_download_url_allowlist_rejects_unsafe_shapes(url: str) -> None:
    assert not _is_allowed_download_url(url)


def test_profile_initialization_creates_private_marker_outside_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    profile = tmp_path / "dedicated-dbpia-profile"

    initialized = initialize_dbpia_profile_dir(
        profile,
        repository_root=repository,
    )

    marker = initialized / DEFAULT_PROFILE_MARKER
    assert initialized == profile.resolve()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "purpose": "forensics-paper-dbpia-browser",
    }
    assert initialized.stat().st_mode & 0o077 == 0
    assert marker.stat().st_mode & 0o077 == 0
    assert (
        validate_dbpia_profile_dir(
            profile,
            repository_root=repository,
        )
        == initialized
    )


def test_profile_guards_reject_repository_paths_and_unmarked_profiles(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="outside the repository"):
        initialize_dbpia_profile_dir(
            repository / "browser-profile",
            repository_root=repository,
        )

    unmarked = tmp_path / "existing-profile"
    unmarked.mkdir(mode=0o700)
    (unmarked / "Cookies").write_text("not a dedicated profile", encoding="utf-8")
    with pytest.raises(ValueError, match="unmarked browser profile"):
        initialize_dbpia_profile_dir(
            unmarked,
            repository_root=repository,
        )


def test_profile_validation_rejects_missing_marker_symlink_and_broad_mode(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="marker is missing"):
        validate_dbpia_profile_dir(profile, repository_root=repository)

    linked_profile = tmp_path / "linked-profile"
    linked_profile.symlink_to(profile, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        validate_dbpia_profile_dir(linked_profile, repository_root=repository)

    initialized = initialize_dbpia_profile_dir(
        profile,
        repository_root=repository,
    )
    initialized.chmod(0o750)
    with pytest.raises(ValueError, match="permissions are too broad"):
        validate_dbpia_profile_dir(initialized, repository_root=repository)


def test_profile_validation_rejects_marker_for_a_different_purpose(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=repository,
    )
    marker = profile / DEFAULT_PROFILE_MARKER
    marker.write_text(
        json.dumps({"schema_version": 1, "purpose": "another-browser"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="marker is invalid"):
        validate_dbpia_profile_dir(profile, repository_root=repository)


def test_exact_identity_and_single_download_control_resolve() -> None:
    button = _button()
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(button,),
            )
        ]
    )
    resolver = _resolver(session)

    result = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.resolved
    assert result.status == "resolved"
    assert result.reason == "authenticated_official_download_control"
    assert result.content == session.content
    assert result.matched_title == TITLE
    assert result.matched_doi == DOI
    assert session.open_calls == [(ARTICLE_URL, 2_000)]
    assert session.download_calls == [(button, NODE, 3_000, 123_456)]
    assert session.search_calls == []
    assert result.identity_provenance == (
        "dbpia_landing_identity_exact",
        "dbpia_download_button_node_exact",
        "native_browser_download_event",
    )


def test_search_ui_unique_exact_title_continues_to_verified_download() -> None:
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(),),
            )
        ],
        search_batches=[
            (
                DbpiaBrowserSearchResult(
                    title=TITLE,
                    article_url=ARTICLE_URL,
                ),
            )
        ],
    )

    result = _resolver(session).resolve(_paper())

    assert result.resolved
    assert session.search_calls == [(TITLE, 2_000, 50)]
    assert session.open_calls == [(ARTICLE_URL, 2_000)]
    assert [attempt.stage for attempt in result.attempts] == [
        "dbpia-browser-search",
        "dbpia-browser-landing",
        "dbpia-browser-download",
    ]
    assert result.identity_provenance[0] == "dbpia_web_title_search_exact"
    assert "dbpia_web_title_search_doi_year_exact" in (
        result.identity_provenance
    )


def test_title_search_without_doi_uses_year_and_pdf_title_identity() -> None:
    bibliography = (
        '<meta name="publisher" content="테스트발행기관">'
        '<meta name="citation_journal_title" content="테스트학술지">'
        '<meta name="citation_volume" content="1">'
        '<meta name="citation_issue" content="1">'
        '<meta name="citation_firstpage" content="1">'
        '<meta name="citation_lastpage" content="10">'
        '<meta name="citation_author" content="홍길동">'
    )
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    include_doi=False,
                    body=bibliography,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )
    paper = replace(
        _paper(),
        doi_original=None,
        doi=None,
        institution_id="테스트발행기관",
        journal_id="테스트학술지",
        issue_id="1_1",
        authors=("홍길동",),
    )

    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert result.matched_doi is None
    assert len(session.search_calls) == 1
    assert len(session.open_calls) == 1
    assert len(session.download_calls) == 1
    assert "dbpia_web_title_search_bibliography_exact" in (
        result.identity_provenance
    )


def test_doi_less_issue_only_row_requires_dbpia_to_omit_volume() -> None:
    bibliography = (
        '<meta name="publisher" content="테스트발행기관">'
        '<meta name="citation_journal_title" content="테스트학술지">'
        '<meta name="citation_issue" content="13">'
        '<meta name="citation_firstpage" content="1">'
        '<meta name="citation_lastpage" content="10">'
        '<meta name="citation_author" content="홍길동">'
    )
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    include_doi=False,
                    body=bibliography,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )
    paper = replace(
        _paper(),
        doi_original=None,
        doi=None,
        institution_id="테스트발행기관",
        journal_id="테스트학술지",
        issue_id="13",
        authors=("홍길동",),
    )

    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert "dbpia_landing_issue_only_exact" in result.identity_provenance
    assert "dbpia_web_title_search_bibliography_exact" in (
        result.identity_provenance
    )
    assert len(session.download_calls) == 1


def test_doi_less_row_with_unparsed_authors_fails_closed_before_search() -> None:
    bibliography = (
        '<meta name="publisher" content="테스트발행기관">'
        '<meta name="citation_journal_title" content="테스트학술지">'
        '<meta name="citation_volume" content="36">'
        '<meta name="citation_issue" content="3">'
        '<meta name="citation_firstpage" content="204">'
        '<meta name="citation_lastpage" content="211">'
        '<meta name="citation_author" content="DBpia에 표시된 저자">'
    )
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    include_doi=False,
                    body=bibliography,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )
    paper = replace(
        _paper(),
        doi_original=None,
        doi=None,
        institution_id="테스트발행기관",
        journal_id="테스트학술지",
        issue_id="36_3",
        first_page="204",
        final_page="211",
        authors=(),
    )

    result = _resolver(session).resolve(paper)

    assert result.status == "missing_metadata"
    assert result.reason == "missing_dbpia_bibliographic_search_metadata"
    assert result.attempts[0].stage == "browser-precheck"
    assert not session.search_calls
    assert not session.open_calls
    assert not session.download_calls


def test_doi_less_issue_only_row_rejects_unverifiable_dbpia_volume() -> None:
    bibliography = (
        '<meta name="publisher" content="테스트발행기관">'
        '<meta name="citation_journal_title" content="테스트학술지">'
        '<meta name="citation_volume" content="13">'
        '<meta name="citation_issue" content="13">'
        '<meta name="citation_firstpage" content="1">'
        '<meta name="citation_lastpage" content="10">'
        '<meta name="citation_author" content="홍길동">'
    )
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    include_doi=False,
                    body=bibliography,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )
    paper = replace(
        _paper(),
        doi_original=None,
        doi=None,
        institution_id="테스트발행기관",
        journal_id="테스트학술지",
        issue_id="13",
        authors=("홍길동",),
    )

    result = _resolver(session).resolve(paper)

    assert result.status == "identity_mismatch"
    assert result.reason == "dbpia_landing_bibliography_mismatch"
    assert not session.download_calls


def test_title_search_requires_exact_year_before_download(
) -> None:
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    include_doi=True,
                    include_year=False,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == (
        "dbpia_search_landing_requires_exact_year"
    )
    assert result.error_code == "identity_mismatch"
    assert len(session.search_calls) == 1
    assert len(session.open_calls) == 1
    assert not session.download_calls


def test_doi_title_search_without_landing_doi_requires_full_bibliography() -> None:
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(include_doi=False),
                buttons=(_button(),),
            )
        ],
        search_batches=[(DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "missing_metadata"
    assert result.reason == "missing_dbpia_bibliographic_identity"
    assert not session.download_calls


def test_doi_less_title_search_missing_bibliography_is_a_precheck() -> None:
    session = FakeSession(snapshots=[])
    paper = replace(_paper(), doi_original=None, doi=None)

    result = _resolver(session).resolve(paper)

    assert result.status == "missing_metadata"
    assert result.reason == "missing_dbpia_bibliographic_search_metadata"
    assert result.attempts[0].stage == "browser-precheck"
    assert not session.search_calls
    assert not session.open_calls


def test_real_kdfs_spacing_variant_is_a_search_shortlist_match() -> None:
    kci_title = "안드로이드 모바일 기기 암호화폐 개인 지갑 앱아티팩트 분석연구"
    dbpia_title = "안드로이드 모바일 기기 암호화폐 개인 지갑 앱 아티팩트 분석연구"
    bibliography = (
        '<meta name="publisher" content="한국디지털포렌식학회">'
        '<meta name="citation_journal_title" content="디지털포렌식연구">'
        '<meta name="citation_volume" content="18">'
        '<meta name="citation_issue" content="1">'
        '<meta name="citation_firstpage" content="11">'
        '<meta name="citation_lastpage" content="23">'
        '<meta name="citation_author" content="심대성">'
        '<meta name="citation_author" content="이상진">'
        '<meta name="citation_author" content="윤지원">'
    )
    session = FakeSession(
        snapshots=[
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                # The production KDFS DBpia landing currently omits
                # citation_doi, so title-search must still proceed when the
                # year and detailed bibliography bind this compact variant.
                document=_landing_document(
                    title=dbpia_title,
                    include_doi=False,
                    body=bibliography,
                ),
                buttons=(_button(),),
            )
        ],
        search_batches=[
            (
                DbpiaBrowserSearchResult(
                    title=dbpia_title,
                    article_url=ARTICLE_URL,
                ),
            )
        ],
    )

    paper = replace(
        _paper(title=kci_title),
        journal_id="디지털포렌식연구",
        institution_id="한국디지털포렌식학회",
        issue_id="18_1",
        first_page="11",
        final_page="23",
        authors=("심대성", "이상진", "윤지원"),
    )
    result = _resolver(session).resolve(paper)

    assert result.resolved
    assert session.search_calls == [(kci_title, 2_000, 50)]
    assert session.open_calls == [(ARTICLE_URL, 2_000)]


def test_search_ui_zero_exact_matches_is_not_found_without_article_open() -> None:
    session = FakeSession(
        snapshots=[],
        search_batches=[
            (
                DbpiaBrowserSearchResult(
                    title="완전히 다른 논문 제목",
                    article_url=ARTICLE_URL,
                ),
            )
        ],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "not_found"
    assert result.reason == "exact_dbpia_search_result_not_found"
    assert result.error_code == "not_found"
    assert not session.open_calls
    assert not session.download_calls


def test_long_search_query_never_accepts_a_truncated_card_title() -> None:
    long_title = "디지털포렌식" + ("장문논문제목" * 20)
    truncated_card_title = long_title[: dbpia_browser._SEARCH_QUERY_MAX_CHARS]
    session = FakeSession(
        snapshots=[],
        search_batches=[
            (
                DbpiaBrowserSearchResult(
                    title=truncated_card_title,
                    article_url=ARTICLE_URL,
                ),
            )
        ],
    )

    result = _resolver(session).resolve(_paper(title=long_title))

    assert result.status == "not_found"
    assert result.reason == "exact_dbpia_search_result_not_found"
    assert not session.open_calls
    assert not session.download_calls


def test_search_ui_multiple_exact_matches_fail_closed_as_ambiguous() -> None:
    second_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE87654321"
    )
    session = FakeSession(
        snapshots=[],
        search_batches=[
            (
                DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),
                DbpiaBrowserSearchResult(TITLE, second_url),
            )
        ],
    )

    result = _resolver(session).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "ambiguous_exact_dbpia_search_results"
    assert result.error_code == "ambiguous"
    assert not session.open_calls
    assert not session.download_calls


def test_search_result_repr_does_not_expose_article_reference() -> None:
    result = DbpiaBrowserSearchResult(TITLE, ARTICLE_URL)

    assert ARTICLE_URL not in repr(result)
    assert NODE not in repr(result)


@pytest.mark.parametrize(
    ("snapshot", "expected_status", "expected_error"),
    [
        (
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button("NODE87654321"),),
            ),
            "identity_mismatch",
            "identity_mismatch",
        ),
        (
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(dom_index=1), _button(dom_index=2)),
            ),
            "identity_mismatch",
            "ambiguous",
        ),
        (
            DbpiaPageSnapshot(
                final_url="https://www.dbpia.co.kr/member/login",
                document=(
                    '<html><input type="password" name="password"></html>'
                ),
                buttons=(),
            ),
            "authentication_required",
            "authentication_required",
        ),
    ],
)
def test_mismatch_ambiguity_and_challenges_never_click(
    snapshot: DbpiaPageSnapshot,
    expected_status: str,
    expected_error: str,
) -> None:
    session = FakeSession([snapshot])

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.status == expected_status
    assert result.error_code == expected_error
    assert session.open_calls == [(ARTICLE_URL, 2_000)]
    assert not session.download_calls


def test_article_without_download_control_is_unavailable_not_node_mismatch(
) -> None:
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(),
            )
        ]
    )

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.status == "access_denied"
    assert result.reason == "dbpia_download_control_unavailable"
    assert result.error_code == "access_denied"
    assert not session.download_calls


def test_hidden_unrelated_download_control_does_not_poison_visible_binding(
) -> None:
    visible = _button()
    hidden_unrelated = _button(
        "NODE87654321",
        dom_index=4,
        visible=False,
    )
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(visible, hidden_unrelated),
            )
        ]
    )

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.resolved
    assert len(session.download_calls) == 1


def test_inert_recaptcha_script_does_not_create_false_challenge() -> None:
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(
                    body=(
                        '<script src="/assets/recaptcha.js">'
                        "const challengeName = 'captcha';"
                        "</script>"
                    )
                ),
                buttons=(_button(),),
            )
        ]
    )

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.resolved
    assert len(session.download_calls) == 1


def test_live_like_hidden_gate_markup_is_not_a_static_challenge() -> None:
    document = _landing_document(
        body="""
        <dialog id="recaptchaModal" style="display: none;">
          <p>reCAPTCHA 자동입력 방지</p>
        </dialog>
        <dialog id="recaptchaAlertModal" style="display: none;">
          <p>로봇이 아닙니다</p>
        </dialog>
        <dialog id="loginLayer" style="display: none;">
          <input type="password"><p>로그인이 필요합니다</p>
        </dialog>
        <dialog id="multiPurchaseLayer" style="display: none;">
          <p>구독 후 이용</p>
        </dialog>
        <div style="display: none;">verification code 인증번호</div>
        """
    )
    snapshot = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=document,
        buttons=(_button(),),
    )

    assert _page_challenge(snapshot) is None
    session = FakeSession([snapshot])
    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.resolved
    assert len(session.download_calls) == 1


def test_authentication_challenge_opens_circuit_for_later_papers() -> None:
    challenge = DbpiaPageSnapshot(
        final_url="https://www.dbpia.co.kr/member/login",
        document='<html><input type="password"></html>',
        buttons=(),
    )
    session = FakeSession([challenge])
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    second = resolver.resolve(_paper(title="다른 논문"), landing_url=ARTICLE_URL)

    assert first.status == "authentication_required"
    assert second.status == "authentication_required"
    assert second.error_code == "authentication_required"
    assert second.attempts[0].stage == "browser-circuit"
    assert len(session.open_calls) == 1
    assert not session.download_calls


def test_three_consecutive_retryable_failures_open_circuit() -> None:
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(),),
                http_status=status,
            )
            for status in (502, 503, 504)
        ]
    )
    resolver = _resolver(session)

    failures = [
        resolver.resolve(_paper(), landing_url=ARTICLE_URL)
        for _ in range(3)
    ]
    circuit = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert [result.status for result in failures] == [
        "retryable_error",
        "retryable_error",
        "retryable_error",
    ]
    assert circuit.status == "retryable_error"
    assert circuit.reason == "dbpia_browser_transient_circuit_open"
    assert circuit.error_code == "consecutive_retryable_failures"
    assert circuit.attempts[0].stage == "browser-circuit"
    assert len(session.open_calls) == 3
    assert not session.download_calls


def test_success_resets_consecutive_retryable_failure_count() -> None:
    retryable_statuses = (502, 503)
    successful = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
    )
    session = FakeSession(
        [
            *(
                DbpiaPageSnapshot(
                    final_url=ARTICLE_URL,
                    document=_landing_document(),
                    buttons=(_button(),),
                    http_status=status,
                )
                for status in retryable_statuses
            ),
            successful,
            *(
                DbpiaPageSnapshot(
                    final_url=ARTICLE_URL,
                    document=_landing_document(),
                    buttons=(_button(),),
                    http_status=status,
                )
                for status in retryable_statuses
            ),
            successful,
        ]
    )
    resolver = _resolver(session)

    results = [
        resolver.resolve(_paper(), landing_url=ARTICLE_URL)
        for _ in range(6)
    ]

    assert [result.status for result in results] == [
        "retryable_error",
        "retryable_error",
        "resolved",
        "retryable_error",
        "retryable_error",
        "resolved",
    ]
    assert len(session.open_calls) == 6


def test_nonretryable_result_resets_transient_failure_streak() -> None:
    retryable = lambda status: DbpiaPageSnapshot(  # noqa: E731
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
        http_status=status,
    )
    mismatch = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(title="다른 논문 제목"),
        buttons=(_button(),),
    )
    success = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
    )
    session = FakeSession(
        [
            retryable(502),
            mismatch,
            retryable(503),
            mismatch,
            retryable(504),
            success,
        ]
    )
    resolver = _resolver(session)

    results = [
        resolver.resolve(_paper(), landing_url=ARTICLE_URL)
        for _ in range(6)
    ]

    assert [result.status for result in results] == [
        "retryable_error",
        "identity_mismatch",
        "retryable_error",
        "identity_mismatch",
        "retryable_error",
        "resolved",
    ]
    assert len(session.open_calls) == 6


def test_article_access_denied_does_not_open_global_circuit() -> None:
    unavailable = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(visible=False),),
    )
    success = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
    )
    session = FakeSession([unavailable, success])
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    second = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert first.status == "access_denied"
    assert second.resolved
    assert len(session.open_calls) == 2
    assert len(session.download_calls) == 1


def test_article_http_403_does_not_open_global_circuit() -> None:
    forbidden = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
        http_status=403,
    )
    success = DbpiaPageSnapshot(
        final_url=ARTICLE_URL,
        document=_landing_document(),
        buttons=(_button(),),
    )
    session = FakeSession([forbidden, success])
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    second = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert first.status == "access_denied"
    assert first.reason == "dbpia_article_access_denied"
    assert second.resolved
    assert len(session.open_calls) == 2
    assert len(session.download_calls) == 1


@pytest.mark.parametrize(
    ("http_status", "expected_status", "expected_error"),
    [
        (401, "authentication_required", "authentication_required"),
        (403, "access_denied", "access_denied"),
        (429, "retryable_error", "retryable_error"),
        (503, "retryable_error", "retryable_error"),
        (404, "not_found", "not_found"),
    ],
)
def test_landing_http_status_is_classified_before_click(
    http_status: int,
    expected_status: str,
    expected_error: str,
) -> None:
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(),),
                http_status=http_status,
            )
        ]
    )

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)

    assert result.status == expected_status
    assert result.error_code == expected_error
    assert not session.download_calls


def test_rate_limit_opens_circuit_immediately() -> None:
    http_status = 429
    expected_status = "retryable_error"
    expected_reason = "dbpia_article_rate_limited"
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(),),
                http_status=http_status,
            )
        ]
    )
    resolver = _resolver(session)

    first = resolver.resolve(_paper(), landing_url=ARTICLE_URL)
    circuit = resolver.resolve(_paper(), landing_url=ARTICLE_URL)

    assert first.status == expected_status
    assert first.reason == expected_reason
    assert circuit.status == expected_status
    assert circuit.reason == expected_reason
    assert circuit.attempts[0].stage == "browser-circuit"
    assert len(session.open_calls) == 1
    assert not session.download_calls


def test_download_reader_rejects_non_regular_file(tmp_path: Path) -> None:
    with pytest.raises(DbpiaBrowserOperationError) as raised:
        _read_download_without_symlink(tmp_path, 1024)

    assert raised.value.status == "invalid_response"
    assert raised.value.error_code == "invalid_pdf"


@pytest.mark.parametrize(
    ("search_title", "submitted_title"),
    [
        (TITLE, TITLE),
        (
            "x" * (dbpia_browser._SEARCH_QUERY_MAX_CHARS + 25),
            "x" * dbpia_browser._SEARCH_QUERY_MAX_CHARS,
        ),
    ],
)
def test_playwright_search_uses_only_official_homepage_controls(
    search_title: str,
    submitted_title: str,
) -> None:
    events: list[tuple[object, ...]] = []

    class Response:
        status = 200

    class Control:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def select_option(self, value: str, **kwargs) -> list[str]:
            events.append(
                ("select_option", self.selector, value, kwargs)
            )
            return [value]

        def fill(self, value: str, **_kwargs) -> None:
            events.append(("fill", self.selector, value))

        def click(self, **_kwargs) -> None:
            events.append(("click", self.selector))

    class ResultLink:
        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return TITLE

        def get_attribute(self, name: str) -> str:
            assert name == "href"
            return f"/journal/articleDetail?nodeId={NODE}"

    class ResultLinks:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> ResultLink:
            assert index == 0
            return ResultLink()

    class Card:
        def locator(self, selector: str) -> ResultLinks:
            events.append(("card-locator", selector))
            return ResultLinks()

    class Cards:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> Card:
            assert index == 0
            return Card()

    class Page:
        url = dbpia_browser.DBPIA_HOME_URL

        def goto(self, url: str, **_kwargs) -> Response:
            events.append(("goto", url))
            self.url = url
            return Response()

        def locator(self, selector: str):
            events.append(("locator", selector))
            if selector == dbpia_browser._SEARCH_CARD_SELECTOR:
                return Cards()
            return Control(selector)

        def wait_for_selector(self, selector: str, **kwargs) -> None:
            events.append(("wait", selector, kwargs.get("state")))
            self.url = "https://www.dbpia.co.kr/search/topSearch"

        def content(self) -> str:
            return "<html><body>검색 결과</body></html>"

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()
    session._owner_thread = threading.get_ident()
    session._dialog_error = None
    session._blocked_document = False
    session._login_attempted = False
    session._ensure_started = lambda: None

    results = session.search_articles(
        search_title,
        timeout_ms=2_000,
        max_results=10,
    )

    assert results == (DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)
    assert [event for event in events if event[0] == "goto"] == [
        ("goto", dbpia_browser.DBPIA_HOME_URL)
    ]
    assert (
        "select_option",
        dbpia_browser._SEARCH_OPTION_SELECTOR,
        "node",
        {"timeout": 2_000, "force": True},
    ) in events
    assert (
        "fill",
        dbpia_browser._SEARCH_INPUT_SELECTOR,
        submitted_title,
    ) in events
    assert ("click", dbpia_browser._SEARCH_SUBMIT_SELECTOR) in events
    assert (
        "wait",
        dbpia_browser._SEARCH_READY_SELECTOR,
        "visible",
    ) in events
    assert (
        "card-locator",
        dbpia_browser._SEARCH_RESULT_LINK_SELECTOR,
    ) in events


def _collect_rendered_search_cards(
    cards: tuple[tuple[str, str], ...],
) -> tuple[DbpiaBrowserSearchResult, ...]:
    class ResultLink:
        def __init__(self, title: str, href: str) -> None:
            self._title = title
            self._href = href

        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return self._title

        def get_attribute(self, name: str) -> str:
            assert name == "href"
            return self._href

    class ResultLinks:
        def __init__(self, title: str, href: str) -> None:
            self._link = ResultLink(title, href)

        def count(self) -> int:
            return 1

        def nth(self, index: int) -> ResultLink:
            assert index == 0
            return self._link

    class Card:
        def __init__(self, title: str, href: str) -> None:
            self._title = title
            self._href = href

        def locator(self, selector: str) -> ResultLinks:
            assert selector == dbpia_browser._SEARCH_RESULT_LINK_SELECTOR
            return ResultLinks(self._title, self._href)

    class Cards:
        def count(self) -> int:
            return len(cards)

        def nth(self, index: int) -> Card:
            return Card(*cards[index])

    class Page:
        def locator(self, selector: str) -> Cards:
            assert selector == dbpia_browser._SEARCH_CARD_SELECTOR
            return Cards()

    session = object.__new__(_PlaywrightDbpiaSession)
    session._page = Page()
    return session._collect_search_results(max_results=50)


def test_search_results_ignore_same_site_non_article_journal_card() -> None:
    results = _collect_rendered_search_cards(
        (
            (TITLE, f"/journal/articleDetail?nodeId={NODE}"),
            ("디지털포렌식연구", "/journal/detail?nodeId=00000123"),
        )
    )

    assert results == (DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)


@pytest.mark.parametrize(
    "unsafe_href",
    [
        f"https://evil.example/journal/articleDetail?nodeId={NODE}",
        f"//evil.example/journal/articleDetail?nodeId={NODE}",
        f"https://user@www.dbpia.co.kr/journal/articleDetail?nodeId={NODE}",
        f"/journal/articleDetail?nodeId={NODE}&redirect=evil.example",
        "javascript:alert(1)",
    ],
)
def test_search_results_never_return_unsafe_navigation_candidate(
    unsafe_href: str,
) -> None:
    with pytest.raises(
        DbpiaBrowserOperationError,
        match="unsafe_dbpia_search_result",
    ):
        _collect_rendered_search_cards(((TITLE, unsafe_href),))


def test_search_ready_selector_requires_visible_result_state() -> None:
    selectors = {
        selector.strip()
        for selector in dbpia_browser._SEARCH_READY_SELECTOR.split(",")
    }

    assert selectors == {
        f"{dbpia_browser._SEARCH_CARD_SELECTOR}:visible",
        f"{dbpia_browser._SEARCH_NO_DATA_SELECTOR}:visible",
    }


def test_configured_credentials_use_scoped_login_controls_once() -> None:
    events: list[tuple[object, ...]] = []

    class HeaderLoginLink:
        def __init__(self, page) -> None:
            self.page = page

        def is_visible(self) -> bool:
            return not self.page.logged_in

        def get_attribute(self, name: str) -> str:
            assert name == "href"
            return "/member/login"

        def click(self, **_kwargs) -> None:
            events.append(("click", dbpia_browser._HEADER_LOGIN_SELECTOR))
            self.page.url = dbpia_browser.DBPIA_LOGIN_URL

    class HeaderLoginLinks:
        def __init__(self, page) -> None:
            self.page = page

        def count(self) -> int:
            return 0 if self.page.logged_in else 1

        def nth(self, index: int) -> HeaderLoginLink:
            assert index == 0
            return HeaderLoginLink(self.page)

    class LoginControl:
        def __init__(self, page, selector: str) -> None:
            self.page = page
            self.selector = selector

        def fill(self, value: str | None, **_kwargs) -> None:
            events.append(("fill", self.selector, value))

        def click(self, **_kwargs) -> None:
            events.append(("click", self.selector))
            self.page.logged_in = True
            self.page.url = dbpia_browser.DBPIA_HOME_URL

    class Page:
        url = dbpia_browser.DBPIA_HOME_URL
        logged_in = False

        def locator(self, selector: str):
            if selector == dbpia_browser._HEADER_LOGIN_SELECTOR:
                return HeaderLoginLinks(self)
            return LoginControl(self, selector)

        def wait_for_selector(self, selector: str, **kwargs) -> None:
            events.append(("wait", selector, kwargs.get("state")))

        def content(self) -> str:
            return "<html><body>로그인 완료</body></html>"

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
        login_id="researcher-id",
        login_password="secret-password",
    )
    session._page = Page()
    session._dialog_error = None
    session._login_attempted = False

    first = session._login_with_configured_credentials(timeout_ms=2_000)
    second = session._login_with_configured_credentials(timeout_ms=2_000)

    assert first is True
    assert second is False
    assert events.count(("click", dbpia_browser._HEADER_LOGIN_SELECTOR)) == 1
    assert (
        "fill",
        dbpia_browser._LOGIN_ID_SELECTOR,
        "researcher-id",
    ) in events
    assert (
        "fill",
        dbpia_browser._LOGIN_PASSWORD_SELECTOR,
        "secret-password",
    ) in events
    assert events.count(("click", dbpia_browser._LOGIN_SUBMIT_SELECTOR)) == 1


def test_active_required_institution_skips_optional_personal_login() -> None:
    events: list[tuple[object, ...]] = []

    class Institution:
        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return "고려대학교"

    class Institutions:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> Institution:
            assert index == 0
            return Institution()

    class LoginLink:
        def is_visible(self) -> bool:
            return True

        def get_attribute(self, name: str) -> str:
            assert name == "href"
            return "/member/login"

        def click(self, **_kwargs) -> None:
            events.append(("click", dbpia_browser._HEADER_LOGIN_SELECTOR))

    class LoginLinks:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> LoginLink:
            assert index == 0
            return LoginLink()

    class Page:
        def locator(self, selector: str):
            if selector == dbpia_browser._INSTITUTION_SELECTOR:
                return Institutions()
            if selector == dbpia_browser._HEADER_LOGIN_SELECTOR:
                return LoginLinks()
            raise AssertionError(f"unexpected selector: {selector}")

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
        login_id="optional-researcher-id",
        login_password="optional-secret-password",
        required_institution_name="고려대학교",
    )
    session._page = Page()
    session._dialog_error = None
    session._login_attempted = False

    assert not session._login_with_configured_credentials(timeout_ms=2_000)
    session._verify_required_institution()
    assert not events


def test_configured_credentials_use_official_institution_complete_modal() -> None:
    events: list[tuple[object, ...]] = []

    class Collection:
        def __init__(self, control, count: int = 1) -> None:
            self.control = control
            self._count = count

        def count(self) -> int:
            return self._count

        def nth(self, index: int):
            assert index == 0
            return self.control

        def is_visible(self) -> bool:
            return self.control.is_visible()

    class Modal:
        def __init__(self, page) -> None:
            self.page = page

        def is_visible(self) -> bool:
            return not self.page.logged_in

    class Control:
        def __init__(self, page, selector: str) -> None:
            self.page = page
            self.selector = selector

        def is_visible(self) -> bool:
            return not self.page.logged_in

        def is_enabled(self) -> bool:
            return True

        def fill(self, value: str | None, **_kwargs) -> None:
            events.append(("fill", self.selector, value))

        def click(self, **_kwargs) -> None:
            events.append(("click", self.selector))
            if self.selector == dbpia_browser._ORG_AUTH_COMPLETE_LOGIN_SELECTOR:
                self.page.url = dbpia_browser.DBPIA_LOGIN_URL
            elif self.selector == dbpia_browser._LOGIN_SUBMIT_SELECTOR:
                self.page.logged_in = True
                self.page.url = dbpia_browser.DBPIA_HOME_URL

    class HeaderLinks:
        def count(self) -> int:
            return 0

    class Page:
        url = dbpia_browser.DBPIA_HOME_URL
        logged_in = False

        def locator(self, selector: str):
            if selector == dbpia_browser._ORG_AUTH_COMPLETE_MODAL_SELECTOR:
                return Collection(Modal(self))
            if selector == dbpia_browser._ORG_AUTH_COMPLETE_LOGIN_SELECTOR:
                return Collection(Control(self, selector))
            if selector == dbpia_browser._HEADER_LOGIN_SELECTOR:
                return HeaderLinks()
            return Control(self, selector)

        def wait_for_selector(self, selector: str, **kwargs) -> None:
            events.append(("wait", selector, kwargs.get("state")))

        def content(self) -> str:
            return "<html><body>기관인증 및 로그인 완료</body></html>"

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
        login_id="researcher-id",
        login_password="secret-password",
    )
    session._page = Page()
    session._dialog_error = None
    session._login_attempted = False

    assert session._login_with_configured_credentials(timeout_ms=2_000)
    assert (
        "click",
        dbpia_browser._ORG_AUTH_COMPLETE_LOGIN_SELECTOR,
    ) in events
    assert ("click", dbpia_browser._LOGIN_SUBMIT_SELECTOR) in events


def test_required_institution_mismatch_is_authentication_failure() -> None:
    class Institution:
        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return "다른 대학교"

    class Institutions:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> Institution:
            assert index == 0
            return Institution()

    class Page:
        def locator(self, selector: str) -> Institutions:
            assert selector == dbpia_browser._INSTITUTION_SELECTOR
            return Institutions()

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
        required_institution_name="고려대학교",
    )
    session._page = Page()

    with pytest.raises(DbpiaBrowserOperationError) as raised:
        session._verify_required_institution()

    assert raised.value.status == "authentication_required"
    assert raised.value.error_code == "authentication_required"


@pytest.mark.parametrize(
    ("selector", "expected_status", "expected_error"),
    [
        ("#recaptchaModal", "captcha_required", "captcha_required"),
        ("#recaptchaAlertModal", "captcha_required", "captcha_required"),
        ("#loginLayer", "authentication_required", "authentication_required"),
        (
            "#authenticateOrganizationA",
            "authentication_required",
            "authentication_required",
        ),
        ("#multiPurchaseLayer", "access_denied", "access_denied"),
        ("#previewLayer", "access_denied", "access_denied"),
    ],
)
def test_only_visible_dbpia_gate_is_classified(
    selector: str,
    expected_status: str,
    expected_error: str,
) -> None:
    class Gate:
        def is_visible(self) -> bool:
            return True

    class Gates:
        def __init__(self, matches: bool) -> None:
            self.matches = matches

        def count(self) -> int:
            return 1 if self.matches else 0

        def nth(self, index: int) -> Gate:
            assert self.matches and index == 0
            return Gate()

    class Page:
        url = ARTICLE_URL

        def locator(self, candidate: str) -> Gates:
            return Gates(candidate == selector)

        def content(self) -> str:
            return _landing_document(
                body='<dialog id="recaptchaModal" style="display:none">captcha</dialog>'
            )

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()

    challenge = session._inspect_current_challenge(
        include_access_gates=(
            selector in dbpia_browser._VISIBLE_ACCESS_GATE_SELECTORS
        ),
    )

    assert challenge is not None
    assert challenge.status == expected_status
    assert challenge.error_code == expected_error


@pytest.mark.parametrize(
    "selector",
    ["#aiChatDialog", "#alertDialog", "#commonOneButtonConfirmAlertModal"],
)
def test_auxiliary_dbpia_dialog_is_not_an_access_gate(selector: str) -> None:
    class Gate:
        def is_visible(self) -> bool:
            return True

    class Gates:
        def __init__(self, matches: bool) -> None:
            self.matches = matches

        def count(self) -> int:
            return 1 if self.matches else 0

        def nth(self, index: int) -> Gate:
            assert self.matches and index == 0
            return Gate()

    class Page:
        url = ARTICLE_URL

        def locator(self, candidate: str) -> Gates:
            return Gates(candidate == selector)

        def content(self) -> str:
            return _landing_document()

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()

    assert (
        session._inspect_current_challenge(include_access_gates=True) is None
    )


def test_access_gate_is_ignored_before_download_attempt() -> None:
    class Gate:
        def is_visible(self) -> bool:
            return True

    class Gates:
        def __init__(self, matches: bool) -> None:
            self.matches = matches

        def count(self) -> int:
            return 1 if self.matches else 0

        def nth(self, index: int) -> Gate:
            assert self.matches and index == 0
            return Gate()

    class Page:
        url = ARTICLE_URL

        def locator(self, candidate: str) -> Gates:
            return Gates(candidate == "#multiPurchaseLayer")

        def content(self) -> str:
            return _landing_document()

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()

    assert session._inspect_current_challenge() is None
    challenge = session._inspect_current_challenge(include_access_gates=True)
    assert challenge is not None
    assert challenge.status == "access_denied"


def test_hidden_dbpia_gate_selectors_do_not_create_a_challenge() -> None:
    class HiddenGate:
        def is_visible(self) -> bool:
            return False

    class HiddenGates:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> HiddenGate:
            assert index == 0
            return HiddenGate()

    class Page:
        url = ARTICLE_URL

        def locator(self, _selector: str) -> HiddenGates:
            return HiddenGates()

        def content(self) -> str:
            return _landing_document(
                body="""
                <dialog id="recaptchaModal">captcha</dialog>
                <dialog id="loginLayer"><input type="password"></dialog>
                """
            )

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()

    assert session._inspect_current_challenge() is None


@pytest.mark.parametrize(
    ("visible_gate", "expected_status", "expected_error"),
    [
        ("#recaptchaModal", "captcha_required", "captcha_required"),
        ("#loginLayer", "authentication_required", "authentication_required"),
        ("#multiPurchaseLayer", "access_denied", "access_denied"),
    ],
)
def test_post_click_visible_gate_is_not_reduced_to_generic_timeout(
    visible_gate: str,
    expected_status: str,
    expected_error: str,
) -> None:
    class DetailLocator:
        def get_attribute(self, _name: str) -> str:
            return f"onClickDownloadNode('{NODE}')"

        def is_visible(self) -> bool:
            return True

        def is_enabled(self) -> bool:
            return True

        def click(self, **_kwargs) -> None:
            return None

    class DetailLocators:
        def nth(self, _index: int) -> DetailLocator:
            return DetailLocator()

    class Gate:
        def is_visible(self) -> bool:
            return True

    class Gates:
        def __init__(self, matches: bool) -> None:
            self.matches = matches

        def count(self) -> int:
            return 1 if self.matches else 0

        def nth(self, index: int) -> Gate:
            assert self.matches and index == 0
            return Gate()

    class FailedDownloadWait:
        def __init__(self, page) -> None:
            self.page = page

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.page.active_gate = visible_gate
            raise RuntimeError("download was not emitted")

    class Page:
        url = ARTICLE_URL
        active_gate: str | None = None

        def locator(self, selector: str):
            if selector == "[onclick]":
                return DetailLocators()
            return Gates(selector == self.active_gate)

        def expect_download(self, **_kwargs) -> FailedDownloadWait:
            return FailedDownloadWait(self)

        def content(self) -> str:
            return _landing_document(
                body='<dialog id="recaptchaModal" style="display:none">captcha</dialog>'
            )

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._owner_thread = threading.get_ident()
    session._page = Page()
    session._dialog_error = None
    session._blocked_document = False

    with pytest.raises(DbpiaBrowserOperationError) as raised:
        session.download_button(
            _button(),
            expected_node_id=NODE,
            timeout_ms=10,
            max_pdf_bytes=1024,
        )

    assert raised.value.status == expected_status
    assert raised.value.error_code == expected_error


def test_runtime_blocks_service_workers_before_installing_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=repository,
    )
    launch_kwargs: dict[str, object] = {}

    class Page:
        def on(self, _event: str, _handler) -> None:
            return None

    class Context:
        pages = [Page()]

        def route(self, _pattern: str, _handler) -> None:
            return None

        def close(self) -> None:
            return None

    class Chromium:
        def launch_persistent_context(self, **kwargs):
            launch_kwargs.update(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

        def stop(self) -> None:
            return None

    class Starter:
        def start(self) -> Playwright:
            return Playwright()

    monkeypatch.setattr(
        dbpia_browser,
        "_load_sync_playwright",
        lambda: (lambda: Starter()),
    )
    session = _PlaywrightDbpiaSession(
        DbpiaBrowserConfig(
            permission_granted=True,
            profile_dir=profile,
            repository_root=repository,
        )
    )

    session._ensure_started()
    session.close()

    assert launch_kwargs["service_workers"] == "block"


def test_resolution_repr_has_no_article_download_or_content_secrets() -> None:
    session = FakeSession(
        [
            DbpiaPageSnapshot(
                final_url=ARTICLE_URL,
                document=_landing_document(),
                buttons=(_button(),),
            )
        ],
        content=b"%PDF-1.7\nunique-secret-pdf-payload\n%%EOF",
    )

    result = _resolver(session).resolve(_paper(), landing_url=ARTICLE_URL)
    rendered = repr(result)

    assert isinstance(result, DbpiaBrowserResolution)
    assert result.resolved
    assert "https://" not in rendered
    assert "nodeId" not in rendered
    assert NODE not in rendered
    assert "unique-secret-pdf-payload" not in rendered
    assert "jwt" not in rendered.casefold()
    assert "cookie" not in rendered.casefold()


def test_playwright_search_dismisses_the_institution_complete_dialog_before_submitting() -> None:
    """The post-institution-auth dialog's backdrop blocks clicks; close it officially."""

    events: list[tuple[object, ...]] = []
    state = {"modal_visible": True}

    class Response:
        status = 200

    class Modal:
        def count(self) -> int:
            return 1

        def is_visible(self) -> bool:
            return state["modal_visible"]

        def wait_for(self, **_kwargs) -> None:
            events.append(("wait-hidden", dbpia_browser._ORG_AUTH_COMPLETE_MODAL_SELECTOR))

    class LaterButton:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> "LaterButton":
            assert index == 0
            return self

        def is_visible(self) -> bool:
            return True

        def is_enabled(self) -> bool:
            return True

        def click(self, **_kwargs) -> None:
            events.append(("click", dbpia_browser._ORG_AUTH_COMPLETE_LATER_SELECTOR))
            state["modal_visible"] = False

    class Control:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def select_option(self, value: str, **_kwargs) -> list[str]:
            return [value]

        def fill(self, value: str, **_kwargs) -> None:
            events.append(("fill", self.selector, value))

        def click(self, **_kwargs) -> None:
            # A click on the search button must only happen after the dialog closed.
            assert state["modal_visible"] is False, "search submit clicked behind the dialog backdrop"
            events.append(("click", self.selector))

    class ResultLink:
        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return TITLE

        def get_attribute(self, name: str) -> str:
            return f"/journal/articleDetail?nodeId={NODE}"

    class ResultLinks:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> ResultLink:
            return ResultLink()

    class Card:
        def locator(self, selector: str) -> ResultLinks:
            return ResultLinks()

    class Cards:
        def count(self) -> int:
            return 1

        def nth(self, index: int) -> Card:
            return Card()

    class Page:
        url = dbpia_browser.DBPIA_HOME_URL

        def goto(self, url: str, **_kwargs) -> Response:
            self.url = url
            return Response()

        def locator(self, selector: str):
            if selector == dbpia_browser._ORG_AUTH_COMPLETE_MODAL_SELECTOR:
                return Modal()
            if selector == dbpia_browser._ORG_AUTH_COMPLETE_LATER_SELECTOR:
                return LaterButton()
            if selector == dbpia_browser._SEARCH_CARD_SELECTOR:
                return Cards()
            return Control(selector)

        def wait_for_selector(self, selector: str, **kwargs) -> None:
            self.url = "https://www.dbpia.co.kr/search/topSearch"

        def content(self) -> str:
            return "<html><body>검색 결과</body></html>"

    session = object.__new__(_PlaywrightDbpiaSession)
    session._config = DbpiaBrowserConfig(
        permission_granted=True,
        profile_dir=Path("/unused/fake-dbpia-profile"),
    )
    session._page = Page()
    session._owner_thread = threading.get_ident()
    session._dialog_error = None
    session._blocked_document = False
    session._login_attempted = False
    session._ensure_started = lambda: None

    results = session.search_articles(TITLE, timeout_ms=2_000, max_results=10)

    assert results == (DbpiaBrowserSearchResult(TITLE, ARTICLE_URL),)
    clicks = [event for event in events if event[0] == "click"]
    assert clicks[0] == ("click", dbpia_browser._ORG_AUTH_COMPLETE_LATER_SELECTOR)
    assert ("click", dbpia_browser._SEARCH_SUBMIT_SELECTOR) in clicks
    assert state["modal_visible"] is False
