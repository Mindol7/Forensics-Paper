from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.Application import level5
from Analyzer.Application.extraction import ExtractionError, normalize_extraction, response_schema
from Analyzer.Application.level5 import App, Level5Error, PaperTargets, normalize_mapping
from Analyzer.Application.registry import Level5Node, load_registry, name_key, squash_key
from Analyzer.technique_taxonomy import load_vocabulary


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def test_names_match_regardless_of_case_width_spacing_and_language(registry) -> None:
    assert name_key("ＫaKaoTalk ") == name_key("kakaotalk") and squash_key("Kakao Talk") == squash_key("KakaoTalk")
    for surface in ("KakaoTalk", "KaKaoTalk", "kakao talk", "카카오톡", "카톡"):
        match = registry.match([surface])
        assert match is not None and match.product == registry.match(["KakaoTalk"]).product, surface
    assert registry.match(["카카오맵"]).method == "pipeline_alias"           # Korean name -> KakaoMap (reviewed list)
    assert registry.match(["MP3 Cutter &amp; Bell Sound Maker"]) is not None  # escaped text in a profile cell
    assert registry.match(["Notes app of some vendor"]) is None              # no substring or fuzzy matching
    assert registry.match(["NTFS"]) is None and registry.match(["Windows 10"]) is None   # not applications


def test_a_product_has_all_its_nodes_and_duplicate_spellings_share_them(registry) -> None:
    discord = registry.candidates(registry.match(["Discord"]).product)
    ids = {node.id for node in discord}
    assert {"COLL-02-04-01-002", "EXAM-04-01-02-003"} <= ids                  # the two nodes of the user's example (3-digit ids)
    assert all(node.title_en == "Discord" for node in discord)
    assert {node.level_3_id for node in discord} >= {"COLL-02-04", "EXAM-04-01"}
    node = next(node for node in discord if node.id == "COLL-02-04-01-002")
    assert (node.level_4_id, node.level_3_id, node.level_2_id, node.level_1_id) == ("COLL-02-04-01", "COLL-02-04", "COLL-02", "COLL")
    assert registry.match(["Chrome"]).product == registry.match(["Google Chrome"]).product
    assert {"EXAM-04-01-01-001", "EXAM-04-01-01-009"} <= {node.id for node in registry.candidates(registry.match(["Chrome"]).product)}


def _extraction(apps: list[dict], **tags: list[dict]) -> dict:
    return {
        "research_targets_ko": "대상", "no_application_reason_ko": "",
        "tags": {name: tags.get(name, []) for name in ("target_device", "target_filesystem", "target_os", "target_app_category", "target_cloud")},
        "target_applications": apps,
    }


def _app(name: str, categories: list[str], cloud: list[str] | None = None, names: list[str] | None = None) -> dict:
    return {"name": name, "names_in_paper": names or [name], "app_categories": categories, "cloud_categories": cloud or [],
            "platforms": ["OS-AND"], "studied_aspects_ko": "DB 분석", "evidence_quote": "본문 근거 문장입니다 열자 이상"}


def test_application_categories_follow_the_named_applications() -> None:
    vocabulary = load_vocabulary()
    payload = _extraction(
        [_app("KakaoTalk", ["APP-MES"], ["CLOUD-SaaS:MES"], ["카카오톡"]), _app("kakaotalk", ["APP-SHA"], names=["KakaoTalk"])],
        target_app_category=[{"id": "APP-WEB", "reason_ko": "r", "evidence_quote": "q"}],
        target_os=[{"id": "OS-AND", "reason_ko": "r", "evidence_quote": "q"}],
    )
    result = normalize_extraction(payload, vocabulary, "본문 근거 문장입니다 열자 이상")
    assert [app["name"] for app in result["target_applications"]] == ["KakaoTalk"]      # same product twice -> merged
    assert result["target_applications"][0]["app_categories"] == ["APP-MES", "APP-SHA"]
    # APP-WEB had no named application: dropped; the applications' categories and cloud tags are added.
    assert result["tag_ids"] == ["OS-AND", "APP-MES", "APP-SHA", "CLOUD-SaaS:MES"]
    assert any("APP-WEB" in note for note in result["adjustments"])
    with pytest.raises(ExtractionError, match="unknown"):
        normalize_extraction(_extraction([], target_os=[{"id": "OS-XYZ", "reason_ko": "", "evidence_quote": ""}]), vocabulary, "")
    schema = response_schema(vocabulary)
    assert schema["properties"]["tags"]["properties"]["target_cloud"]["items"]["properties"]["id"]["enum"][0] == "CLOUD-SaaS:MES"


def _node(identifier: str, title: str) -> Level5Node:
    return Level5Node(
        id=identifier, title_ko=title, title_en=title, level_4_id=identifier.rsplit("-", 1)[0], level_4_ko="메신저", level_4_en="Messengers",
        level_3_id=identifier[:10], level_3_ko="애플리케이션 아티팩트 조사", level_3_en="Application Artifact Examination",
        level_3_description_ko="", level_3_description_en="", level_2_id=identifier[:7], level_2_ko="g", level_2_en="g",
        level_1_id=identifier[:4], level_1_ko="조사", level_1_en="Examination",
    )


