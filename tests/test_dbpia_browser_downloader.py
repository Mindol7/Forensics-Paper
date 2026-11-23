from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter

from PaperPipeline.dbpia_browser import (
    DBPIA_BROWSER_RESOLVER_NAME,
    DbpiaBrowserAttempt,
    DbpiaBrowserResolution,
)
from PaperPipeline.downloader import (
    DownloadConfig,
    PdfDownloader,
    download_artifact_is_current,
)
from PaperPipeline.platform_resolver import PlatformResolution
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths


LANDING = (
    "https://www.dbpia.co.kr/journal/articleDetail?"
    "nodeId=NODE12345678"
)
TITLE = "An Exact Browser Download Forensics Paper"
DOI_LESS_BIBLIOGRAPHY_PROVENANCE = (
    "dbpia_landing_publisher_exact",
    "dbpia_landing_journal_exact",
    "dbpia_landing_volume_issue_exact",
    "dbpia_landing_page_range_exact",
    "dbpia_landing_authors_exact",
)


def _paper() -> PaperCandidate:
    return PaperCandidate(
        database_id=11,
        source="kci",
        source_id="ART009999999",
        title=TITLE,
        doi_original=None,
        doi=None,
        landing_url=None,
        abstract=None,
        publication_year=2024,
        first_page="1",
        final_page="12",
        journal_id="Test Forensics Journal",
        institution_id="Test Forensics Publisher",
        issue_id="1_1",
        authors=("Alice Example",),
    )


def _pdf(marker: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    metadata = {"/Title": TITLE}
    if marker is not None:
        metadata["/Subject"] = marker
    writer.add_metadata(metadata)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_pdf(text: str) -> bytes:
    escaped = (
        text.encode("latin-1")
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
    )
    stream = b"BT\n/F1 12 Tf\n72 720 Td\n(" + escaped + b") Tj\nET\n"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        4: (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream"
        ),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * 6
    for object_id in range(1, 6):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode())
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for object_id in range(1, 6):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(pdf)


class FakeBrowserResolver:
    def __init__(
        self,
        resolution: DbpiaBrowserResolution,
        *,
        expected_landing: str | None = LANDING,
    ) -> None:
        self.resolution = resolution
        self.expected_landing = expected_landing
        self.calls = 0

    def resolve(self, paper: PaperCandidate, *, landing_url: str | None = None):
        assert landing_url == self.expected_landing
        self.calls += 1
        if (
            self.resolution.resolved
            and not paper.doi
            and not self.resolution.identity_provenance
        ):
            return replace(
                self.resolution,
                identity_provenance=(
                    *DOI_LESS_BIBLIOGRAPHY_PROVENANCE,
                    "dbpia_landing_identity_exact",
                    "dbpia_download_button_node_exact",
                    "native_browser_download_event",
                ),
            )
        return self.resolution


def test_native_browser_bytes_use_common_atomic_manifest_without_urls(
    tmp_path: Path,
) -> None:
    doi = "10.22798/kdfs.2024.18.1.11"
    paper = replace(_paper(), doi_original=doi, doi=doi)
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
            identity_provenance=("native_browser_download_event",),
            attempts=(
                DbpiaBrowserAttempt("dbpia-browser-download", "downloaded"),
            ),
        )
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
            ephemeral_platform_landing_source_ids=frozenset(
                {paper.source_id}
            ),
        ),
        dbpia_browser_resolver=resolver,
    )

    manifest = downloader.download(paper, paths)
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"] == {
        "name": DBPIA_BROWSER_RESOLVER_NAME,
        "is_open_access": False,
        "license": None,
        "version": None,
        "landing_url": None,
        "download_url": None,
    }
    assert paths.pdf.read_bytes().startswith(b"%PDF-")
    assert "NODE12345678" not in serialized
    assert "nodeId=" not in serialized
    assert "jwt=" not in serialized.casefold()
    assert "key=" not in serialized.casefold()

    reused = downloader.download(paper, paths)
    assert reused["_runtime"]["action"] == "reused"
    assert resolver.calls == 1


