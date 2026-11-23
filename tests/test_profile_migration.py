from __future__ import annotations

import io
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from Analyzer.MD.audit_profile_migration import (
    MigrationAuditError,
    _normalize,
    audit_migration,
    compare_document,
    new_record_id,
)


LEGACY = '''---
subject: "A paper"
identifier: "REP-DP-000001"
classification:
  - EXM-04-01 # 애플리케이션 아티팩트 조사
tags:
  - APP-CST
  - CLOUD-SaaS:STO
last_updated: "2026-09-09"
author: "조민혁"
---

# A paper

## 1. 논문 정보
| 항목 | 내용 |
|---|---|
| DOI | N/A |
| URL | https://www.kci.go.kr/?artiId=ART002577216 |
| 권호 | 14_1 |
| OA 여부 | Not Open Access |
| 저자 | A; B |

## 2. 초록
<div>
Original abstract.
</div>

## 3. 키워드
Dropbox, NTFS

## 4. 대상 애플리케이션
| ID | 이름 |
|---|---|
|  | Dropbox |

## 5. 참고문헌
| ID | 제목 |
|---|---|
| REP-DP-000001 | [A citation](https://example.org/) |
'''


def migrated(text: str = LEGACY, language: str = "ko") -> str:
    return _normalize(text, language, legacy=True).replace('last_updated: "<date>"', 'last_updated: "2026-09-14"')