def _choice(identifier: str, fit: str = "정확히 부합") -> dict:
    return {"id": identifier, "fit": fit, "reason_ko": "근거", "evidence_quote": "본문 근거 문장입니다 열자 이상"}


def test_mapping_keeps_only_candidates_and_one_node_per_duplicate_registration() -> None:
    apps = {"A1": App("Chrome", ["Chrome"], ["APP-WEB"], [], ["OS-WIN"], "", "", product="Google Chrome")}
    candidates = {"A1": [_node("EXAM-03-03-01-010", "Google Chrome"), _node("EXAM-04-01-01-001", "Chrome"), _node("EXAM-04-01-01-009", "Google Chrome")]}
    answer = normalize_mapping({"applications": [{
        "app_id": "A1", "identity_confirmed": True, "identity_reason_ko": "같은 제품", "not_selected_summary_ko": "",
        "selections": [_choice("EXAM-04-01-01-009"), _choice("EXAM-04-01-01-001"), _choice("EXAM-03-03-01-010", "대체로 부합")],
    }]}, apps, candidates, "본문 근거 문장입니다 열자 이상")["A1"]
    # Same Level-4 under two spellings: the node named as in the paper ("Chrome") stays.
    assert [item["id"] for item in answer["selections"]] == ["EXAM-03-03-01-010", "EXAM-04-01-01-001"]
    assert answer["dropped_duplicates"] == ["EXAM-04-01-01-009"]

    with pytest.raises(Level5Error, match="not a candidate"):
        normalize_mapping({"applications": [{"app_id": "A1", "identity_confirmed": True, "identity_reason_ko": "", "not_selected_summary_ko": "",
                                             "selections": [_choice("COLL-02-04-01-002")]}]}, apps, candidates, "")
    # The same product, but no node fits what the paper did: kept blank, never forced onto a node.
    no_fit = normalize_mapping({"applications": [{"app_id": "A1", "identity_confirmed": True, "identity_reason_ko": "", "not_selected_summary_ko": "파일시스템 메타데이터만 분석",
                                                  "selections": []}]}, apps, candidates, "")["A1"]
    assert no_fit["identity_confirmed"] and no_fit["selections"] == []
    mismatch = normalize_mapping({"applications": [{"app_id": "A1", "identity_confirmed": False, "identity_reason_ko": "다른 제품",
                                                    "not_selected_summary_ko": "", "selections": [_choice("EXAM-04-01-01-001")]}]},
                                 apps, candidates, "")["A1"]
    assert mismatch["selections"] == [] and not mismatch["identity_confirmed"]


def test_profile_rows_are_one_per_level5_id_in_each_language() -> None:
    application = {"name": "Hancom Office", "level5": [
        {"id": "EXAM-04-01-11-003", "title_ko": "한컴오피스 HWP", "title_en": "Hancom Office HWP"},
        {"id": "EXAM-04-01-11-010", "title_ko": "Hancom Office", "title_en": "Hancom Office"},
    ]}
    assert level5.profile_rows(application, "ko") == [("EXAM-04-01-11-003", "한컴오피스 HWP"), ("EXAM-04-01-11-010", "Hancom Office")]
    assert level5.profile_rows(application, "en")[0] == ("EXAM-04-01-11-003", "Hancom Office HWP")
    assert level5.profile_rows({"name": "KakaoMap", "level5": []}, "ko") == [(None, "KakaoMap")]