def test_native_browser_can_search_by_title_without_api_or_node_mapping(
    tmp_path: Path,
) -> None:
    doi = "10.22798/kdfs.2024.18.1.11"
    paper = replace(_paper(), doi_original=doi, doi=doi)
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
            matched_doi=doi,
            identity_provenance=(
                "dbpia_web_title_search_exact",
                "dbpia_web_title_search_doi_year_exact",
                "native_browser_download_event",
            ),
        ),
        expected_landing=None,
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(enable_dbpia_browser_download=True),
        dbpia_browser_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert resolver.calls == 1
    assert any(
        attempt.get("landing_source") == "dbpia_browser_title_search"
        for attempt in manifest["attempts"]
    )
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "nodeId=" not in serialized
    assert "NODE" not in serialized


def test_authenticated_browser_reuses_current_artifact_even_with_force(
    tmp_path: Path,
) -> None:
    paper = _paper()
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="must_not_run",
            paper=paper,
            content=_pdf(),
        )
    )
    downloader = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=resolver,
    )

    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloaded = downloader.download(paper, paths)
    forced = downloader.download(paper, paths, force=True)

    assert downloaded["status"] == "downloaded"
    assert forced["_runtime"]["action"] == "reused"
    assert resolver.calls == 1


def test_named_dbpia_campaign_replaces_current_pdf_once_and_then_reuses(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    initial_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="initial_download",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=initial_resolver,
    ).download(paper, paths)
    original = paths.pdf.read_bytes()
    replacement = _text_pdf(TITLE)
    campaign_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="campaign_download",
            paper=paper,
            content=replacement,
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )
    campaign = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="dbpia-full-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=campaign_resolver,
    )

    replaced = campaign.download(paper, paths)
    reused = campaign.download(paper, paths)

    assert original != replacement
    assert paths.pdf.read_bytes() == replacement
    assert replaced["_runtime"] == {
        "action": "redownloaded",
        "browser_attempted": True,
    }
    assert replaced["dbpia_browser_redownload"]["status"] == "replaced"
    assert reused["_runtime"] == {
        "action": "redownload_reused",
        "browser_attempted": False,
    }
    assert campaign_resolver.calls == 1


def test_doi_less_campaign_reuse_binds_every_stored_bibliographic_field(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="dbpia-bibliography-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="campaign_download",
                paper=paper,
                content=_pdf(),
                content_type="application/pdf",
                matched_title=TITLE,
            )
        ),
    ).download(paper, paths)

    assert manifest["paper"]["bibliographic_identity"]["sha256"]
    assert download_artifact_is_current(paper, paths, manifest)
    mutations = (
        replace(paper, publication_year=2023),
        replace(paper, institution_id="Different Publisher"),
        replace(paper, journal_id="Different Journal"),
        replace(paper, issue_id="1_2"),
        replace(paper, first_page="2"),
        replace(paper, final_page="13"),
        replace(paper, authors=("Mallory Example",)),
    )
    assert all(
        not download_artifact_is_current(changed, paths, manifest)
        for changed in mutations
    )


def test_doi_less_missing_source_authors_fail_closed_without_artifact(
    tmp_path: Path,
) -> None:
    paper = replace(_paper(), authors=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    provenance = tuple(
        "dbpia_landing_expected_authors_unavailable"
        if value == "dbpia_landing_authors_exact"
        else value
        for value in DOI_LESS_BIBLIOGRAPHY_PROVENANCE
    )
    manifest = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="missing-author-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="campaign_download",
                paper=paper,
                content=_pdf(),
                matched_title=TITLE,
                identity_provenance=provenance,
            )
        ),
    ).download(paper, paths)

    assert manifest["status"] == "not_found"
    assert manifest["paper"]["bibliographic_identity"] is None
    assert not paths.pdf.exists()
    assert any(
        attempt.get("reason") == "dbpia_missing_strict_bibliographic_identity"
        for attempt in manifest["attempts"]
    )


