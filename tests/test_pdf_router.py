from __future__ import annotations

import json
from pathlib import Path

import pytest

from Core.pdf_router import PdfRoute, load_pdf_routes, paper_is_eligible_for_provider
from PaperPipeline.records import PaperCandidate


def _paper(**changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 1,
        "source": "kci",
        "source_id": "ART1",
        "title": "title",
        "doi_original": None,
        "doi": None,
        "landing_url": None,
        "abstract": None,
        "publication_year": 2025,
        "first_page": "1",
        "final_page": "10",
        "journal_id": None,
        "institution_id": None,
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def test_default_routes_are_journal_aware_and_support_doi_prefixes() -> None:
    routes = load_pdf_routes(Path("Processor/refs/pdf_routes.json"))

    assert paper_is_eligible_for_provider(
        _paper(journal_id=" 경희법학 ", institution_id="법학연구소"),
        "kyobo",
        routes,
    )
    assert not paper_is_eligible_for_provider(
        _paper(journal_id="다른 법학", institution_id="법학연구소"),
        "kyobo",
        routes,
    )
    assert paper_is_eligible_for_provider(
        _paper(
            journal_id="디지털포렌식연구",
            institution_id="한국디지털포렌식학회",
        ),
        "jams",
        routes,
    )
    assert paper_is_eligible_for_provider(
        _paper(doi_original="http://dx.doi.org/10.1016/J.ICTE.2025.02.011"),
        "sciencedirect",
        routes,
    )
    assert paper_is_eligible_for_provider(_paper(), "kci", routes)


def test_institution_only_and_empty_routes_are_rejected() -> None:
    with pytest.raises(ValueError, match="institution-only"):
        PdfRoute(provider="kyobo", institution="법학연구소")
    with pytest.raises(ValueError, match="identity condition"):
        PdfRoute(provider="jams")


def test_loader_rejects_unknown_fields_duplicates_and_symlinks(tmp_path: Path) -> None:
    route_file = tmp_path / "routes.json"
    route_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": [
                    {"provider": "kyobo", "journal": "경희법학"},
                    {"provider": "kyobo", "journal": "경희법학"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_pdf_routes(route_file)

    route_file.write_text(
        '{"schema_version": 1, "routes": [{"provider": "jams", "bad": 1}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid fields"):
        load_pdf_routes(route_file)

    target = tmp_path / "target.json"
    target.write_text('{"schema_version": 1, "routes": []}', encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_pdf_routes(link)