def test_the_empty_list_has_an_unregistered_sheet_and_a_registered_but_unmapped_sheet(tmp_path: Path, registry) -> None:
    target = tmp_path / "applicationempty_level5.xlsx"
    target.write_bytes(b"")                                            # the 0-byte placeholder is replaced, not opened
    papers = [PaperTargets("ART000000001", "KNOW-DOMP-0000001", 1, "ok", basis="PDF", applications=[
        {"name": "Discord", "display_name": "Discord", "status": "mapped", "level5": [{"id": "EXAM-04-01-02-003"}]},
        {"name": "QQ", "display_name": "QQ", "status": "unregistered", "level5": []},
        {"name": "Line Lite", "display_name": "Line Lite", "status": "identity_mismatch", "level5": []},
        {"name": "Netflix", "display_name": "Netflix", "status": "no_fit", "level5": [], "reason_short_ko": "TV 시스템 로그만 분석",
         "not_selected_summary_ko": "webOS 시스템 로그의 실행 기록만 분석했다. webOS 노드는 없다."},
    ])]
    first = level5.export_empty_list(papers, registry, target)
    assert (first["rows"], first["unregistered_rows"], first["no_fit_rows"]) == (3, 2, 1)
    workbook = load_workbook(target, read_only=True)
    assert workbook.sheetnames == ["등록되지 않은 Application", "등록은 되었지만 매핑되지 않은 Application"]
    assert list(workbook["등록되지 않은 Application"].iter_rows(values_only=True)) == [
        ("ID", "Target Application"), ("KNOW-DOMP-0000001", "QQ"), ("KNOW-DOMP-0000001", "Line Lite"),
    ]
    assert list(workbook["등록은 되었지만 매핑되지 않은 Application"].iter_rows(values_only=True)) == [
        ("ID", "Target Application", "사유"), ("KNOW-DOMP-0000001", "Netflix", "TV 시스템 로그만 분석"),
    ]
    workbook.close()
    listed = [("KNOW-DOMP-0000001", "QQ"), ("KNOW-DOMP-0000001", "Line Lite"), ("KNOW-DOMP-0000001", "Netflix")]
    assert level5.empty_list_rows(target) == listed

    # A paper that is not final in this run keeps its rows (with the reason) as they were.
    level5.export_empty_list([PaperTargets("ART000000001", "KNOW-DOMP-0000001", 1, "pending")], registry, target)
    assert level5.empty_list_rows(target) == listed
    assert level5._read_empty_list(target)[level5.EMPTY_NO_FIT_SHEET] == [("KNOW-DOMP-0000001", "Netflix", "TV 시스템 로그만 분석")]
    # Without a one-line reason the first sentence is used.
    papers[0].applications[3].pop("reason_short_ko")
    level5.export_empty_list(papers, registry, target)
    assert level5._read_empty_list(target)[level5.EMPTY_NO_FIT_SHEET][0][2] == "webOS 시스템 로그의 실행 기록만 분석했다."
    # Once DF-Insight registers QQ and it is mapped, its row leaves the list.
    papers[0].applications[1].update(status="mapped", level5=[{"id": "EXAM-04-01-02-099"}])
    assert level5.export_empty_list(papers, registry, target)["rows"] == 2
    assert level5.empty_list_rows(target) == listed[1:]

    # Step 5 must not silently skip a list it cannot read; a run rebuilds it instead.
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")
    with pytest.raises(Level5Error, match="cannot read"):
        level5.empty_list_rows(broken)
    assert level5.export_empty_list(papers, registry, broken)["rows"] == 2


def test_the_empty_list_reads_the_earlier_one_sheet_layout(tmp_path: Path) -> None:
    from openpyxl import Workbook

    legacy = tmp_path / "legacy.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = level5.LEGACY_EMPTY_SHEET
    sheet.append(["ID", "source_id", "Target Application", "논문 내 표기", "사유"])
    sheet.append(["KNOW-DOMP-0000001", "ART000000001", "QQ", "QQ", level5.REASON_TEXT["unregistered"]])
    sheet.append(["KNOW-DOMP-0000002", "ART000000002", "Netflix", "넷플릭스", f"{level5.REASON_TEXT['no_fit']} — webOS 로그만 분석했다. 그 밖에는 없다."])
    workbook.save(legacy)
    assert level5.empty_list_rows(legacy) == [("KNOW-DOMP-0000001", "QQ"), ("KNOW-DOMP-0000002", "Netflix")]
    assert level5._read_empty_list(legacy)[level5.EMPTY_NO_FIT_SHEET] == [("KNOW-DOMP-0000002", "Netflix", "webOS 로그만 분석했다.")]