def archive_and_md(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "before.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for language in ("kor", "eng"):
            raw = LEGACY.encode()
            member = tarfile.TarInfo(f"Analyzer/Paper/MD/{language}/REP-DP-000001.md")
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
    md = tmp_path / "MD"
    directory = md / "KNOW-DOMP-0000001"
    directory.mkdir(parents=True)
    for language in ("ko", "en"):
        (directory / f"{language}.md").write_text(migrated(language=language), encoding="utf-8")
    return archive, md


def test_identifier_conversion_is_strict_and_preserves_ordinal() -> None:
    assert new_record_id("REP-DP-000001") == "KNOW-DOMP-0000001"
    assert new_record_id("REP-DP-000527") == "KNOW-DOMP-0000527"
    with pytest.raises(MigrationAuditError):
        new_record_id("REF-DP-000001")


def test_only_approved_presentation_changes_pass() -> None:
    result = compare_document(LEGACY, migrated(), "REP-DP-000001", "ko")
    assert result["source_id"] == "ART002577216"
    assert result["classification"] == ["EXAM-04-01"]
    assert result["tags"] == ["APP-CST", "CLOUD-SaaS:CST"]
    assert result["reference_count"] == 1
    assert result["internal_reference_ids"] == ["KNOW-DOMP-0000001"]
    assert result["application_count"] == 1


@pytest.mark.parametrize("before,after", [
    ("Original abstract.", "Changed abstract."),
    ("Dropbox, NTFS", "Dropbox"),
    ("|  | Dropbox |", "| TGT-0001 | Dropbox |"),
    ("A citation", "Changed citation"),
    ("APP-CST", "APP-WEB"),
    ("EXAM-04-01", "EXAM-04-02"),
    ("| OA 여부 | X |", "| OA 여부 | O |"),
    ("| 권호 | 14권 1호 |", "| 권호 | 14권 2호 |"),
])
def test_evidence_changes_are_rejected(before: str, after: str) -> None:
    with pytest.raises(MigrationAuditError):
        compare_document(LEGACY, migrated().replace(before, after), "REP-DP-000001", "ko")


def test_lossless_bilingual_directory_audit(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    result = audit_migration(archive, md, expected_papers=1)
    assert result["status"] == "passed"
    assert result["markdown_count"] == 2
    assert result["languages"]["ko"]["reference_count"] == 1


def test_duplicate_or_missing_language_is_rejected(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    (md / "KNOW-DOMP-0000001" / "en.md").unlink()
    with pytest.raises(MigrationAuditError, match="file set mismatch"):
        audit_migration(archive, md, expected_papers=1)


def test_legacy_dirs_require_explicit_pre_removal_flag(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    (md / "kor").mkdir()
    with pytest.raises(MigrationAuditError, match="Legacy"):
        audit_migration(archive, md, expected_papers=1)
    assert audit_migration(archive, md, expected_papers=1, allow_legacy=True)["status"] == "passed"


def test_dangling_registered_reference_is_rejected(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    text = migrated().replace("| KNOW-DOMP-0000001 |", "| KNOW-DOMP-0000002 |")
    (md / "KNOW-DOMP-0000001" / "ko.md").write_text(text, encoding="utf-8")
    with pytest.raises(MigrationAuditError):
        audit_migration(archive, md, expected_papers=1)


def test_symlink_document_is_rejected(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    path = md / "KNOW-DOMP-0000001" / "ko.md"
    replacement = tmp_path / "outside.md"
    path.rename(replacement)
    path.symlink_to(replacement)
    with pytest.raises(MigrationAuditError, match="Unsafe"):
        audit_migration(archive, md, expected_papers=1)


def test_unexpected_non_md_file_is_rejected(tmp_path: Path) -> None:
    archive, md = archive_and_md(tmp_path)
    (md / "KNOW-DOMP-0000001" / "unexpected.txt").write_text("data", encoding="utf-8")
    with pytest.raises(MigrationAuditError, match="Unexpected file"):
        audit_migration(archive, md, expected_papers=1)


def graphs_and_md(tmp_path: Path) -> tuple[Path, Path, Path]:
    archive, md = archive_and_md(tmp_path)
    graph_dir = tmp_path / "web" / "data" / "graph"
    asset_dir = graph_dir / "papers" / "KNOW-DOMP-0000001"
    asset_dir.mkdir(parents=True)
    hashes = {}
    for language in ("ko", "en"):
        raw = (md / "KNOW-DOMP-0000001" / f"{language}.md").read_bytes()
        (asset_dir / f"{language}.md").write_bytes(raw)
        hashes[language] = hashlib.sha256(raw).hexdigest()
    old_paper = {
        "record_id": "REP-DP-000001", "author_ids": ["author-1"],
        "author_names": ["A", "B"], "affiliations": [],
    }
    old_graph = {
        "papers": {"ART002577216": old_paper}, "citation_edges": [],
        "authors": [{"id": "author-1", "paper_ids": ["ART002577216"]}],
        "institutions": [{"id": "institution-1", "paper_ids": ["ART002577216"]}],
    }
    new_graph = {
        **old_graph,
        "papers": {"ART002577216": {
            **old_paper, "record_id": "KNOW-DOMP-0000001",
            "md": {language: f"./data/graph/papers/KNOW-DOMP-0000001/{language}.md" for language in ("ko", "en")},
            "md_sha256": hashes,
        }},
    }
    with tarfile.open(archive, "r:gz") as stream:
        members = [(member, stream.extractfile(member).read()) for member in stream.getmembers()]
    with tarfile.open(archive, "w:gz") as stream:
        for member, raw in members:
            stream.addfile(member, io.BytesIO(raw))
        for name in ("author", "institution", "reference"):
            raw = json.dumps(old_graph).encode()
            member = tarfile.TarInfo(f"web/DF-Insight/data/graph/{name}-graph.json")
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
            (graph_dir / f"{name}-graph.json").write_text(json.dumps(new_graph), encoding="utf-8")
    return archive, md, graph_dir


def test_graphs_keep_memberships_citations_and_identical_md_copies(tmp_path: Path) -> None:
    archive, md, graphs = graphs_and_md(tmp_path)
    report = audit_migration(archive, md, expected_papers=1, graph_dir=graphs)
    assert set(report["graphs"]) == {"author", "institution", "reference"}
    assert report["graphs"]["reference"]["bilingual_copies_verified"] == 2


@pytest.mark.parametrize("change", ["citation", "membership", "hash", "path"])
def test_graph_regressions_are_rejected(tmp_path: Path, change: str) -> None:
    archive, md, graphs = graphs_and_md(tmp_path)
    path = graphs / "author-graph.json"
    payload = json.loads(path.read_text())
    if change == "citation":
        payload["citation_edges"] = [{"source": "ART002577216", "target": "ART002577216", "method": "kci_link", "reference_numbers": [1]}]
    elif change == "membership":
        payload["authors"][0]["paper_ids"] = []
    elif change == "hash":
        payload["papers"]["ART002577216"]["md_sha256"]["ko"] = "0" * 64
    else:
        payload["papers"]["ART002577216"]["md"]["ko"] = "../../outside.md"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MigrationAuditError):
        audit_migration(archive, md, expected_papers=1, graph_dir=graphs)


def test_modified_web_md_copy_is_rejected(tmp_path: Path) -> None:
    archive, md, graphs = graphs_and_md(tmp_path)
    path = graphs / "papers" / "KNOW-DOMP-0000001" / "ko.md"
    path.write_text(path.read_text().replace("Original abstract.", "Wrong copy."), encoding="utf-8")
    with pytest.raises(MigrationAuditError, match="differs"):
        audit_migration(archive, md, expected_papers=1, graph_dir=graphs)
