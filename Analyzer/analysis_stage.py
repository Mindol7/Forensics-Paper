"""Analyze newly collected PDFs with Claude and write their evidence files.

This is the stage between PDF collection and the derived exporters.  For every
paper in ``Analyzer/Manifest/paper_files.json`` whose PDF has no analysis
record yet (or whose PDF bytes changed since the last analysis) it:

1. loads the database metadata as a :class:`PaperCandidate`;
2. runs :class:`PaperPipeline.claude_analyzer.ClaudePaperAnalyzer`;
3. renders the Structure documents, Level-3 manifest, document-type decision
   and analysis record through :mod:`Analyzer.evidence_writer`;
4. regenerates ``Analyzer/Structure/INDEX.md`` when anything was written.

Papers keep the ``paper_index`` they already have; new papers receive the next
free index so every exporter sees one contiguous corpus.  Without Claude
credentials the stage does not fail the pipeline: it reports the papers as
pending so the derived exporters run on the analyzed subset.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from Analyzer.evidence_writer import (
    DEFAULT_ANALYSIS_DIR,
    DEFAULT_LV3_DIR,
    DEFAULT_STRUCTURE_DIR,
    DEFAULT_TYPE_DECISION_DIR,
    EvidenceWriterError,
    PaperMetadata,
    load_vocabulary,
    regenerate_structure_index,
    write_paper_artifacts,
)
from PaperPipeline.claude_analyzer import (
    AnalysisVocabulary,
    ClaudeAnalysisError,
    ClaudeAuthenticationError,
    ClaudeConfig,
    ClaudePaperAnalyzer,
    ClaudeRefusalError,
    find_claude_code_executable,
)
from PaperPipeline.db_source import iter_paper_candidates
from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    PaperFileRecord,
    load_paper_file_index,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths, load_json_object, sha256_file
from Util.logger import ProgressLogger


ANALYZER_DIR = Path(__file__).resolve().parent
DEFAULT_PAPERS_DIR = ANALYZER_DIR / "Paper"
DEFAULT_MANIFEST_DIR = ANALYZER_DIR / "Manifest"
COMPONENT = "ANALYZE"


class AnalysisStageError(RuntimeError):
    """The stage could not even start (bad index, unreadable database)."""


@dataclass(frozen=True, slots=True)
class AnalysisTarget:
    record: PaperFileRecord
    paths: ArtifactPaths
    reason: str  # "new" | "pdf_changed" | "rerun"
    existing_paper_index: int | None


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    source_id: str
    status: str  # "analyzed" | "dry_run" | "failed" | "not_attempted"
    reason: str
    paper_index: int | None = None
    error: str | None = None
    usage: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class AnalysisStageResult:
    status: str  # "ok" | "partial" | "skipped" | "failed"
    model: str
    credentials_available: bool
    outcomes: tuple[AnalysisOutcome, ...] = ()
    up_to_date_count: int = 0
    pending_source_ids: tuple[str, ...] = ()
    index_regenerated: bool = False
    detail: str = ""

    @property
    def analyzed_count(self) -> int:
        return sum(outcome.status == "analyzed" for outcome in self.outcomes)

    @property
    def failed_count(self) -> int:
        return sum(outcome.status == "failed" for outcome in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "credentials_available": self.credentials_available,
            "analyzed_count": self.analyzed_count,
            "failed_count": self.failed_count,
            "up_to_date_count": self.up_to_date_count,
            "pending_source_ids": list(self.pending_source_ids),
            "index_regenerated": self.index_regenerated,
            "detail": self.detail,
            "outcomes": [
                {
                    "source_id": outcome.source_id,
                    "status": outcome.status,
                    "reason": outcome.reason,
                    "paper_index": outcome.paper_index,
                    "error": outcome.error,
                    "usage": dict(outcome.usage) if outcome.usage else None,
                }
                for outcome in self.outcomes
            ],
        }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def has_api_credentials() -> bool:
    """True when the SDK's credential chain will find something to send."""

    if os.getenv("ANTHROPIC_API_KEY", "").strip() or os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip():
        return True
    try:
        from anthropic.lib.credentials import default_credentials
    except Exception:  # pragma: no cover - older SDK without the chain
        return False
    try:
        return default_credentials() is not None
    except Exception:
        return False


