from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module
import scripts.process_papers as process_papers
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.spo_resolver import (
    SPO_RESOLVER_NAME,
    SpoResolution,
    SpoResolverAttempt,
    SpoResolverConfig,
)
from PaperPipeline.storage import ArtifactPaths


TITLE = "SPO exact forensic integration paper"
POST_URL = (
    "https://www.spo.go.kr/site/spo/ex/board/View.do?"
    "bcIdx=1080251&cbIdx=1303"
)
PDF_URL = (
    "https://www.spo.go.kr/common/board/Download.do?"
    "bcIdx=1080251&cbIdx=1303&streFileNm=server-paper.pdf"
)
ZIP_URL = PDF_URL.removesuffix("server-paper.pdf") + "server-bundle.zip"


def _paper(
    *,
    source_id: str = "ART003197138",
    doi: str | None = None,
    journal: str = "법과학의 신동향",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=300,
        source="kci",
        source_id=source_id,
        title=TITLE,
        doi_original=doi,
        doi=doi,
        landing_url=f"https://www.kci.go.kr/article/{source_id}",
        abstract=None,
        publication_year=2025,
        first_page="1",
        final_page="12",
        journal_id=journal,
        institution_id="대검찰청",
        issue_id="11",
        authors=("Alice Example(University)",),
    )


def _pdf_with_metadata_title(title: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class NoNetworkHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bytes(self, url: str, **_kwargs) -> HttpFetch:
        self.calls.append(url)
        raise AssertionError(f"unexpected downloader network request: {url}")

    def post_form_bytes(self, url: str, **_kwargs) -> HttpFetch:
        self.calls.append(url)
        raise AssertionError(f"unexpected downloader POST request: {url}")


def _permission_required(paper: PaperCandidate) -> SpoResolution:
    return SpoResolution(
        status="permission_required",
        reason="spo_automated_copying_not_enabled",
        paper=paper,
        error_code="permission_required",
    )


def _resolved(
    paper: PaperCandidate,
    *,
    body: bytes,
    archive_entry: str | None = None,
) -> SpoResolution:
    url = ZIP_URL if archive_entry is not None else PDF_URL
    fetch = HttpFetch(
        requested_url=url,
        final_url=url,
        status_code=200,
        headers={
            "Content-Type": "application/pdf",
            **(
                {"X-SPO-Archive-Entry": archive_entry}
                if archive_entry is not None
                else {}
            ),
        },
        body=body,
        redirects=(),
    )
    return SpoResolution(
        status="resolved",
        reason="official_spo_pdf_exact_content_identity",
        paper=paper,
        candidate=ResolverCandidate(
            url=url,
            resolver=SPO_RESOLVER_NAME,
            landing_url=POST_URL,
            is_open_access=None,
            verified_doi=paper.doi,
        ),
        fetch=fetch,
        post_url=POST_URL,
        attachment_url=url,
        archive_entry=archive_entry,
        matched_title=TITLE if paper.doi is None else None,
        matched_doi=paper.doi,
        attempts=(
            SpoResolverAttempt(
                stage="spo-pdf-identity",
                url=url,
                status="matched",
                http_status=200,
                detail=archive_entry,
            ),
        ),
        skipped_kci_links=2,
        http_status=200,
    )


def _patch_spo_factory(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[PaperCandidate], SpoResolution],
) -> tuple[list[SpoResolverConfig], list[str]]:
    configs: list[SpoResolverConfig] = []
    resolved_source_ids: list[str] = []

    class StubSpoResolver:
        def __init__(self, config: SpoResolverConfig) -> None:
            configs.append(config)

        def resolve(self, paper: PaperCandidate) -> SpoResolution:
            resolved_source_ids.append(paper.source_id)
            return factory(paper)

    monkeypatch.setattr(
        downloader_module,
        "SpoPublicPdfResolver",
        StubSpoResolver,
    )
    return configs, resolved_source_ids


