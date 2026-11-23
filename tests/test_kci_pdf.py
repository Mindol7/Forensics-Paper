from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import Collector.kci_pdf as kci_pdf_module
from Collector.kci_pdf import (
    PDF_PROVIDER_ORDER,
    KciPdfCollector,
    KciPdfConfig,
    PaperPipelineProviderRunner,
    PdfProvider,
    ProviderContext,
    ProviderFunctionRunner,
    ProviderRunReport,
    SourceIdCommandBuilder,
    StageStatus,
    build_default_provider_runners,
    default_manifest_dir,
    default_papers_dir,
)
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    build_paper_file_records,
    load_paper_file_index,
    paper_file_index_document,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths, load_json_object


def _paper(source_id: str, database_id: int = 1) -> PaperCandidate:
    return PaperCandidate(
        database_id=database_id,
        source="kci",
        source_id=source_id,
        title=f"title {source_id}",
        doi_original=f"10.1000/{source_id}",
        doi=f"10.1000/{source_id}",
        landing_url=None,
        abstract=None,
        publication_year=2026,
        first_page="1",
        final_page="2",
    )


@dataclass
class _MutableValidator:
    current: set[str] = field(default_factory=set)
    current_by_root: dict[Path, set[str]] = field(default_factory=dict)

    def is_current(
        self,
        paper: PaperCandidate,
        root: Path,
        manifest_root: Path | None = None,
    ) -> bool:
        del manifest_root
        return paper.source_id in self.current or paper.source_id in self.current_by_root.get(
            root, set()
        )


@dataclass
class _Debugger:
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list
    )

    def record_stage(self, provider, before_source_ids, after_source_ids):
        self.calls.append(
            (provider, tuple(before_source_ids), tuple(after_source_ids))
        )


def test_strict_order_passes_only_durably_unresolved_papers(tmp_path: Path) -> None:
    validator = _MutableValidator(current={"already"})
    debugger = _Debugger()
    calls: list[tuple[str, tuple[str, ...]]] = []
    runners = {}

    for provider in PdfProvider:
        def callback(papers, _context, *, provider=provider):
            source_ids = tuple(paper.source_id for paper in papers)
            calls.append((provider.value, source_ids))
            validator.current.add(source_ids[0])
            return ProviderRunReport(
                provider=provider,
                attempted_source_ids=frozenset(source_ids),
                successful_source_ids=frozenset({source_ids[0]}),
            )

        runners[provider] = ProviderFunctionRunner(provider, callback)

    papers = [_paper("already", 1)] + [
        _paper(source_id, index)
        for index, source_id in enumerate("abcdefgh", start=2)
    ]
    result = KciPdfCollector(
        runners,
        config=KciPdfConfig(
            papers_dir=tmp_path,
            enable_dbpia=True,
            enable_kyobo=True,
            enable_jams=True,
            enable_sciencedirect=True,
        ),
        artifact_validator=validator,
        debugger=debugger,
    ).collect(papers)

    assert calls == [
        ("kci", ("a", "b", "c", "d", "e", "f", "g", "h")),
        ("unpaywall", ("b", "c", "d", "e", "f", "g", "h")),
        ("openalex", ("c", "d", "e", "f", "g", "h")),
        ("semantic_scholar", ("d", "e", "f", "g", "h")),
        ("dbpia", ("e", "f", "g", "h")),
        ("kyobo", ("f", "g", "h")),
        ("jams", ("g", "h")),
        ("sciencedirect", ("h",)),
    ]
    assert tuple(call[0] for call in debugger.calls) == PDF_PROVIDER_ORDER
    assert result.unresolved_source_ids == ()
    assert result.newly_collected_source_ids == frozenset("abcdefgh")


def test_reported_success_without_current_artifact_stays_unresolved(
    tmp_path: Path,
) -> None:
    validator = _MutableValidator()

    def callback(papers, context):
        source_ids = frozenset(paper.source_id for paper in papers)
        return ProviderRunReport(
            provider=context.provider,
            attempted_source_ids=source_ids,
            successful_source_ids=source_ids,
        )

    runner = ProviderFunctionRunner(PdfProvider.KCI, callback)
    result = KciPdfCollector(
        {PdfProvider.KCI: runner},
        config=KciPdfConfig(papers_dir=tmp_path),
        artifact_validator=validator,
    ).collect([_paper("a")])

    assert result.unresolved_source_ids == ("a",)
    assert result.stages[0].non_durable_reported_source_ids == frozenset({"a"})