def test_each_input_has_its_own_level5_result_and_the_profile_keeps_the_pdf_one(registry) -> None:
    from Analyzer.LV import lv_experiment as lv

    paper = lv.Paper(source_id="ART000000005", paper_index=5, title="t", title_kor="t", title_eng="", abstract_kor="", abstract_eng="",
                     abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None, record="KNOW-DOMP-0000005")
    discord = App("Discord", ["Discord"], ["APP-MES"], [], ["OS-WIN"], "로컬 캐시 분석", "", product=registry.match(["Discord"]).product)
    qq = App("QQ", ["QQ"], ["APP-MES"], [], ["OS-AND"], "DB 분석", "")
    nodes = level5.candidate_nodes(registry, discord.product)
    basis_version = f"{level5.MAP_PROMPT_VERSION}+{level5.BASIS_PROMPT_VERSION}"

    def record(version: str, selections: list[str], confirmed: bool = True) -> dict:
        return {"prompt_version": version, "apps": {discord.key: {
            "identity_confirmed": confirmed, "identity_reason_ko": "같은 제품", "not_selected_summary_ko": "초록에는 그 앱에 대해 한 일이 없음",
            "selections": [{"id": node_id, "fit": "정확히 부합", "reason_ko": "근거", "evidence_quote": "q"} for node_id in selections],
            "candidates_sha256": level5.candidates_sha(nodes), "hint_sha256": discord.hint_sha(), "product": discord.product}}}

    extraction_record = {"status": "ok", "basis": "PDF", "result": {"tag_ids": ["APP-MES"]}}
    targets = level5.build_paper_targets(
        paper, extraction_record, [discord, qq], record(level5.MAP_PROMPT_VERSION, ["COLL-02-04-01-002", "EXAM-04-01-02-003"]), registry,
        basis_records={"Abstract": record(basis_version, ["EXAM-04-01-02-003"]), "Summary": record(basis_version, [])},
        available={"Abstract": True, "Summary": True},
    )
    by_basis = targets.applications[0]["level5_by_basis"]
    assert (by_basis["PDF"]["status"], by_basis["PDF"]["ids"]) == ("mapped", ["COLL-02-04-01-002", "EXAM-04-01-02-003"])
    assert (by_basis["Abstract"]["status"], by_basis["Abstract"]["ids"]) == ("mapped", ["EXAM-04-01-02-003"])
    assert by_basis["Summary"]["status"] == "no_fit"
    # The profile (section 4) keeps the PDF-Based ids.
    assert [item["id"] for item in targets.applications[0]["level5"]] == ["COLL-02-04-01-002", "EXAM-04-01-02-003"]
    assert {basis: value["status"] for basis, value in targets.applications[1]["level5_by_basis"].items()} == dict.fromkeys(level5.BASES, "unregistered")

    # Another input's result is only taken from that input's own mapping (prompt version); a missing input is "unavailable".
    other = level5.build_paper_targets(
        paper, extraction_record, [discord], record(level5.MAP_PROMPT_VERSION, ["EXAM-04-01-02-003"]), registry,
        basis_records={"Abstract": record(level5.MAP_PROMPT_VERSION, ["EXAM-04-01-02-003"])}, available={"Abstract": True, "Summary": False},
    ).applications[0]["level5_by_basis"]
    assert (other["Abstract"]["status"], other["Summary"]["status"]) == ("pending", "unavailable")
    # A paper without a PDF has only its abstract: the primary result is the Abstract-Based one.
    no_pdf = level5.build_paper_targets(
        paper, {**extraction_record, "basis": "Abstract"}, [discord], record(level5.MAP_PROMPT_VERSION, ["EXAM-04-01-02-003"]), registry,
    ).applications[0]["level5_by_basis"]
    assert {basis: value["status"] for basis, value in no_pdf.items()} == {"PDF": "unavailable", "Abstract": "mapped", "Summary": "unavailable"}


def test_abstract_and_summary_mappings_read_only_their_own_input(tmp_path: Path, registry, monkeypatch) -> None:
    import types

    from Analyzer.LV import lv_experiment as lv

    monkeypatch.setattr(level5, "MAP_CACHE", tmp_path / "cache" / "level5")
    monkeypatch.setattr(level5, "basis_input", lambda paper, basis: ("디스코드 캐시를 분석했다.", f"[{basis}] 디스코드 캐시를 분석했다."))
    paper = lv.Paper(source_id="ART000000005", paper_index=5, title="t", title_kor="디스코드 연구", title_eng="", abstract_kor="디스코드 캐시를 분석했다.",
                     abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None,
                     record="KNOW-DOMP-0000005")
    discord = App("Discord", ["디스코드"], ["APP-MES"], [], ["OS-WIN"], "1단계 메모: 로컬 캐시", "", product=registry.match(["Discord"]).product)
    seen: list[tuple[str, str, dict]] = []

    class Caller:
        cli = types.SimpleNamespace(model="claude-opus-5")

        def call(self, *, system_prompt, prompt, schema, validate, tags):
            seen.append((system_prompt, prompt, tags))
            payload = {"applications": [{"app_id": "A1", "identity_confirmed": True, "identity_reason_ko": "같은 제품", "not_selected_summary_ko": "",
                                         "selections": [{"id": "EXAM-04-01-02-003", "fit": "대체로 부합", "reason_ko": "캐시",
                                                         "evidence_quote": "디스코드 캐시를 분석했다."}]}]}
            return {"result": validate(payload), "model": "m", "usage": {}, "attempts": 1, "fallback_for": None}

    record = level5.Mapper(Caller(), registry, "Summary").map_paper(paper, [discord])
    system, prompt, tags = seen[0]
    assert tags["task"] == "l5_map_summary" and "입력 안내 (Summary-Based)" in system
    assert prompt.startswith("[Summary]") and "1단계" not in prompt          # no Step-1 notes (they come from the PDF)
    assert record["prompt_version"] == f"{level5.MAP_PROMPT_VERSION}+{level5.BASIS_PROMPT_VERSION}"
    assert record["apps"][discord.key]["selections"][0]["quote_verified"]
    assert (tmp_path / "cache" / "level5_summary" / "ART000000005.json").is_file()
    level5.Mapper(Caller(), registry, "Summary").map_paper(paper, [discord])
    assert len(seen) == 1                                                     # cached
    # The primary mapping keeps the Step-1 notes and its own cache.
    level5.Mapper(Caller(), registry).map_paper(paper, [discord])
    assert seen[1][2]["task"] == "l5_map" and "1단계 추출 내용: 1단계 메모: 로컬 캐시" in seen[1][1]
    assert (tmp_path / "cache" / "level5" / "ART000000005.json").is_file()


