from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from PaperPipeline.claude_analyzer import (
    ANALYSIS_RESPONSE_SCHEMA,
    STRUCTURE_RESPONSE_SCHEMA,
    AnalysisVocabulary,
    ClaudeAnalysisError,
    ClaudeConfig,
    ClaudePaperAnalyzer,
    ClaudeRefusalError,
    TaxonomyEntry,
    build_analysis_system_prompt,
    prepare_input,
    validate_analysis_response,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths


VOCABULARY = AnalysisVocabulary(
    taxonomy=(
        TaxonomyEntry("EXM-04-01", "EXM-04", "애플리케이션 및 파일 포렌식", "Application and File Forensics", "애플리케이션 아티팩트 조사", "Application Artifact Examination"),
        TaxonomyEntry("COL-02-04", "COL-02", "네트워크 및 클라우드 데이터 수집", "Network and Cloud Data Collection", "클라우드 데이터 수집", "Cloud Data Collection"),
    ),
    taxonomy_sha256="a" * 64,
    application_categories=("클라우드 스토리지", "메신저"),
)


def _paper(source_id: str = "ART000000001") -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=source_id,
        title="드롭박스 흔적 조사",
        doi_original=None,
        doi=None,
        landing_url=None,
        abstract="Windows PC의 Dropbox 흔적을 분석한다.",
        publication_year=2026,
        first_page="1",
        final_page="12",
        authors=("홍길동(테스트대학교)",),
        journal_id="디지털포렌식연구",
    )


def _write_pdf(path: Path, pages: int = 3) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)


def _analysis_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "relevance": {
            "verdict": "관련",
            "research_type": "EMPIRICAL_ARTIFACT",
            "summary_ko": "Windows PC에 남은 Dropbox 흔적과 NTFS 시간정보를 함께 분석한 연구다.",
            "summary_en": "The paper analyzes Dropbox traces on Windows PCs together with NTFS timestamps.",
            "rationale_ko": "실제 PC에서 Dropbox 아티팩트를 직접 관찰하고 실험하였다.",
            "explicit_limitations_ko": "",
            "explicit_limitations_en": "",
            "scope_evidence": {
                "status": "OBS",
                "pages": [{"start": 1, "end": 2}, {"start": 3, "end": 3}],
                "support_ko": "PDF pp.1–2는 분석 대상을, p.3은 실험 결과를 제시한다.",
                "support_en": "Pages 1–2 present the targets and page 3 the experiment.",
            },
        },
        "keywords": {
            "device": ["PC"],
            "filesystem": ["NTFS"],
            "operating_system": ["Windows"],
            "application": [{"name": "Dropbox", "category": "클라우드 스토리지"}],
            "cloud": ["SaaS"],
            "rationale_ko": "연구가 직접 다룬 증거 대상만 기록하였다.",
        },
        "level3": {
            "mappings": [
                {
                    "path_id": "EXM-04-01",
                    "confidence": 0.97,
                    "rationale_ko": "Dropbox 로컬 흔적을 직접 분석해 업로드와 동기화 행위를 판별한다.",
                }
            ],
            "not_applicable_reason_ko": "",
        },
        "document_type": {
            "subtype": "ar",
            "confidence": 0.95,
            "boundary_candidate": False,
            "rationale_ko": "실증 아티팩트 분석에 해당하는 독자적 연구 기여이며 리뷰나 학위논문이 아니다.",
            "boundary_reason_ko": "di/cp/dp/re/sh 성립 요건을 배제했다.",
        },
        "english_title": "Investigating traces of a Dropbox application for Windows",
    }
    for key, value in overrides.items():
        payload[key] = value
    return payload


