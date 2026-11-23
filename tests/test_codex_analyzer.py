from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from PaperPipeline.codex_analyzer import (
    CodexAnalysisError,
    CodexConfig,
    CodexPaperAnalyzer,
    build_codex_output_schema,
    build_codex_prompt,
    validate_model_output,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths
from PaperPipeline.taxonomy import load_taxonomy
from PaperPipeline.text_extractor import TextExtractionResult


def _pdf_escape(text: str) -> bytes:
    return text.encode().replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _write_text_pdf(path: Path, text: str) -> None:
    stream = b"BT /F1 12 Tf 72 720 Td (" + _pdf_escape(text) + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(content)


def _taxonomy(tmp_path: Path):
    path = tmp_path / "taxonomy.json"
    path.write_text(
        json.dumps({"ANL-03 · 앱": {"ANL-03-01": "애플리케이션 아티팩트 조사"}}),
        encoding="utf-8",
    )
    return load_taxonomy(path)


def _paper() -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART1",
        title="Android App Forensics",
        doi_original="https://doi.org/10.1234/test",
        doi="10.1234/test",
        landing_url=None,
        abstract=None,
        publication_year=2024,
        first_page="1",
        final_page="1",
    )


def _model_output(excerpt: str = "Android application artifacts") -> dict:
    evidence = [{"page": 1, "excerpt": excerpt}]
    return {
        "summary_ko": "안드로이드 앱 아티팩트를 분석한 연구이다.",
        "taxonomy_mappings": [
            {
                "code": "ANL-03-01",
                "confidence": 0.98,
                "rationale_ko": "앱 아티팩트를 직접 조사한다.",
                "evidence": evidence,
            }
        ],
        "target_devices": [
            {
                "canonical_name": "Android smartphone",
                "device_type": "smartphone",
                "manufacturer": None,
                "model": None,
                "confidence": 0.9,
                "evidence": evidence,
            }
        ],
        "target_operating_systems": [
            {
                "name": "Android",
                "version": "14",
                "distribution": None,
                "related_device": "Android smartphone",
                "confidence": 0.9,
                "evidence": evidence,
            }
        ],
        "target_applications": [
            {
                "name": "Example App",
                "version": None,
                "vendor": None,
                "package_identifier": "org.example.app",
                "platform": "Android",
                "confidence": 0.8,
                "evidence": evidence,
            }
        ],
        "limitations": [],
    }


def test_schema_enumerates_only_loaded_level_three_codes(tmp_path: Path) -> None:
    taxonomy = _taxonomy(tmp_path)
    schema = build_codex_output_schema(taxonomy)
    enum = schema["properties"]["taxonomy_mappings"]["items"]["properties"]["code"]["enum"]
    assert enum == ["ANL-03-01"]
    assert schema["additionalProperties"] is False


def test_semantic_validation_rejects_hallucinated_evidence(tmp_path: Path) -> None:
    taxonomy = _taxonomy(tmp_path)
    with pytest.raises(CodexAnalysisError, match="not present"):
        validate_model_output(
            _model_output("This sentence is fabricated"),
            taxonomy=taxonomy,
            paper_pages={1: "Android application artifacts were extracted."},
        )


def test_semantic_validation_preserves_multiple_versions_of_same_target(
    tmp_path: Path,
) -> None:
    taxonomy = _taxonomy(tmp_path)
    value = _model_output()
    second_os = dict(value["target_operating_systems"][0])
    second_os["version"] = "13"
    value["target_operating_systems"].append(second_os)

    validated = validate_model_output(
        value,
        taxonomy=taxonomy,
        paper_pages={1: "Android application artifacts were extracted."},
    )

    assert [item["version"] for item in validated["target_operating_systems"]] == [
        "14",
        "13",
    ]


def test_analyzer_writes_provenance_and_reuses_current_result(tmp_path: Path) -> None:
    taxonomy = _taxonomy(tmp_path)
    paths = ArtifactPaths.for_source(tmp_path, "ART1")
    _write_text_pdf(paths.pdf, "Android application artifacts on Android 14")
    calls: list[list[str]] = []

    def fake_runner(command: list[str], **kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="codex-cli test\n", stderr="")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(_model_output()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    analyzer = CodexPaperAnalyzer(
        CodexConfig(
            executable=sys.executable,
            model="test-model",
            retries=0,
            min_text_chars=10,
        ),
        repository_root=Path(__file__).resolve().parents[1],
        runner=fake_runner,
    )

    first = analyzer.analyze(_paper(), paths, taxonomy)
    second = analyzer.analyze(_paper(), paths, taxonomy)

    assert first["status"] == "completed"
    assert first["taxonomy"]["mappings"][0]["name"] == "애플리케이션 아티팩트 조사"
    assert first["targets"]["operating_systems"][0]["name"] == "Android"
    assert first["artifacts"]["pdf_sha256"]
    assert first["provenance"]["backend"] == "codex-cli"
    assert second["_runtime"]["action"] == "reused"
    assert sum(1 for command in calls if "exec" in command) == 1


def test_prompt_marks_paper_as_untrusted(tmp_path: Path) -> None:
    taxonomy = _taxonomy(tmp_path)
    extraction = TextExtractionResult(
        source_path=tmp_path / "paper.pdf",
        output_path=tmp_path / "paper.txt",
        page_count=1,
        raw_char_count=100,
        written_char_count=100,
        output_char_count=120,
        page_char_counts=(100,),
        written_page_char_counts=(100,),
        truncated=False,
        sha256="0" * 64,
    )
    prompt = build_codex_prompt(
        paper=_paper(),
        text_path=extraction.output_path,
        taxonomy=taxonomy,
        extraction=extraction,
    )
    assert "신뢰할 수 없는 데이터" in prompt
    assert "네트워크를 사용하거나 파일을 수정하지 마세요" in prompt
    assert "ANL-03-01" in prompt