def test_one_line_reasons_are_cached_and_fall_back_to_the_first_sentence(tmp_path: Path, monkeypatch) -> None:
    from Analyzer.claude_cli import ClaudeCliError

    monkeypatch.setattr(level5, "MAP_CACHE", tmp_path / "cache" / "level5")
    calls: list[dict] = []

    class Caller:
        def call(self, *, system_prompt, prompt, schema, validate, tags):
            calls.append(tags)
            item_ids = schema["properties"]["reasons"]["items"]["properties"]["item_id"]["enum"]
            answer = {"reasons": [{"item_id": item_id, "reason_short_ko": "TV(webOS) 시스템 로그만 분석"} for item_id in item_ids]}
            return {"result": validate(answer), "model": "m", "usage": {}, "attempts": 1, "fallback_for": None}

    class Failing:
        def call(self, **_kwargs):
            raise ClaudeCliError("no answer")

    items = [("k1", "Netflix", "논문은 webOS 시스템 로그만 분석했다. 그래서 맞는 노드가 없다.")]
    assert level5.short_reasons(items, Caller()) == {"k1": "TV(webOS) 시스템 로그만 분석"}
    assert level5.short_reasons(items, Failing()) == {"k1": "TV(webOS) 시스템 로그만 분석"}     # cached: not asked again
    assert len(calls) == 1 and calls[0]["task"] == "l5_reason"
    assert level5.short_reasons([("k2", "QQ", "다른 이유이다. 둘째 문장.")], Failing(), log=lambda _m: None) == {"k2": "다른 이유이다."}
    assert level5.short_reasons([("k3", "QQ", "")], None) == {"k3": level5.NO_FIT_SHORT}