def _structure_payload() -> dict[str, Any]:
    evidence = {
        "status": "OBS",
        "pages": [{"start": 3, "end": 3}],
        "support_ko": "직접 대상 맥락에서 명시된 값이다.",
        "support_en": "Value stated in direct-target context.",
    }
    return {
        "identification": {
            "version": "",
            "root_path": "%USERPROFILE%\\AppData\\Local\\Dropbox",
            "root_path_status": "OBS",
            "install_check_path": "",
            "identification_evidence": evidence,
        },
        "features": [
            {
                "name_ko": "직접 아티팩트 분석",
                "name_en": "Direct artifact analysis",
                "description_ko": "Dropbox 흔적을 분석한다.",
                "description_en": "Analyzes Dropbox traces.",
                "default_provided_ko": "",
                "default_provided_en": "",
            }
        ],
        "preservation_ko": "",
        "preservation_en": "",
        "artifacts": [
            {
                "name": "home.db",
                "related_feature": "직접 아티팩트 분석",
                "status": "OBS",
                "path": "",
                "access_privilege_ko": "",
                "access_privilege_en": "",
                "collection_method_ko": "",
                "collection_method_en": "",
                "decryption_required_ko": "",
                "decryption_required_en": "",
                "decryption_dependencies_ko": "",
                "decryption_dependencies_en": "",
                "storage_format_ko": "정형",
                "storage_format_en": "Structured",
                "stored_content_ko": "",
                "stored_content_en": "",
                "retention_ko": "",
                "retention_en": "",
                "default_provided_ko": "",
                "default_provided_en": "",
                "evidence": evidence,
            }
        ],
        "decryption_ko": "",
        "decryption_en": "",
        "anti_forensics_ko": "",
        "anti_forensics_en": "",
        "tools": [],
        "excluded_candidate_count": 0,
    }


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    stop_details: Any = None


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


def _json_response(payload: dict[str, Any]) -> _Response:
    return _Response(content=[_Block("text", json.dumps(payload, ensure_ascii=False))])


def _analyzer(client: _FakeClient, **config: Any) -> ClaudePaperAnalyzer:
    config.setdefault("backend", "api")
    return ClaudePaperAnalyzer(
        ClaudeConfig(model="claude-opus-5", **config),
        client_factory=lambda _config: client,
    )


def _paths(tmp_path: Path) -> ArtifactPaths:
    papers_dir = tmp_path / "Paper"
    manifest_dir = tmp_path / "Manifest"
    _write_pdf(papers_dir / "논문.pdf")
    return ArtifactPaths.for_source(
        papers_dir, "ART000000001", manifest_root=manifest_dir, pdf_filename="논문.pdf"
    )


