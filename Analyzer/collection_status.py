"""Collection tracker: ``Analyzer/KCI-Collector.json``.

One file that answers "where does the collection stand?" without opening a
database or a workbook: how many KCI forensic papers are collected, how many
were removed, and — per paper — whether the full-text PDF, the Level
classification, OA, Special Issue, Type, Tags, the reference list and the MD
profile are done.  It is written for people who are not developers, so the
keys and values are plain Korean sentences.

The pipeline rewrites the file at the end of every run (``python3 main.py``);
``python3 -m Analyzer.collection_status`` refreshes it on its own.  Nothing
reads this file back except to keep each paper's first-seen date and the
day-by-day history, so it is safe to delete: the next run rebuilds it.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from Analyzer import deliverables
from Analyzer.deliverables import NO, YES, Identity

ANALYZER_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
TRACKER_PATH = ANALYZER_DIR / "KCI-Collector.json"
PAPER_DIRECTORY = ANALYZER_DIR / "Paper"
PROFILE_DIRECTORY = PAPER_DIRECTORY / "MD"
PAPER_FILE_INDEX = ANALYZER_DIR / "Manifest" / "paper_files.json"
MANIFEST_DIRECTORY = ANALYZER_DIR / "Manifest"
# The Level result the profiles show (Jev AI, the jev stage); Analyzer/LV/levels.json is the earlier method.
LEVELS_JSON = ANALYZER_DIR / "JevAI" / "levels.json"
EXCLUDED_PAPERS = REPOSITORY_ROOT / "Processor" / "refs" / "excluded_papers.json"
LOCATED_SOURCES = REPOSITORY_ROOT / "Processor" / "refs" / "located_pdf_sources.json"

HISTORY_LIMIT = 400


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pdf_files() -> dict[str, str]:
    """source_id -> PDF filename for the papers whose PDF is really on disk."""

    from PaperPipeline.paper_files import load_paper_file_index

    try:
        records = load_paper_file_index(PAPER_FILE_INDEX)
    except Exception:  # an unreadable index means "no PDF known", which the file then shows
        return {}
    return {
        source_id: record.filename for source_id, record in records.items()
        if (PAPER_DIRECTORY / record.filename).is_file()
    }


def _missing_pdf_reasons() -> dict[str, str]:
    payload = _read_json(LOCATED_SOURCES)
    reasons: dict[str, str] = {}
    if isinstance(payload, dict):
        for key in ("excluded_foreign_publisher_sites", "no_public_full_text"):
            block = payload.get(key)
            if isinstance(block, dict):
                for source_id in block.get("source_ids", ()):
                    reasons[str(source_id)] = str(block.get("reason", ""))
    return reasons


def _first_seen(source_id: str, previous: Mapping[str, Any], today: str) -> str:
    """The day the paper entered the collection: kept once known, else the first PDF attempt, else today."""

    known = previous.get(source_id)
    if isinstance(known, str) and known:
        return known
    manifest = _read_json(MANIFEST_DIRECTORY / f"{source_id}.download.json")
    started = manifest.get("started_at") if isinstance(manifest, dict) else None
    if isinstance(started, str) and len(started) >= 10:
        return started[:10]
    return today


def _previous_state(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return {}, []
    def entries(key: str) -> list[Any]:
        value = payload.get(key)
        return value if isinstance(value, list) else []  # a hand-edited or damaged file must not stop the rebuild

    dates = {
        str(item.get("KCI 논문 ID")): str(item.get("수집일"))
        for item in entries("논문별 현황") if isinstance(item, dict) and item.get("KCI 논문 ID") and item.get("수집일")
    }
    history = [item for item in entries("날짜별 수집 이력") if isinstance(item, dict) and item.get("날짜")]
    return dates, history


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(name, 0o644)  # mkstemp creates 0600; this is a report everyone on the machine may read
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _optional_rows(path: Path, sheet: str) -> dict[str, dict[str, Any]]:
    try:
        return deliverables.read_rows(path, sheet)
    except (deliverables.DeliverableError, KeyError, OSError):
        return {}


def build_status(
    *, identities: Sequence[Identity] | None = None, today: date | None = None, tracker_path: Path = TRACKER_PATH,
) -> dict[str, Any]:
    now = datetime.now().astimezone()
    day = (today or now.date()).isoformat()
    papers = list(identities) if identities is not None else deliverables.load_identities()
    previous_dates, history = _previous_state(tracker_path)

    pdfs = _pdf_files()
    missing_reasons = _missing_pdf_reasons()
    levels_payload = _read_json(LEVELS_JSON)
    levels = levels_payload.get("papers", {}) if isinstance(levels_payload, dict) else {}
    empty_level_note = (levels_payload.get("empty_note") if isinstance(levels_payload, dict) else None) or "confidence 0.80 이상으로 확정된 Level이 없어 비워 둠"
    oa = _optional_rows(deliverables.OA_SOURCE, "OA 목록")
    special = _optional_rows(deliverables.SPECIAL_SOURCE, "Special Issue 목록")
    types = _optional_rows(deliverables.TYPE_SOURCE, "논문 유형 목록")
    targets = deliverables.read_targets(required=False)
    references = _optional_rows(deliverables.REFERENCE_SOURCE, "논문별 요약")
    excluded_payload = _read_json(EXCLUDED_PAPERS)
    removed = excluded_payload.get("papers", []) if isinstance(excluded_payload, dict) else []

    rows: list[dict[str, Any]] = []
    counts = {key: 0 for key in ("pdf", "level_done", "level_any", "oa", "special", "type", "tags", "ref_checked", "ref_present", "md")}
    for paper in papers:
        source_id = paper.source_id
        has_pdf = source_id in pdfs
        level = levels.get(source_id) if isinstance(levels, dict) else None
        oa_flag, oa_label = deliverables.oa_values(oa.get(source_id))
        special_flag, _special_label, special_name = deliverables.special_values(special.get(source_id))
        type_code, type_name, _type_description = deliverables.type_values(types.get(source_id))
        tag_groups = deliverables.tags_by_axis(targets.get(source_id))
        reference_flag, reference_count = deliverables.reference_values(references.get(source_id))
        profile = PROFILE_DIRECTORY / paper.record_id if paper.record_id else None
        has_profile = bool(profile and (profile / "ko.md").is_file() and (profile / "en.md").is_file())

        counts["pdf"] += has_pdf
        counts["level_done"] += level is not None and level.get("status") != "pending"
        counts["level_any"] += bool(level and level.get("lv3"))
        counts["oa"] += oa_flag != ""
        counts["special"] += special_flag != ""
        counts["type"] += type_code != ""
        counts["tags"] += tag_groups is not None
        counts["ref_checked"] += reference_flag != ""
        counts["ref_present"] += reference_flag == YES
        counts["md"] += has_profile

        if has_pdf:
            pdf_text = "수집 완료"
        else:
            pdf_text = "미수집 — " + (missing_reasons.get(source_id) or "아직 원문을 받지 못했습니다 (다음 실행에서 다시 시도)")
        if level is None:
            level_text, level_ids = "미분류 — Level 결과(jev 단계)에 아직 없습니다", []
        elif level.get("status") == "pending":
            reason = f" ({level['basis_reason']})" if level.get("basis_reason") else ""
            level_text, level_ids = f"미분류 — Level 분류가 아직 끝나지 않았습니다{reason} (다음 실행에서 이어서 분류)", []
        elif level.get("lv3"):
            level_ids = list(level["lv3"])
            note = f" ({level.get('basis_reason') or '원문을 읽을 수 없음'} → 초록으로 분류)" if level.get("basis") == "Abstract" else ""
            level_text = f"분류 완료 — Level-3 {len(level_ids)}개 확정{note}"
        else:
            level_ids, level_text = [], f"분류 완료 — {empty_level_note}"
        if tag_groups is None:
            tags_text, tag_list = "미추출 — 태그·대상 애플리케이션 결과가 아직 없습니다", []
        else:
            tag_list = [tag for axis in deliverables.AXES for tag in tag_groups[axis]]
            basis_note = " (원문 PDF가 없어 초록으로 추출)" if (targets.get(source_id) or {}).get("basis") == "Abstract" else ""
            tags_text = (f"추출 완료 — 태그 {len(tag_list)}개" if tag_list else "추출 완료 — 해당하는 태그 없음") + basis_note
        applications = deliverables.application_names(targets.get(source_id))
        if has_profile:
            profile_text = "생성 완료 (한국어·영어)"
        elif not has_pdf:
            profile_text = "미생성 — 원문 PDF가 없어 논문 Type을 정할 수 없습니다(MD는 원문 PDF가 있는 논문만 만듭니다)"
        elif tag_groups is None or level is None or level.get("status") == "pending":
            # The type no longer holds a profile back (it shows N/A until the analysis); the classification does.
            profile_text = "미생성 — Jev AI 분류 결과(Level·태그·대상 애플리케이션)가 아직 없습니다"
        else:
            profile_text = "미생성 — MD 단계를 실행하면 생성됩니다"

        rows.append({
            "번호(paper_index)": paper.paper_index,
            "record_id": paper.record_id,
            "KCI 논문 ID": source_id,
            "제목": paper.title,
            "학술지": paper.journal,
            "발행연도": paper.publication_year,
            "수집일": _first_seen(source_id, previous_dates, day),
            "원문 PDF": pdf_text,
            "Level 분류": level_text,
            "Level-3": level_ids,
            "OA(Open Access)": {YES: "확인 완료 — Open Access", NO: "확인 완료 — Open Access 아님"}.get(oa_flag, _oa_reason(oa.get(source_id))),
            "Special Issue": {YES: f"확인 완료 — Special Issue ({special_name})" if special_name else "확인 완료 — Special Issue",
                              NO: "확인 완료 — Special Issue 아님"}.get(special_flag, _special_reason(special.get(source_id))),
            "논문 Type": (f"분류 완료 — {type_name} ({type_code})" if type_code
                        else "미분류 — 원문 분석이 아직 끝나지 않았습니다" if has_pdf else "미분류 — 원문 PDF가 없어 분류할 수 없습니다"),
            "Tags": tags_text,
            "Tags 목록": tag_list,
            "애플리케이션 명": applications,
            "참고문헌(ref)": (f"수집 완료 — {reference_count}건" if reference_flag == YES
                          else "수집 완료 — KCI에 참고문헌 정보가 없는 논문" if reference_flag == NO else "미수집"),
            "MD 파일": profile_text,
        })

    total = len(papers)

    def progress(done: int) -> str:
        return f"{done} / {total}편" + (f" ({done / total:.1%})" if total else "")

    summary = {
        # 총 = 현재 + 제거: the first three lines must read as one sum, so nobody has to
        # wonder whether the removed papers are inside or outside the current count.
        "총 논문수(지금까지 선별된 KCI 포렌식 논문 = 현재 + 제거)": total + len(removed),
        "현재 수집된 KCI 포렌식 논문 수": total,
        "제거된 포렌식 논문 수": len(removed),
        "원문 PDF 수집": progress(counts["pdf"]),
        "원문 PDF 미수집 논문 수": total - counts["pdf"],
        "Level 분류를 거친 논문": progress(counts["level_done"]),
        "Level이 1개 이상 확정된 논문": progress(counts["level_any"]),
        "OA 확인": progress(counts["oa"]),
        "Special Issue 확인": progress(counts["special"]),
        "논문 Type 분류": progress(counts["type"]),
        "Tags 추출": progress(counts["tags"]),
        "참고문헌(ref) 수집 시도": progress(counts["ref_checked"]),
        "참고문헌 정보가 있는 논문": progress(counts["ref_present"]),
        "MD 파일 생성": progress(counts["md"]),
    }
    entry = {
        "날짜": day,
        "총 논문수": total + len(removed),
        "수집된 논문 수": total,
        "제거된 논문 수": len(removed),
        "원문 PDF": counts["pdf"],
        "Level 분류": counts["level_done"],
        "OA": counts["oa"],
        "Special Issue": counts["special"],
        "Type": counts["type"],
        "Tags": counts["tags"],
        "참고문헌": counts["ref_checked"],
        "MD 파일": counts["md"],
    }
    history = [item for item in history if item.get("날짜") != day] + [entry]
    history = sorted(history, key=lambda item: str(item.get("날짜")))[-HISTORY_LIMIT:]

    return {
        "안내": [
            "KCI 디지털 포렌식 논문 수집 현황판입니다. python3 main.py 를 실행할 때마다 자동으로 다시 만들어지므로 직접 고치지 않습니다.",
            "'요약'의 첫 세 줄은 '총 논문수 = 현재 수집된 논문 수 + 제거된 논문 수'입니다(예: 555 = 517 + 38). 제거된 논문은 현재 수집된 논문 수에 들어 있지 않습니다.",
            "논문 번호(번호 = KNOW-DOMP-번호)는 원문 PDF가 있는 논문을 KCI 논문 ID 순으로 1번부터 매기고, PDF가 없는 논문을 그 뒤에 둡니다. 논문을 제거하거나 PDF를 새로 구하면 번호가 다시 정렬되므로, 오래된 워크북의 번호는 지금과 다를 수 있습니다(바뀐 기록: Analyzer/Manifest/renumber_history.json).",
            "'요약'은 전체 숫자, '날짜별 수집 이력'은 날짜마다의 변화, '논문별 현황'은 논문 한 편씩의 진행 상태, '제거된 논문'은 검토 후 수집 목록에서 뺀 논문입니다.",
            "각 항목의 값은 '완료 — 내용' 또는 '미… — 이유' 형태로 적혀 있습니다. '수집일'은 그 논문이 수집 목록에 처음 들어온 날(원문 PDF 수집을 처음 시도한 날)입니다.",
        ],
        "마지막 갱신": now.strftime("%Y-%m-%d %H:%M"),
        "요약": summary,
        "날짜별 수집 이력": history,
        "번호 정렬 상태": _numbering_state(),
        "아직 끝나지 않은 일": _open_items(rows),
        "논문별 현황": rows,
        "제거된 논문": [
            {"KCI 논문 ID": item.get("source_id"),
             # A removed paper has no number any more; the one it had has since gone to another paper.
             "제거 당시 번호(지금은 다른 논문의 번호일 수 있음)": item.get("former_record_id") or item.get("record_id"),
             "제목": item.get("title"), "학술지": item.get("journal"),
             "발행연도": item.get("publication_year"), "제거일": item.get("removed_on"), "제거 사유": item.get("reason")}
            for item in removed if isinstance(item, dict)
        ],
    }


def _numbering_state() -> str:
    """Whether the paper numbers follow the rule (PDF papers by KCI id from 1, then papers without a PDF)."""

    try:
        from Analyzer.numbering import numbering_health
        from Storage.db import sqlite_database_path
        from config import load_settings

        health = numbering_health(database=Path(sqlite_database_path(load_settings(validate=False).database_url)))
    except Exception as exc:  # noqa: BLE001 - the tracker must always be written
        return f"확인하지 못함 ({type(exc).__name__})"
    if health.get("in_order"):
        return "정렬됨 — 원문 PDF가 있는 논문을 KCI 논문 ID 순으로 1번부터, 그 뒤에 PDF가 없는 논문 (빈 번호 없음)"
    return (f"정렬 필요 — 번호가 바뀌어야 하는 논문 {health.get('papers_to_renumber', '?')}편. "
            "`python3 main.py -renumber`를 실행하면 번호를 다시 매기고 워크북·MD·그래프를 새로 만듭니다.")


def _oa_reason(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "미확인 — OA 단계를 아직 거치지 않았습니다"
    if row.get("collection_status") == "no_record":
        return "미확인 — KCI 공개 저장소(OAI)에 이 논문의 기록이 없습니다"
    return f"미확인 — KCI에서 확인하지 못했습니다 ({row.get('error_code') or row.get('collection_status') or '사유 미상'}) · 다음 실행에서 다시 시도"


def _special_reason(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "미확인 — Special Issue 단계를 아직 거치지 않았습니다"
    code = str(row.get("error_code") or "")
    if "authentication" in code:
        return "미확인 — KCI 로그인이 필요합니다 (python3 main.py -special --special-interactive-login)"
    return f"미확인 — KCI 서지정보로 확정하지 못했습니다 ({code or row.get('collection_status') or '사유 미상'})"


def _open_items(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """What is still missing, as short readable lists (paper number + title)."""

    def label(row: Mapping[str, Any]) -> str:
        number = row["번호(paper_index)"]
        return f"{number}번 {row['제목']}" if number is not None else f"(번호 없음) {row['제목']}"

    checks = {
        "원문 PDF가 없는 논문": lambda row: not str(row["원문 PDF"]).startswith("수집 완료"),
        "Level 분류를 아직 거치지 않은 논문": lambda row: str(row["Level 분류"]).startswith("미분류"),
        "OA가 확인되지 않은 논문": lambda row: str(row["OA(Open Access)"]).startswith("미확인"),
        "Special Issue가 확인되지 않은 논문": lambda row: str(row["Special Issue"]).startswith("미확인"),
        "Type·Tags가 아직 없는 논문": lambda row: str(row["논문 Type"]).startswith("미분류") or str(row["Tags"]).startswith("미추출"),
        "참고문헌을 아직 수집하지 않은 논문": lambda row: str(row["참고문헌(ref)"]) == "미수집",
        "MD 파일이 없는 논문": lambda row: str(row["MD 파일"]).startswith("미생성"),
    }
    return {title: [label(row) for row in rows if check(row)] for title, check in checks.items()}


def update_tracker(*, today: date | None = None, tracker_path: Path = TRACKER_PATH) -> dict[str, Any]:
    status = build_status(today=today, tracker_path=tracker_path)
    _atomic_write(tracker_path, status)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="수집 현황판(Analyzer/KCI-Collector.json) 갱신")
    parser.parse_args(argv)
    status = update_tracker()
    print(f"wrote {TRACKER_PATH}")
    for key, value in status["요약"].items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