def test_direct_doi_less_browser_result_requires_full_bibliography_provenance(
    tmp_path: Path,
) -> None:
    paper = _paper()
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="insufficient_fake_identity",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
            identity_provenance=("native_browser_download_event",),
        )
    )

    manifest = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=resolver,
    ).download(paper, ArtifactPaths.for_source(tmp_path, paper.source_id))

    assert manifest["status"] == "not_found"
    assert any(
        attempt.get("reason") == "dbpia_missing_strict_bibliographic_identity"
        for attempt in manifest["attempts"]
    )


@pytest.mark.parametrize(
    "provenance",
    [
        tuple(
            "dbpia_landing_expected_authors_unavailable"
            if value == "dbpia_landing_authors_exact"
            else value
            for value in DOI_LESS_BIBLIOGRAPHY_PROVENANCE
        ),
        tuple(
            "dbpia_landing_issue_only_exact"
            if value == "dbpia_landing_volume_issue_exact"
            else value
            for value in DOI_LESS_BIBLIOGRAPHY_PROVENANCE
        ),
    ],
)
def test_doi_less_boundary_rejects_impossible_provenance_cross_binding(
    tmp_path: Path,
    provenance: tuple[str, ...],
) -> None:
    paper = _paper()
    manifest = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="impossible_provenance",
                paper=paper,
                content=_pdf(),
                matched_title=TITLE,
                identity_provenance=provenance,
            )
        ),
    ).download(paper, ArtifactPaths.for_source(tmp_path, paper.source_id))

    assert manifest["status"] != "downloaded"


def test_issue_only_boundary_rejects_volume_issue_provenance(
    tmp_path: Path,
) -> None:
    paper = replace(_paper(), issue_id="13")
    manifest = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="impossible_issue_provenance",
                paper=paper,
                content=_pdf(),
                matched_title=TITLE,
                identity_provenance=DOI_LESS_BIBLIOGRAPHY_PROVENANCE,
            )
        ),
    ).download(paper, ArtifactPaths.for_source(tmp_path, paper.source_id))

    assert manifest["status"] != "downloaded"


def test_named_dbpia_campaign_failure_preserves_current_pdf_and_manifest(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    initial_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="initial_download",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=initial_resolver,
    ).download(paper, paths)
    original_pdf = paths.pdf.read_bytes()
    original_manifest = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )
    failed_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="manual_session_refresh_required",
            paper=paper,
            error_code="authentication_required",
            attempts=(
                DbpiaBrowserAttempt(
                    "dbpia-browser",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )
    result = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="dbpia-full-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=failed_resolver,
    ).download(paper, paths)
    persisted = json.loads(paths.download_manifest.read_text(encoding="utf-8"))

    assert paths.pdf.read_bytes() == original_pdf
    assert persisted["status"] == "downloaded"
    assert persisted["pdf"] == original_manifest["pdf"]
    assert persisted["resolver"] == original_manifest["resolver"]
    assert persisted["dbpia_browser_redownload"]["status"] == "failed"
    assert result["_runtime"]["action"] == "redownload_failed"
    assert failed_resolver.calls == 1


def test_legacy_doi_less_artifact_without_fingerprint_fails_closed(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="legacy_download",
                paper=paper,
                content=_pdf(),
                matched_title=TITLE,
            )
        ),
    ).download(paper, paths)
    legacy = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    legacy["paper"].pop("bibliographic_identity", None)
    paths.download_manifest.write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )
    assert not download_artifact_is_current(paper, paths, legacy)
    failed = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="manual_session_refresh_required",
            paper=paper,
            error_code="authentication_required",
            attempts=(
                DbpiaBrowserAttempt(
                    "dbpia-browser",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )

    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="legacy-refresh",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=failed,
    ).download(paper, paths)
    persisted = json.loads(paths.download_manifest.read_text(encoding="utf-8"))

    assert persisted["status"] == "authentication_required"
    assert persisted["dbpia_browser_redownload"]["status"] == "failed"
    assert not download_artifact_is_current(paper, paths, persisted)


