from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module
import scripts.process_papers as process_papers
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.elsevier_resolver import (
    ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL,
    ElsevierResolution,
    ElsevierResolverConfig,
)
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths


ELSEVIER_DOI = "10.1016/j.fsidi.2026.301234"
ELSEVIER_URL = (
    f"{ELSEVIER_ARTICLE_RETRIEVAL_BASE_URL}/"
    "10.1016%2Fj.fsidi.2026.301234"
)
RESOLVER_NAME = "elsevier-article-retrieval"


def _paper(
    *,
    source_id: str = "ART003000001",
    title: str = "Elsevier forensic integration study",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=31,
        source="kci",
        source_id=source_id,
        title=title,
        doi_original=f"https://doi.org/{ELSEVIER_DOI}",
        doi=ELSEVIER_DOI,
        landing_url="https://www.kci.go.kr/example",
        abstract=None,
        publication_year=2026,
        first_page="1",
        final_page="12",
    )


def _pdf_with_metadata_title(title: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _fetch(
    *,
    body: bytes = b"",
    status: int = 200,
    content_type: str = "application/pdf",
) -> HttpFetch:
    return HttpFetch(
        requested_url=ELSEVIER_URL,
        final_url=ELSEVIER_URL,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body,
        redirects=(),
    )


class NoNetworkHttp:
    """Downloader HTTP seam that returns deterministic public-source misses."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del max_bytes, max_html_bytes, headers
        self.calls.append(url)
        return HttpFetch(
            requested_url=url,
            final_url=url,
            status_code=404,
            headers={"Content-Type": "text/plain"},
            body=b"not found",
            redirects=(),
        )

    def post_form_bytes(
        self,
        url: str,
        *,
        data: dict[str, str],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del data, max_bytes, headers
        raise AssertionError(f"unexpected POST in networkless test: {url}")


def _patch_elsevier_resolution(
    monkeypatch: pytest.MonkeyPatch,
    build_resolution: Callable[[PaperCandidate], ElsevierResolution],
) -> list[ElsevierResolverConfig]:
    configs: list[ElsevierResolverConfig] = []

    class StubElsevierResolver:
        def __init__(self, config: ElsevierResolverConfig) -> None:
            configs.append(config)

        def resolve(self, paper: PaperCandidate) -> ElsevierResolution:
            return build_resolution(paper)

    monkeypatch.setattr(
        downloader_module,
        "ElsevierArticleResolver",
        StubElsevierResolver,
    )
    return configs


def _attempt(
    manifest: dict[str, Any],
    resolver: str = RESOLVER_NAME,
) -> dict[str, Any]:
    return next(
        item
        for item in manifest["attempts"]
        if item.get("resolver") == resolver
    )


def _read_manifest(paths: ArtifactPaths) -> dict[str, Any]:
    return json.loads(paths.download_manifest.read_text(encoding="utf-8"))


def test_successful_elsevier_pdf_is_identity_validated_and_atomically_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = _paper()
    content = _pdf_with_metadata_title(paper.title or "")
    fetch = _fetch(body=content)
    configs = _patch_elsevier_resolution(
        monkeypatch,
        lambda candidate: ElsevierResolution(
            source_id=candidate.source_id,
            doi=candidate.doi,
            status="resolved",
            reason="official_elsevier_pdf_retrieved",
            fetch=fetch,
            http_status=200,
        ),
    )
    http = NoNetworkHttp()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            elsevier_api_key="integration-elsevier-key",
            max_pdf_bytes=7 * 1024 * 1024,
        ),
        http=http,  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"] == {
        "name": RESOLVER_NAME,
        "is_open_access": None,
        "license": None,
        "version": None,
        "landing_url": None,
        "download_url": ELSEVIER_URL,
    }
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert manifest["pdf"]["matched_doi"] is None
    assert manifest["pdf"]["bytes"] == len(content)
    assert manifest["pdf"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["pdf"]["content_type"] == "application/pdf"
    assert paths.pdf.read_bytes() == content
    assert _read_manifest(paths) == manifest
    assert not http.calls

    assert len(configs) == 1
    assert configs[0].api_key == "integration-elsevier-key"
    assert configs[0].max_pdf_bytes == 7 * 1024 * 1024
    attempt = _attempt(manifest)
    assert attempt["status"] == "downloaded"
    assert attempt["resolution_status"] == "resolved"
    assert attempt["reason"] == "official_elsevier_pdf_retrieved"
    assert attempt["http_status"] == 200
    assert attempt["bytes"] == len(content)
    assert attempt["page_count"] == 1


def test_elsevier_pdf_that_fails_project_identity_validation_is_not_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = _paper()
    content = _pdf_with_metadata_title("A different article")
    fetch = _fetch(body=content)
    _patch_elsevier_resolution(
        monkeypatch,
        lambda candidate: ElsevierResolution(
            source_id=candidate.source_id,
            doi=candidate.doi,
            status="resolved",
            reason="official_elsevier_pdf_retrieved",
            fetch=fetch,
            http_status=200,
        ),
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(elsevier_api_key="integration-key"),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "invalid_pdf"
    assert manifest["last_error"]["code"] == "invalid_pdf"
    assert not paths.pdf.exists()
    assert _read_manifest(paths)["status"] == "invalid_pdf"
    attempt = _attempt(manifest)
    assert attempt["status"] == "failed"
    assert attempt["resolution_status"] == "resolved"
    assert attempt["error_code"] == "invalid_pdf"
    assert "identity could not be confirmed" in attempt["message"]


def test_missing_elsevier_key_is_audited_without_constructing_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedElsevierResolver:
        def __init__(self, _config: ElsevierResolverConfig) -> None:
            raise AssertionError("Elsevier resolver must not run without a key")

    monkeypatch.setattr(
        downloader_module,
        "ElsevierArticleResolver",
        UnexpectedElsevierResolver,
    )
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(elsevier_api_key=None),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "not_found"
    assert not paths.pdf.exists()
    attempt = _attempt(manifest)
    assert attempt == {
        "resolver": RESOLVER_NAME,
        "status": "skipped",
        "error_code": "api_key_not_configured",
    }


def test_strict_oa_mode_skips_entitlement_pdf_and_records_not_open_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedElsevierResolver:
        def __init__(self, _config: ElsevierResolverConfig) -> None:
            raise AssertionError("non-OA Elsevier retrieval must be skipped")

    monkeypatch.setattr(
        downloader_module,
        "ElsevierArticleResolver",
        UnexpectedElsevierResolver,
    )
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            elsevier_api_key="configured-key",
            require_open_access_evidence=True,
        ),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "not_open_access"
    assert manifest["last_error"]["code"] == "not_open_access"
    assert not paths.pdf.exists()
    attempt = _attempt(manifest)
    assert attempt == {
        "resolver": RESOLVER_NAME,
        "status": "skipped",
        "error_code": "oa_evidence_required",
    }


@pytest.mark.parametrize(
    (
        "resolution_status",
        "reason",
        "http_status",
        "error_code",
        "expected_manifest_status",
    ),
    [
        (
            "retryable_error",
            "elsevier_rate_limited",
            429,
            "retryable_error",
            "retryable_error",
        ),
        (
            "not_entitled",
            "elsevier_not_entitled",
            403,
            "not_entitled",
            "not_entitled",
        ),
        (
            "not_found",
            "elsevier_article_not_found",
            404,
            "not_found",
            "not_found",
        ),
    ],
)
def test_elsevier_failures_propagate_to_terminal_manifest_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolution_status: str,
    reason: str,
    http_status: int,
    error_code: str,
    expected_manifest_status: str,
) -> None:
    paper = _paper(source_id=f"ART00300{http_status}")
    fetch = _fetch(status=http_status)
    _patch_elsevier_resolution(
        monkeypatch,
        lambda candidate: ElsevierResolution(
            source_id=candidate.source_id,
            doi=candidate.doi,
            status=resolution_status,
            reason=reason,
            fetch=fetch,
            http_status=http_status,
            error_code=error_code,
        ),
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(elsevier_api_key="configured-key"),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == expected_manifest_status
    assert manifest["last_error"]["code"] == expected_manifest_status
    assert not paths.pdf.exists()
    attempt = _attempt(manifest)
    assert attempt["resolution_status"] == resolution_status
    assert attempt["reason"] == reason
    assert attempt["http_status"] == http_status
    assert attempt["error_code"] == error_code


def test_process_cli_maps_trimmed_elsevier_key_into_download_runtime() -> None:
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--elsevier-api-key",
            "  process-integration-key  ",
            "--max-pdf-mb",
            "17",
        ]
    )

    process_papers._validate_args(args)
    runtime = process_papers._download_runtime(args)

    assert runtime.config.elsevier_api_key == "process-integration-key"
    assert runtime.config.max_pdf_bytes == 17 * 1024 * 1024
    assert runtime.config.connect_timeout == args.connect_timeout
    assert runtime.config.read_timeout == args.read_timeout
    assert runtime.config.request_gate is not None
    assert runtime.config.request_gate.__self__ is runtime.rate_limiter


def test_sciencedirect_scope_is_api_only_and_passes_institution_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = _paper(source_id="ART003000999")
    content = _pdf_with_metadata_title(paper.title or "")
    fetch = _fetch(body=content)
    configs = _patch_elsevier_resolution(
        monkeypatch,
        lambda candidate: ElsevierResolution(
            source_id=candidate.source_id,
            doi=candidate.doi,
            status="resolved",
            reason="official_elsevier_pdf_retrieved",
            fetch=fetch,
            http_status=200,
        ),
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            provider_scope="sciencedirect",
            elsevier_api_key="api-key",
            elsevier_institution_token="institution-token",
        ),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["provider_scope"] == "sciencedirect"
    assert set(manifest["provider_attempts"]) == {"sciencedirect"}
    assert manifest["resolver"]["name"] == RESOLVER_NAME
    assert configs[0].api_key == "api-key"
    assert configs[0].institution_token == "institution-token"
    assert "institution-token" not in paths.download_manifest.read_text(
        encoding="utf-8"
    )