def test_permission_gated_stages_disabled_and_dry_run_never_invoke_runners(
    tmp_path: Path,
) -> None:
    invoked: list[str] = []

    def callback(_papers, context):
        invoked.append(context.provider.value)
        return ProviderRunReport(provider=context.provider)

    dbpia = ProviderFunctionRunner(PdfProvider.DBPIA, callback)
    disabled = KciPdfCollector(
        {PdfProvider.DBPIA: dbpia},
        config=KciPdfConfig(papers_dir=tmp_path / "disabled"),
        artifact_validator=_MutableValidator(),
    ).collect([_paper("a")])
    dbpia_stage = next(
        stage for stage in disabled.stages if stage.provider is PdfProvider.DBPIA
    )
    assert dbpia_stage.status is StageStatus.DISABLED
    assert invoked == []

    all_runners = {
        provider: ProviderFunctionRunner(provider, callback)
        for provider in PdfProvider
    }
    dry = KciPdfCollector(
        all_runners,
        config=KciPdfConfig(
            papers_dir=tmp_path / "dry",
            enable_dbpia=True,
            enable_kyobo=True,
            enable_jams=True,
            enable_sciencedirect=True,
            dry_run=True,
        ),
        artifact_validator=_MutableValidator(),
    ).collect([_paper("b")])
    assert all(stage.status is StageStatus.DRY_RUN for stage in dry.stages)
    assert invoked == []


def test_dry_run_does_not_extend_an_existing_title_index(tmp_path: Path) -> None:
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    index_path = manifest_dir / PAPER_FILE_INDEX_NAME
    document = paper_file_index_document(
        build_paper_file_records([("ART000000001", "기존 논문")])
    )
    index_path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    before = index_path.read_bytes()

    result = KciPdfCollector(
        {},
        config=KciPdfConfig(
            papers_dir=papers_dir,
            manifest_dir=manifest_dir,
            dry_run=True,
        ),
    ).collect([_paper("ART000000002")])

    assert index_path.read_bytes() == before
    assert result.unresolved_source_ids == ("ART000000002",)


