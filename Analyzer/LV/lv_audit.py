#!/usr/bin/env python3
"""Accuracy audit of the Level experiment.

``packets``  picks a stratified sample of papers and writes, per paper, the union of
             the Level-3 candidates the three cases produced — without saying which
             case or confidence they came from — for independent reviewers who read
             the full text.
``score``    turns the reviewers' verdicts into precision/recall per case and per
             confidence band, and writes ``(YYMMDD)-LV-Audit.xlsx``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from Analyzer.LV import lv_experiment as lv  # noqa: E402

AUDIT_DIR = lv.CACHE_DIR / "audit"
VERDICTS = ("맞음", "애매", "틀림")


def _result(case: int, source_id: str) -> dict[str, Any] | None:
    # Only records of the current prompts and taxonomy (the same ones the workbooks use).
    record = lv.load_case_record(case, source_id)
    return record["result"] if record and record.get("status") == "ok" else None


def _stable_order(values: Sequence[str], salt: str) -> list[str]:
    return sorted(values, key=lambda value: hashlib.sha256(f"{salt}:{value}".encode()).hexdigest())


def build_packets(sample_size: int) -> list[dict[str, Any]]:
    techniques, _sha = lv.load_techniques()
    papers = [paper for paper in lv.load_corpus(lv.DEFAULT_DATABASE, techniques) if paper.pdf_path is not None]
    rows: list[tuple[str, lv.Paper, dict[int, dict[str, Any]]]] = []
    for paper in papers:
        results = {case: _result(case, paper.source_id) for case in lv.CASE_LABELS}
        if not all(results.values()) or not (lv.CACHE_DIR / "text" / f"{paper.source_id}.txt").is_file():
            continue
        primaries = {case: (results[case]["mappings"][0]["path_id"] if results[case]["mappings"] else "") for case in results}
        confirmed = {case: {m["path_id"] for m in results[case]["mappings"] if m["confidence"] >= lv.CONFIRMED_CONFIDENCE} for case in results}
        if len(set(primaries.values())) > 1:
            stratum = "primary 불일치"
        elif not (confirmed[1] == confirmed[2] == confirmed[3]):
            stratum = "확정 집합 불일치"
        elif any(subject in paper.subject_code for subject in ("법", "정책", "행정", "경찰")):
            stratum = "일치·법정책"
        else:
            stratum = "일치·기술"
        rows.append((stratum, paper, results))
    by_stratum: dict[str, list[tuple[str, lv.Paper, dict[int, dict[str, Any]]]]] = defaultdict(list)
    for row in rows:
        by_stratum[row[0]].append(row)
    quota = {"primary 불일치": 0.35, "확정 집합 불일치": 0.30, "일치·법정책": 0.15, "일치·기술": 0.20}
    chosen: list[tuple[str, lv.Paper, dict[int, dict[str, Any]]]] = []
    for stratum, share in quota.items():
        pool = sorted(by_stratum.get(stratum, []), key=lambda row: hashlib.sha256(row[1].source_id.encode()).hexdigest())
        chosen.extend(pool[: max(1, round(sample_size * share))])
    chosen = chosen[:sample_size] if len(chosen) > sample_size else chosen

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "taxonomy.txt").write_text(lv.taxonomy_listing(techniques) + "\n", encoding="utf-8")
    packets: list[dict[str, Any]] = []
    for stratum, paper, results in chosen:
        union: set[str] = set()
        for result in results.values():
            union.update(mapping["path_id"] for mapping in result["mappings"])
        packets.append(
            {
                "source_id": paper.source_id,
                "record_id": paper.record_id,
                "title": paper.display_title,
                "stratum": stratum,
                "text_path": str(lv.CACHE_DIR / "text" / f"{paper.source_id}.txt"),
                "taxonomy_path": str(AUDIT_DIR / "taxonomy.txt"),
                "candidates": [
                    {"path_id": path_id, "level_2": techniques[path_id].level_2, "level_3": techniques[path_id].level_3,
                     "definition": techniques[path_id].description}
                    for path_id in _stable_order(sorted(union), paper.source_id)
                ],
            }
        )
    (AUDIT_DIR / "packets.json").write_text(json.dumps(packets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    population = Counter(row[0] for row in rows)
    print(json.dumps({"population": population, "sampled": Counter(p["stratum"] for p in packets), "packets": len(packets)}, ensure_ascii=False))
    return packets


def score(verdict_path: Path, stamp: str | None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    techniques, _sha = lv.load_techniques()
    packets = {packet["source_id"]: packet for packet in json.loads((AUDIT_DIR / "packets.json").read_text(encoding="utf-8"))}
    reviews = json.loads(verdict_path.read_text(encoding="utf-8"))
    # Numbers are re-sorted when the corpus changes (Analyzer/numbering.py), so the id of a sampled
    # paper is looked up now, by KCI id — never copied from the packet written at sampling time.
    profile_index = lv.MANIFEST_DIR / "paper_profiles.json"
    current_record: dict[str, str] = json.loads(profile_index.read_text(encoding="utf-8")).get("records", {}) if profile_index.is_file() else {}
    # reviews: [{source_id, reviewer, verdicts: [{path_id, verdict, reason}], missing: [{path_id, reason}]}]
    by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        if review and review.get("source_id") in packets:
            by_paper[review["source_id"]].append(review)

    def consensus(source_id: str, path_id: str) -> tuple[str, list[str]]:
        votes = [
            item["verdict"] for review in by_paper[source_id] for item in review.get("verdicts", [])
            if item.get("path_id") == path_id and item.get("verdict") in VERDICTS
        ]
        if not votes:
            return "", votes
        if all(vote == "맞음" for vote in votes):
            return "맞음", votes
        if all(vote == "틀림" for vote in votes):
            return "틀림", votes
        return "애매", votes

    detail: list[list[Any]] = []
    case_counts: dict[int, Counter[str]] = {case: Counter() for case in lv.CASE_LABELS}
    band_counts: dict[str, Counter[str]] = defaultdict(Counter)
    recall: dict[int, Counter[str]] = {case: Counter() for case in lv.CASE_LABELS}
    for source_id, packet in packets.items():
        if not by_paper.get(source_id):
            continue
        reference = {c["path_id"] for c in packet["candidates"] if consensus(source_id, c["path_id"])[0] == "맞음"}
        missing_votes = Counter(item["path_id"] for review in by_paper[source_id] for item in review.get("missing", []) if item.get("path_id") in techniques)
        reference_with_missing = reference | {path_id for path_id, votes in missing_votes.items() if votes >= 2}
        for case in lv.CASE_LABELS:
            result = _result(case, source_id)
            if result is None:  # no current answer for this case (no PDF, or not classified yet)
                continue
            confirmed = {m["path_id"] for m in result["mappings"] if m["confidence"] >= lv.CONFIRMED_CONFIDENCE}
            recall[case]["reference"] += len(reference_with_missing)
            recall[case]["found"] += len(reference_with_missing & confirmed)
            for mapping in result["mappings"]:
                verdict, votes = consensus(source_id, mapping["path_id"])
                decision = lv.decision(mapping["confidence"])
                band = "0.90~1.00" if mapping["confidence"] >= 0.9 else "0.80~0.89" if mapping["confidence"] >= 0.8 else "0.65~0.79" if mapping["confidence"] >= 0.65 else "0.50~0.64"
                band_counts[band][verdict or "미검토"] += 1
                if decision == "확정":
                    case_counts[case][verdict or "미검토"] += 1
                detail.append(
                    [current_record.get(source_id, ""), source_id, packet["title"], packet["stratum"], case, mapping["rank"], mapping["path_id"],
                     techniques[mapping["path_id"]].level_3, mapping["confidence"], decision, verdict, " / ".join(votes)]
                )
        for path_id, votes in missing_votes.items():
            detail.append([current_record.get(source_id, ""), source_id, packet["title"], packet["stratum"], "누락 지적", "", path_id, techniques[path_id].level_3, "", "", f"검토자 {votes}명", ""])

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "케이스별 정확도"
    sheet.append(["케이스", "확정 배정 수", "맞음", "애매", "틀림", "정밀도(맞음/확정)", "엄격 오류율(틀림/확정)", "재현율(검토자 합의 기준 확정 포함 비율)"])
    for case in lv.CASE_LABELS:
        counts = case_counts[case]
        total = sum(counts[v] for v in VERDICTS)
        sheet.append([
            f"case {case}: {lv.CASE_LABELS[case]}", total, counts["맞음"], counts["애매"], counts["틀림"],
            f"{counts['맞음'] / total:.1%}" if total else "", f"{counts['틀림'] / total:.1%}" if total else "",
            f"{recall[case]['found'] / recall[case]['reference']:.1%}" if recall[case]["reference"] else "",
        ])
    bands = workbook.create_sheet("confidence 구간별 정확도")
    bands.append(["confidence 구간", "배정 수", "맞음", "애매", "틀림", "맞음 비율", "틀림 비율"])
    for band in ("0.90~1.00", "0.80~0.89", "0.65~0.79", "0.50~0.64"):
        counts = band_counts[band]
        total = sum(counts[v] for v in VERDICTS)
        bands.append([band, total, counts["맞음"], counts["애매"], counts["틀림"], f"{counts['맞음'] / total:.1%}" if total else "", f"{counts['틀림'] / total:.1%}" if total else ""])
    rows = workbook.create_sheet("검토 상세")
    rows.append(["record_id", "source_id", "title", "표본 층", "케이스", "rank", "LV3_id", "LV3", "confidence", "판정", "검토 합의", "검토자별 의견"])
    for row in detail:
        rows.append(row)
    notes = workbook.create_sheet("검토 방법")
    for line in (
        ["표본", f"{len([s for s in packets if by_paper.get(s)])}편 (primary 불일치·확정 집합 불일치·일치 논문을 층화 추출)"],
        ["검토자", "논문마다 독립된 검토자 2명이 본문 전체를 읽고, 어느 케이스가 낸 후보인지·confidence가 얼마인지 모르는 상태에서 후보별로 맞음/애매/틀림을 판정"],
        ["합의", "두 검토자 모두 맞음 = 맞음, 모두 틀림 = 틀림, 그 외 = 애매"],
        ["재현율 기준", "검토자 합의로 맞음인 후보 + 두 검토자가 모두 누락이라고 지적한 기술"],
        ["생성일", date.today().isoformat()],
    ):
        notes.append(line)
    for target in workbook.worksheets:
        for cell in target[1]:
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")
        for row in target.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        target.column_dimensions["A"].width = 44
        target.column_dimensions["C"].width = 40
    target_path = lv.LV_DIR / f"({stamp or date.today().strftime('%y%m%d')})-LV-Audit.xlsx"
    workbook.save(target_path)
    return target_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Level 실험 정확도 감사")
    sub = parser.add_subparsers(dest="command", required=True)
    packets = sub.add_parser("packets")
    packets.add_argument("--sample", type=int, default=60)
    scoring = sub.add_parser("score")
    scoring.add_argument("--verdicts", type=Path, default=AUDIT_DIR / "verdicts.json")
    scoring.add_argument("--date", default=None)
    args = parser.parse_args(argv)
    if args.command == "packets":
        build_packets(args.sample)
    else:
        print(f"wrote {score(args.verdicts, args.date)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