def test_legacy_doi_less_committing_campaign_without_fingerprint_is_not_reused(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    initial = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="legacy-commit",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="legacy_campaign_download",
                paper=paper,
                content=_pdf(),
                matched_title=TITLE,
            )
        ),
    ).download(paper, paths)
    interrupted = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    interrupted["status"] = "committing"
    interrupted["paper"].pop("bibliographic_identity", None)
    interrupted.pop("_dbpia_browser_commit_rollback", None)
    paths.download_manifest.write_text(
        json.dumps(interrupted, ensure_ascii=False),
        encoding="utf-8",
    )
    retry = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="manual_session_refresh_required",
            paper=paper,
            error_code="authentication_required",
        )
    )

    result = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="legacy-commit",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=retry,
    ).download(paper, paths)

    assert initial["dbpia_browser_redownload"]["status"] == "downloaded"
    assert result["_runtime"]["action"] == "redownload_failed"
    assert retry.calls == 1
    assert not download_artifact_is_current(paper, paths, result)


def test_campaign_does_not_mistake_pre_campaign_commit_recovery_for_completion(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    initial_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="initial_download",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=initial_resolver,
    ).download(paper, paths)
    interrupted = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )
    interrupted["status"] = "committing"
    paths.download_manifest.write_text(
        json.dumps(interrupted, ensure_ascii=False),
        encoding="utf-8",
    )
    campaign_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="campaign_download",
            paper=paper,
            content=_text_pdf(TITLE),
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )

    result = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="dbpia-full-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=campaign_resolver,
    ).download(paper, paths)

    assert result["_runtime"]["action"] == "redownloaded"
    assert campaign_resolver.calls == 1


def test_campaign_recovers_old_pair_when_crash_precedes_pdf_replace(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    initial_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="initial_download",
            paper=paper,
            content=_pdf(),
            content_type="application/pdf",
            matched_title=TITLE,
        )
    )
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=initial_resolver,
    ).download(paper, paths)
    old_pdf = paths.pdf.read_bytes()
    old_manifest = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )
    replacement = _text_pdf(TITLE)
    interrupted = dict(old_manifest)
    interrupted["status"] = "committing"
    interrupted["pdf"] = {
        **old_manifest["pdf"],
        "sha256": hashlib.sha256(replacement).hexdigest(),
        "bytes": len(replacement),
    }
    interrupted["_dbpia_browser_commit_rollback"] = old_manifest
    paths.download_manifest.write_text(
        json.dumps(interrupted, ensure_ascii=False),
        encoding="utf-8",
    )
    failed_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="manual_session_refresh_required",
            paper=paper,
            error_code="authentication_required",
            attempts=(
                DbpiaBrowserAttempt(
                    "dbpia-browser",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )

    result = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            dbpia_browser_redownload_campaign="dbpia-full-test",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=failed_resolver,
    ).download(paper, paths)
    persisted = json.loads(paths.download_manifest.read_text(encoding="utf-8"))

    assert result["_runtime"]["action"] == "redownload_failed"
    assert paths.pdf.read_bytes() == old_pdf
    assert persisted["status"] == "downloaded"
    assert persisted["pdf"] == old_manifest["pdf"]
    assert "_dbpia_browser_commit_rollback" not in persisted
    assert failed_resolver.calls == 1


