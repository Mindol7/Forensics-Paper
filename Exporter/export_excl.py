"""Compatibility wrapper for the historical misspelled module name."""

from Exporter.report_generator import (
    EXPORT_COLUMNS,
    MetadataReportResult,
    export_papers_to_excel,
    generate_metadata_report,
    generate_report,
)

__all__ = [
    "EXPORT_COLUMNS",
    "MetadataReportResult",
    "export_papers_to_excel",
    "generate_metadata_report",
    "generate_report",
]
