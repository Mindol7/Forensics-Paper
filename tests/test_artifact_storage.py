from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from PaperPipeline import storage
from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    PaperFileIndexError,
    build_paper_file_records,
    extend_paper_file_index_if_present,
    load_paper_file_index,
    paper_file_index_document,
    title_pdf_filename,
)
from PaperPipeline.storage import (
    ArtifactPaths,
    load_json_object,
    safe_source_stem,
    sha256_file,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)


def test_safe_source_stem_sanitizes_traversal_and_limits_length() -> None:
    first = safe_source_stem(" KCI:ARTI/123 ")
    second = safe_source_stem("../../etc/passwd")
    controlled = safe_source_stem("a\x00b\tc")
    long_value = safe_source_stem("x" * 200)
    assert first.startswith("KCI_ARTI_123--")
    assert second.startswith("etc_passwd--")
    assert controlled.startswith("a_b_c--")
    assert long_value.startswith("x" * 148 + "--")
    assert len(long_value) == 160
    assert first != safe_source_stem("KCI/ARTI:123")


@pytest.mark.parametrize("source_id", ["", " ", ".", "..", "...", "한글", "\x00"])
def test_safe_source_stem_rejects_empty_or_fully_unsafe_values(source_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe or empty source_id"):
        safe_source_stem(source_id)


def test_artifact_paths_keep_every_artifact_beneath_the_requested_root(
    tmp_path: Path,
) -> None:
    paths = ArtifactPaths.for_source(tmp_path, "../../KCI:ARTI/123")

    assert paths.root == tmp_path
    assert paths.stem.startswith("KCI_ARTI_123--")
    assert paths.pdf == tmp_path / f"{paths.stem}.pdf"
    assert paths.download_manifest == tmp_path / f"{paths.stem}.download.json"
    assert paths.text == tmp_path / f"{paths.stem}.txt"
    assert paths.analysis == tmp_path / f"{paths.stem}.json"
    assert paths.analysis_error == tmp_path / f"{paths.stem}.analysis-error.json"
    assert paths.lock == tmp_path / f".{paths.stem}.lock"
    assert all(
        path.parent == tmp_path
        for path in (
            paths.pdf,
            paths.download_manifest,
            paths.text,
            paths.analysis,
            paths.analysis_error,
            paths.lock,
        )
    )


def test_artifact_paths_can_separate_pdf_and_state_roots(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "Analyzer" / "Paper"
    manifest_root = tmp_path / "Analyzer" / "Manifest"
    paths = ArtifactPaths.for_source(
        paper_root,
        "ART000000001",
        manifest_root=manifest_root,
    )

    assert paths.root == paper_root
    assert paths.state_root == manifest_root
    assert paths.pdf == paper_root / "ART000000001.pdf"
    assert paths.download_manifest == manifest_root / "ART000000001.download.json"
    assert paths.text == manifest_root / "ART000000001.txt"
    assert paths.analysis == manifest_root / "ART000000001.json"
    assert paths.analysis_error == manifest_root / "ART000000001.analysis-error.json"
    assert paths.lock == manifest_root / ".ART000000001.lock"


def test_title_pdf_filename_is_portable_and_collision_safe() -> None:
    assert title_pdf_filename("KakaoTalk forensic study") == (
        "KakaoTalk forensic study.pdf"
    )
    assert title_pdf_filename("APFS/NTFS: A comparison") == (
        "APFS∕NTFS∶ A comparison.pdf"
    )
    assert title_pdf_filename("CON") == "＿CON.pdf"
    assert title_pdf_filename(".hidden study") == "＿.hidden study.pdf"
    records = build_paper_file_records(
        [("ART1", "같은 제목"), ("ART2", "같은 제목")]
    )
    assert [record.filename for record in records] == [
        "같은 제목 [ART1].pdf",
        "같은 제목 [ART2].pdf",
    ]
    assert all(len(record.filename.encode("utf-8")) <= 255 for record in records)


def test_atomic_write_supports_a_maximum_length_title_filename(tmp_path: Path) -> None:
    filename = title_pdf_filename("가" * 200, source_id="ART000000001")
    target = tmp_path / filename
    assert len(filename.encode("utf-8")) <= 255

    write_bytes_atomic(target, b"%PDF-long-name")

    assert target.read_bytes() == b"%PDF-long-name"
    assert not list(tmp_path.glob("*.part"))


def test_artifact_paths_resolve_title_pdf_without_renaming_state_files(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "Analyzer" / "Paper"
    manifest_root = tmp_path / "Analyzer" / "Manifest"
    manifest_root.mkdir(parents=True)
    records = build_paper_file_records([("ART000000001", "논문 제목")])
    (manifest_root / PAPER_FILE_INDEX_NAME).write_text(
        json.dumps(paper_file_index_document(records), ensure_ascii=False),
        encoding="utf-8",
    )

    paths = ArtifactPaths.for_source(
        paper_root,
        "ART000000001",
        manifest_root=manifest_root,
    )

    assert paths.pdf == paper_root / "논문 제목.pdf"
    assert paths.download_manifest == manifest_root / "ART000000001.download.json"
    assert paths.lock == manifest_root / ".ART000000001.lock"

    with pytest.raises(PaperFileIndexError, match="missing from existing"):
        ArtifactPaths.for_source(
            paper_root,
            "ART000000002",
            manifest_root=manifest_root,
        )


def test_artifact_paths_reject_unsafe_explicit_pdf_filename(tmp_path: Path) -> None:
    with pytest.raises(PaperFileIndexError, match="unsafe PDF filename"):
        ArtifactPaths.for_source(
            tmp_path,
            "ART000000001",
            pdf_filename="../../outside.pdf",
        )


def test_existing_paper_file_index_is_extended_append_only(tmp_path: Path) -> None:
    paper_root = tmp_path / "Paper"
    manifest_root = tmp_path / "Manifest"
    paper_root.mkdir()
    manifest_root.mkdir()
    index_path = manifest_root / PAPER_FILE_INDEX_NAME
    initial = build_paper_file_records([("ART1", "첫 논문")])
    index_path.write_text(
        json.dumps(paper_file_index_document(initial), ensure_ascii=False),
        encoding="utf-8",
    )

    result = extend_paper_file_index_if_present(
        index_path,
        [("ART1", "DB 표기가 달라진 첫 논문"), ("ART2", "둘째/논문")],
        papers_dir=paper_root,
    )

    assert result["ART1"].filename == "첫 논문.pdf"
    assert result["ART2"].filename == "둘째∕논문.pdf"
    assert load_paper_file_index(index_path) == result


def test_paper_file_index_rejects_duplicate_or_unsafe_filename(tmp_path: Path) -> None:
    index_path = tmp_path / PAPER_FILE_INDEX_NAME
    document = paper_file_index_document(
        build_paper_file_records([("ART1", "첫 논문"), ("ART2", "둘째 논문")])
    )
    document["papers"][1]["filename"] = "첫 논문.pdf"
    index_path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(PaperFileIndexError, match="duplicate source_id or filename"):
        load_paper_file_index(index_path)


def test_atomic_byte_and_text_writes_replace_content_and_hash_in_chunks(
    tmp_path: Path,
) -> None:
    binary_path = tmp_path / "nested" / "paper.pdf"
    original = b"%PDF-old"
    replacement = b"%PDF-new-content"

    write_bytes_atomic(binary_path, original)
    binary_path.chmod(0o640)
    assert binary_path.read_bytes() == original
    write_bytes_atomic(binary_path, replacement)

    assert binary_path.read_bytes() == replacement
    assert sha256_file(binary_path, chunk_size=3) == hashlib.sha256(
        replacement
    ).hexdigest()
    assert binary_path.stat().st_mode & 0o777 == 0o640
    assert not list(binary_path.parent.glob(f".{binary_path.name}.*.part"))

    text_path = tmp_path / "nested" / "paper.txt"
    write_text_atomic(text_path, "한글 forensic text")
    assert text_path.read_bytes() == "한글 forensic text".encode("utf-8")
    assert not list(text_path.parent.glob(f".{text_path.name}.*.part"))


def test_atomic_write_failure_preserves_old_target_and_removes_part_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "manifest.json"
    target.write_bytes(b"old")

    def fail_replace(source: Path | str, destination: Path | str) -> None:
        raise OSError(f"cannot replace {destination}")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(OSError, match="cannot replace"):
        write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(f".{target.name}.*.part"))


def test_json_write_is_utf8_formatted_and_loads_only_objects(tmp_path: Path) -> None:
    path = tmp_path / "analysis.json"
    value = {
        "source_id": "KCI-1",
        "대상 디바이스": ["스마트폰"],
        "nested": {"matched": True},
    }

    write_json_atomic(path, value)
    raw = path.read_bytes()

    assert raw.endswith(b"\n")
    assert b"\\ud" not in raw
    assert json.loads(raw.decode("utf-8")) == value
    assert load_json_object(path) == value
    assert not list(tmp_path.glob(f".{path.name}.*.part"))

    array_path = tmp_path / "array.json"
    array_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json_object(array_path) is None

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{", encoding="utf-8")
    assert load_json_object(invalid_path) is None

    non_utf8_path = tmp_path / "non-utf8.json"
    non_utf8_path.write_bytes(b'{"value":"\xff"}')
    assert load_json_object(non_utf8_path) is None
    assert load_json_object(tmp_path / "missing.json") is None
