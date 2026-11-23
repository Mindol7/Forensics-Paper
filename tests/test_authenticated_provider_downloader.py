from __future__ import annotations

import io
import json
from pathlib import Path

from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module

from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.jams_browser import (
    JAMS_BROWSER_RESOLVER_NAME,
    JamsBrowserAttempt,
    JamsBrowserResolution,
)
from PaperPipeline.kyobo_browser import (
    KYOBO_BROWSER_RESOLVER_NAME,
    KyoboBrowserAttempt,
    KyoboBrowserResolution,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths


TITLE = "Authenticated Provider Digital Forensics Study"


def _paper(*, source_id: str, doi: str | None = None) -> PaperCandidate:
    return PaperCandidate(
        database_id=41,
        source="kci",
        source_id=source_id,
        title=TITLE,
        doi_original=doi,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=2026,
        first_page="1",
        final_page="1",
        journal_id="디지털포렌식연구",
        institution_id="한국디지털포렌식학회",
        issue_id="20_1",
        authors=("Kim",),
    )


def _pdf(*, include_title: bool = True, pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _index in range(pages):
        writer.add_blank_page(width=612, height=792)
    if include_title:
        writer.add_metadata({"/Title": TITLE})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _KyoboResolver:
    def __init__(self, resolution: KyoboBrowserResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> KyoboBrowserResolution:
        assert paper.source_id == self.resolution.paper.source_id
        assert landing_url is None
        self.calls += 1
        return self.resolution


class _JamsResolver:
    def __init__(self, resolution: JamsBrowserResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        public_resolution=None,
    ) -> JamsBrowserResolution:
        assert paper.source_id == self.resolution.paper.source_id
        assert public_resolution is None
        self.calls += 1
        return self.resolution


_COMPLETE_KYOBO_PROVENANCE = (
    "kyobo_landing_title_normalized_exact",
    "kyobo_landing_doi_unique_observed",
    "kyobo_landing_provider_doi_bibliographic_identity",
    "kyobo_landing_year_exact",
    "kyobo_landing_journal_exact",
    "kyobo_landing_publisher_exact",
    "kyobo_landing_volume_issue_exact",
    "kyobo_landing_page_range_exact",
    "kyobo_landing_authors_exact",
    "kyobo_landing_canonical_article_exact",
    "kyobo_web_title_search_exact",
    "kyobo_visible_down_control_exact",
    "native_browser_download_event",
)


def _kyobo_resolution(
    paper: PaperCandidate,
    *,
    content: bytes | None = None,
    provenance: tuple[str, ...] = _COMPLETE_KYOBO_PROVENANCE,
    landing_page_count: int = 1,
) -> KyoboBrowserResolution:
    return KyoboBrowserResolution(
        status="resolved",
        reason="authenticated_kyobo_exact_pdf_identity",
        paper=paper,
        content=content if content is not None else _pdf(),
        content_type="application/pdf",
        matched_title=TITLE,
        matched_doi="10.1234/kyobo.2026.1",
        landing_first_page="1",
        landing_final_page=str(landing_page_count),
        landing_page_count=landing_page_count,
        identity_provenance=provenance,
        attempts=(KyoboBrowserAttempt("kyobo-browser-download", "downloaded"),),
    )


def test_kyobo_scope_commits_validated_bytes_without_transport_url(
    tmp_path: Path,
) -> None:
    paper = _paper(source_id="ART-KYOBO")
    resolver = _KyoboResolver(
        _kyobo_resolution(paper)
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="kyobo",
            enable_kyobo_browser_download=True,
        ),
        kyobo_browser_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == KYOBO_BROWSER_RESOLVER_NAME
    assert manifest["resolver"]["landing_url"] is None
    assert manifest["resolver"]["download_url"] is None
    assert (
        manifest["pdf"]["identity_status"]
        == "kyobo_browser_bibliographic_identity_match"
    )
    assert manifest["pdf"]["matched_doi"] == "10.1234/kyobo.2026.1"
    assert paths.pdf.read_bytes().startswith(b"%PDF-")
    assert resolver.calls == 1
    assert downloader_module.download_artifact_is_current(
        paper,
        paths,
        manifest,
    )

    tampered = json.loads(json.dumps(manifest))
    tampered["attempts"][-1]["identity_provenance"].remove(
        "kyobo_pdf_page_count_exact"
    )
    assert not downloader_module.download_artifact_is_current(
        paper,
        paths,
        tampered,
    )


def test_kyobo_complete_identity_accepts_unextractable_pdf_but_not_incomplete_chain(
    tmp_path: Path,
) -> None:
    paper = _paper(source_id="ART-KYOBO-SCAN")
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    complete = PdfDownloader(
        DownloadConfig(
            provider_scope="kyobo",
            enable_kyobo_browser_download=True,
        ),
        kyobo_browser_resolver=_KyoboResolver(
            _kyobo_resolution(paper, content=_pdf(include_title=False))
        ),
    ).download(paper, paths)

    assert complete["status"] == "downloaded"
    assert downloader_module.download_artifact_is_current(
        paper,
        paths,
        complete,
    )

    incomplete_paper = _paper(source_id="ART-KYOBO-INCOMPLETE")
    incomplete_paths = ArtifactPaths.for_source(
        tmp_path,
        incomplete_paper.source_id,
    )
    incomplete = PdfDownloader(
        DownloadConfig(
            provider_scope="kyobo",
            enable_kyobo_browser_download=True,
        ),
        kyobo_browser_resolver=_KyoboResolver(
            _kyobo_resolution(
                incomplete_paper,
                provenance=tuple(
                    value
                    for value in _COMPLETE_KYOBO_PROVENANCE
                    if value != "kyobo_landing_authors_exact"
                ),
            )
        ),
    ).download(incomplete_paper, incomplete_paths)

    assert incomplete["status"] != "downloaded"
    assert not incomplete_paths.pdf.exists()


def test_kyobo_one_page_landing_difference_requires_pdf_title_identity(
    tmp_path: Path,
) -> None:
    paper = _paper(source_id="ART-KYOBO-COVER")
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="kyobo",
            enable_kyobo_browser_download=True,
        ),
        kyobo_browser_resolver=_KyoboResolver(
            _kyobo_resolution(
                paper,
                content=_pdf(include_title=True),
                landing_page_count=2,
            )
        ),
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert (
        "kyobo_pdf_page_count_off_by_one_title_exact"
        in manifest["attempts"][-1]["identity_provenance"]
    )
    assert downloader_module.download_artifact_is_current(
        paper,
        paths,
        manifest,
    )

    no_title_paper = _paper(source_id="ART-KYOBO-COVER-NO-TITLE")
    no_title_paths = ArtifactPaths.for_source(tmp_path, no_title_paper.source_id)
    no_title_manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="kyobo",
            enable_kyobo_browser_download=True,
        ),
        kyobo_browser_resolver=_KyoboResolver(
            _kyobo_resolution(
                no_title_paper,
                content=_pdf(include_title=False),
                landing_page_count=2,
            )
        ),
    ).download(no_title_paper, no_title_paths)

    assert no_title_manifest["status"] != "downloaded"
    assert not no_title_paths.pdf.exists()