def test_step5_maps_an_application_once_dfinsight_registers_it(tmp_path: Path, monkeypatch) -> None:
    """Steps 2-5 end to end with a tiny registry and a scripted model: QQ is unregistered, then registered."""

    import json
    import re
    import threading
    import types

    from Analyzer.Application import application_list, extraction
    from Analyzer.Application.registry import load_registry as real_load_registry
    from Analyzer.LV import lv_experiment as lv

    techniques = tmp_path / "df" / "data" / "techniques"
    techniques.mkdir(parents=True)

    def node(identifier: str, level: int, title: str) -> None:
        parent = identifier.rsplit("-", 1)[0] if level > 3 else None
        (techniques / f"{identifier}.json").write_text(json.dumps({
            "id": identifier, "level": level, "parent_id": parent, "title": {"ko": title, "en": title},
            "description": {"ko": "설명", "en": "description"}, "related": {}, "children": [],
        }), encoding="utf-8")

    node("EXAM-04-01", 3, "Application Artifact Examination")
    node("EXAM-04-01-02", 4, "Messengers")
    node("EXAM-04-01-02-001", 5, "KakaoTalk")
    monkeypatch.setattr(level5, "load_registry", lambda: real_load_registry(techniques, tmp_path / "none.json", tmp_path / "none2.json"))
    for name, value in (("MAP_CACHE", tmp_path / "map"), ("RESOLVE_CACHE", tmp_path / "resolve"),
                        ("TARGETS_JSON", tmp_path / "targets.json"), ("EMPTY_LIST", tmp_path / "empty.xlsx")):
        monkeypatch.setattr(level5, name, value)
    monkeypatch.setattr(application_list, "DEFAULT_OUTPUT", tmp_path / "application_list.xlsx")
    monkeypatch.setattr(extraction, "CACHE_DIR", tmp_path / "extract")

    vocabulary = level5.load_vocabulary()

    def paper(source_id: str, index: int) -> lv.Paper:
        return lv.Paper(source_id=source_id, paper_index=index, title="t", title_kor="메신저 연구", title_eng="", abstract_kor="카카오톡과 QQ의 DB를 분석했다.",
                        abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None,
                        record=f"KNOW-DOMP-{index:07d}")

    def extracted(source_id: str, *apps: str) -> None:
        level5._write_json(tmp_path / "extract" / f"{source_id}.json", {
            "schema_version": 1, "prompt_version": extraction.PROMPT_VERSION, "status": "ok", "source_id": source_id, "basis": "Abstract",
            "vocabulary_sha256": vocabulary.sha256, "result": {"tag_ids": ["OS-AND", "APP-MES"], "target_applications": [
                {"name": name, "names_in_paper": [name], "app_categories": ["APP-MES"], "cloud_categories": [], "platforms": ["OS-AND"],
                 "studied_aspects_ko": "DB 분석", "evidence_quote": "카카오톡과 QQ의 DB를 분석했다."} for name in apps]},
        })

    papers = [paper("ART000000001", 1), paper("ART000000002", 2)]
    extracted("ART000000001", "KakaoTalk", "QQ")
    extracted("ART000000002", "카카오 메신저")          # only the LLM name check can place this one
    calls: list[tuple[str, str]] = []

    class ScriptedCaller:
        def __init__(self, cli, retries=3):
            self.cli = types.SimpleNamespace(model="claude-opus-5")
            self.stop = threading.Event()

        def call(self, *, system_prompt, prompt, schema, validate, tags):
            calls.append((tags["task"], tags["source_id"]))
            if tags["task"] == "l5_resolve":
                answers = [{"item_id": item_id, "registered_title": "KakaoTalk" if "카카오" in line else None, "reason_ko": "판정"}
                           for item_id, line in re.findall(r"^(N\d+)\. (.*)$", prompt, re.MULTILINE)]
                return {"result": validate({"names": answers}), "model": "m", "usage": {}, "attempts": 1, "fallback_for": None}
            blocks = re.findall(r"^(A\d+)\. .*?\n(?:.*\n)*?   - ([A-Z]{4}-\d{2}-\d{2}-\d{2}-\d{3}) ", prompt, re.MULTILINE)
            payload = {"applications": [{"app_id": app_id, "identity_confirmed": True, "identity_reason_ko": "같은 제품", "not_selected_summary_ko": "",
                                         "selections": [{"id": node_id, "fit": "정확히 부합", "reason_ko": "앱 DB 분석", "evidence_quote": "카카오톡과 QQ의 DB를 분석했다."}]}
                                        for app_id, node_id in blocks]}
            return {"result": validate(payload), "model": "m", "usage": {}, "attempts": 1, "fallback_for": None}

    monkeypatch.setattr(level5, "ClaudeCli", lambda model, timeout: object())
    monkeypatch.setattr(level5, "RetryingCaller", ScriptedCaller)

    def run(only=None):
        return level5.run_stage(papers, workers=2, model="claude-opus-5", timeout=1, retries=1, extract=False, only=only, log=lambda _m: None)

    def applications(source_id: str) -> dict:
        data = json.loads((tmp_path / "targets.json").read_text(encoding="utf-8"))["papers"][source_id]
        return {app["name"]: (app["status"], [item["id"] for item in app["level5"]]) for app in data["applications"]}

    first = run()
    assert first["pending"] == 0 and first["empty_rows"] == 1
    assert applications("ART000000001") == {"KakaoTalk": ("mapped", ["EXAM-04-01-02-001"]), "QQ": ("unregistered", [])}
    assert applications("ART000000002") == {"카카오 메신저": ("mapped", ["EXAM-04-01-02-001"])}
    assert level5.empty_list_rows(tmp_path / "empty.xlsx") == [("KNOW-DOMP-0000001", "QQ")]
    # Without a PDF the paper has only its abstract: no Abstract-/Summary-Based calls, and the PDF/Summary columns say so.
    by_basis = json.loads((tmp_path / "targets.json").read_text(encoding="utf-8"))["papers"]["ART000000001"]["applications"][0]["level5_by_basis"]
    assert {basis: value["status"] for basis, value in by_basis.items()} == {"PDF": "unavailable", "Abstract": "mapped", "Summary": "unavailable"}
    assert {task for task, _source in calls} == {"l5_resolve", "l5_map"}
    assert load_workbook(tmp_path / "application_list.xlsx", read_only=True).sheetnames == ["개요", "PDF-Based", "Abstract-Based", "Summary-Based", "종합본"]

    # DF-Insight registers QQ; Step 5 re-checks only the listed paper.
    node("EXAM-04-01-02-002", 5, "QQ")
    calls.clear()
    listed_ids = {record_id for record_id, _name in level5.empty_list_rows(tmp_path / "empty.xlsx")}
    listed = {paper.source_id for paper in papers if paper.record_id in listed_ids}
    second = run(only=listed)
    assert applications("ART000000001") == {"KakaoTalk": ("mapped", ["EXAM-04-01-02-001"]), "QQ": ("mapped", ["EXAM-04-01-02-002"])}
    # The other paper was not asked again, and its earlier name check still holds under the new registry.
    assert applications("ART000000002") == {"카카오 메신저": ("mapped", ["EXAM-04-01-02-001"])}
    assert calls == [("l5_map", "ART000000001")] and second["resolve"] == {"earlier_answer": 1}
    assert second["empty_rows"] == 0 and level5.empty_list_rows(tmp_path / "empty.xlsx") == []
    assert second["pending"] == 0