def _spo_attempt(manifest: dict) -> dict:
    return next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == SPO_RESOLVER_NAME
    )


@pytest.mark.parametrize(
    "journal",
    [
        "법과학의 신동향",
        "형사법의 신동향",
        "SER000001908",
    ],
)
def test_default_disabled_spo_is_permission_audit_with_zero_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
) -> None:
    configs, resolved_source_ids = _patch_spo_factory(
        monkeypatch,
        _permission_required,
    )
    paper = _paper(journal=journal)
    http = NoNetworkHttp()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "permission_required"
    assert manifest["last_error"]["code"] == "permission_required"
    assert not paths.pdf.exists()
    assert not http.calls
    # The downloader policy gate does not even call the injected resolver.
    assert resolved_source_ids == []
    assert len(configs) == 1
    assert configs[0].permission_granted is False
    attempt = _spo_attempt(manifest)
    assert attempt == {
        "resolver": SPO_RESOLVER_NAME,
        "status": "skipped",
        "resolution_status": "permission_required",
        "reason": "spo_automated_copying_not_enabled",
        "skipped_kci_links": 0,
        "error_code": "permission_required",
    }


def test_enabled_spo_bytes_pass_common_validation_and_atomic_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _pdf_with_metadata_title(TITLE)
    configs, resolved_source_ids = _patch_spo_factory(
        monkeypatch,
        lambda paper: _resolved(paper, body=content),
    )
    real_validate_pdf = downloader_module.validate_pdf
    validation_calls: list[dict[str, object]] = []

    def recording_validate_pdf(body: bytes, **kwargs):
        validation_calls.append(dict(kwargs))
        return real_validate_pdf(body, **kwargs)

    monkeypatch.setattr(
        downloader_module,
        "validate_pdf",
        recording_validate_pdf,
    )
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            enable_spo_download=True,
            max_pdf_bytes=7 * 1024 * 1024,
        ),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert paths.pdf.read_bytes() == content
    assert manifest["resolver"] == {
        "name": SPO_RESOLVER_NAME,
        "is_open_access": None,
        "license": None,
        "version": None,
        "landing_url": POST_URL,
        "download_url": PDF_URL,
    }
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert manifest["pdf"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["pdf"]["bytes"] == len(content)
    assert (
        json.loads(paths.download_manifest.read_text(encoding="utf-8"))
        == manifest
    )
    assert validation_calls == [
        {
            "expected_doi": None,
            "expected_title": TITLE,
            "require_identity": True,
        }
    ]
    assert resolved_source_ids == [paper.source_id]
    assert configs[0].permission_granted is True
    assert configs[0].max_attachment_bytes == 7 * 1024 * 1024
    attempt = _spo_attempt(manifest)
    assert attempt["status"] == "downloaded"
    assert attempt["resolution_status"] == "resolved"
    assert attempt["post_url"] == POST_URL
    assert attempt["attachment_url"] == PDF_URL
    assert attempt["matched_title"] == TITLE
    assert attempt["skipped_kci_links"] == 2
    assert attempt["page_count"] == 1


def test_doi_bearing_spo_pdf_without_stamp_is_revalidated_and_persisted(
    tmp_path: Path,
) -> None:
    doi = "10.23026/crclps.2023..81.003"
    paper = _paper(doi=doi)
    content = _pdf_with_metadata_title(TITLE)

    class TitleMatchedSpo:
        def resolve(self, received: PaperCandidate) -> SpoResolution:
            resolved = _resolved(received, body=content)
            assert resolved.candidate is not None
            return replace(
                resolved,
                candidate=replace(
                    resolved.candidate,
                    verified_doi=None,
                ),
                matched_title=TITLE,
                matched_doi=None,
            )

    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(enable_spo_download=True),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        spo_resolver=TitleMatchedSpo(),
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert manifest["pdf"]["matched_doi"] is None
    attempt = _spo_attempt(manifest)
    assert attempt["matched_title"] == TITLE
    assert "matched_doi" not in attempt


def test_zip_member_provenance_is_retained_in_existing_manifest_fields(
    tmp_path: Path,
) -> None:
    paper = _paper()
    content = _pdf_with_metadata_title(TITLE)
    archive_entry = "papers/selected.pdf"

    class ResolvedSpo:
        def resolve(self, received: PaperCandidate) -> SpoResolution:
            return _resolved(
                received,
                body=content,
                archive_entry=archive_entry,
            )

    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(enable_spo_download=True),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        spo_resolver=ResolvedSpo(),
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["download_url"] == ZIP_URL
    assert manifest["resolver"]["version"] == (
        f"zip-entry:{archive_entry}"
    )
    assert manifest["pdf"]["content_type"] == "application/pdf"
    attempt = _spo_attempt(manifest)
    assert attempt["archive_entry"] == archive_entry
    assert attempt["bytes"] == len(content)


def test_project_validation_rejects_bad_bytes_even_if_spo_claims_resolved(
    tmp_path: Path,
) -> None:
    paper = _paper()
    wrong_pdf = _pdf_with_metadata_title("A different paper")

    class IncorrectSpo:
        def resolve(self, received: PaperCandidate) -> SpoResolution:
            return _resolved(received, body=wrong_pdf)

    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    manifest = PdfDownloader(
        DownloadConfig(enable_spo_download=True),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        spo_resolver=IncorrectSpo(),
    ).download(paper, paths)

    assert manifest["status"] == "invalid_pdf"
    assert manifest["last_error"]["code"] == "invalid_pdf"
    assert not paths.pdf.exists()
    attempt = _spo_attempt(manifest)
    assert attempt["status"] == "failed"
    assert attempt["resolution_status"] == "resolved"
    assert attempt["error_code"] == "invalid_pdf"
    assert "identity could not be confirmed" in attempt["message"]


def test_non_spo_row_does_not_enter_shared_spo_resolver(
    tmp_path: Path,
) -> None:
    paper = _paper(journal="다른 학술지")

    class UnexpectedSpo:
        def resolve(self, _paper: PaperCandidate) -> SpoResolution:
            raise AssertionError("non-SPO rows must not enter the resolver")

    manifest = PdfDownloader(
        DownloadConfig(),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        spo_resolver=UnexpectedSpo(),
    ).download(
        paper,
        ArtifactPaths.for_source(tmp_path, paper.source_id),
    )

    assert manifest["status"] == "identifier_unresolved"
    assert not any(
        str(attempt.get("resolver") or "").startswith(SPO_RESOLVER_NAME)
        for attempt in manifest["attempts"]
    )


def test_cli_defaults_off_and_opt_in_prints_written_permission_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = process_papers.build_parser()
    default_args = parser.parse_args(["download"])
    process_papers._validate_args(default_args)
    default_runtime = process_papers._download_runtime(default_args)

    assert default_args.enable_spo_download is False
    assert default_runtime.config.enable_spo_download is False
    assert default_runtime.spo_resolver.config.permission_granted is False

    enabled_args = parser.parse_args(
        ["download", "--enable-spo-download", "--max-pdf-mb", "17"]
    )
    process_papers._validate_args(enabled_args)
    warning = capsys.readouterr().err
    enabled_runtime = process_papers._download_runtime(enabled_args)

    assert "서면 허가" in warning
    assert enabled_runtime.config.enable_spo_download is True
    assert enabled_runtime.spo_resolver.config.permission_granted is True
    assert (
        enabled_runtime.spo_resolver.config.max_attachment_bytes
        == 17 * 1024 * 1024
    )
    assert enabled_runtime.spo_resolver.config.requests_per_second == 0.5


def test_cli_help_states_spo_is_disabled_without_written_permission(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = process_papers.build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["download", "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert "--enable-spo-download" in output
    assert "기본 비활성" in output
    assert "서면 허가" in normalized_output
