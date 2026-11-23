from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from Analyzer.MD.paper_md import (
    ALLOWED_TAGS,
    MISSING,
    PaperMarkdownError,
    Technique,
    CorpusData,
    KeywordRecord,
    assign_record_ids,
    classification_for_techniques,
    generate,
    load_corpus,
    load_target_registry,
    load_technique_taxonomy,
    load_profile_identities,
    save_profile_identities,
    recover_profile_identities,
    validate_templates,
    parse_authors,
    render_document,
    tags_for_keyword,
    target_applications_for_keyword,
)


@pytest.fixture(scope="module")
def corpus() -> CorpusData:
    return load_corpus()


def _by_source_id(corpus: CorpusData):
    return {paper.type_record.source_id: paper for paper in corpus.papers}


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _legacy_source_ids() -> frozenset[str]:
    """The originally reviewed papers that are still in the corpus.

    Numbers are re-sorted whenever the corpus changes (Analyzer/numbering.py),
    so the reviewed set is identified by KCI id, never by ``paper_index``.
    """

    snapshot = REPOSITORY_ROOT / "Analyzer" / "Manifest" / "keyword_intelligence_legacy.json"
    return frozenset(row["source_id"] for row in json.loads(snapshot.read_text(encoding="utf-8"))["rows"])


LEGACY_SOURCE_IDS = _legacy_source_ids()
LEGACY_CORPUS_SIZE = len(LEGACY_SOURCE_IDS)


def _legacy_papers(corpus: CorpusData):
    return [paper for paper in corpus.papers if paper.type_record.source_id in LEGACY_SOURCE_IDS]


def test_corpus_uses_database_metadata_for_all_pdf_papers(
    corpus: CorpusData,
) -> None:
    papers = _by_source_id(corpus)

    # The corpus grows with every monthly increment; the reviewed 527 papers
    # are its floor and every paper carries database metadata.
    assert len(corpus.papers) == len(papers) >= LEGACY_CORPUS_SIZE
    assert corpus.metadata_coverage == len(corpus.papers)
    assert set(corpus.pending_source_ids).isdisjoint(papers)
    assert all(paper.metadata["database_id"] is not None for paper in corpus.papers)
    # This DB title contains a subtitle omitted by the PDF/type index.  Keeping it
    # proves that the stale KCI workbook is not being used as the metadata master.
    assert papers["ART002904713"].metadata["title_kor"].endswith(
        "대법원 2022. 1. 27. 선고 2021도11170 판결"
    )


def test_evidence_workbooks_reconcile_to_expected_corpus_counts(
    corpus: CorpusData,
) -> None:
    papers = _by_source_id(corpus)
    legacy = _legacy_papers(corpus)
    assert len(legacy) == LEGACY_CORPUS_SIZE

    # Evidence workbooks are consumed through explicit statuses only.
    assert corpus.oa_unconfirmed == sum(
        paper.oa_classification == MISSING for paper in corpus.papers
    )
    assert {paper.oa_classification for paper in corpus.papers} <= {
        MISSING, "Open Access", "Not Open Access"
    }
    assert corpus.special_unconfirmed == sum(
        paper.is_special_issue is None for paper in corpus.papers
    )
    # Reviewed verdicts for the legacy corpus are stable across increments.
    assert {
        source_id
        for source_id, paper in papers.items()
        if paper.is_special_issue and source_id in LEGACY_SOURCE_IDS
    } <= {"ART002910949", "ART003038377", "ART003298960"}
    assert sum(len(paper.reference_rows) for paper in legacy) >= 10_000
    # Levels come from the Level result (Jev AI, the jev stage), never from the analysis evidence.
    from Analyzer.MD.paper_md import LEVELS_JSON

    levels = json.loads(LEVELS_JSON.read_text(encoding="utf-8"))["papers"]
    assert all(paper.classification == tuple(levels[source_id]["lv3"]) for source_id, paper in papers.items())
    assert corpus.non_forensic_count == sum(1 for paper in corpus.papers if not paper.classification)


def _targets() -> dict:
    from Analyzer.MD.paper_md import TARGETS_JSON

    return json.loads(TARGETS_JSON.read_text(encoding="utf-8"))["papers"]