def test_a_registered_app_without_a_fitting_node_stays_blank_and_listed(tmp_path: Path, registry) -> None:
    """Step 3 found the product but no node for what the paper did (e.g. file-system metadata only)."""

    from Analyzer.LV import lv_experiment as lv

    paper = lv.Paper(source_id="ART000000009", paper_index=9, title="t", title_kor="t", title_eng="", abstract_kor="", abstract_eng="",
                     abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None, record="KNOW-DOMP-0000009")
    app = App("Netflix", ["netflix"], ["APP-MUL"], [], ["OS-ETC"], "webOS 시스템 로그의 실행 기록", "", product=registry.match(["Netflix"]).product)
    nodes = level5.candidate_nodes(registry, app.product)
    mapping = {"prompt_version": level5.MAP_PROMPT_VERSION, "apps": {app.key: {
        "identity_confirmed": True, "identity_reason_ko": "같은 제품", "selections": [], "not_selected_summary_ko": "webOS 노드 없음",
        "candidates_sha256": level5.candidates_sha(nodes), "hint_sha256": app.hint_sha(), "product": app.product}}}
    record = {"status": "ok", "basis": "PDF", "result": {"tag_ids": ["OS-ETC", "APP-MUL"]}}
    targets = level5.build_paper_targets(paper, record, [app], mapping, registry)
    assert targets.status == "ok"
    entry = targets.applications[0]
    assert (entry["status"], entry["level5"]) == ("no_fit", []) and "webOS 노드 없음" in entry["reason_ko"]
    assert entry["not_selected_summary_ko"] == "webOS 노드 없음"
    assert level5.profile_rows(entry, "ko") == [(None, "Netflix")]
    result = level5.export_empty_list([targets], registry, tmp_path / "empty.xlsx")
    assert (result["rows"], result["no_fit_rows"]) == (1, 1)
    assert level5.empty_list_rows(tmp_path / "empty.xlsx") == [("KNOW-DOMP-0000009", "Netflix")]
    assert level5._read_empty_list(tmp_path / "empty.xlsx")[level5.EMPTY_NO_FIT_SHEET][0][2] == "webOS 노드 없음"


def test_a_comparison_result_must_be_for_todays_input_and_a_missing_summary_is_pending(registry) -> None:
    from Analyzer.LV import lv_experiment as lv

    paper = lv.Paper(source_id="ART000000005", paper_index=5, title="t", title_kor="t", title_eng="", abstract_kor="", abstract_eng="",
                     abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None, record="KNOW-DOMP-0000005")
    discord = App("Discord", ["Discord"], ["APP-MES"], [], ["OS-WIN"], "로컬 캐시 분석", "", product=registry.match(["Discord"]).product)
    nodes = level5.candidate_nodes(registry, discord.product)
    basis_version = f"{level5.MAP_PROMPT_VERSION}+{level5.BASIS_PROMPT_VERSION}"

    def record(version: str, hint: str, context: str) -> dict:
        return {"prompt_version": version, "context_sha256": context, "apps": {discord.key: {
            "identity_confirmed": True, "identity_reason_ko": "같은 제품", "not_selected_summary_ko": "",
            "selections": [{"id": "EXAM-04-01-02-003", "fit": "정확히 부합", "reason_ko": "근거", "evidence_quote": "q"}],
            "candidates_sha256": level5.candidates_sha(nodes), "hint_sha256": hint, "product": discord.product}}}

    extraction_record = {"status": "ok", "basis": "PDF", "result": {"tag_ids": ["APP-MES"]}}
    primary = record(level5.MAP_PROMPT_VERSION, discord.hint_sha(), "pdf-now")

    def by_basis(abstract: dict, contexts: dict, available: dict) -> dict:
        targets = level5.build_paper_targets(paper, extraction_record, [discord], primary, registry,
                                             basis_records={"Abstract": abstract}, available=available, contexts=contexts)
        return {basis: value["status"] for basis, value in targets.applications[0]["level5_by_basis"].items()}

    current = {"primary": "pdf-now", "Abstract": "abstract-now"}
    abstract = record(basis_version, discord.basis_hint_sha(), "abstract-now")
    assert by_basis(abstract, current, {"Abstract": True, "Summary": "pending"}) == {"PDF": "mapped", "Abstract": "mapped", "Summary": "pending"}
    # Mapped from an older abstract (or summary): not shown as today's result.
    assert by_basis({**abstract, "context_sha256": "abstract-before"}, current, {"Abstract": True})["Abstract"] == "pending"
    # The profile's own mapping, too, must be for today's text.
    assert by_basis(abstract, {**current, "primary": "pdf-changed"}, {"Abstract": True})["PDF"] == "pending"
    # The Abstract prompt has no Step-1 notes: new notes do not make its answer stale, and an answer stored
    # with the full Step-1 hint (before the basis hint existed) still counts.
    changed = App("Discord", ["Discord"], ["APP-MES"], [], ["OS-WIN"], "다르게 쓴 1단계 메모", "", product=discord.product)
    targets = level5.build_paper_targets(paper, extraction_record, [changed], record(level5.MAP_PROMPT_VERSION, changed.hint_sha(), "pdf-now"),
                                         registry, basis_records={"Abstract": abstract}, available={"Abstract": True}, contexts=current)
    assert targets.applications[0]["level5_by_basis"]["Abstract"]["status"] == "mapped"
    legacy = record(basis_version, discord.hint_sha(), "abstract-now")
    assert by_basis(legacy, current, {"Abstract": True})["Abstract"] == "mapped"


def test_a_paper_without_a_pdf_has_no_pdf_or_summary_result_even_for_an_unregistered_app(registry) -> None:
    from Analyzer.LV import lv_experiment as lv

    paper = lv.Paper(source_id="ART000000006", paper_index=6, title="t", title_kor="t", title_eng="", abstract_kor="", abstract_eng="",
                     abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None, record="KNOW-DOMP-0000006")
    qq = App("QQ", ["QQ"], ["APP-MES"], [], ["OS-AND"], "DB 분석", "")
    targets = level5.build_paper_targets(paper, {"status": "ok", "basis": "Abstract", "result": {"tag_ids": []}}, [qq], None, registry)
    statuses = {basis: value["status"] for basis, value in targets.applications[0]["level5_by_basis"].items()}
    assert statuses == {"PDF": "unavailable", "Abstract": "unregistered", "Summary": "unavailable"}


