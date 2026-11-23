"""Export PDF collection debugger statistics to Excel."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def export_pdf_statistics(
    database: Path,
    papers_dir: Path,
    output_path: Path | None = None,
    *,
    manifest_dir: Path | None = None,
    output_dir: Path = Path("reports"),
    generated_at: datetime | None = None,
    compatibility_papers_dirs: Sequence[Path] = (),
) -> dict[str, Any]:
    """Create the audited society-level PDF workbook used by operations."""

    from Util.pdf_statistics import export_report

    timestamp = generated_at or datetime.now(SEOUL)
    destination = output_path or (
        output_dir / f"pdf_collection_by_society_{timestamp:%Y%m%d}.xlsx"
    )
    return export_report(
        database,
        papers_dir,
        destination,
        manifest_dir=manifest_dir,
        compatibility_papers_dirs=compatibility_papers_dirs,
    )


# Short alias for callers that view Util/exporter as a single-purpose module.
export_statistics_to_excel = export_pdf_statistics