def test_known_paper_rendering_uses_only_supported_evidence_tags(
    corpus: CorpusData,
) -> None:
    paper = _by_source_id(corpus)["ART002577216"]
    target = _targets()["ART002577216"]

    # Tags and the application table come from the Jev AI results (the jev stage).
    assert paper.tags == tuple(tag for tag in ALLOWED_TAGS if tag in target["tags"]) and paper.tags
    expected_rows = [
        (item["id"], item["title_ko"]) for application in target["applications"] for item in application["level5"]
    ] + [(None, application["name"]) for application in target["applications"] if not application["level5"]]
    assert {(identifier, kor) for identifier, kor, _eng in paper.target_applications} == set(expected_rows)
    document = render_document(
        paper,
        language="kor",
        updated_on="2026-08-31",
        author="조민혁",
    )
    assert '| DOI | N/A |' in document
    # The database row carries KCI's serial id (SER000003109); the reviewed name table names the journal.
    assert '| 논문지 | 디지털포렌식연구 |' in document and 'SER000003109' not in document
    assert 'identifier: "KNOW-DOMP-0000001"' in document
    assert "classification:\n  - EXAM-04-01 # 애플리케이션 아티팩트 조사\n" in document
    assert "tags:\n" + "".join(f"  - {tag}\n" for tag in paper.tags) in document
    assert "## 1. 기본 정보" in document
    assert "| 권호 | 14권 1호 |" in document
    assert "| OA 여부 | X |" in document
    assert "| 저자 | 남기욱, 김동현, 최지성, 이상진 |" in document
    table = "".join(f"| {identifier or ''} | {kor} |\n" for identifier, kor, _eng in paper.target_applications)
    assert "## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n" + table in document
    assert "| EXAM-04-01-07-05 | Dropbox |" in document           # Dropbox's application artifacts (Windows app, local DB files)
    assert "## 5. 참고문헌\n| ID | REF |\n|---|---|\n" in document
    assert "관련 기술" not in document
    assert not any(token in document for token in ("{{", "<!--", "키워드1"))

    special = _by_source_id(corpus)["ART002910949"]
    special_document = render_document(
        special,
        language="eng",
        updated_on="2026-08-31",
        author="Jo Minhyuk",
    )
    assert "| Type | Special Issue |" in special_document


def test_tag_mapping_does_not_invent_tags_for_unsupported_leaf_values() -> None:
    record = KeywordRecord(
        doi=None,
        title="test",
        # None of these leaf values exist in the 260909 vocabulary: the DEV axis
        # covers only PC and mobile, and the other three are simply unknown.
        target_device="CCTV",
        target_os="Symbian",
        target_app="Easy Voice Recorder",
        target_app_category="존재하지 않는 분류",
        filesystem="ZFS",
        cloud="Azure",
    )

    # Every axis is closed, so an unmapped leaf yields no tag at all, and
    # ``target_app`` is not a tag axis any more: it never invents one.
    assert tags_for_keyword(record) == ()
    assert set(tags_for_keyword(record)) <= set(ALLOWED_TAGS)
    # A SaaS deployment without a category that has a SaaS flavour stays untagged.
    assert tags_for_keyword(replace(record, cloud="SaaS")) == ()
    assert tags_for_keyword(
        replace(record, cloud="SaaS", target_app_category="클라우드 스토리지")
    ) == ("APP-CST", "CLOUD-SaaS:CST")


def test_expanded_axes_now_cover_values_the_old_vocabulary_dropped() -> None:
    record = KeywordRecord(
        doi=None,
        title="test",
        target_device=None,
        target_os="QNX, Wear OS",
        target_app=None,
        target_app_category="멀티미디어 재생 및 편집 | 가상자산 지갑",
        filesystem="F2FS",
        cloud=None,
    )

    # Uncommon operating systems land on OS-ETC, a recorded file system maps to
    # its storage family, and every workbook application category has a code.
    assert tags_for_keyword(record) == ("FS-FLH", "OS-ETC", "APP-MUL", "APP-CWA")


