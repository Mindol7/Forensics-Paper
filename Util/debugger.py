"""PDF collection tracing and reproducible artifact statistics.

The stage trace contains aggregate counts only; URLs, credentials, cookies and
paper identifiers are intentionally not persisted.  The authoritative paper-
level audit is rebuilt from the SQLite rows and the validated download
manifests whenever a report is requested.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PDF_PROVIDER_ORDER: tuple[str, ...] = (
    "kci",
    "unpaywall",
    "openalex",
    "semantic_scholar",
    "dbpia",
    "kyobo",
    "jams",
    "sciencedirect",
)


@dataclass(frozen=True, slots=True)
class PdfStageStatistics:
    provider: str
    attempted_count: int
    collected_count: int
    remaining_count: int
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PdfCollectionSnapshot:
    target_count: int
    collected_count: int
    missing_count: int
    society_count: int
    route_count: int
    manifest_files: int
    pdf_files: int
    integrity_failures: tuple[str, ...]
    orphan_manifests: tuple[str, ...]
    orphan_pdfs: tuple[str, ...]


class PdfCollectionDebugger:
    """Record provider-stage coverage and enforce unresolved-only hand-off."""

    def __init__(self, journal_path: Path | None = None) -> None:
        self.journal_path = journal_path
        self._records: list[PdfStageStatistics] = []
        self._lock = threading.Lock()

    @property
    def records(self) -> tuple[PdfStageStatistics, ...]:
        with self._lock:
            return tuple(self._records)

    def record_stage(
        self,
        provider: str,
        before_source_ids: Iterable[str],
        after_source_ids: Iterable[str],
    ) -> PdfStageStatistics:
        before = frozenset(before_source_ids)
        after = frozenset(after_source_ids)
        if provider not in PDF_PROVIDER_ORDER:
            raise ValueError(f"unknown PDF provider: {provider}")
        if not after.issubset(before):
            raise ValueError("a PDF stage may only remove items from the unresolved set")

        with self._lock:
            expected_index = len(self._records)
            if expected_index >= len(PDF_PROVIDER_ORDER):
                raise ValueError("all PDF provider stages have already been recorded")
            expected = PDF_PROVIDER_ORDER[expected_index]
            if provider != expected:
                raise ValueError(f"PDF provider order violation: expected {expected}, got {provider}")
            record = PdfStageStatistics(
                provider=provider,
                attempted_count=len(before),
                collected_count=len(before - after),
                remaining_count=len(after),
                recorded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._records.append(record)
            if self.journal_path is not None:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                with self.journal_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                    stream.flush()
            return record


def inspect_pdf_collection(
    database: Path,
    papers_dir: Path,
    *,
    manifest_dir: Path | None = None,
    compatibility_papers_dirs: Sequence[Path] = (),
) -> PdfCollectionSnapshot:
    """Audit manifests/PDF integrity and return the report headline metrics."""

    # This import is deliberately local: the existing audited report engine is
    # retained as the compatibility implementation while callers use the new
    # debugger/exporter boundary.
    from Util.pdf_statistics import (
        _load_papers,
        _require_inputs,
        build_detail_rows,
        build_frames,
    )

    database = database.resolve()
    papers_dir = papers_dir.resolve()
    manifest_dir = (manifest_dir or papers_dir).resolve()
    _require_inputs(
        database,
        papers_dir,
        Path("audit.xlsx").resolve(),
        manifest_dir=manifest_dir,
    )
    papers = _load_papers(database)
    detail_rows, audit = build_detail_rows(
        papers,
        papers_dir,
        compatibility_papers_dirs,
        manifest_dir=manifest_dir,
    )
    society, _society_route, route, detail, _missing = build_frames(detail_rows)
    collected = int((detail["수집 여부"] == "수집").sum())
    return PdfCollectionSnapshot(
        target_count=len(detail),
        collected_count=collected,
        missing_count=len(detail) - collected,
        society_count=len(society),
        route_count=len(route),
        manifest_files=int(audit["manifest_files"]),
        pdf_files=int(audit["pdf_files"]),
        integrity_failures=tuple(audit["integrity_failures"]),
        orphan_manifests=tuple(audit["orphan_manifests"]),
        orphan_pdfs=tuple(audit["orphan_pdfs"]),
    )