def test_two_call_analysis_returns_a_validated_record(tmp_path: Path) -> None:
    client = _FakeClient([_json_response(_analysis_payload()), _json_response(_structure_payload())])
    record = _analyzer(client).analyze(_paper(), _paths(tmp_path), VOCABULARY)

    assert record["schema_version"] == "2.0-claude"
    assert record["provenance"]["input_mode"] == "pdf"
    assert record["provenance"]["pdf_page_count"] == 3
    assert record["provenance"]["taxonomy_sha256"] == "a" * 64
    # Adjacent ranges (1–2 and 3) are merged into one span.
    assert record["relevance"]["scope_evidence"]["pages"] == "PDF pp. 1–3"
    assert record["level3"]["mappings"] == [
        {
            "rank": 1,
            "role": "primary",
            "path_id": "EXM-04-01",
            "confidence": 0.97,
            "rationale": "Dropbox 로컬 흔적을 직접 분석해 업로드와 동기화 행위를 판별한다.",
        }
    ]
    assert record["keywords"]["application"] == [{"name": "Dropbox", "category": "클라우드 스토리지"}]
    assert record["structure"]["artifacts"][0]["name"] == "home.db"
    assert record["structure"]["artifacts"][0]["evidence"]["pages"] == "PDF p. 3"
    assert record["review"]["status"] == "approved"
    assert record["review"]["mode"] == "automated"
    assert record["usage"]["calls"] == 2
    assert record["doi"] is None
    # Blank fixture pages have no text layer, so the decision is forced into
    # boundary review exactly as the type exporter demands for image-only PDFs.
    assert record["document_type"]["boundary_candidate"] is True
    assert "텍스트 계층" in record["document_type"]["boundary_reason"]

    # Requests use structured outputs against the declared schemas and cache the
    # shared rule prefix; the document travels as a PDF block.
    first, second = client.messages.requests
    assert first["model"] == "claude-opus-5"
    assert first["output_config"]["format"] == {"type": "json_schema", "schema": ANALYSIS_RESPONSE_SCHEMA}
    assert second["output_config"]["format"]["schema"] == STRUCTURE_RESPONSE_SCHEMA
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["messages"][0]["content"][0]["type"] == "document"
    assert first["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"
    assert "thinking" not in first
    assert "EXM-04-01" in build_analysis_system_prompt(VOCABULARY)


def test_non_forensic_papers_skip_the_structure_call(tmp_path: Path) -> None:
    payload = _analysis_payload(
        relevance={
            **_analysis_payload()["relevance"],
            "verdict": "비관련",
            "research_type": "NON_FORENSIC",
        },
        level3={"mappings": [], "not_applicable_reason_ko": "위변조 이미지를 생성하는 방법만 제안하며 증거 분석을 수행하지 않는다."},
    )
    client = _FakeClient([_json_response(payload)])
    record = _analyzer(client).analyze(_paper(), _paths(tmp_path), VOCABULARY)

    assert record["structure"] is None
    assert record["level3"]["mappings"] == []
    assert record["level3"]["not_applicable_reason"].startswith("위변조")
    # Non-forensic papers never carry evidence targets.
    assert record["keywords"]["application"] == []
    assert len(client.messages.requests) == 1


def test_dry_run_prepares_the_input_without_calling_the_api(tmp_path: Path) -> None:
    client = _FakeClient([])
    record = _analyzer(client, dry_run=True).analyze(_paper(), _paths(tmp_path), VOCABULARY)

    assert record["dry_run"] is True
    assert record["provenance"]["pdf_page_count"] == 3
    assert "relevance" not in record
    assert client.messages.requests == []


def test_refusal_and_truncation_are_reported_not_parsed(tmp_path: Path) -> None:
    refusal = _Response(content=[], stop_reason="refusal")
    with pytest.raises(ClaudeRefusalError):
        _analyzer(_FakeClient([refusal])).analyze(_paper(), _paths(tmp_path), VOCABULARY)
    truncated = _Response(content=[_Block("text", "{")], stop_reason="max_tokens")
    with pytest.raises(ClaudeAnalysisError, match="max_tokens"):
        _analyzer(_FakeClient([truncated])).analyze(_paper(), _paths(tmp_path), VOCABULARY)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda p: p["level3"]["mappings"][0].__setitem__("path_id", "ZZZ-99-99"), "unknown Level-3"),
        (lambda p: p["level3"]["mappings"][0].__setitem__("confidence", 0.5), "below"),
        (lambda p: p["keywords"]["application"][0].__setitem__("category", "게임기"), "outside the fixed vocabulary"),
        (lambda p: p["relevance"]["scope_evidence"].__setitem__("pages", [{"start": 2, "end": 9}]), "outside PDF pages"),
        (lambda p: p["relevance"].__setitem__("research_type", "NON_FORENSIC"), "must agree"),
        (lambda p: p["level3"].__setitem__("mappings", []), "at least one Level-3 mapping"),
        (lambda p: p["document_type"].__setitem__("confidence", 0.6), "review threshold"),
    ],
)
def test_semantic_validation_rejects_inconsistent_responses(mutation, message) -> None:
    payload = _analysis_payload()
    mutation(payload)
    with pytest.raises(ClaudeAnalysisError, match=message):
        validate_analysis_response(payload, vocabulary=VOCABULARY, page_count=3)


def test_non_ar_and_review_survey_decisions_are_boundary_candidates() -> None:
    payload = _analysis_payload()
    payload["document_type"].update(subtype="re", boundary_candidate=False)
    record = validate_analysis_response(payload, vocabulary=VOCABULARY, page_count=3)
    assert record["document_type"]["boundary_candidate"] is True

    payload = _analysis_payload()
    payload["relevance"]["research_type"] = "REVIEW_SURVEY"
    record = validate_analysis_response(payload, vocabulary=VOCABULARY, page_count=3)
    assert record["document_type"]["boundary_candidate"] is True