def test_classification_maps_workbook_titles_and_drops_withdrawn_ones() -> None:
    taxonomy = load_technique_taxonomy()

    def technique(rank: int, level_3: str) -> Technique:
        return Technique(
            rank=rank, level_2="", level_3=level_3, level_2_en="", level_3_en=""
        )

    # Exact title, a title the taxonomy renamed, and a duplicate that collapses.
    assert classification_for_techniques(
        (
            technique(1, "애플리케이션 아티팩트 조사"),
            technique(2, "안티포렌식 행위 지표 식별"),
            technique(3, "애플리케이션 아티팩트 조사"),
        ),
        taxonomy,
        "TEST",
    ) == ("EXAM-04-01", "CMNF-01-04")

    # ``증거 생애주기 관리`` has no counterpart in the current taxonomy, so it is
    # dropped rather than reassigned; a paper left with nothing renders ``[]``.
    assert classification_for_techniques(
        (technique(1, "증거 생애주기 관리"),), taxonomy, "TEST"
    ) == ()

    with pytest.raises(PaperMarkdownError):
        classification_for_techniques(
            (technique(1, "존재하지 않는 기술"),), taxonomy, "TEST"
        )


def _registry(root: Path) -> Path:
    """A registry in DF-Insight's current layout (``TAGT-`` ids, one folder per target)."""

    entries = {
        "TAGT-0000001": ("Google Chrome", "Google Chrome", "OS-WIN"),
        "TAGT-0000002": ("Google Chrome", "Google Chrome", "OS-AND"),
        "TAGT-0000003": ("카카오톡", "KakaoTalk", "OS-WIN"),
        "TAGT-0000004": ("카카오톡", "KakaoTalk", "OS-AND"),
        "TAGT-0000005": ("Gmail", "Gmail", None),
    }
    for identifier, (kor, eng, operating_system) in entries.items():
        (root / identifier).mkdir(parents=True)
        tags = f"tags:\n  - {operating_system}\n" if operating_system else "tags: []\n"
        (root / identifier / "ko.md").write_text(f'---\nsubject: "{kor}"\nidentifier: "{identifier}"\n{tags}---\n# {kor}\n', encoding="utf-8")
        (root / identifier / "en.md").write_text(f'---\nsubject: "{eng}"\nidentifier: "{identifier}"\n{tags}---\n# {eng}\n', encoding="utf-8")
    return root


def test_target_application_id_requires_a_platform_the_paper_studies(tmp_path: Path) -> None:
    registry = load_target_registry(_registry(tmp_path / "targets_md"))

    def keyword(target_app: str, target_os: str | None) -> KeywordRecord:
        return KeywordRecord(
            doi=None,
            title="test",
            target_device=None,
            target_os=target_os,
            target_app=target_app,
            target_app_category=None,
            filesystem=None,
            cloud=None,
        )

    # A registry entry is platform-specific, so Chrome on Windows resolves and
    # the same name on Linux does not: naming the Windows target there would
    # assert a platform the paper never studied.
    assert target_applications_for_keyword(keyword("Google Chrome", "Windows"), registry) == (("TAGT-0000001", "Google Chrome"),)
    assert target_applications_for_keyword(keyword("Google Chrome", "Linux"), registry) == ((None, "Google Chrome"),)
    # KakaoTalk is registered for two platforms; a paper covering both cannot be
    # narrowed to one entry, and an unregistered name stays blank.
    assert target_applications_for_keyword(keyword("KakaoTalk", "Windows, Android"), registry) == ((None, "KakaoTalk"),)
    assert target_applications_for_keyword(keyword("TeraBox, Gmail", "Android"), registry) == ((None, "TeraBox"), ("TAGT-0000005", "Gmail"))
    with pytest.raises(PaperMarkdownError, match="unexpected target id"):
        (tmp_path / "bad" / "APP-1").mkdir(parents=True)
        load_target_registry(tmp_path / "bad")


def test_generated_profiles_carry_registered_level5_ids(corpus: CorpusData) -> None:
    """Registered applications carry DF-Insight Level-5 ids (one row per id); unregistered ones a blank id."""

    from Analyzer.MD.paper_md import LEVEL5_ID_RE, _level5_registry_ids

    registry = _level5_registry_ids()
    rows = [row for paper in corpus.papers for row in paper.target_applications]
    assert any(identifier for identifier, _kor, _eng in rows) and any(identifier is None for identifier, _kor, _eng in rows)
    assert all(LEVEL5_ID_RE.fullmatch(identifier) and identifier in registry for identifier, _kor, _eng in rows if identifier)
    for paper in corpus.papers:
        identifiers = [identifier for identifier, _kor, _eng in paper.target_applications if identifier]
        assert len(identifiers) == len(set(identifiers))
        # An unregistered application keeps the same name in both languages.
        assert all(kor == eng for identifier, kor, eng in paper.target_applications if identifier is None)


