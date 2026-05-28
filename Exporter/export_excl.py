from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from Core.normalizer import NormalizedPaper


def _flatten_paper(paper: NormalizedPaper) -> dict[str, object]:
    payload = asdict(paper)
    payload["keywords"] = "; ".join(paper.keywords)
    payload["matched_keyword"] = "; ".join(paper.matched_keyword)
    payload["authors"] = "; ".join(paper.authors)
    payload["matched_queries"] = "; ".join(paper.matched_queries)
    payload["relevance_reasons"] = "; ".join(paper.relevance_reasons)
    return payload


def export_papers_to_excel(papers: Iterable[NormalizedPaper], output_path: Path) -> Path:
    rows = [_flatten_paper(paper) for paper in papers]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    dataframe = pd.DataFrame(rows)
    if dataframe.empty:
        dataframe = pd.DataFrame(columns=[
            "source",
            "source_id",
            "title",
            "doi",
            "abstract",
            "keywords",
            "journal_id",
            "registered_at",
            "updated_at",
            "relevance_score",
            "is_relevant",
            "summary",
        ])

    dataframe.to_excel(output_path, index=False)
    _autosize_columns(output_path)
    return output_path


def _autosize_columns(path: Path) -> None:
    workbook = load_workbook(path)
    worksheet = workbook.active
    for column_cells in worksheet.columns:
        max_length = 0
        column_letter = get_column_letter(column_cells[0].column)
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            max_length = max(max_length, len(value))
        worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 80)
    workbook.save(path)