def test_oversized_pdf_falls_back_to_the_extracted_text(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config = ClaudeConfig(max_pdf_bytes=10, min_text_chars=1)
    # Blank pages yield no text, so the fallback is impossible and must fail loudly.
    with pytest.raises(ClaudeAnalysisError, match="yields no text"):
        prepare_input(paths, config)


def test_prepared_input_records_pdf_provenance(tmp_path: Path) -> None:
    prepared = prepare_input(_paths(tmp_path), ClaudeConfig())
    assert prepared.mode == "pdf"
    assert len(prepared.pdf_sha256) == 64
    assert prepared.pdf_page_count == 3
    # Blank pages produce no extractable text; provenance says so explicitly.
    assert prepared.text_sha256 is None


# --------------------------------------------------------------------------- #
# Claude Code CLI transport
# --------------------------------------------------------------------------- #

def _cli_payload(structured: dict[str, Any] | None, *, is_error: bool = False, result: str = "") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success" if not is_error else "error",
            "is_error": is_error,
            "result": result or (json.dumps(structured, ensure_ascii=False) if structured else ""),
            "structured_output": structured,
            "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
            "modelUsage": {"claude-haiku-4-5-20251001": {}, "claude-fable-5-1": {}},
        },
        ensure_ascii=False,
    )


class _FakeCli:
    def __init__(self, outputs: list[tuple[int, str, str]]) -> None:
        self.outputs = list(outputs)
        self.commands: list[list[str]] = []

    def __call__(self, command, *, timeout):
        self.commands.append(list(command))
        return self.outputs.pop(0)


def _cli_config(tmp_path: Path, **overrides: Any) -> ClaudeConfig:
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    overrides.setdefault("model", "claude-opus-5")
    return ClaudeConfig(backend="claude-code", claude_code_executable=str(exe), **overrides)


def test_claude_code_transport_builds_a_headless_structured_command(tmp_path: Path) -> None:
    from PaperPipeline.claude_analyzer import AnalysisUsage, ClaudeCodeTransport

    cli = _FakeCli([(0, _cli_payload({"answer": 1}), "")])
    transport = ClaudeCodeTransport(_cli_config(tmp_path), runner=cli)
    usage = AnalysisUsage()
    structured, model = transport.request(
        system_prompt="규칙", prompt="질문", schema={"type": "object", "properties": {}, "additionalProperties": False, "required": []},
        usage=usage,
    )
    assert structured == {"answer": 1} and model == "claude-fable-5-1"
    command = cli.commands[0]
    assert command[0] == str(tmp_path / "claude") and command[1:3] == ["-p", "질문"]
    assert "--json-schema" in command and "--system-prompt" in command
    assert command[command.index("--tools") + 1] == "Read"
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--no-session-persistence" in command
    assert "--model" not in command  # claude-opus-5 is the CLI's default alias
    assert usage.calls == 1 and usage.input_tokens == 120 and usage.output_tokens == 40


def test_claude_code_transport_reports_login_and_cli_errors(tmp_path: Path) -> None:
    from PaperPipeline.claude_analyzer import AnalysisUsage, ClaudeAuthenticationError, ClaudeCodeTransport

    schema = {"type": "object", "properties": {}, "additionalProperties": False, "required": []}
    with pytest.raises(ClaudeAuthenticationError, match="not logged in"):
        ClaudeCodeTransport(
            _cli_config(tmp_path), runner=_FakeCli([(1, _cli_payload(None, is_error=True, result="Not logged in · Please run /login"), "")])
        ).request(system_prompt="s", prompt="p", schema=schema, usage=AnalysisUsage())
    with pytest.raises(ClaudeAnalysisError, match="claude-code error"):
        ClaudeCodeTransport(
            _cli_config(tmp_path), runner=_FakeCli([(1, _cli_payload(None, is_error=True, result="rate limited"), "")])
        ).request(system_prompt="s", prompt="p", schema=schema, usage=AnalysisUsage())
    with pytest.raises(ClaudeAnalysisError, match="no JSON"):
        ClaudeCodeTransport(
            _cli_config(tmp_path), runner=_FakeCli([(2, "", "boom")])
        ).request(system_prompt="s", prompt="p", schema=schema, usage=AnalysisUsage())
    # A missing binary is an authentication-class failure: nothing can run.
    with pytest.raises(ClaudeAuthenticationError, match="not found"):
        ClaudeCodeTransport(ClaudeConfig(backend="claude-code", claude_code_executable=str(tmp_path / "missing")))