def test_targets_must_be_final_and_name_registered_nodes(tmp_path: Path) -> None:
    from Analyzer.MD.paper_md import _load_targets

    path = tmp_path / "targets.json"

    def write(record: dict) -> None:
        path.write_text(json.dumps({"papers": {"ART000000001": record}}, ensure_ascii=False), encoding="utf-8")

    # DF-Insight's Level-5 ids end in two digits since 2026-10 (EXAM-04-01-02-03 = Discord).
    node = {"id": "EXAM-04-01-02-03", "title_ko": "Discord", "title_en": "Discord"}
    write({"status": "ok", "tags": ["APP-MES", "OS-AND"], "applications": [
        {"name": "Discord", "level5": [node]}, {"name": "카카오맵 X", "level5": []}]})
    tags, rows = _load_targets(["ART000000001"], path)["ART000000001"]
    assert tags == ("OS-AND", "APP-MES")                                   # ALLOWED_TAGS order
    assert rows == (("EXAM-04-01-02-03", "Discord", "Discord"), (None, "카카오맵 X", "카카오맵 X"))

    write({"status": "pending", "tags": [], "applications": []})
    with pytest.raises(PaperMarkdownError, match="not final"):
        _load_targets(["ART000000001"], path)                              # "not extracted yet" is never "no applications"
    write({"status": "ok", "tags": [], "applications": [{"name": "Discord", "level5": [{**node, "id": "EXAM-04-01-02-3"}]}]})
    with pytest.raises(PaperMarkdownError, match="not a Level-5 id"):
        _load_targets(["ART000000001"], path)
    for stale in ("EXAM-04-01-02-003", "EXAM-04-01-02-99"):                 # the old 3-digit form, an id DF-Insight does not have
        write({"status": "ok", "tags": [], "applications": [{"name": "Discord", "level5": [{**node, "id": stale}]}]})
        with pytest.raises(PaperMarkdownError, match="not a DF-Insight node"):
            _load_targets(["ART000000001"], path)
    write({"status": "ok", "tags": [], "applications": [{"name": "Discord", "level5": [node, node]}]})
    with pytest.raises(PaperMarkdownError, match="appears twice"):
        _load_targets(["ART000000001"], path)
    write({"status": "ok", "tags": ["APP-XYZ"], "applications": []})
    with pytest.raises(PaperMarkdownError, match="invalid tags"):
        _load_targets(["ART000000001"], path)
    with pytest.raises(PaperMarkdownError, match="missing"):
        _load_targets(["ART000000002"], path)


def test_record_ids_number_the_corpus_by_ascending_source_id() -> None:
    assigned = assign_record_ids(
        ["ART003064124", "ART002577216", "ART002910949"]
    )

    assert assigned == {
        "ART002577216": "KNOW-DOMP-0000001",
        "ART002910949": "KNOW-DOMP-0000002",
        "ART003064124": "KNOW-DOMP-0000003",
    }


def test_record_ids_are_not_renumbered_when_older_papers_arrive() -> None:
    reserved = {
        "ART002910949": "KNOW-DOMP-0000010",
        "ART003064124": "KNOW-DOMP-0000011",
    }
    assert assign_record_ids(["ART002577216", "ART002910949"], reserved) == {
        "ART002577216": "KNOW-DOMP-0000012",
        "ART002910949": "KNOW-DOMP-0000010",
    }
    assert reserved["ART002910949"] == "KNOW-DOMP-0000010"
    with pytest.raises(PaperMarkdownError, match="duplicate"):
        assign_record_ids([], {key: "KNOW-DOMP-0000001" for key in reserved})
    with pytest.raises(PaperMarkdownError, match="invalid"):
        assign_record_ids([], {"ART002910949": "KNOW-DOMP-0000000"})


def test_identity_index_roundtrip_and_reassignment_guard(tmp_path: Path, corpus: CorpusData) -> None:
    index = tmp_path / "paper_profiles.json"
    save_profile_identities(corpus, index)
    expected = {p.type_record.source_id: p.record_id for p in corpus.papers}
    assert load_profile_identities(index) == expected
    save_profile_identities(corpus, index)
    changed = replace(corpus, papers=(replace(corpus.papers[0], record_id="KNOW-DOMP-0009999"),))
    with pytest.raises(PaperMarkdownError, match="reassign"):
        save_profile_identities(changed, index)
    assert load_profile_identities(index) == expected