def test_redownload_campaign_replaces_current_pdf_and_records_safe_audit(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    old_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=_pdf("old-artifact"),
        )
    )
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=old_resolver,
    ).download(paper, paths)
    old_bytes = paths.pdf.read_bytes()
    old_manifest = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )

    replacement = _pdf("campaign-replacement")
    campaign_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=replacement,
        )
    )
    manifest = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="full-refresh-20260803",
            platform_landing_urls={paper.source_id: LANDING},
            # This route would run first during an ordinary download.  A
            # campaign must ignore it and use only the browser resolver.
            kci_journal_pdf_urls={
                paper.source_id: "https://example.org/must-not-run.pdf"
            },
        ),
        dbpia_browser_resolver=campaign_resolver,
    ).download(paper, paths)

    audit = manifest["dbpia_browser_redownload"]
    assert paths.pdf.read_bytes() == replacement
    assert paths.pdf.read_bytes() != old_bytes
    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == DBPIA_BROWSER_RESOLVER_NAME
    assert manifest["_runtime"] == {
        "action": "redownloaded",
        "browser_attempted": True,
    }
    assert audit["campaign"] == "full-refresh-20260803"
    assert audit["status"] == "replaced"
    assert audit["previous_sha256"] == old_manifest["pdf"]["sha256"]
    assert audit["replacement_sha256"] == manifest["pdf"]["sha256"]
    assert campaign_resolver.calls == 1
    assert all(
        str(attempt.get("resolver") or "").startswith(
            DBPIA_BROWSER_RESOLVER_NAME
        )
        for attempt in manifest["attempts"]
    )
    serialized_audit = json.dumps(audit, ensure_ascii=False)
    assert "http" not in serialized_audit.casefold()
    assert "NODE" not in serialized_audit


def test_redownload_campaign_failure_preserves_current_pdf_and_manifest(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="authenticated_official_download_control",
                paper=paper,
                content=_pdf("retained"),
            )
        ),
    ).download(paper, paths)
    retained_bytes = paths.pdf.read_bytes()
    retained_manifest = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )
    failure_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="dbpia_authentication_requires_manual_refresh",
            paper=paper,
            error_code="authentication_required",
            attempts=(
                DbpiaBrowserAttempt(
                    "dbpia-browser",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )

    manifest = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="full-refresh-20260803",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=failure_resolver,
    ).download(paper, paths)

    stored = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    audit = stored.pop("dbpia_browser_redownload")
    assert stored == retained_manifest
    assert paths.pdf.read_bytes() == retained_bytes
    assert manifest["status"] == "downloaded"
    assert manifest["pdf"] == retained_manifest["pdf"]
    assert manifest["downloaded_at"] == retained_manifest["downloaded_at"]
    assert manifest["_runtime"] == {
        "action": "redownload_failed",
        "browser_attempted": True,
        "failure_status": "authentication_required",
    }
    assert audit["status"] == "failed"
    assert audit["failure_status"] == "authentication_required"
    assert failure_resolver.calls == 1


def test_completed_campaign_reuses_but_new_campaign_retries(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    first_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=_pdf("campaign-one"),
        )
    )
    first = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="campaign-one",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=first_resolver,
    )

    downloaded = first.download(paper, paths)
    reused = first.download(paper, paths, force=True)

    assert downloaded["_runtime"]["action"] == "redownloaded"
    assert reused["_runtime"] == {
        "action": "redownload_reused",
        "browser_attempted": False,
    }
    assert first_resolver.calls == 1

    second_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=_pdf("campaign-two"),
        )
    )
    retried = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="campaign-two",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=second_resolver,
    ).download(paper, paths)

    assert retried["_runtime"]["action"] == "redownloaded"
    assert retried["dbpia_browser_redownload"]["status"] == "replaced"
    assert retried["dbpia_browser_redownload"]["campaign"] == "campaign-two"
    assert second_resolver.calls == 1


