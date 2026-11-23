from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from Collector import located_pdf as lp
from Collector.kci_pdf import download_artifact_is_intact
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths


def _paper(**changes) -> PaperCandidate:
    values = dict(
        database_id=1, source="kci", source_id="ART009000001",
        title="디지털증거 압수ㆍ수색 시 피압수자등의 권리 고지의무 도입방안", doi_original=None, doi=None,
        landing_url=None, abstract=None, publication_year=2024, first_page="535", final_page="561",
        alternate_titles=("Plans to introduce the obligation to notify rights of seized persons",),
    )
    values.update(changes)
    return PaperCandidate(**values)


def _blank_pdf(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_titles_compare_without_punctuation_including_the_korean_middle_dot() -> None:
    assert lp.compact("압수ㆍ수색") == lp.compact("압수·수색") == lp.compact("압수 수색")
    paper = _paper()
    assert lp.title_matches(paper, ["KISS", "디지털증거 압수·수색 시 피압수자등의 권리 고지의무 도입방안"])
    assert lp.title_matches(paper, ["Plans to Introduce the Obligation to Notify Rights of Seized Persons"])
    # a journal name or a different paper never matches
    assert lp.title_matches(paper, ["연세법학", "Journal Archive", "디지털증거 압수수색의 개선방안"]) is None


def test_store_writes_an_artifact_the_collector_accepts(tmp_path: Path) -> None:
    papers_dir, manifest_dir = tmp_path / "Paper", tmp_path / "Manifest"
    papers_dir.mkdir(); manifest_dir.mkdir()
    (manifest_dir / "paper_files.json").write_text(
        json.dumps({"schema_version": "1.0", "filename_policy": "title-nfc-portable-v1", "paper_count": 0, "papers": []}), encoding="utf-8"
    )
    paper = _paper()
    # an earlier failed attempt is kept as history
    ArtifactPaths.for_source(papers_dir, paper.source_id, manifest_root=manifest_dir, pdf_filename="x.pdf")
    paths = lp.store(
        paper, _blank_pdf(3), platform=lp.PLATFORMS["kiss"], landing="https://kiss.kstudy.com/Detail/Ar?key=1",
        evidence=["landing_title:..."], open_access=False, papers_dir=papers_dir, manifest_dir=manifest_dir,
    )
    assert paths.pdf.is_file() and paths.pdf.name.endswith(".pdf") and "디지털증거" in paths.pdf.name
    manifest = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    assert manifest["status"] == "downloaded" and manifest["pdf"]["page_count"] == 3
    assert manifest["resolver"]["name"] == "located-source-browser:kiss"
    assert manifest["pdf"]["identity_status"] == "located_source_landing_title_match"  # blank pages: image-only rule
    assert download_artifact_is_intact(paper, paths)

    with pytest.raises(lp.LocatedPdfError, match="not a PDF"):
        lp.store(paper, b"<html>login</html>", platform=lp.PLATFORMS["kiss"], landing="x", evidence=[],
                 open_access=False, papers_dir=papers_dir, manifest_dir=manifest_dir)


def test_only_domestic_platforms_are_configured_and_the_table_is_consistent() -> None:
    assert set(lp.PLATFORMS) == {"koreascience", "kricm", "ksbe", "kci_portal", "dbpia", "kyobo", "kiss"}
    table = json.loads(lp.DEFAULT_SOURCES.read_text(encoding="utf-8"))
    assert {source["platform"] for source in table["sources"]} <= set(lp.PLATFORMS)
    assert len({source["source_id"] for source in table["sources"]}) == len(table["sources"])
    assert lp.located_source_ids() == {source["source_id"] for source in table["sources"]}
    assert table["excluded_foreign_publisher_sites"]["source_ids"]


def test_unattended_runs_never_open_a_window(tmp_path: Path, monkeypatch) -> None:
    opened: list[str] = []

    class NoSession:
        def __init__(self, platform, **_kwargs):
            opened.append(platform.name)
            raise AssertionError("a browser must not start")

    monkeypatch.setattr(lp, "Session", NoSession)
    monkeypatch.setattr(lp, "iter_paper_candidates", lambda *args, **kwargs: iter([_paper()]))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": [{"source_id": "ART009000001", "platform": "kiss", "landing_url": "https://kiss.kstudy.com/x"}]}), encoding="utf-8")
    (tmp_path / "Manifest").mkdir(); (tmp_path / "Paper").mkdir()
    report = lp.run(sources_path=sources, allow_window=False, papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
                    database_url=f"sqlite:///{tmp_path / 'x.db'}")
    assert opened == [] and report["downloaded"] == []
    assert report["failed"]["ART009000001"].startswith("window_required")