def test_templates_and_taxonomy_have_exact_bilingual_coverage() -> None:
    taxonomy = load_technique_taxonomy()
    validate_templates(taxonomy)
    assert len(taxonomy.identifiers) == 79
    assert set(taxonomy.title_kor) == set(taxonomy.title_eng)
    assert all(re.fullmatch(r"[A-Z]{4}-\d{2}-\d{2}", key) for key in taxonomy.identifiers)


def test_lost_index_recovers_bilingual_published_identity_without_renumbering(tmp_path: Path) -> None:
    directory = tmp_path / "KNOW-DOMP-0000050"
    directory.mkdir()
    content = ('---\nidentifier: "KNOW-DOMP-0000050"\n---\n'
               '| URL | https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART002910949 |\n')
    for filename in ("ko.md", "en.md"):
        (directory / filename).write_text(content, encoding="utf-8")
    recovered = recover_profile_identities(tmp_path)
    assert recovered == {"ART002910949": "KNOW-DOMP-0000050"}
    assert assign_record_ids(["ART002577216", "ART002910949"], recovered)["ART002577216"] == "KNOW-DOMP-0000051"
    (directory / "en.md").write_text(content.replace("ART002910949", "ART002577216"), encoding="utf-8")
    with pytest.raises(PaperMarkdownError, match="bilingual"):
        recover_profile_identities(tmp_path)


def test_reference_ids_resolve_only_to_papers_inside_this_corpus(
    corpus: CorpusData,
) -> None:
    papers = _by_source_id(corpus)
    record_ids = {paper.record_id for paper in corpus.papers}

    resolved = {
        record_id
        for paper in corpus.papers
        for record_id in paper.reference_record_ids.values()
    }
    assert resolved
    assert resolved <= record_ids

    citing = papers["ART002995250"]
    orders = set(citing.reference_record_ids)
    assert orders <= {
        int(row["reference_order"]) for row in citing.reference_rows
    }
    for row in citing.reference_rows:
        order = int(row["reference_order"])
        cited = row.get("referenced_kci_source_id")
        expected = papers[cited].record_id if cited in papers else None
        assert citing.reference_record_ids.get(order) == expected


def test_author_parser_preserves_nested_affiliation_and_marks_malformed_value() -> None:
    names, affiliations = parse_authors(
        ["홍길동(대학교(대학원))", "김영희", "박지후(동국대학교("]
    )

    assert names == "홍길동, 김영희, 박지후"
    assert affiliations.splitlines() == [
        "홍길동: 대학교(대학원)",
        "김영희: N/A",
        "박지후: N/A",
    ]


def test_generate_and_validate_all_bilingual_documents(
    tmp_path: Path,
) -> None:
    output = tmp_path / "MD"
    summary = generate(output, updated_on="2026-08-31")

    assert summary.paper_count >= LEGACY_CORPUS_SIZE
    assert summary.markdown_count == 2 * summary.paper_count
    assert len(list(output.glob("KNOW-DOMP-*/ko.md"))) == summary.paper_count
    assert len(list(output.glob("KNOW-DOMP-*/en.md"))) == summary.paper_count
    assert not (output / "kor").exists()
    assert not (output / "eng").exists()
    assert not list(output.rglob(".*.tmp"))
    assert generate(
        output, updated_on="2026-08-31", validate_only=True
    ) == summary

    for path in output.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert re.search(r'^identifier: "KNOW-DOMP-[0-9]{7}"$', text, re.MULTILINE)
        assert json.loads(
            re.search(r'^identifier: ("[^"]+")$', text, re.MULTILINE).group(1)
        ) == path.parent.name
        assert path.name in {"ko.md", "en.md"}
        expected_author = "조민혁" if path.name == "ko.md" else "Jo Minhyuk"
        assert f'author: "{expected_author}"' in text
        assert "OpenAI Codex" not in text


def test_generation_never_deletes_unexpected_legacy_documents(tmp_path: Path) -> None:
    output = tmp_path / "MD"
    legacy = output / "kor" / "REP-DP-000001.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("user-owned content", encoding="utf-8")
    with pytest.raises(PaperMarkdownError, match="unexpected Markdown"):
        generate(output, updated_on="2026-09-14")
    assert legacy.read_text(encoding="utf-8") == "user-owned content"