def test_redownload_campaign_budget_deferral_preserves_current_artifact(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=FakeBrowserResolver(
            DbpiaBrowserResolution(
                status="resolved",
                reason="authenticated_official_download_control",
                paper=paper,
                content=_pdf("retained-after-deferral"),
            )
        ),
    ).download(paper, paths)
    retained_bytes = paths.pdf.read_bytes()
    retained_manifest = json.loads(
        paths.download_manifest.read_text(encoding="utf-8")
    )
    deferred_resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="retryable_error",
            reason="dbpia_browser_attempt_budget_exhausted",
            paper=paper,
            error_code="browser_attempt_limit_reached",
            attempts=(
                DbpiaBrowserAttempt(
                    "browser-budget",
                    "failed",
                    "browser_attempt_limit_reached",
                ),
            ),
        )
    )

    manifest = PdfDownloader(
        DownloadConfig(
            dbpia_browser_redownload_campaign="full-refresh-20260803",
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=deferred_resolver,
    ).download(paper, paths)

    stored = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    audit = stored.pop("dbpia_browser_redownload")
    assert stored == retained_manifest
    assert paths.pdf.read_bytes() == retained_bytes
    assert manifest["_runtime"] == {
        "action": "redownload_deferred",
        "browser_attempted": False,
        "failure_status": "retryable_error",
    }
    assert audit["status"] == "deferred"
    assert audit["browser_attempted"] is False


def test_authentication_failure_is_terminal_for_doi_less_paper(
    tmp_path: Path,
) -> None:
    paper = _paper()
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="authentication_required",
            reason="dbpia_authentication_requires_manual_refresh",
            paper=paper,
            error_code="authentication_required",
            attempts=(
                DbpiaBrowserAttempt(
                    "dbpia-browser",
                    "failed",
                    "authentication_required",
                ),
            ),
        )
    )
    manifest = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=resolver,
    ).download(paper, ArtifactPaths.for_source(tmp_path, paper.source_id))

    assert manifest["status"] == "authentication_required"
    assert manifest["last_error"]["code"] == "authentication_required"
    assert resolver.calls == 1


def test_anonymous_identity_failure_never_falls_back_to_credentials(
    tmp_path: Path,
) -> None:
    paper = _paper()
    browser = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="must_not_be_used",
            paper=paper,
            content=_pdf(),
        )
    )

    class IdentityMismatchPlatform:
        def resolve(
            self,
            candidate: PaperCandidate,
            *,
            landing_url: str | None = None,
        ) -> PlatformResolution:
            assert candidate == paper
            assert landing_url == LANDING
            return PlatformResolution(
                status="identity_mismatch",
                reason="platform_landing_title_mismatch",
                paper=paper,
                platform="dbpia",
                error_code="identity_mismatch",
            )

    manifest = PdfDownloader(
        DownloadConfig(
            enable_platform_download=True,
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        platform_resolver=IdentityMismatchPlatform(),
        dbpia_browser_resolver=browser,
    ).download(paper, ArtifactPaths.for_source(tmp_path, paper.source_id))

    assert manifest["status"] != "downloaded"
    assert browser.calls == 0


def test_browser_layout_spaced_doi_is_validated_and_reusable(
    tmp_path: Path,
) -> None:
    doi = "10.29305/tj.2025.6.208.26"
    title = "Exact Browser Layout Forensics Title"
    paper = PaperCandidate(
        database_id=12,
        source="kci",
        source_id="ART009999998",
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=2025,
        first_page="1",
        final_page="10",
    )
    content = _text_pdf(
        f"{title} DOI: 10.29305/tj.2025.6.208 .26"
    )
    resolver = FakeBrowserResolver(
        DbpiaBrowserResolution(
            status="resolved",
            reason="authenticated_official_download_control",
            paper=paper,
            content=content,
            content_type="application/pdf",
            matched_title=title,
            matched_doi=doi,
            identity_provenance=(
                "dbpia_landing_identity_exact",
                "dbpia_download_button_node_exact",
                "native_browser_download_event",
            ),
        )
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            enable_dbpia_browser_download=True,
            platform_landing_urls={paper.source_id: LANDING},
        ),
        dbpia_browser_resolver=resolver,
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["pdf"]["identity_status"] == (
        "dbpia_browser_doi_layout_title_match"
    )
    assert manifest["pdf"]["matched_doi"] == doi
    interrupted = json.loads(paths.download_manifest.read_text(encoding="utf-8"))
    interrupted["status"] = "committing"
    paths.download_manifest.write_text(
        json.dumps(interrupted, ensure_ascii=False),
        encoding="utf-8",
    )
    recovered = downloader.download(paper, paths)
    reused = downloader.download(paper, paths)
    assert recovered["_runtime"]["action"] == "recovered"
    assert reused["_runtime"]["action"] == "reused"
    assert resolver.calls == 1