def has_claude_credentials(config: ClaudeConfig | None = None) -> bool:
    """True when the configured backend can run.

    ``api`` needs SDK credentials; ``claude-code`` needs the CLI binary (its
    subscription login is verified by the first request).  ``auto`` accepts
    either.
    """

    backend = (config or ClaudeConfig.from_environment()).backend
    executable = (config.claude_code_executable if config else None)
    if backend == "api":
        return has_api_credentials()
    if backend == "claude-code":
        return find_claude_code_executable(executable) is not None
    return has_api_credentials() or find_claude_code_executable(executable) is not None


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, ClaudeAuthenticationError):
        return True
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    auth_types = tuple(
        getattr(anthropic, name)
        for name in ("AuthenticationError", "PermissionDeniedError")
        if hasattr(anthropic, name)
    )
    if auth_types and isinstance(exc, auth_types):
        return True
    return type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError", "CredentialsError"}


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #

def _existing_paper_index(source_id: str, *, lv3_dir: Path, analysis_dir: Path) -> int | None:
    for path in (analysis_dir / f"{source_id}.json", lv3_dir / f"{source_id}.json"):
        value = load_json_object(path) if path.is_file() else None
        index = value.get("paper_index") if value else None
        if type(index) is int and index >= 1:
            return index
    # A number promised before the analysis existed (see the reservation file).
    return load_paper_index_reservations(Path(lv3_dir).parent).get(source_id)


RESERVATION_FILENAME = "paper_index_reservations.json"


def load_paper_index_reservations(manifest_dir: Path) -> dict[str, int]:
    """Numbers promised to selected papers before their analysis exists.

    Deliverables (for example the Level workbooks) may have to number a paper
    whose PDF arrived late or is still missing.  The promise is recorded here
    and the analysis stage uses it.  ``Analyzer/numbering.py`` writes this file: it numbers every
    selected paper that has no analysis evidence yet.
    """

    value = load_json_object(Path(manifest_dir) / RESERVATION_FILENAME) or {}
    records = value.get("reservations") if isinstance(value, dict) else None
    if not isinstance(records, dict):
        return {}
    return {str(source_id): index for source_id, index in records.items() if type(index) is int and index > 0}


def next_paper_index(*, lv3_dir: Path, analysis_dir: Path) -> int:
    """One past the largest ``paper_index`` any manifest, record or reservation holds.

    Only a fallback: the pipeline's ``renumber`` stage (``Analyzer/numbering.py``) gives every
    selected paper its number before the analysis runs, and keeps the numbers sorted and
    without gaps (so the number of a removed paper is handed to the next paper in line).
    """

    highest = max(load_paper_index_reservations(Path(lv3_dir).parent).values(), default=0)
    for directory in (lv3_dir, analysis_dir):
        if not directory.is_dir():
            continue
        for path in directory.glob("ART*.json"):
            value = load_json_object(path)
            index = value.get("paper_index") if value else None
            if type(index) is int and index > highest:
                highest = index
    return highest + 1


def _evidence_pdf_sha256(
    source_id: str, *, analysis_dir: Path, lv3_dir: Path, structure_dir: Path | None
) -> str | None:
    """The PDF hash the existing evidence for ``source_id`` was produced from.

    A Claude analysis record is authoritative.  Papers documented before the
    analysis stage existed (the reviewed legacy corpus) have no record, but
    their Level-3 manifest pins ``pdf_sha256`` and their Structure documents
    exist; that evidence is just as complete and must not be re-analyzed.
    """

    record = load_json_object(analysis_dir / f"{source_id}.json")
    if record is not None and not record.get("dry_run"):
        provenance = record.get("provenance") or {}
        value = provenance.get("pdf_sha256")
        return value if isinstance(value, str) else None
    manifest = load_json_object(lv3_dir / f"{source_id}.json")
    if manifest is None:
        return None
    if structure_dir is not None and not (
        (structure_dir / "kor" / f"{source_id}.md").is_file()
        and (structure_dir / "eng" / f"{source_id}.md").is_file()
    ):
        return None
    value = manifest.get("pdf_sha256")
    return value if isinstance(value, str) else None