def test_jams_scope_commits_validated_bytes_without_transport_url(
    tmp_path: Path,
) -> None:
    paper = _paper(source_id="ART-JAMS", doi="10.22798/kdfs.2026.20.1.1")
    resolver = _JamsResolver(
        JamsBrowserResolution(
            status="resolved",
            reason="authenticated_kdfs_jams_exact_pdf_identity",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
            matched_doi=paper.doi,
            identity_provenance=(
                "jams_download_control_exactly_one",
                "native_browser_download_event",
                "kdfs_pdf_identity_exact",
            ),
            attempts=(JamsBrowserAttempt("jams-browser-download", "downloaded"),),
        )
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="jams",
            enable_jams_browser_download=True,
            enable_kdfs_download=False,
        ),
        jams_browser_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == JAMS_BROWSER_RESOLVER_NAME
    assert manifest["resolver"]["landing_url"] is None
    assert manifest["resolver"]["download_url"] is None
    assert (
        manifest["pdf"]["identity_status"]
        == "jams_browser_bibliographic_identity_match"
    )
    assert downloader_module.download_artifact_is_current(
        paper,
        paths,
        manifest,
    )
    assert resolver.calls == 1


def test_authenticated_provider_failure_is_sanitized_and_classified(
    tmp_path: Path,
) -> None:
    paper = _paper(source_id="ART-JAMS-AUTH")
    resolver = _JamsResolver(
        JamsBrowserResolution(
            status="authentication_required",
            reason="jams_authentication_requires_manual_refresh",
            error_code="authentication_required",
            paper=paper,
            attempts=(
                JamsBrowserAttempt(
                    "jams-browser-detail",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="jams",
            enable_jams_browser_download=True,
        ),
        jams_browser_resolver=resolver,
    ).download(paper, paths)

    serialized = json.dumps(manifest, ensure_ascii=False).casefold()
    assert manifest["status"] == "authentication_required"
    assert not paths.pdf.exists()
    assert "cookie" not in serialized
    assert "token=" not in serialized
    assert "password" not in serialized


def test_download_config_wires_jams_interactive_login_to_browser_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = []

    class CaptureJamsResolver:
        def __init__(self, config) -> None:
            captured.append(config)

    monkeypatch.setattr(
        downloader_module,
        "AuthenticatedKdfsJamsBrowserResolver",
        CaptureJamsResolver,
    )

    downloader_module.PdfDownloader(
        downloader_module.DownloadConfig(
            enable_jams_browser_download=True,
            jams_browser_profile_dir=tmp_path / "private-profile",
            repository_root=tmp_path / "repository",
            jams_browser_headless=False,
            jams_browser_interactive_login=True,
        )
    )

    assert len(captured) == 1
    assert captured[0].interactive_login is True
    assert captured[0].headless is False