def test_claude_code_backend_analyzes_from_the_text_artifact(tmp_path: Path, monkeypatch) -> None:
    from PaperPipeline import claude_analyzer as module

    paths = _paths(tmp_path)
    text_path = paths.text
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("=== PAGE 1 ===\n본문\n=== PAGE 2 ===\n실험\n=== PAGE 3 ===\n결론\n", encoding="utf-8")
    real_prepare = module.prepare_input

    def prepare_with_text(paths, config, *, embed_document=True):
        prepared = real_prepare(paths, config, embed_document=embed_document)
        return module.PreparedInput(**{**prepared.__dict__, "text_path": text_path, "text_sha256": "d" * 64, "text_char_count": 12}) if hasattr(prepared, "__dict__") else prepared

    # PreparedInput is a slots dataclass; rebuild it explicitly.
    def prepare_with_text(paths, config, *, embed_document=True):  # noqa: F811
        prepared = real_prepare(paths, config, embed_document=embed_document)
        return module.PreparedInput(
            mode=prepared.mode, pdf_sha256=prepared.pdf_sha256, pdf_page_count=prepared.pdf_page_count,
            text_sha256="d" * 64, text_char_count=12, document_block=prepared.document_block,
            pdf_path=prepared.pdf_path, text_path=text_path,
        )

    monkeypatch.setattr(module, "prepare_input", prepare_with_text)
    cli = _FakeCli([(0, _cli_payload(_analysis_payload()), ""), (0, _cli_payload(_structure_payload()), "")])
    analyzer = ClaudePaperAnalyzer(_cli_config(tmp_path), cli_runner=cli)
    record = analyzer.analyze(_paper(), paths, VOCABULARY)

    assert record["backend"] == "claude-code"
    assert record["model"] == "claude-fable-5-1"
    assert record["provenance"]["input_mode"] == "files"
    assert record["level3"]["mappings"][0]["path_id"] == "EXM-04-01"
    assert record["structure"]["artifacts"][0]["name"] == "home.db"
    assert record["review"]["reviewer"] == "Claude (claude-fable-5-1) automated analysis"
    assert record["usage"]["calls"] == 2
    prompt = cli.commands[0][2]
    assert str(text_path.resolve()) in prompt and "=== PAGE N ===" in prompt
    assert "PDF 총 페이지 수: 3" in prompt
    assert "EXM-04-01" in cli.commands[0][cli.commands[0].index("--system-prompt") + 1]


def test_claude_code_backend_refuses_image_only_pdfs(tmp_path: Path) -> None:
    # Blank fixture pages yield no text artifact, which the CLI cannot analyze.
    analyzer = ClaudePaperAnalyzer(_cli_config(tmp_path), cli_runner=_FakeCli([]))
    with pytest.raises(ClaudeAnalysisError, match="extracted text artifact"):
        analyzer.analyze(_paper(), _paths(tmp_path), VOCABULARY)


def test_backend_resolution_prefers_api_key_then_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    config = _cli_config(tmp_path)
    assert ClaudeConfig(backend="auto", claude_code_executable=config.claude_code_executable).resolved_backend() == "claude-code"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert ClaudeConfig(backend="auto", claude_code_executable=config.claude_code_executable).resolved_backend() == "api"
    assert ClaudeConfig(backend="claude-code").resolved_backend() == "claude-code"
    with pytest.raises(ValueError, match="backend must be one of"):
        ClaudeConfig(backend="bogus")


def test_page_ranges_are_sorted_and_merged_instead_of_rejected() -> None:
    from PaperPipeline.claude_analyzer import _pages_to_text

    assert _pages_to_text([{"start": 3, "end": 3}, {"start": 1, "end": 2}], page_count=5, field="x") == "PDF pp. 1–3"
    assert _pages_to_text([{"start": 1, "end": 2}, {"start": 2, "end": 4}, {"start": 5, "end": 5}], page_count=5, field="x") == "PDF pp. 1–5"
    assert _pages_to_text([{"start": 4, "end": 4}, {"start": 1, "end": 1}], page_count=5, field="x") == "PDF pp. 1, 4"
    assert _pages_to_text([{"start": 3, "end": 1}], page_count=5, field="x") == "PDF pp. 1–3"
    with pytest.raises(ClaudeAnalysisError, match="outside PDF pages"):
        _pages_to_text([{"start": 1, "end": 9}], page_count=5, field="x")