def select_targets(
    records: Mapping[str, PaperFileRecord],
    *,
    papers_dir: Path,
    manifest_dir: Path,
    analysis_dir: Path,
    lv3_dir: Path,
    structure_dir: Path | None = None,
    source_ids: Sequence[str] | None = None,
    force: bool = False,
) -> tuple[list[AnalysisTarget], list[str], list[str]]:
    """Return (targets, up-to-date ids, ids whose PDF is missing).

    A paper is up to date when its evidence (analysis record, or legacy Level-3
    manifest plus Structure documents) pins the SHA-256 of the PDF currently
    on disk.  ``force`` re-analyzes papers that already have an analysis
    record; legacy evidence is never discarded by ``force``.
    """

    wanted = set(source_ids) if source_ids else None
    targets: list[AnalysisTarget] = []
    current: list[str] = []
    missing_pdf: list[str] = []
    for source_id, record in records.items():
        if wanted is not None and source_id not in wanted:
            continue
        paths = ArtifactPaths.for_source(
            papers_dir, source_id, manifest_root=manifest_dir, pdf_filename=record.filename
        )
        if paths.pdf.is_symlink() or not paths.pdf.is_file():
            missing_pdf.append(source_id)
            continue
        existing = load_json_object(analysis_dir / f"{source_id}.json")
        has_record = existing is not None and not existing.get("dry_run")
        existing_index = _existing_paper_index(source_id, lv3_dir=lv3_dir, analysis_dir=analysis_dir)
        evidence_sha256 = _evidence_pdf_sha256(
            source_id, analysis_dir=analysis_dir, lv3_dir=lv3_dir, structure_dir=structure_dir
        )
        if evidence_sha256 is not None and not (force and has_record):
            if evidence_sha256 == sha256_file(paths.pdf):
                current.append(source_id)
                continue
            reason = "pdf_changed"
        elif force and has_record:
            reason = "rerun"
        else:
            reason = "new"
        targets.append(
            AnalysisTarget(
                record=record, paths=paths, reason=reason, existing_paper_index=existing_index
            )
        )
    return targets, current, missing_pdf


def load_candidates(database_url: str, source_ids: Sequence[str]) -> dict[str, PaperCandidate]:
    if not source_ids:
        return {}
    engine = create_engine(database_url, future=True)
    try:
        candidates = {
            candidate.source_id: candidate
            for candidate in iter_paper_candidates(
                engine, source_ids=list(source_ids), include_missing_doi=True
            )
        }
    finally:
        engine.dispose()
    return candidates


def _metadata_for(record: PaperFileRecord, candidate: PaperCandidate) -> PaperMetadata:
    english = next(
        (title for title in candidate.alternate_titles if title and _looks_english(title)),
        None,
    )
    return PaperMetadata(
        source_id=record.source_id,
        title=record.title,
        title_eng=english,
        authors=tuple(candidate.authors),
        journal=candidate.journal_id,
        publication_year=candidate.publication_year,
        doi=candidate.doi or candidate.doi_original,
        pdf_filename=record.filename,
    )