def test_one_line_reasons_are_asked_only_for_the_selected_papers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(level5, "MAP_CACHE", tmp_path / "cache" / "level5")
    asked: list[str] = []

    class Caller:
        def call(self, *, system_prompt, prompt, schema, validate, tags):
            asked.append(tags["source_id"])
            item_ids = schema["properties"]["reasons"]["items"]["properties"]["item_id"]["enum"]
            return {"result": validate({"reasons": [{"item_id": item_id, "reason_short_ko": "짧은 이유"} for item_id in item_ids]}),
                    "model": "m", "usage": {}, "attempts": 1, "fallback_for": None}

    items = [("ART1|0", "Netflix", "첫 문장이다. 둘째 문장."), ("ART2|0", "QQ", "다른 논문의 이유이다. 둘째.")]
    assert level5.short_reasons(items, Caller(), allowed={"ART1"}) == {"ART1|0": "짧은 이유", "ART2|0": "다른 논문의 이유이다."}
    assert asked == ["ART1"]


def test_a_paper_whose_input_changed_since_step1_waits_for_a_new_extraction(tmp_path: Path, monkeypatch) -> None:
    """Step 1 read the PDF, but today the paper has no PDF text (or the other way round): it is not final."""

    from Analyzer.Application import application_list, extraction
    from Analyzer.LV import lv_experiment as lv

    for name, value in (("MAP_CACHE", tmp_path / "map"), ("RESOLVE_CACHE", tmp_path / "resolve"),
                        ("TARGETS_JSON", tmp_path / "targets.json"), ("EMPTY_LIST", tmp_path / "empty.xlsx")):
        monkeypatch.setattr(level5, name, value)
    monkeypatch.setattr(application_list, "DEFAULT_OUTPUT", tmp_path / "application_list.xlsx")
    monkeypatch.setattr(extraction, "CACHE_DIR", tmp_path / "extract")
    vocabulary = level5.load_vocabulary()
    paper = lv.Paper(source_id="ART000000007", paper_index=7, title="t", title_kor="메신저 연구", title_eng="", abstract_kor="QQ의 DB를 분석했다.",
                     abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None,
                     record="KNOW-DOMP-0000007")
    level5._write_json(tmp_path / "extract" / "ART000000007.json", {
        "schema_version": 1, "prompt_version": extraction.PROMPT_VERSION, "status": "ok", "source_id": "ART000000007", "basis": "PDF",
        "vocabulary_sha256": vocabulary.sha256, "result": {"tag_ids": ["APP-MES"], "target_applications": [
            {"name": "QQ", "names_in_paper": ["QQ"], "app_categories": ["APP-MES"], "cloud_categories": [], "platforms": ["OS-AND"],
             "studied_aspects_ko": "DB", "evidence_quote": "QQ의 DB를 분석했다."}]},
    })
    summary = level5.run_stage([paper], workers=1, model="claude-opus-5", timeout=1, retries=1, extract=False, call_llm=False, log=lambda _m: None)
    assert summary["pending"] == 1
    data = level5.json.loads((tmp_path / "targets.json").read_text(encoding="utf-8"))["papers"]["ART000000007"]
    assert data["status"] == "pending" and "다시 추출" in data["reason"]


def test_rows_of_a_paper_that_is_not_final_follow_the_paper_when_numbers_move(tmp_path: Path, registry) -> None:
    target = tmp_path / "empty.xlsx"
    final = PaperTargets("ART000000001", "KNOW-DOMP-0000003", 3, "ok", basis="PDF", applications=[
        {"name": "QQ", "display_name": "QQ", "status": "unregistered", "level5": []}])
    level5.export_empty_list([final], registry, target)
    # Renumbering: ART000000001 is now KNOW-DOMP-0000004 (final), a new pending paper took KNOW-DOMP-0000003.
    moved = PaperTargets("ART000000001", "KNOW-DOMP-0000004", 4, "ok", basis="PDF", applications=final.applications)
    newcomer = PaperTargets("ART000000009", "KNOW-DOMP-0000003", 3, "pending")
    level5.export_empty_list([moved, newcomer], registry, target, previous_ids={"KNOW-DOMP-0000003": "ART000000001"})
    assert level5.empty_list_rows(target) == [("KNOW-DOMP-0000004", "QQ")]
    # A pending paper keeps its own earlier rows, under its id of today.
    level5.export_empty_list([PaperTargets("ART000000001", "KNOW-DOMP-0000005", 5, "pending")], registry, target,
                             previous_ids={"KNOW-DOMP-0000004": "ART000000001"})
    assert level5.empty_list_rows(target) == [("KNOW-DOMP-0000005", "QQ")]