def _seed_index(manifest_dir: Path) -> Path:
    index_path = manifest_dir / PAPER_FILE_INDEX_NAME
    index_path.write_text(
        json.dumps(
            paper_file_index_document(
                build_paper_file_records([("ART000000001", "기존 논문")])
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return index_path


def test_incremental_collection_keeps_a_title_filename_once_the_pdf_is_durable(
    tmp_path: Path,
) -> None:
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    index_path = _seed_index(manifest_dir)
    new_paper = replace(
        _paper("ART000000002"),
        title="신규/논문: 증분 수집",
    )

    # The validator reports the artifact as current, i.e. a provider committed
    # a valid PDF under the reserved title filename during the run.
    KciPdfCollector(
        {},
        config=KciPdfConfig(
            papers_dir=papers_dir,
            manifest_dir=manifest_dir,
        ),
        artifact_validator=_MutableValidator(current={new_paper.source_id}),
    ).collect([new_paper])

    indexed = load_paper_file_index(index_path)
    assert indexed[new_paper.source_id].filename == "신규∕논문∶ 증분 수집.pdf"
    assert ArtifactPaths.for_source(
        papers_dir,
        new_paper.source_id,
        manifest_root=manifest_dir,
    ).pdf == papers_dir / "신규∕논문∶ 증분 수집.pdf"


def test_incremental_collection_releases_a_reservation_without_a_pdf(
    tmp_path: Path,
) -> None:
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    index_path = _seed_index(manifest_dir)
    # The previously indexed paper has its PDF on disk; only the new paper's
    # empty reservation may be released.
    (papers_dir / load_paper_file_index(index_path)["ART000000001"].filename).write_bytes(b"%PDF-1.7 ok")
    new_paper = replace(_paper("ART000000002"), title="신규 논문")

    result = KciPdfCollector(
        {},
        config=KciPdfConfig(
            papers_dir=papers_dir,
            manifest_dir=manifest_dir,
        ),
        artifact_validator=_MutableValidator(),
    ).collect([new_paper])

    # Downstream stages treat the index as the analysable corpus, so a paper
    # that no provider could deliver must not appear in it.
    assert result.unresolved_source_ids == (new_paper.source_id,)
    indexed = load_paper_file_index(index_path)
    assert set(indexed) == {"ART000000001"}
    assert indexed["ART000000001"].filename == "기존 논문.pdf"


def test_pre_existing_index_entries_survive_an_unresolved_rerun(tmp_path: Path) -> None:
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    index_path = _seed_index(manifest_dir)
    existing = replace(_paper("ART000000001"), title="기존 논문")
    # The PDF exists but is (temporarily) invalid: the paper is retried and its
    # long-standing index entry is kept.
    (papers_dir / load_paper_file_index(index_path)["ART000000001"].filename).write_bytes(b"%PDF-1.7 stale")

    KciPdfCollector(
        {},
        config=KciPdfConfig(papers_dir=papers_dir, manifest_dir=manifest_dir),
        artifact_validator=_MutableValidator(),
    ).collect([existing])

    assert set(load_paper_file_index(index_path)) == {"ART000000001"}


def test_stale_reservations_from_an_interrupted_run_are_released(tmp_path: Path) -> None:
    """An index entry without any PDF file is a leftover reservation, not a paper."""
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    index_path = manifest_dir / PAPER_FILE_INDEX_NAME
    index_path.write_text(
        json.dumps(
            paper_file_index_document(
                build_paper_file_records([("ART000000001", "기존 논문"), ("ART000000002", "끊긴 예약")])
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (papers_dir / load_paper_file_index(index_path)["ART000000001"].filename).write_bytes(b"%PDF-1.7 ok")

    KciPdfCollector(
        {},
        config=KciPdfConfig(papers_dir=papers_dir, manifest_dir=manifest_dir),
        artifact_validator=_MutableValidator(current_by_root={papers_dir: {"ART000000001"}}),
    ).collect([replace(_paper("ART000000001"), title="기존 논문")])

    assert set(load_paper_file_index(index_path)) == {"ART000000001"}


def test_candidates_are_stably_deduplicated_case_insensitively(
    tmp_path: Path,
) -> None:
    result = KciPdfCollector(
        {},
        config=KciPdfConfig(papers_dir=tmp_path, dry_run=True),
        artifact_validator=_MutableValidator(),
    ).collect([_paper("ART001", 1), _paper("art001", 2), _paper("ART002", 3)])
    assert result.selected_source_ids == ("ART001", "ART002")


def test_legacy_root_is_read_only_reuse_source(tmp_path: Path) -> None:
    current_root = tmp_path / "Analyzer" / "Paper"
    legacy_root = tmp_path / "Papers"
    validator = _MutableValidator(current_by_root={legacy_root: {"legacy"}})
    seen: list[tuple[str, ...]] = []

    def callback(papers, context):
        seen.append(tuple(paper.source_id for paper in papers))
        return ProviderRunReport(provider=context.provider)

    result = KciPdfCollector(
        {PdfProvider.KCI: ProviderFunctionRunner(PdfProvider.KCI, callback)},
        config=KciPdfConfig(
            papers_dir=current_root,
            compatibility_papers_dirs=(legacy_root,),
        ),
        artifact_validator=validator,
    ).collect([_paper("legacy"), _paper("new", 2)])
    assert seen == [("new",)]
    assert result.initially_current_source_ids == frozenset({"legacy"})
    assert not legacy_root.exists()


def test_default_path_reads_environment_at_configuration_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYZER_PAPER_DIR", "custom/papers")
    monkeypatch.setenv("ANALYZER_MANIFEST_DIR", "custom/manifests")
    assert default_papers_dir() == Path("custom/papers")
    assert default_manifest_dir() == Path("custom/manifests")
    config = KciPdfConfig()
    assert config.papers_dir == Path("custom/papers")
    assert config.manifest_dir == Path("custom/manifests")


def test_default_runner_factory_uses_supported_anonymous_and_credentialed_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    base = build_default_provider_runners(KciPdfConfig())
    assert set(base) == {PdfProvider.KCI, PdfProvider.SEMANTIC_SCHOLAR}
    assert all(isinstance(runner, PaperPipelineProviderRunner) for runner in base.values())

    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.org")
    monkeypatch.setenv("OPENALEX_API_KEY", "issued-key")
    monkeypatch.setenv("ELSEVIER_API_KEY", "elsevier-key")
    configured = build_default_provider_runners(
        KciPdfConfig(
            enable_dbpia=True,
            enable_kyobo=True,
            enable_jams=True,
            enable_sciencedirect=True,
        )
    )
    assert tuple(configured) == tuple(PdfProvider)


def test_jams_provider_options_wire_interactive_login_into_download_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[DownloadConfig] = []

    class CaptureDownloader:
        dbpia_browser_resolver = None
        kyobo_browser_resolver = None
        jams_browser_resolver = None

        def __init__(self, config: DownloadConfig, *, http=None) -> None:
            captured.append(config)

        @staticmethod
        def download(_paper, _paths, *, force=False):
            return {"status": "not_found"}

    monkeypatch.setattr(kci_pdf_module, "PdfDownloader", CaptureDownloader)
    monkeypatch.setattr(
        kci_pdf_module,
        "download_artifact_is_current",
        lambda *_args, **_kwargs: False,
    )
    paper = PaperCandidate(
        database_id=151,
        source="kci",
        source_id="ART003071078",
        title="iOS knowledgeC.db 활용 삭제된 메신저 앱 사용 행위추적 방안 연구",
        doi_original="10.22798/KDFS.2024.18.1.63",
        doi="10.22798/KDFS.2024.18.1.63",
        landing_url=None,
        abstract=None,
        publication_year=2024,
        first_page="63",
        final_page="78",
        journal_id="디지털포렌식연구",
        institution_id="한국디지털포렌식학회",
        issue_id="18_1",
        authors=("안치현",),
    )
    collector = KciPdfCollector(
        {PdfProvider.JAMS: PaperPipelineProviderRunner(PdfProvider.JAMS)},
        config=KciPdfConfig(
            papers_dir=tmp_path,
            enable_jams=True,
            provider_options={
                "jams": {
                    "interactive_login": True,
                }
            },
        ),
        artifact_validator=_MutableValidator(),
    )

    collector.collect([paper])

    assert len(captured) == 1
    assert captured[0].jams_browser_interactive_login is True
    assert captured[0].jams_browser_headless is False


def test_source_id_command_builder_keeps_credentials_out_of_argv() -> None:
    builder = SourceIdCommandBuilder(("python3", "stage.py"))
    context = ProviderContext(
        provider=PdfProvider.KCI,
        papers_dir=Path("Analyzer/Paper"),
        database_url="mysql://user:secret@example/db",
        options={},
    )
    command = builder.build([_paper("ART;not-shell")], context)
    assert command[-2:] == ("--source-id", "ART;not-shell")
    assert all("secret" not in argument for argument in command)


def test_scoped_downloader_records_only_selected_provider(tmp_path: Path) -> None:
    paper = _paper("ART000000001")
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(provider_scope="unpaywall", unpaywall_email=None)
    )
    manifest = downloader.download(paper, paths)

    assert manifest["provider_scope"] == "unpaywall"
    assert set(manifest["provider_attempts"]) == {"unpaywall"}
    assert {attempt["resolver"] for attempt in manifest["attempts"]} == {
        "unpaywall"
    }
    assert load_json_object(paths.download_manifest)["status"] == "not_found"


def test_invalid_scoped_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="provider_scope"):
        PdfDownloader(DownloadConfig(provider_scope="combined"))


def test_intact_hash_verified_downloads_stay_current_after_metadata_drift(tmp_path: Path) -> None:
    """A reviewed PDF must never become a download target because DB metadata changed."""
    from Collector.kci_pdf import download_artifact_is_intact, ManifestArtifactValidator
    from PaperPipeline.storage import ArtifactPaths, write_json_atomic
    import hashlib

    from analysis_fixtures import file_record, write_index

    papers_dir = tmp_path / "Paper"
    manifest_dir = tmp_path / "Manifest"
    papers_dir.mkdir()
    manifest_dir.mkdir()
    pdf = papers_dir / "논문.pdf"
    pdf.write_bytes(b"%PDF-1.7\nreviewed corpus bytes")
    write_index(manifest_dir, [file_record("ART000000001", "예전 제목", "논문.pdf")])
    paper = PaperCandidate(
        database_id=1, source="kci", source_id="ART000000001", title="갱신된 제목",
        doi_original=None, doi="10.1234/new-doi", landing_url=None, abstract=None,
        publication_year=2024, first_page="1", final_page="9",
    )
    paths = ArtifactPaths.for_source(papers_dir, paper.source_id, manifest_root=manifest_dir, pdf_filename="논문.pdf")
    manifest = {
        "schema_version": "1.1",
        "paper": {"source_id": "ART000000001", "title": "예전 제목", "doi": None},
        "status": "downloaded",
        "resolver": {"name": "kyobo-authenticated-browser"},
        "pdf": {"path": "논문.pdf", "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "bytes": pdf.stat().st_size, "page_count": 3},
        "attempts": [],
    }
    write_json_atomic(paths.download_manifest, manifest)

    # The strict identity policy rejects the drifted manifest, the byte-level
    # check accepts it, and the collector's validator therefore keeps it current.
    assert download_artifact_is_intact(paper, paths) is True
    assert ManifestArtifactValidator().is_current(paper, papers_dir, manifest_dir) is True

    # Any byte change, filename mismatch, or non-downloaded status breaks intactness.
    pdf.write_bytes(b"%PDF-1.7\nreplaced bytes")
    assert download_artifact_is_intact(paper, paths) is False
    pdf.write_bytes(b"%PDF-1.7\nreviewed corpus bytes")
    write_json_atomic(paths.download_manifest, {**manifest, "status": "unmanaged_existing_file"})
    assert download_artifact_is_intact(paper, paths) is False
    write_json_atomic(paths.download_manifest, {**manifest, "pdf": {**manifest["pdf"], "path": "other.pdf"}})
    assert download_artifact_is_intact(paper, paths) is False


def test_provider_runner_never_hands_an_intact_artifact_to_the_downloader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PaperPipeline.storage import ArtifactPaths, write_json_atomic
    import hashlib

    downloads: list[str] = []

    class RecordingDownloader:
        dbpia_browser_resolver = None
        kyobo_browser_resolver = None
        jams_browser_resolver = None

        def __init__(self, config, *, http=None) -> None:
            pass

        @staticmethod
        def download(paper, _paths, *, force=False):
            downloads.append(paper.source_id)
            return {"status": "unmanaged_existing_file"}

    monkeypatch.setattr(kci_pdf_module, "PdfDownloader", RecordingDownloader)
    monkeypatch.setattr(kci_pdf_module, "download_artifact_is_current", lambda *_a, **_k: False)
    paper = PaperCandidate(
        database_id=1, source="kci", source_id="ART000000001", title="제목", doi_original=None, doi=None,
        landing_url=None, abstract=None, publication_year=2024, first_page="1", final_page="9",
    )
    papers_dir = tmp_path / "Paper"
    manifest_dir = tmp_path / "Manifest"
    papers_dir.mkdir(); manifest_dir.mkdir()
    pdf = papers_dir / f"{paper.source_id}.pdf"
    pdf.write_bytes(b"%PDF-1.7\nbytes")
    paths = ArtifactPaths.for_source(papers_dir, paper.source_id, manifest_root=manifest_dir, pdf_filename=pdf.name)
    write_json_atomic(paths.download_manifest, {
        "schema_version": "1.1", "paper": {"source_id": paper.source_id, "title": "제목"}, "status": "downloaded",
        "resolver": {"name": "kci-ui"},
        "pdf": {"path": pdf.name, "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "bytes": pdf.stat().st_size, "page_count": 2},
        "attempts": [],
    })
    runner = PaperPipelineProviderRunner(PdfProvider.KCI)
    report = runner.run(
        (paper,),
        kci_pdf_module.ProviderContext(
            provider=PdfProvider.KCI, papers_dir=papers_dir, database_url=None, options={},
            manifest_dir=manifest_dir, progress=lambda _message: None,
        ),
    )
    assert downloads == []
    assert report.successful_source_ids == frozenset({paper.source_id})
    assert json.loads(paths.download_manifest.read_text(encoding="utf-8"))["status"] == "downloaded"