def test_levels_come_from_the_level_stage_result(tmp_path: Path) -> None:
    from Analyzer.MD.paper_md import _load_levels, load_technique_taxonomy

    taxonomy = load_technique_taxonomy()
    known = taxonomy.identifiers[0]
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"papers": {"ART000000001": {"lv3": [known]}, "ART000000002": {"lv3": []}}}), encoding="utf-8")

    levels, without_level = _load_levels(["ART000000001", "ART000000002"], taxonomy, path)
    assert levels == {"ART000000001": (known,), "ART000000002": ()} and without_level == 1

    with pytest.raises(PaperMarkdownError, match="run the Level stage first"):
        _load_levels(["ART000000003"], taxonomy, path)
    path.write_text(json.dumps({"papers": {"ART000000001": {"status": "pending", "lv3": []}}}), encoding="utf-8")
    with pytest.raises(PaperMarkdownError, match="not finished"):
        _load_levels(["ART000000001"], taxonomy, path)  # "not classified yet" must never be published as "no Level"
    path.write_text(json.dumps({"papers": {"ART000000001": {"lv3": ["XXXX-99-99"]}}}), encoding="utf-8")
    with pytest.raises(PaperMarkdownError, match="invalid Level-3 ids"):
        _load_levels(["ART000000001"], taxonomy, path)


def test_a_new_paper_takes_the_number_of_its_paper_index_when_it_is_free() -> None:
    from Analyzer.MD.paper_md import assign_record_ids

    existing = {"ART000000001": "KNOW-DOMP-0000001", "ART000000005": "KNOW-DOMP-0000007"}
    assigned = assign_record_ids(
        ["ART000000001", "ART000000002", "ART000000003", "ART000000004"], existing,
        preferred_numbers={"ART000000002": 9, "ART000000003": 7, "ART000000004": 8},
    )
    assert assigned["ART000000001"] == "KNOW-DOMP-0000001"          # never renumbered
    assert assigned["ART000000002"] == "KNOW-DOMP-0000009"          # its own paper_index
    assert assigned["ART000000004"] == "KNOW-DOMP-0000008"
    assert assigned["ART000000003"] == "KNOW-DOMP-0000010"          # 7 belongs to another paper -> next unused
    assert len(set(assigned.values())) == len(assigned)


def test_reference_cell_uses_the_kci_value_for_cdata_and_structured_references() -> None:
    from Analyzer.MD.paper_md import format_reference

    cdata_row = {
        "reference_order": 1,
        "reference_citation_text": "Agrawal, N.M. 2000. Superiority of lansoprazole. Archives 160: 1455–1461.",
        "reference_title": None,
        "reference_author": None,
    }
    # A CDATA-only reference is rendered exactly as KCI delivered it.
    assert format_reference(cdata_row, "kor") == cdata_row["reference_citation_text"]
    assert format_reference(cdata_row, "eng") == cdata_row["reference_citation_text"]
    structured = {
        "reference_order": 2,
        "reference_citation_text": None,
        "reference_author": "정일훈 ; 이상진",
        "reference_title": "IaaS 디지털 포렌식",
        "reference_journal_name": "정보보호학회논문지",
        "reference_volume": "21",
        "reference_issue": "6",
        "reference_pages": "55-66",
        "reference_publication_year": "2011",
    }
    # KCI separates authors with " ; "; the profile lists them with ", ".
    assert format_reference(structured, "kor") == "정일훈, 이상진. IaaS 디지털 포렌식. 정보보호학회논문지. 권 21. 호 6. 쪽 55-66. 2011."
    assert format_reference(structured, "eng") == "정일훈, 이상진. IaaS 디지털 포렌식. 정보보호학회논문지. Vol. 21. No. 6. pp. 55-66. 2011."
    assert format_reference({**structured, "reference_author": "Smith, J.;  ; Doe, A. "}, "eng").startswith("Smith, J., Doe, A.. IaaS")
    assert format_reference({**structured, "reference_author": " ; "}, "eng").startswith("IaaS 디지털 포렌식.")


