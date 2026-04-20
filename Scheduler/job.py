from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from main import run_pipeline


@dataclass
class JobResult:
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    incremental_count: int
    relevant_count: int
    export_path: Path | None


def run_kci_job() -> JobResult:
    result = run_pipeline()
    return JobResult(
        raw_count=result.raw_count,
        normalized_count=result.normalized_count,
        deduplicated_count=result.deduplicated_count,
        incremental_count=result.incremental_count,
        relevant_count=result.relevant_count,
        export_path=result.export_path,
    )