def _looks_english(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and all(ord(character) < 0x3000 for character in letters)


# --------------------------------------------------------------------------- #
# Stage
# --------------------------------------------------------------------------- #

def run_analysis_stage(
    *,
    database_url: str,
    papers_dir: Path = DEFAULT_PAPERS_DIR,
    manifest_dir: Path = DEFAULT_MANIFEST_DIR,
    analysis_dir: Path = DEFAULT_ANALYSIS_DIR,
    structure_dir: Path = DEFAULT_STRUCTURE_DIR,
    lv3_dir: Path = DEFAULT_LV3_DIR,
    type_decision_dir: Path = DEFAULT_TYPE_DECISION_DIR,
    config: ClaudeConfig | None = None,
    analyzer: Any | None = None,
    vocabulary: AnalysisVocabulary | None = None,
    source_ids: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    logger: ProgressLogger | None = None,
    credentials_check: Callable[[], bool] | None = None,
) -> AnalysisStageResult:
    """Analyze every indexed PDF that lacks a current analysis record.

    ``analyzer`` may be any object with ``analyze(paper, paths, vocabulary)``;
    the default is a :class:`ClaudePaperAnalyzer` built from ``config``.
    Authentication failures stop the stage after the first attempt and leave
    the remaining papers pending rather than burning retries.
    """

    logger = logger or ProgressLogger()
    config = config or ClaudeConfig.from_environment()
    if credentials_check is None:
        credentials_check = lambda: has_claude_credentials(config)  # noqa: E731
    index_path = manifest_dir / PAPER_FILE_INDEX_NAME
    try:
        records = load_paper_file_index(index_path)
    except Exception as exc:  # PaperFileIndexError or OSError
        raise AnalysisStageError(f"unable to read the paper index {index_path}: {exc}") from exc
    targets, current, missing_pdf = select_targets(
        records,
        papers_dir=papers_dir,
        manifest_dir=manifest_dir,
        analysis_dir=analysis_dir,
        lv3_dir=lv3_dir,
        structure_dir=structure_dir,
        source_ids=source_ids,
        force=force,
    )
    if limit is not None and limit >= 0:
        deferred = targets[limit:]
        targets = targets[:limit]
    else:
        deferred = []
    pending_after: list[str] = [target.record.source_id for target in deferred]
    if missing_pdf:
        logger.emit(COMPONENT, f"PDF가 없어 분석을 건너뛴 색인 항목 {len(missing_pdf)}건")
    if not targets:
        logger.emit(
            COMPONENT,
            "분석할 새 PDF가 없습니다",
            counters={"up_to_date": len(current), "deferred": len(deferred)},
        )
        return AnalysisStageResult(
            status="ok",
            model=config.model,
            credentials_available=True,
            up_to_date_count=len(current),
            pending_source_ids=tuple(pending_after),
            detail="nothing to analyze",
        )

    credentials_available = True
    if analyzer is None:
        if not config.dry_run and not credentials_check():
            credentials_available = False
            pending_after = [target.record.source_id for target in targets] + pending_after
            logger.emit(
                COMPONENT,
                "Claude 분석 백엔드를 사용할 수 없어 분석 단계를 건너뜁니다 "
                "(API: ANTHROPIC_API_KEY 또는 `ant auth login`; claude-code: 로그인된 Claude Code CLI)",
                counters={"pending": len(pending_after)},
            )
            return AnalysisStageResult(
                status="skipped",
                model=config.model,
                credentials_available=False,
                outcomes=tuple(
                    AnalysisOutcome(t.record.source_id, "not_attempted", t.reason, t.existing_paper_index)
                    for t in targets
                ),
                up_to_date_count=len(current),
                pending_source_ids=tuple(pending_after),
                detail="missing Claude credentials",
            )
        analyzer = ClaudePaperAnalyzer(config)
    vocabulary = vocabulary or load_vocabulary()
    candidates = load_candidates(
        database_url, [target.record.source_id for target in targets]
    )
    next_index = next_paper_index(lv3_dir=lv3_dir, analysis_dir=analysis_dir)

    outcomes: list[AnalysisOutcome] = []
    wrote_any = False
    aborted = False
    logger.emit(
        COMPONENT,
        f"Claude 논문 분석 시작 — 이번에 분석할 논문 {len(targets)}편, 이미 분석된 논문 {len(current)}편 "
        f"(방식: {'Claude Code 구독' if config.resolved_backend() == 'claude-code' else 'Anthropic API'}, 모델 {config.model}, 논문당 보통 5~15분)",
        current=0,
        total=len(targets),
        counters={"up_to_date": len(current)},
    )
    status_labels = {"analyzed": "분석 완료", "failed": "분석 실패", "not_attempted": "시도하지 않음", "dry_run": "점검만 함"}
    for position, target in enumerate(targets, start=1):
        source_id = target.record.source_id
        if not aborted:
            logger.emit(COMPONENT, f"[{position}/{len(targets)}] 분석 중: {source_id} {target.record.title[:50]}")
        if aborted:
            outcomes.append(AnalysisOutcome(source_id, "not_attempted", target.reason, target.existing_paper_index))
            pending_after.append(source_id)
            continue
        candidate = candidates.get(source_id)
        if candidate is None:
            outcomes.append(
                AnalysisOutcome(source_id, "failed", target.reason, target.existing_paper_index,
                                error="database row is missing")
            )
            pending_after.append(source_id)
            continue
        # The index title is the identity every exporter binds to; the DB title
        # travels with the metadata as an alternate form.
        alternates = tuple(
            dict.fromkeys(
                title for title in (candidate.title, *candidate.alternate_titles)
                if title and title != target.record.title
            )
        )
        paper = replace(candidate, title=target.record.title, alternate_titles=alternates)
        try:
            record = analyzer.analyze(paper, target.paths, vocabulary)
            if record.get("dry_run"):
                outcomes.append(AnalysisOutcome(source_id, "dry_run", target.reason, target.existing_paper_index))
                pending_after.append(source_id)
                continue
            paper_index = target.existing_paper_index
            if paper_index is None:
                paper_index = next_index
                next_index += 1
            write_paper_artifacts(
                record,
                _metadata_for(target.record, candidate),
                paper_index=paper_index,
                analysis_dir=analysis_dir,
                structure_dir=structure_dir,
                lv3_dir=lv3_dir,
                type_decision_dir=type_decision_dir,
            )
            wrote_any = True
            outcomes.append(
                AnalysisOutcome(
                    source_id, "analyzed", target.reason, paper_index, usage=record.get("usage")
                )
            )
        except ClaudeRefusalError as exc:
            outcomes.append(AnalysisOutcome(source_id, "failed", target.reason, target.existing_paper_index, error=str(exc)))
            pending_after.append(source_id)
        except ClaudeAuthenticationError as exc:
            outcomes.append(AnalysisOutcome(source_id, "failed", target.reason, target.existing_paper_index, error=str(exc)))
            pending_after.append(source_id)
            aborted = True
            credentials_available = False
            logger.emit(COMPONENT, f"인증 실패로 분석을 중단합니다: {exc}")
        except (ClaudeAnalysisError, EvidenceWriterError) as exc:
            outcomes.append(AnalysisOutcome(source_id, "failed", target.reason, target.existing_paper_index, error=str(exc)))
            pending_after.append(source_id)
        except Exception as exc:  # SDK/network errors: keep the pipeline alive
            outcomes.append(
                AnalysisOutcome(source_id, "failed", target.reason, target.existing_paper_index,
                                error=f"{type(exc).__name__}: {exc}")
            )
            pending_after.append(source_id)
            if _is_auth_error(exc):
                aborted = True
                credentials_available = False
                logger.emit(COMPONENT, f"인증 실패로 분석을 중단합니다: {type(exc).__name__}")
        last = outcomes[-1]
        logger.emit(
            COMPONENT,
            f"[{position}/{len(targets)}] {status_labels.get(last.status, last.status)}: {source_id}"
            + (f" — {last.error[:200]}" if last.error else ""),
            current=position,
            total=len(targets),
            counters={
                "analyzed": sum(o.status == "analyzed" for o in outcomes),
                "failed": sum(o.status == "failed" for o in outcomes),
            },
        )

    index_regenerated = False
    if wrote_any:
        regenerate_structure_index(structure_dir)
        index_regenerated = True
    analyzed = sum(outcome.status == "analyzed" for outcome in outcomes)
    failed = sum(outcome.status == "failed" for outcome in outcomes)
    if config.dry_run and analyzer is not None and analyzed == 0 and failed == 0:
        status = "skipped"
        detail = "dry run: requests prepared, nothing sent or written"
    elif failed == 0 and not aborted and not deferred:
        status = "ok"
        detail = f"analyzed {analyzed}"
    elif analyzed:
        status = "partial"
        detail = f"analyzed {analyzed}, failed {failed}, pending {len(pending_after)}"
    else:
        status = "failed"
        detail = f"analyzed 0, failed {failed}, pending {len(pending_after)}"
    logger.emit(COMPONENT, f"분석 종료: {detail}")
    return AnalysisStageResult(
        status=status,
        model=config.model,
        credentials_available=credentials_available,
        outcomes=tuple(outcomes),
        up_to_date_count=len(current),
        pending_source_ids=tuple(dict.fromkeys(pending_after)),
        index_regenerated=index_regenerated,
        detail=detail,
    )


__all__ = [
    "AnalysisOutcome",
    "AnalysisStageError",
    "AnalysisStageResult",
    "AnalysisTarget",
    "has_api_credentials",
    "has_claude_credentials",
    "load_candidates",
    "next_paper_index",
    "run_analysis_stage",
    "select_targets",
]