def test_references_are_loaded_from_the_verbatim_reference_info_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from Analyzer.MD import paper_md

    xml_dir = tmp_path / "api-response" / "referenceInfo"
    xml_dir.mkdir(parents=True)
    segment = (
        b'<referenceInfo>\r\n        <reference refebibl-id="REF1" type-name="\xed\x95\x99\xec\x88\xa0\xec\xa7\x80">'
        b"<![CDATA[Weaver, A.L. 2002. Ulcer prevention. Archives 162: 2248\xe2\x80\x932249.]]></reference>\r\n"
        b'        <reference refebibl-id="REF2" arti-id="ART000000002"><title>Structured</title><author>Kim</author></reference>\r\n'
        b"      </referenceInfo>"
    )
    (xml_dir / "ART000000001.xml").write_bytes(segment)
    manifest = tmp_path / "api-response" / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "xml_directory": "referenceInfo",
        "papers": [
            {"source_id": "ART000000001", "status": "collected", "reference_count": 2,
             "file": "referenceInfo/ART000000001.xml", "sha256": hashlib.sha256(segment).hexdigest()},
            {"source_id": "ART000000002", "status": "no_reference_data", "reference_count": 0, "file": None},
            {"source_id": "ART000000003", "status": "failed", "reference_count": None, "file": None},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(paper_md, "REFERENCE_MANIFEST", manifest)

    rows, unavailable = paper_md._load_references(["ART000000001", "ART000000002", "ART000000003"])

    assert unavailable == 2
    assert rows["ART000000002"] == () and rows["ART000000003"] == ()
    first, second = rows["ART000000001"]
    assert first["reference_citation_text"] == "Weaver, A.L. 2002. Ulcer prevention. Archives 162: 2248–2249."
    assert second["reference_title"] == "Structured" and second["referenced_kci_source_id"] == "ART000000002"
    assert [row["reference_order"] for row in rows["ART000000001"]] == [1, 2]
    with pytest.raises(PaperMarkdownError, match="no entry"):
        paper_md._load_references(["ART000000009"])
    # An edited file no longer matches the manifest digest.
    (xml_dir / "ART000000001.xml").write_bytes(segment.replace(b"Weaver", b"Weaver!"))
    with pytest.raises(PaperMarkdownError, match="digest"):
        paper_md._load_references(["ART000000001"])


def test_a_paper_without_a_type_decision_gets_its_profile_once_its_classification_is_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The type comes from the analysis stage; the Jev AI classification does not wait for it."""

    import hashlib

    from Analyzer.MD import paper_md
    from PaperPipeline.paper_files import PaperFileRecord, paper_file_index_document

    papers_dir = tmp_path / "Paper"
    papers_dir.mkdir()
    records = [PaperFileRecord(f"ART00000000{number}", f"논문 {number}", f"논문 {number}.pdf") for number in range(1, 6)]
    for record in records[:4]:
        (papers_dir / record.filename).write_bytes(b"%PDF-1.4 " + record.source_id.encode())   # paper 5: no PDF on disk yet
    index = tmp_path / "paper_files.json"
    index.write_text(json.dumps(paper_file_index_document(records), ensure_ascii=False), encoding="utf-8")
    known = load_technique_taxonomy().identifiers[0]
    levels = {record.source_id: {"status": "classified", "lv3": [known]} for record in records}
    levels["ART000000002"] = {"status": "pending", "lv3": []}       # no Jev result yet
    levels["ART000000003"] = {"status": "classified", "lv3": []}    # final, with no Level-3 at or above the threshold
    targets = {source_id: {"status": "pending" if source_id == "ART000000002" else "ok", "tags": [], "applications": []} for source_id in levels}
    (tmp_path / "levels.json").write_text(json.dumps({"papers": levels}), encoding="utf-8")
    (tmp_path / "targets.json").write_text(json.dumps({"papers": targets}), encoding="utf-8")
    for name, value in (("PAPER_INDEX", index), ("PAPER_DIRECTORY", papers_dir),
                        ("LEVELS_JSON", tmp_path / "levels.json"), ("TARGETS_JSON", tmp_path / "targets.json")):
        monkeypatch.setattr(paper_md, name, value)
    identities = {f"ART00000000{number}": f"KNOW-DOMP-000000{number}" for number in (1, 2, 3, 5)}   # 4: not numbered yet

    untyped = paper_md._untyped_papers([record.source_id for record in records], identities)
    # 2: classification not final; 4: no number yet (the renumber stage first); 5: its PDF is not on disk.
    assert [record.source_id for record in untyped] == ["ART000000001", "ART000000003"]
    first = untyped[0]
    assert (first.subtype, first.subtype_des, first.doi, first.paper_index) == (None, None, None, 0)
    assert first.pdf_filename == "논문 1.pdf" and first.pdf_sha256 == hashlib.sha256(b"%PDF-1.4 ART000000001").hexdigest()
    assert paper_md._no_keywords(first).doi is None and paper_md._no_keywords(first).target_app is None
    assert paper_md._untyped_papers([], identities) == ()


def test_a_profile_without_a_type_decision_shows_na_never_a_guessed_type() -> None:
    from Analyzer.MD import paper_md

    record = paper_md.TypeRecord(0, "ART000000001", "제목", "제목", "제목", None, None, None, "제목.pdf", "0" * 64)
    paper = paper_md.PaperData(
        record_id="KNOW-DOMP-0000001", type_record=record, keyword=paper_md._no_keywords(record),
        metadata={"title_kor": "제목", "title_eng": "Title", "url": "https://www.kci.go.kr/x?sereArticleSearchBean.artiId=ART000000001"},
        is_special_issue=False, oa_classification="Open Access", reference_rows=(), techniques=(), reference_record_ids={},
        classification=(), target_applications=(), tags=(),
    )
    assert "| 유형 | N/A |" in render_document(paper, language="kor", updated_on="2026-10-26", author="조민혁")
    assert "| Type | N/A |" in render_document(paper, language="eng", updated_on="2026-10-26", author="Jo Minhyuk")
    # A Special-Issue verdict comes from KCI, not from the analysis; a type decision shows as before.
    assert "| 유형 | Special Issue |" in render_document(replace(paper, is_special_issue=True), language="kor", updated_on="2026-10-26", author="조민혁")
    typed = replace(paper, type_record=replace(record, subtype="ar", subtype_des="Article"))
    assert "| 유형 | Article |" in render_document(typed, language="kor", updated_on="2026-10-26", author="조민혁")


def test_a_typed_paper_without_its_jev_result_waits_unless_it_already_has_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jev AI results arrive after the analysis: a new paper waits for them, the rest of the corpus is still profiled."""

    from Analyzer.MD import paper_md

    known = load_technique_taxonomy().identifiers[0]
    levels = {
        "ART000000001": {"status": "classified", "lv3": [known]},     # final
        "ART000000002": {"status": "pending", "lv3": []},             # no Jev result yet, no profile: waits
        "ART000000003": {"status": "pending", "lv3": []},             # no Jev result, but already published: stays (and stops the run)
        "ART000000005": {"status": "classified", "lv3": []},          # final although its targets are not
    }
    targets = {source_id: {"status": "ok", "tags": [], "applications": []} for source_id in levels}
    targets["ART000000002"]["status"] = targets["ART000000003"]["status"] = targets["ART000000005"]["status"] = "pending"
    (tmp_path / "levels.json").write_text(json.dumps({"papers": levels}), encoding="utf-8")
    (tmp_path / "targets.json").write_text(json.dumps({"papers": targets}), encoding="utf-8")
    monkeypatch.setattr(paper_md, "LEVELS_JSON", tmp_path / "levels.json")
    monkeypatch.setattr(paper_md, "TARGETS_JSON", tmp_path / "targets.json")
    records = [paper_md.TypeRecord(number, f"ART00000000{number}", "제목", "제목", "Title", None, "ar", "Article", f"{number}.pdf", "0" * 64)
               for number in range(1, 6)]                                # 4: no record at all (the jev stage has not seen it)
    identities = {f"ART00000000{number}": f"KNOW-DOMP-000000{number}" for number in range(1, 6)}
    output = tmp_path / "MD"
    (output / "KNOW-DOMP-0000003").mkdir(parents=True)

    kept, waiting = paper_md._typed_papers_waiting_for_jev(records, identities, output)
    assert [record.source_id for record in kept] == ["ART000000001", "ART000000003"]
    assert waiting == ("ART000000002", "ART000000004", "ART000000005")
    assert paper_md._typed_papers_waiting_for_jev((), identities, output) == ((), ())
    # The published paper that reached the loaders stops the run with what to do.
    with pytest.raises(PaperMarkdownError, match="already has a profile"):
        paper_md._load_levels(["ART000000003"], load_technique_taxonomy(), tmp_path / "levels.json")
    with pytest.raises(PaperMarkdownError, match="already have a profile"):
        paper_md._load_targets(["ART000000003"], tmp_path / "targets.json")
