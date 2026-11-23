from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module
from PaperPipeline.dcs_resolver import (
    DcsResolution,
    DcsResolverAttempt,
)
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import ArtifactPaths, load_json_object


DOI = "10.9728/dcs.2025.26.5.1363"
TITLE = "Decryption and Artifact Analysis of KakaoTalk Data"
SOURCE_ID = "ART003000001"
LANDING_URL = (
    "http://journal.dcs.or.kr/_common/do.php?"
    "a=full&b=12&bidx=321&aidx=654"
)
PDF_URL = "http://journal.dcs.or.kr/xml/654/654.pdf"


class PdfOnlyHttp:
    """A network-free HTTP seam that permits only the expected PDF fetch."""

    def __init__(self, body: bytes | None = None) -> None:
        self.body = body
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
        if url != PDF_URL or self.body is None:
            raise AssertionError(f"unexpected network request: {url}")
        return HttpFetch(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            body=self.body,
            redirects=(),
        )


def _paper() -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=SOURCE_ID,
        title=TITLE,
        doi_original=f"http://dx.doi.org/{DOI}",
        doi=DOI,
        landing_url=None,
        abstract=None,
        publication_year=2025,
        first_page=None,
        final_page=None,
    )


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _candidate() -> ResolverCandidate:
    return ResolverCandidate(
        url=PDF_URL,
        resolver="dcs-public-reader",
        landing_url=LANDING_URL,
        is_open_access=True,
        verified_doi=DOI,
    )


def _resolved() -> DcsResolution:
    return DcsResolution(
        status="resolved",
        reason="official_reader_exact_identity",
        candidate=_candidate(),
        attempts=(
            DcsResolverAttempt(
                stage="doi-landing",
                url=f"https://doi.org/{DOI}",
                status="fetched",
                http_status=200,
                final_url=LANDING_URL,
            ),
        ),
    )


def _patch_dcs_resolution(
    monkeypatch: Any,
    resolution: DcsResolution,
) -> tuple[list[dict[str, Any]], list[PaperCandidate]]:
    initializations: list[dict[str, Any]] = []
    resolved_papers: list[PaperCandidate] = []

    class StubDcsResolver:
        def __init__(
            self,
            http: object,
            *,
            allow_http: bool = False,
            **kwargs: Any,
        ) -> None:
            initializations.append(
                {
                    "http": http,
                    "allow_http": allow_http,
                    **kwargs,
                }
            )

        def resolve(self, paper: PaperCandidate) -> DcsResolution:
            resolved_papers.append(paper)
            return resolution

    monkeypatch.setattr(
        downloader_module,
        "DcsPublicPdfResolver",
        StubDcsResolver,
    )
    return initializations, resolved_papers


def _disable_unrelated_doi_network(monkeypatch: Any) -> None:
    """Keep the integration boundary deterministic after a DCS miss."""

    monkeypatch.setattr(
        PdfDownloader,
        "_crossref_candidates",
        lambda *_args, **_kwargs: ([], False),
    )
    monkeypatch.setattr(
        PdfDownloader,
        "_doi_landing_candidates",
        lambda *_args, **_kwargs: [],
    )


def _dcs_attempts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == "dcs-public-reader"
    ]


def test_dcs_plain_http_path_requires_explicit_downloader_opt_in(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class MustNotBeConstructed:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("DCS resolver ran without HTTP opt-in")

    monkeypatch.setattr(
        downloader_module,
        "DcsPublicPdfResolver",
        MustNotBeConstructed,
    )
    _disable_unrelated_doi_network(monkeypatch)
    http = PdfOnlyHttp()
    paths = ArtifactPaths.for_source(tmp_path, SOURCE_ID)

    manifest = PdfDownloader(
        DownloadConfig(allow_http=False),
        http=http,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_found"
    assert not http.calls
    assert not paths.pdf.exists()
    attempts = _dcs_attempts(manifest)
    assert any(
        attempt.get("status") == "skipped"
        and attempt.get("error_code") == "http_not_explicitly_allowed"
        for attempt in attempts
    )


def test_verified_dcs_candidate_is_validated_and_atomically_saved(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    initializations, resolved_papers = _patch_dcs_resolution(
        monkeypatch,
        _resolved(),
    )
    _disable_unrelated_doi_network(monkeypatch)
    pdf = _blank_pdf()
    http = PdfOnlyHttp(pdf)
    paths = ArtifactPaths.for_source(tmp_path, SOURCE_ID)

    manifest = PdfDownloader(
        DownloadConfig(allow_http=True),
        http=http,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert initializations
    assert initializations[0]["http"] is http
    assert initializations[0]["allow_http"] is True
    assert resolved_papers == [_paper()]
    assert http.calls == [PDF_URL]
    assert paths.pdf.read_bytes() == pdf
    assert load_json_object(paths.download_manifest)["status"] == "downloaded"
    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "dcs-public-reader"
    assert manifest["resolver"]["landing_url"] == LANDING_URL
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert manifest["pdf"]["matched_doi"] == DOI
    assert any(
        attempt.get("status") == "downloaded"
        for attempt in _dcs_attempts(manifest)
    )


def test_invalid_dcs_candidate_bytes_are_not_persisted(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _patch_dcs_resolution(monkeypatch, _resolved())
    _disable_unrelated_doi_network(monkeypatch)
    http = PdfOnlyHttp(b"<html>not a PDF</html>")
    paths = ArtifactPaths.for_source(tmp_path, SOURCE_ID)

    manifest = PdfDownloader(
        DownloadConfig(allow_http=True),
        http=http,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "invalid_pdf"
    assert not paths.pdf.exists()
    assert any(
        attempt.get("status") == "failed"
        and attempt.get("error_code") == "invalid_pdf"
        for attempt in _dcs_attempts(manifest)
    )


def test_dcs_resolution_failure_is_retained_in_manifest_audit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    failure = DcsResolution(
        status="identity_mismatch",
        reason="reader_title_mismatch",
        attempts=(
            DcsResolverAttempt(
                stage="dcs-reader",
                url=(
                    "http://journal.dcs.or.kr/_PR/view/index.php?"
                    "bidx=321&aidx=654"
                ),
                status="fetched",
                http_status=200,
                final_url=(
                    "http://journal.dcs.or.kr/_PR/view/index.php?"
                    "bidx=321&aidx=654"
                ),
            ),
        ),
    )
    _patch_dcs_resolution(monkeypatch, failure)
    _disable_unrelated_doi_network(monkeypatch)
    http = PdfOnlyHttp()
    paths = ArtifactPaths.for_source(tmp_path, SOURCE_ID)

    manifest = PdfDownloader(
        DownloadConfig(allow_http=True),
        http=http,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_found"
    assert not http.calls
    assert not paths.pdf.exists()
    attempts = _dcs_attempts(manifest)
    assert any(
        attempt.get("status") == "failed"
        and attempt.get("resolution_status") == "identity_mismatch"
        and attempt.get("reason") == "reader_title_mismatch"
        for attempt in attempts
    )
