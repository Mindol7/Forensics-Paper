from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import Any

from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module
from PaperPipeline.downloader import (
    DownloadConfig,
    PdfDownloader,
    download_artifact_is_current,
)
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.jiis_resolver import (
    JIIS_RESOLVER_NAME,
    JiisResolution,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import ArtifactPaths


TITLE = "Vision Transformer-based Deepfake Detection Using Multiscale Features"
DOI = "10.13088/jiis.2024.30.2.275"
SOURCE_ID = "ART003095258"
DETAIL_URL = "https://jiisonline.org/index.php?mnu=archive&archiveId=1035"
PDF_URL = "https://jiisonline.org/files/DLA/deepfake.pdf"


class OfflineHttp:
    """Serve one optional PDF and turn every fallback lookup into a local 404."""

    def __init__(self, pdf: bytes | None = None) -> None:
        self.pdf = pdf
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del max_bytes, max_html_bytes, headers
        self.get_calls.append(url)
        if url == PDF_URL and self.pdf is not None:
            return _fetch(
                url,
                self.pdf,
                content_type="application/pdf",
            )
        return _fetch(
            url,
            b"not found",
            status=404,
            content_type="text/plain",
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
        self.post_calls.append(url)
        raise AssertionError(f"unexpected HTTP POST in offline test: {url}")


def _fetch(
    url: str,
    body: bytes,
    *,
    status: int = 200,
    content_type: str,
) -> HttpFetch:
    return HttpFetch(
        requested_url=url,
        final_url=url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body,
        redirects=(),
    )


def _pdf_with_title(title: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _paper(*, doi: str = DOI) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=SOURCE_ID,
        title=TITLE,
        doi_original=f"https://doi.org/{doi}",
        doi=doi,
        landing_url=f"https://www.kci.go.kr/article/{SOURCE_ID}",
        abstract=None,
        publication_year=2024,
        first_page="275",
        final_page="285",
        alternate_titles=(
            "멀티스케일 특성을 활용한 비전 트랜스포머 기반 딥페이크 탐지에 관한 연구",
        ),
    )


def _install_fake_resolver(
    monkeypatch: Any,
    factory: Callable[[PaperCandidate], JiisResolution],
) -> list[PaperCandidate]:
    resolved_papers: list[PaperCandidate] = []

    class FakeJiisResolver:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def resolve(self, paper: PaperCandidate) -> JiisResolution:
            resolved_papers.append(paper)
            return factory(paper)

    monkeypatch.setattr(
        downloader_module,
        "JiisPublicSearchResolver",
        FakeJiisResolver,
    )
    return resolved_papers


def _jiis_resolution_attempt(manifest: dict[str, Any]) -> dict[str, Any]:
    matches = [
        attempt
        for attempt in manifest["attempts"]
        if str(attempt.get("resolver", "")).startswith(JIIS_RESOLVER_NAME)
        and "resolution_status" in attempt
    ]
    assert len(matches) == 1
    return matches[0]


def test_jiis_candidate_passes_project_pdf_identity_validation_and_is_saved(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    paper = _paper()
    candidate = ResolverCandidate(
        url=PDF_URL,
        resolver=JIIS_RESOLVER_NAME,
        landing_url=DETAIL_URL,
        is_open_access=True,
        # The JIIS detail page has no DOI; the PDF must prove its own identity.
        verified_doi=None,
    )
    resolver_calls = _install_fake_resolver(
        monkeypatch,
        lambda received: JiisResolution(
            status="resolved",
            reason="exact_official_title_bibliography_and_pdf_link",
            paper=received,
            candidate=candidate,
            detail_url=DETAIL_URL,
            matched_title=TITLE,
            queried_titles=(TITLE,),
            query_count=1,
            exact_title_matches=1,
            bibliographic_matches=1,
            http_status=200,
        ),
    )
    http = OfflineHttp(_pdf_with_title(TITLE))
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
    ).download(paper, paths)

    assert resolver_calls == [paper]
    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == JIIS_RESOLVER_NAME
    assert manifest["resolver"]["landing_url"] == DETAIL_URL
    assert manifest["resolver"]["download_url"] == PDF_URL
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert manifest["pdf"]["matched_doi"] is None
    assert paths.pdf.read_bytes() == http.pdf
    assert download_artifact_is_current(paper, paths, manifest)
    assert http.get_calls == [PDF_URL]
    assert not http.post_calls

    audit = _jiis_resolution_attempt(manifest)
    assert audit["resolution_status"] == "resolved"
    assert audit["reason"] == "exact_official_title_bibliography_and_pdf_link"
    assert audit["status"] in {"ok", "downloaded"}


def test_jiis_no_match_is_audited_and_falls_through_to_not_found(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    paper = _paper()
    resolver_calls = _install_fake_resolver(
        monkeypatch,
        lambda received: JiisResolution(
            status="no_match",
            reason="search_bibliography_mismatch",
            paper=received,
            queried_titles=(TITLE,),
            query_count=1,
            exact_title_matches=1,
            bibliographic_matches=0,
            http_status=200,
        ),
    )
    http = OfflineHttp()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
    ).download(paper, paths)

    assert resolver_calls == [paper]
    assert manifest["status"] == "not_found"
    assert not paths.pdf.exists()
    assert not http.post_calls
    audit = _jiis_resolution_attempt(manifest)
    assert audit["status"] == "skipped"
    assert audit["resolution_status"] == "no_match"
    assert audit["reason"] == "search_bibliography_mismatch"
    assert audit.get("error_code") not in {
        "request_error",
        "retryable_error",
    }


def test_jiis_transient_failure_is_audited_and_controls_retryable_status(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    paper = _paper()
    resolver_calls = _install_fake_resolver(
        monkeypatch,
        lambda received: JiisResolution(
            status="request_error",
            reason="jiis_search_request_failed",
            paper=received,
            queried_titles=(TITLE,),
            query_count=1,
            error_code="retryable_error",
        ),
    )
    http = OfflineHttp()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
    ).download(paper, paths)

    assert resolver_calls == [paper]
    assert manifest["status"] == "retryable_error"
    assert manifest["last_error"]["code"] == "retryable_error"
    assert not paths.pdf.exists()
    assert not http.post_calls
    audit = _jiis_resolution_attempt(manifest)
    assert audit["status"] == "failed"
    assert audit["resolution_status"] == "request_error"
    assert audit["reason"] == "jiis_search_request_failed"
    assert audit["error_code"] == "retryable_error"


def test_jiis_is_prefix_gated_without_a_download_config_switch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    resolver_calls = _install_fake_resolver(
        monkeypatch,
        lambda received: JiisResolution(
            status="no_match",
            reason="should_not_be_called",
            paper=received,
        ),
    )
    paper = _paper(doi="10.3745/JIPS.04.0245")
    http = OfflineHttp()

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
    ).download(
        paper,
        ArtifactPaths.for_source(tmp_path, paper.source_id),
    )

    assert manifest["status"] == "not_found"
    assert not resolver_calls
    assert not any(
        field.name.startswith("jiis")
        for field in fields(DownloadConfig)
    )
    assert not any(
        str(attempt.get("resolver", "")).startswith(JIIS_RESOLVER_NAME)
        for attempt in manifest["attempts"]
    )
    assert not http.post_calls
