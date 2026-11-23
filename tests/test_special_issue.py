from __future__ import annotations

import hashlib
import json
import importlib.util
import sqlite3
import stat
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "Analyzer" / "Special-Issue" / "special_issue.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "forensics_paper_special_issue",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("special_issue_module_not_loadable")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves postponed annotations through ``sys.modules``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


special_issue = _load_module()


EMPTY_SPECIAL = b"""@article{Volume={16},
Number={1},
Special-Number={},
Pages={24-37},
DA={2026-08-23}
}
"""

NONEMPTY_SPECIAL = b"""@article{Volume={16},
Number={1},
Special-Number={2026 Digital Forensics Special Issue},
Pages={24-37},
Article-Number={A-12},
DA={2026-08-23}
}
"""

MISSING_SPECIAL = b"""@article{Volume={16},
Number={1},
Pages={24-37},
DA={2026-08-23}
}
"""

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 23, 17, 0, tzinfo=SEOUL)


def _error_type() -> type[BaseException]:
    for name in (
        "InvalidBibtexError",
        "InvalidSpecialIssueResponse",
        "SpecialIssueCollectionError",
    ):
        candidate = getattr(special_issue, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
    return ValueError


def _value(parsed: object, name: str) -> object:
    if isinstance(parsed, dict):
        return parsed[name]
    return getattr(parsed, name)


def _make_database(path: Path, rows: list[tuple[object, ...]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE kci_papers (
        id INTEGER PRIMARY KEY,
        source_id TEXT,
        title TEXT,
        title_kor TEXT,
        title_eng TEXT,
        doi TEXT,
        publication_year INTEGER,
        journal_id TEXT,
        institution_id TEXT,
        issue_id TEXT
        )"""
    )
    connection.executemany(
        "INSERT INTO kci_papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_parse_keyless_kci_bibtex_empty_special_number_is_valid_negative() -> None:
    parsed = special_issue.parse_kci_bibtex(EMPTY_SPECIAL)

    assert _value(parsed, "volume") == "16"
    assert _value(parsed, "number") == "1"
    assert _value(parsed, "special_number") == ""
    assert _value(parsed, "pages") == "24-37"
    assert _value(parsed, "article_number") is None
    assert _value(parsed, "bibtex_date") == "2026-08-23"
    assert _value(parsed, "special_number_field_present") is True
    assert _value(parsed, "is_special_issue") is False


def test_parse_keyless_kci_bibtex_nonempty_special_number_is_positive() -> None:
    parsed = special_issue.parse_kci_bibtex(NONEMPTY_SPECIAL)

    assert _value(parsed, "special_number") == "2026 Digital Forensics Special Issue"
    assert _value(parsed, "article_number") == "A-12"
    assert _value(parsed, "is_special_issue") is True


def test_special_number_uses_nfkc_and_unicode_whitespace_before_decision() -> None:
    parsed = special_issue.parse_kci_bibtex(
        """\ufeff@article{Volume={１６},
Number={１},
Special-Number={\u3000Ｓｐｅｃｉａｌ\u00a0Ｉｓｓｕｅ\u3000},
Pages={１－９},
DA={2026-08-23}
}
"""
    )

    assert _value(parsed, "volume") == "16"
    assert _value(parsed, "number") == "1"
    assert _value(parsed, "special_number") == "Special Issue"
    assert _value(parsed, "is_special_issue") is True

    whitespace = special_issue.parse_kci_bibtex(
        EMPTY_SPECIAL.replace(b"Special-Number={}", "Special-Number={　 }".encode())
    )
    assert _value(whitespace, "special_number") == ""
    assert _value(whitespace, "is_special_issue") is False


@pytest.mark.parametrize("token", ["0", "N", "\uc5c6\uc74c", "-"])
def test_every_nonempty_special_number_is_special_by_user_rule(token: str) -> None:
    parsed = special_issue.parse_kci_bibtex(
        EMPTY_SPECIAL.replace(b"Special-Number={}", f"Special-Number={{{token}}}".encode())
    )

    assert _value(parsed, "special_number") == token
    assert _value(parsed, "is_special_issue") is True


def test_missing_special_number_is_unresolved_and_never_a_negative() -> None:
    parsed = special_issue.parse_kci_bibtex(MISSING_SPECIAL)

    assert _value(parsed, "special_number") is None
    assert _value(parsed, "special_number_field_present") is False
    assert _value(parsed, "is_special_issue") is None
    assert _value(parsed, "classification") == "Unresolved"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            b"@article{Special-Number={},Special-Number={Special}}",
            "duplicate_bibtex_field",
        ),
        (
            b"@article{Special-Number={Special",
            "unclosed_bibtex_value",
        ),
        (
            b"<html><body>Login required</body></html>",
            "html_instead_of_bibtex",
        ),
        (
            EMPTY_SPECIAL + b"unexpected trailing response",
            "trailing_bibtex_content",
        ),
    ],
)
def test_parser_rejects_unresolved_or_unsafe_evidence(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(_error_type(), match=code):
        special_issue.parse_kci_bibtex(payload)


def test_parser_rejects_payload_above_explicit_size_bound() -> None:
    with pytest.raises(_error_type(), match="bibtex_too_large"):
        special_issue.parse_kci_bibtex(
            EMPTY_SPECIAL + b" " * 32,
            max_bytes=len(EMPTY_SPECIAL),
        )


def test_load_kci_papers_is_read_only_and_orders_by_database_id(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    _make_database(
        database,
        [
            (
                9,
                "ART000000009",
                "\uc81c\ubaa9 9",
                "\uc81c\ubaa9 9",
                "Title 9",
                None,
                2026,
                "J9",
                "I9",
                "9_1",
            ),
            (
                3,
                "ART000000003",
                "\uc81c\ubaa9 3",
                "\uc81c\ubaa9 3",
                "Title 3",
                "10.1000/ABC",
                2024,
                "J3",
                "I3",
                "3_2",
            ),
        ],
    )

    papers = special_issue.load_kci_papers(database)

    assert [paper.database_id for paper in papers] == [3, 9]
    assert [paper.paper_index for paper in papers] == [1, 2]
    assert [paper.source_id for paper in papers] == [
        "ART000000003",
        "ART000000009",
    ]
    assert papers[0].issue_id == "3_2"


def test_load_kci_papers_rejects_duplicate_and_malformed_art_ids(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.db"
    _make_database(
        duplicate,
        [
            (1, "ART000000001", "A", None, None, None, 2024, None, None, None),
            (2, "ART000000001", "B", None, None, None, 2025, None, None, None),
        ],
    )
    malformed = tmp_path / "malformed.db"
    _make_database(
        malformed,
        [(1, "ART1", "A", None, None, None, 2024, None, None, None)],
    )

    with pytest.raises(Exception, match="duplicate_kci_source_id"):
        special_issue.load_kci_papers(duplicate)
    with pytest.raises(Exception, match="invalid_kci_source_id"):
        special_issue.load_kci_papers(malformed)


def test_build_detail_url_is_exact_and_rejects_untrusted_ids() -> None:
    url = special_issue.build_detail_url("ART002830209")

    assert url == (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
        "ciSereArtiView.kci?"
        "sereArticleSearchBean.artiId=ART002830209"
    )

    for unsafe in (
        "ART002830209&x=1",
        "ART002830209#fragment",
        "art002830209",
        "ART00283020",
        " ART002830209",
    ):
        with pytest.raises((TypeError, ValueError)):
            special_issue.build_detail_url(unsafe)


def test_identity_title_normalization_removes_only_orphaned_terminal_separator() -> None:
    base = "디지털 증거의 관련성 - 대법원 판결을 중심으로"

    assert special_issue._normalize_identity_title(base + " -") == (
        special_issue._normalize_identity_title(base)
    )
    assert special_issue._normalize_identity_title(base + " - 후속 부제") != (
        special_issue._normalize_identity_title(base)
    )
    assert special_issue._normalize_identity_title("A-B") == "a-b"


def _paper(
    index: int = 1,
    *,
    source_id: str | None = None,
    title: str = "시험 논문",
) -> object:
    return special_issue.KciPaper(
        paper_index=index,
        database_id=index,
        source_id=source_id or f"ART{index:09d}",
        title=title,
        title_kor=title,
        title_eng=f"Test paper {index}",
        doi_raw=f"https://doi.org/10.1000/{index}",
        doi=f"10.1000/{index}",
        journal_id="시험학술지",
        institution_id="시험기관",
        issue_id=f"{index}_1",
        publication_year=2026,
    )


def _classified_result(
    paper: object,
    content: bytes,
    *,
    title_hash: str | None = None,
) -> object:
    metadata = special_issue.parse_kci_bibtex(content)
    digest = title_hash or hashlib.sha256(content).hexdigest()
    return special_issue.PaperSpecialIssueResult(
        paper=paper,
        status="classified",
        collected_at=NOW.isoformat(),
        attempts=1,
        metadata=metadata,
        identity_verified=True,
        bibtex_file=f"{paper.source_id}.bib",
        bibtex_sha256=digest,
        bibtex_size=len(content),
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [(EMPTY_SPECIAL, False), (NONEMPTY_SPECIAL, True)],
)
def test_cache_roundtrip_preserves_positive_and_valid_negative_evidence(
    tmp_path: Path,
    content: bytes,
    expected: bool,
) -> None:
    paper = _paper()
    cache_dir = tmp_path / "cache"
    bibtex_dir = tmp_path / "BibTex"
    cache = special_issue.SpecialIssueCache(cache_dir, bibtex_dir)

    saved = cache.save(_classified_result(paper, content), content)
    loaded = cache.load(paper)

    assert saved.bibtex_file == f"{paper.source_id}.bib"
    assert loaded is not None
    assert loaded.paper == paper
    assert loaded.cache_hit is True
    assert loaded.attempts == 0
    assert loaded.is_special_issue is expected
    assert loaded.metadata.special_number_field_present is True
    assert loaded.bibtex_sha256 == hashlib.sha256(
        (bibtex_dir / f"{paper.source_id}.bib").read_bytes()
    ).hexdigest()
    assert stat.S_IMODE((cache_dir / f"{paper.source_id}.json").stat().st_mode) == 0o600


def test_cache_rejects_fingerprint_and_bibtex_hash_mismatches(tmp_path: Path) -> None:
    paper = _paper(title="원래 제목")
    cache = special_issue.SpecialIssueCache(tmp_path / "cache", tmp_path / "BibTex")
    saved = cache.save(_classified_result(paper, EMPTY_SPECIAL), EMPTY_SPECIAL)

    changed_paper = replace(paper, title="수정된 제목")
    assert cache.load(changed_paper) is None

    artifact = tmp_path / "BibTex" / saved.bibtex_file
    artifact.write_bytes(NONEMPTY_SPECIAL)
    assert cache.load(paper) is None


def test_cache_refuses_symlinked_evidence(tmp_path: Path) -> None:
    paper = _paper()
    bibtex_dir = tmp_path / "BibTex"
    cache = special_issue.SpecialIssueCache(tmp_path / "cache", bibtex_dir)
    saved = cache.save(_classified_result(paper, EMPTY_SPECIAL), EMPTY_SPECIAL)
    artifact = bibtex_dir / saved.bibtex_file
    external = tmp_path / "external.bib"
    external.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(external)

    assert cache.load(paper) is None


def test_cache_refuses_to_persist_field_missing_as_negative(tmp_path: Path) -> None:
    paper = _paper()
    missing = special_issue.parse_kci_bibtex(MISSING_SPECIAL)
    result = special_issue.PaperSpecialIssueResult(
        paper=paper,
        status="field_missing",
        collected_at=NOW.isoformat(),
        attempts=1,
        metadata=missing,
        identity_verified=True,
        error_code="special_number_field_missing",
    )
    cache = special_issue.SpecialIssueCache(tmp_path / "cache", tmp_path / "BibTex")

    with pytest.raises(ValueError, match="only classified"):
        cache.save(result, MISSING_SPECIAL)
    assert cache.load(paper) is None


def test_validate_detail_url_accepts_only_one_exact_https_kci_article() -> None:
    source_id = "ART002830209"
    exact = special_issue.build_detail_url(source_id)

    assert special_issue.validate_detail_url(
        exact,
        expected_source_id=source_id,
    ) == exact

    unsafe = (
        exact.replace("https://", "http://"),
        exact.replace("www.kci.go.kr", "evil.example"),
        exact + "&sereArticleSearchBean.artiId=ART000000001",
        exact + "#fragment",
        exact.replace(source_id, "ART000000001"),
    )
    for value in unsafe:
        with pytest.raises(ValueError, match="invalid KCI detail URL"):
            special_issue.validate_detail_url(value, expected_source_id=source_id)


def test_complete_workbook_uses_canonical_path_and_exact_rule(tmp_path: Path) -> None:
    negative_paper = _paper(1, title='=HYPERLINK("https://evil.example")')
    positive_paper = _paper(2)
    results = (
        _classified_result(negative_paper, EMPTY_SPECIAL),
        _classified_result(positive_paper, NONEMPTY_SPECIAL),
    )
    run = special_issue.CollectionRun(
        results=results,
        total_database_papers=2,
        selection_complete=True,
        network_requests=2,
        cache_hits=0,
    )
    output = tmp_path / "special_list.xlsx"

    exported = special_issue.export_special_issue_workbook(
        run,
        output,
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert exported == output
    assert output.is_file()
    assert not (tmp_path / "special_list.partial.xlsx").exists()
    workbook = load_workbook(output, data_only=False)
    try:
        assert workbook.sheetnames == ["Special Issue 목록", "요약", "검증"]
        listing = workbook["Special Issue 목록"]
        headers = [cell.value for cell in listing[1]]
        by_name = {name: index + 1 for index, name in enumerate(headers)}
        assert listing.max_row == 3
        assert [
            listing.cell(row, by_name["source_id"]).value for row in range(2, 4)
        ] == [negative_paper.source_id, positive_paper.source_id]
        assert [
            listing.cell(row, by_name["special_number_field_present"]).value
            for row in range(2, 4)
        ] == [True, True]
        assert [
            listing.cell(row, by_name["is_special_issue"]).value
            for row in range(2, 4)
        ] == [False, True]
        assert [
            listing.cell(row, by_name["special_issue_classification"]).value
            for row in range(2, 4)
        ] == ["Not Special Issue", "Special Issue"]
        assert listing.cell(2, by_name["title"]).data_type == "s"
        assert not any(
            cell.data_type == "f"
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
        )
        summary = {
            row[0].value: row[1].value
            for row in workbook["요약"].iter_rows(min_row=2)
        }
        assert summary["paper_count"] == 2
        assert summary["special_issue_yes"] == 1
        assert summary["not_special_issue_no"] == 1
        assert summary["unresolved_total"] == 0
        assert summary["classification_complete"] is True
        validation = {
            row[0].value: row[3].value
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["classification_rule_consistent"] == "PASS"
        assert validation["canonical_ready"] == "PASS"
    finally:
        workbook.close()


def test_unresolved_workbook_is_partial_and_never_calls_missing_field_no(
    tmp_path: Path,
) -> None:
    classified = _classified_result(_paper(1), EMPTY_SPECIAL)
    missing_metadata = special_issue.parse_kci_bibtex(MISSING_SPECIAL)
    missing = special_issue.PaperSpecialIssueResult(
        paper=_paper(2),
        status="field_missing",
        collected_at=NOW.isoformat(),
        attempts=1,
        metadata=missing_metadata,
        identity_verified=True,
        error_code="special_number_field_missing",
    )
    run = special_issue.CollectionRun(
        results=(classified, missing),
        total_database_papers=2,
        selection_complete=True,
        network_requests=2,
        cache_hits=0,
    )
    canonical = tmp_path / "special_list.xlsx"

    exported = special_issue.export_special_issue_workbook(
        run,
        canonical,
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert exported == tmp_path / "special_list.partial.xlsx"
    assert exported.is_file()
    assert not canonical.exists()
    workbook = load_workbook(exported, data_only=False)
    try:
        listing = workbook["Special Issue 목록"]
        headers = [cell.value for cell in listing[1]]
        by_name = {name: index + 1 for index, name in enumerate(headers)}
        assert listing.cell(3, by_name["special_number_field_present"]).value is False
        assert listing.cell(3, by_name["is_special_issue"]).value is None
        assert listing.cell(3, by_name["special_issue_classification"]).value == "Unresolved"
        summary = {
            row[0].value: row[1].value
            for row in workbook["요약"].iter_rows(min_row=2)
        }
        assert summary["not_special_issue_no"] == 1
        assert summary["field_missing"] == 1
        assert summary["unresolved_total"] == 1
        assert summary["classification_complete"] is False
        validation = {
            row[0].value: row[3].value
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["canonical_ready"] == "FAIL"
    finally:
        workbook.close()


def test_canonical_export_rejects_forged_classification_rule(
    tmp_path: Path,
) -> None:
    """A canonical report must not merely write a FAIL validation row.

    Even an internally constructed result must be rejected when its boolean
    contradicts the one authorized rule: non-empty ``Special-Number`` is Y.
    """

    output = tmp_path / "special_list.xlsx"
    with pytest.raises((ValueError, special_issue.SpecialIssueError), match="classification"):
        forged = special_issue.ParsedBibtex(
            volume="16",
            number="1",
            special_number="Special Issue",
            pages="1-10",
            article_number=None,
            bibtex_date="2026-08-23",
            special_number_field_present=True,
            is_special_issue=False,
        )
        result = special_issue.PaperSpecialIssueResult(
            paper=_paper(),
            status="classified",
            collected_at=NOW.isoformat(),
            attempts=1,
            metadata=forged,
            identity_verified=True,
            bibtex_file="ART000000001.bib",
            bibtex_sha256="0" * 64,
            bibtex_size=1,
        )
        run = special_issue.CollectionRun(
            results=(result,),
            total_database_papers=1,
            selection_complete=True,
            network_requests=0,
            cache_hits=1,
        )
        special_issue.export_special_issue_workbook(
            run,
            output,
            database=tmp_path / "source.db",
            generated_at=NOW,
        )

    assert not output.exists()


def test_cache_survives_a_shift_of_the_row_position(tmp_path: Path) -> None:
    """Removing one paper shifts every later paper_index/database_id; their evidence stays valid."""

    paper = _paper(7)
    cache = special_issue.SpecialIssueCache(tmp_path / "cache", tmp_path / "BibTex")
    cache.save(_classified_result(paper, EMPTY_SPECIAL), EMPTY_SPECIAL)

    shifted = replace(paper, paper_index=5, database_id=3)
    assert shifted.fingerprint == paper.fingerprint
    loaded = cache.load(shifted)
    assert loaded is not None and loaded.cache_hit is True and loaded.paper == shifted

    # Records written by earlier versions carried the positional digest and are still accepted as written.
    record = tmp_path / "cache" / f"{paper.source_id}.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["paper_fingerprint"] = paper.positional_fingerprint
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load(paper) is not None
    assert cache.load(replace(paper, doi="10.1000/other")) is None


class _Field:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    def count(self) -> int:
        return len(self.values)

    def input_value(self) -> str:
        return self.values[0]

    def nth(self, index: int) -> "_Field":
        return _Field([self.values[index]])

    def inner_text(self) -> str:
        return self.values[0]


class _DetailPage:
    """The three identity fields of a KCI detail page; ``states`` is consumed one per (re)load."""

    def __init__(self, source_id: str, states: list[dict[str, list[str]]]) -> None:
        self.url = special_issue.build_detail_url(source_id)
        self.states = states
        self.reloads = 0

    def locator(self, selector: str) -> _Field:
        return _Field(self.states[0].get(selector, []))

    def reload(self, **_kwargs: object) -> object:
        self.reloads += 1
        if len(self.states) > 1:
            self.states.pop(0)
        return type("Response", (), {"status": 200})()


def _session(page: _DetailPage) -> object:
    session = object.__new__(special_issue.PlaywrightKciBibtexSession)
    session.page = page
    session.request_count = 0
    session.config = special_issue.KciBrowserConfig(permission_granted=True, profile_dir=Path("/nonexistent"))
    return session


def test_identity_check_reloads_a_transient_page_and_accepts_a_blank_doi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(special_issue.time, "sleep", lambda _seconds: None)
    paper = _paper(3)
    good = {"input#hdnArtiId": [paper.source_id], "#artiTitle": ["시험 논문", "시험 논문"], "input#hdnDoi": ["10.1000/3"]}

    # A half-rendered answer (no identity fields) is reloaded once and then verified.
    page = _DetailPage(paper.source_id, [{}, good])
    session = _session(page)
    session._validate_detail_identity(paper)
    assert page.reloads == 1 and session.request_count == 1

    # KCI shows no DOI although the API record has one: the article id and the exact title still identify the paper.
    page = _DetailPage(paper.source_id, [{**good, "input#hdnDoi": [""]}])
    _session(page)._validate_detail_identity(paper)
    assert page.reloads == 0

    # A different DOI, a different title or a different article id stays a mismatch even after the reload.
    for wrong in ({**good, "input#hdnDoi": ["10.1000/999"]}, {**good, "#artiTitle": ["다른 논문"]},
                  {**good, "input#hdnArtiId": ["ART000000999"]}):
        page = _DetailPage(paper.source_id, [wrong])
        with pytest.raises(special_issue.KciBrowserError, match="detail_identity_mismatch"):
            _session(page)._validate_detail_identity(paper)
        assert page.reloads == 1


def test_cached_papers_stay_classified_after_the_login_expires(tmp_path: Path) -> None:
    """An expired session stops new fetches only; verified evidence on disk is still reported."""

    first, second, third = _paper(1), _paper(2), _paper(3)
    cache = special_issue.SpecialIssueCache(tmp_path / "cache", tmp_path / "BibTex")
    cache.save(_classified_result(first, EMPTY_SPECIAL), EMPTY_SPECIAL)
    cache.save(_classified_result(third, NONEMPTY_SPECIAL), NONEMPTY_SPECIAL)

    class ExpiredSession:
        request_count = 0

        def fetch(self, paper: object) -> bytes:
            raise special_issue.KciBrowserError("authentication_required")

    run = special_issue.collect_special_issues(
        [first, second, third], ExpiredSession(), cache=cache, total_database_papers=3, selection_complete=True,
        retries=0, sleeper=lambda _seconds: None, now=lambda: NOW,
    )

    assert [(result.status, result.cache_hit, result.error_code) for result in run.results] == [
        ("classified", True, None), ("failed", False, "authentication_required"), ("classified", True, None),
    ]
    assert run.results[2].is_special_issue is True
