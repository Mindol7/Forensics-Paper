from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from PaperPipeline.downloader import (
    DownloadConfig,
    KCI_DOI_CONFLICT_IDENTITY_STATUS,
    PdfDownloader,
    PdfValidationError,
    _attempt_error,
    _failure_status,
    _looks_like_html,
    download_artifact_is_current,
    validate_pdf,
)
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import ArtifactPaths, load_json_object, write_json_atomic


def _blank_pdf(title: str = "Timestamp study") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_pdf(page_texts: list[str]) -> bytes:
    """Return a small standards-compliant PDF with extractable Latin-1 text."""

    def escaped(value: str) -> bytes:
        return (
            value.encode("latin-1")
            .replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(")
            .replace(b")", b"\\)")
        )

    font_id = 3 + 2 * len(page_texts)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids: list[int] = []
    for index, text in enumerate(page_texts):
        page_id = 3 + 2 * index
        content_id = page_id + 1
        page_ids.append(page_id)
        stream = (
            b"BT\n/F1 12 Tf\n72 720 Td\n("
            + escaped(text)
            + b") Tj\nET\n"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream"
        )
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode()
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (font_id + 1)
    for object_id in range(1, font_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode())
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {font_id + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, font_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


class FakeHttp:
    def __init__(self, responses: dict[str, list[HttpFetch]]) -> None:
        self.responses = {key: list(values) for key, values in responses.items()}
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
        return self.responses[url].pop(0)

    def post_form_bytes(
        self,
        url: str,
        *,
        data: dict[str, str],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        del data, max_bytes, headers
        self.calls.append(url)
        return self.responses[url].pop(0)


def _fetch(
    url: str,
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/html",
    final_url: str | None = None,
) -> HttpFetch:
    return HttpFetch(
        requested_url=url,
        final_url=final_url or url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body,
        redirects=(),
    )


def _paper(doi: str | None = "10.3745/JIPS.04.0245") -> PaperCandidate:
    return PaperCandidate(
        database_id=7,
        source="kci",
        source_id="ART002856685",
        title="Timestamp study",
        doi_original=f"http://dx.doi.org/{doi}" if doi else None,
        doi=doi,
        landing_url="https://www.kci.go.kr/example",
        abstract=None,
        publication_year=2022,
        first_page="402",
        final_page="410",
    )


def test_html_detection_handles_mislabelled_uppercase_bytes() -> None:
    fetch = _fetch(
        "https://publisher.example/article",
        b"  <HTML><head></head></HTML>",
        content_type="application/octet-stream",
    )

    assert _looks_like_html(fetch)


def test_failure_status_prefers_retryable_429_over_coexisting_404() -> None:
    attempts = [{"http_status": 404}, {"http_status": 429}]

    assert _failure_status(attempts) == "retryable_error"


def test_failure_status_keeps_404_only_as_not_found() -> None:
    assert _failure_status([{"http_status": 404}]) == "not_found"


@pytest.mark.parametrize(
    ("terminal_attempt", "expected"),
    [
        ({"error_code": "too_large"}, "too_large"),
        ({"error_code": "invalid_pdf"}, "invalid_pdf"),
        ({"http_status": 403}, "access_denied"),
    ],
)
def test_failure_status_keeps_terminal_outcomes_ahead_of_retryable(
    terminal_attempt: dict[str, Any],
    expected: str,
) -> None:
    assert _failure_status([terminal_attempt, {"http_status": 429}]) == expected


@pytest.mark.parametrize(
    "transient_attempt",
    [
        {"error_code": "request_error"},
        {"error_code": "retryable_error"},
        {"error_code": "browser_attempt_limit_reached"},
        {"error_code": "consecutive_retryable_failures"},
        {"http_status": 408},
        {"http_status": 425},
        {"http_status": 599},
    ],
)
def test_failure_status_recognizes_retryable_codes_and_http_statuses(
    transient_attempt: dict[str, Any],
) -> None:
    assert (
        _failure_status([{"http_status": 410}, transient_attempt])
        == "retryable_error"
    )


def test_downloads_citation_pdf_url_and_writes_audited_manifest(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    landing_url = "https://jips-k.org/article"
    pdf_url = "https://jips-k.org/papers/full.pdf"
    html = f'<html><head><meta name="citation_pdf_url" content="{pdf_url}"></head></html>'
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"not found", status=404, content_type="text/plain")],
            doi_url: [_fetch(doi_url, html.encode(), final_url=landing_url)],
            pdf_url: [_fetch(pdf_url, _blank_pdf(), content_type="application/pdf")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]

    manifest = downloader.download(_paper(), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "publisher-metadata"
    assert manifest["pdf"]["page_count"] == 1
    assert len(manifest["pdf"]["sha256"]) == 64
    assert paths.pdf.read_bytes().startswith(b"%PDF-")
    assert paths.download_manifest.is_file()
    assert doi_url in fake.calls
    assert pdf_url in fake.calls


def test_accepts_scanned_pdf_bound_to_exact_landing_doi(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    landing_url = "https://publisher.example/article"
    pdf_url = "https://publisher.example/scanned.pdf"
    html = (
        '<meta name="citation_doi" content="10.3745/JIPS.04.0245">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
    )
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, html.encode(), final_url=landing_url)],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Unextractable scanned document"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]

    manifest = downloader.download(_paper(), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert manifest["pdf"]["matched_doi"] == "10.3745/JIPS.04.0245"
    assert download_artifact_is_current(_paper(), paths, manifest)
    assert downloader.download(_paper(), paths)["_runtime"]["action"] == "reused"


def test_downloads_allowlisted_publisher_pdf_anchor(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    landing_url = "https://ksbe-jbe.org/article"
    pdf_url = "https://ksbe-jbe.org/xml/38975/38975.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    b'<a href="/xml/38975/38975.pdf">PDF</a>',
                    final_url=landing_url,
                )
            ],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Image-only publisher PDF"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "publisher-explicit-pdf-link"
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert pdf_url in fake.calls


def test_downloads_ksbe_inline_pdf_action_bound_to_article(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    landing_url = "https://ksbe-jbe.org/_common/do.php?a=full&b=13&aidx=38975"
    pdf_url = "https://ksbe-jbe.org/xml/38975/38975.pdf"
    document = """
    <script>
      var use_yn = "Y";
      window.open('/xml/38975/38975.pdf', 'pdfwin');
    </script>
    """
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, document.encode(), final_url=landing_url)],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Image-only KSBE article"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "publisher-explicit-pdf-link"
    assert pdf_url in fake.calls


def test_rejects_unavailable_or_mismatched_ksbe_inline_action(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    landing_url = "https://ksbe-jbe.org/_common/do.php?a=full&aidx=38975"
    document = """
    <script>
      var use_yn = "N";
      window.open('/xml/38976/38976.pdf', 'pdfwin');
    </script>
    """
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, document.encode(), final_url=landing_url)],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert manifest["status"] == "not_found"
    assert "https://ksbe-jbe.org/xml/38976/38976.pdf" not in fake.calls


def test_does_not_follow_generic_pdf_anchor_on_untrusted_host(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    supplement_url = "https://publisher.example/supplement.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<a href="{supplement_url}">Download PDF</a>'.encode(),
                    final_url="https://publisher.example/article",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert manifest["status"] == "not_found"
    assert supplement_url not in fake.calls


def test_downloads_google_drive_explicit_viewer_action(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    file_id = "1re9-BkAZz8FAy600N7VRNx90hzixbO6K"
    landing_url = f"https://drive.google.com/file/d/{file_id}/view"
    pdf_url = (
        "https://drive.usercontent.google.com/uc?"
        f"id={file_id}&export=download"
    )
    document = (
        "https://drive.usercontent.google.com/uc?"
        f"id\\u003d{file_id}\\u0026export\\u003ddownload"
    )
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, document.encode(), final_url=landing_url)],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Shared Drive PDF"),
                    content_type="application/octet-stream",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "google-drive-explicit-download"
    assert pdf_url in fake.calls


def test_source_id_sitemap_download_supports_missing_doi(tmp_path: Path) -> None:
    pdf_url = (
        "https://journal.kci.go.kr/example/archive/"
        "articlePdf?artiId=ART002856685"
    )
    fake = FakeHttp(
        {
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Image-only KCI Journal PDF"),
                    content_type="application/pdf",
                )
            ]
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(
        DownloadConfig(kci_journal_pdf_urls={"ART002856685": pdf_url}),
        http=fake,  # type: ignore[arg-type]
    )

    manifest = downloader.download(_paper(None), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "kci-journal-sitemap"
    assert manifest["pdf"]["identity_status"] == "resolver_source_id_match"
    assert manifest["pdf"]["matched_source_id"] == "ART002856685"
    assert download_artifact_is_current(_paper(None), paths, manifest)
    assert downloader.download(_paper(None), paths)["_runtime"]["action"] == "reused"
    recovered_doi = "10.3745/JIPS.04.0245"
    resolved = replace(
        _paper(None),
        doi_original=recovered_doi,
        doi=recovered_doi,
        doi_source="openalex-title",
        identifier_resolution={
            "resolver": "openalex-title",
            "status": "resolved",
            "doi": recovered_doi,
            "matched_title": _paper(None).title,
            "matched_year": _paper(None).publication_year,
        },
    )

    reused = downloader.download(resolved, paths)

    assert reused["_runtime"]["action"] == "reused"
    assert reused["paper"]["doi"] == recovered_doi
    assert reused["paper"]["doi_source"] == "openalex-title"
    assert fake.calls == [pdf_url]


def test_downloads_semantic_scholar_verified_oa_candidate(tmp_path: Path) -> None:
    paper = _paper()
    pdf_url = "https://pdfs.semanticscholar.org/example/paper.pdf"
    candidate = ResolverCandidate(
        url=pdf_url,
        resolver="semantic-scholar",
        license="CCBY",
        version="GREEN",
        is_open_access=True,
        verified_doi=paper.doi,
    )
    resolution = {
        "resolver": "semantic-scholar",
        "status": "resolved",
        "reason": "exact_doi_title_year_open_access_match",
        "doi": paper.doi,
        "cache_hit": False,
        "http_status": 200,
        "error_code": None,
        "open_access_pdf": {
            "license": "CCBY",
            "status": "GREEN",
            "disclaimer": "Verify the source license.",
        },
    }
    fake = FakeHttp(
        {
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Semantic Scholar indexed paper"),
                    content_type="application/pdf",
                )
            ]
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            semantic_scholar_candidates={paper.source_id: candidate},
            semantic_scholar_resolutions={paper.source_id: resolution},
        ),
        http=fake,  # type: ignore[arg-type]
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "semantic-scholar"
    assert manifest["resolver"]["is_open_access"] is True
    assert manifest["attempts"][2]["resolver"] == "semantic-scholar-metadata"
    assert manifest["attempts"][2]["resolution_status"] == "resolved"
    assert manifest["attempts"][2]["disclaimer"] == "Verify the source license."
    assert fake.calls == [pdf_url]


def test_semantic_scholar_rate_limit_is_recorded_as_retryable() -> None:
    paper = _paper()
    attempts: list[dict[str, object]] = []
    downloader = PdfDownloader(
        DownloadConfig(
            semantic_scholar_resolutions={
                paper.source_id: {
                    "resolver": "semantic-scholar",
                    "status": "http_error",
                    "reason": "semantic_scholar_http_error",
                    "http_status": 429,
                }
            }
        )
    )

    downloader._record_semantic_scholar_resolution(paper, attempts)

    assert attempts == [
        {
            "resolver": "semantic-scholar-metadata",
            "status": "failed",
            "resolution_status": "http_error",
            "reason": "semantic_scholar_http_error",
            "cache_hit": False,
            "error_code": "retryable_error",
            "metadata_http_status": 429,
        }
    ]


def test_unpaywall_oa_landing_uses_only_citation_pdf_metadata(
    tmp_path: Path,
) -> None:
    doi = "10.3745/JIPS.04.0245"
    api_url = (
        f"https://api.unpaywall.org/v2/{doi}?"
        "email=researcher%40example.org"
    )
    landing_url = "https://repository.example/item/24"
    pdf_url = "https://repository.example/item/24.pdf"
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": None,
            "url_for_landing_page": landing_url,
            "license": "cc-by",
            "version": "acceptedVersion",
        },
        "oa_locations": [],
    }
    document = (
        f'<meta name="citation_doi" content="https://doi.org/{doi}">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
    )
    fake = FakeHttp(
        {
            api_url: [
                _fetch(
                    api_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            landing_url: [_fetch(landing_url, document.encode())],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Scanned repository document"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(unpaywall_email="researcher@example.org"),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"] == {
        "name": "unpaywall-landing",
        "is_open_access": True,
        "license": "cc-by",
        "version": "acceptedVersion",
        "landing_url": landing_url,
        "download_url": pdf_url,
    }
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert download_artifact_is_current(_paper(), paths, manifest)
    assert fake.calls == [api_url, landing_url, pdf_url]


def test_openalex_oa_landing_uses_typed_pdf_link(tmp_path: Path) -> None:
    doi = "10.3745/JIPS.04.0245"
    api_url = (
        "https://api.openalex.org/works/"
        f"https://doi.org/{doi}?api_key=openalex-test-key"
    )
    landing_url = "https://archive.example/records/24"
    pdf_url = "https://archive.example/files/24"
    payload = {
        "open_access": {"is_oa": True},
        "best_oa_location": {
            "is_oa": True,
            "pdf_url": None,
            "landing_page_url": landing_url,
            "license": "cc-by-nc",
            "version": "publishedVersion",
        },
        "locations": [],
    }
    document = (
        f'<meta name="citation_doi" content="{doi}">'
        f'<link rel="alternate" type="application/pdf" href="{pdf_url}">'
    )
    fake = FakeHttp(
        {
            api_url: [
                _fetch(
                    api_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            landing_url: [_fetch(landing_url, document.encode())],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Image-only archive document"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(openalex_api_key="openalex-test-key"),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "openalex-landing"
    assert manifest["resolver"]["license"] == "cc-by-nc"
    assert manifest["resolver"]["version"] == "publishedVersion"
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert fake.calls == [api_url, landing_url, pdf_url]


def test_oa_landing_direct_pdf_is_promoted_as_candidate(tmp_path: Path) -> None:
    doi = "10.3745/JIPS.04.0245"
    api_url = (
        f"https://api.unpaywall.org/v2/{doi}?"
        "email=researcher%40example.org"
    )
    landing_url = "https://repository.example/direct/24"
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": None,
            "url_for_landing_page": landing_url,
            "license": "cc0",
            "version": "submittedVersion",
        },
    }
    pdf = _blank_pdf(_paper().title or "")
    fake = FakeHttp(
        {
            api_url: [
                _fetch(
                    api_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            # The first response discovers that the landing URL is itself a
            # PDF; the normal candidate fetch then validates and persists it.
            landing_url: [
                _fetch(landing_url, pdf, content_type="application/pdf"),
                _fetch(landing_url, pdf, content_type="application/pdf"),
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(unpaywall_email="researcher@example.org"),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == "unpaywall-landing"
    assert manifest["resolver"]["license"] == "cc0"
    assert fake.calls == [api_url, landing_url, landing_url]


def test_oa_landing_rejects_mismatched_citation_doi(tmp_path: Path) -> None:
    doi = "10.3745/JIPS.04.0245"
    api_url = (
        f"https://api.unpaywall.org/v2/{doi}?"
        "email=researcher%40example.org"
    )
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    doi_url = f"https://doi.org/{doi}"
    landing_url = "https://repository.example/wrong-record"
    wrong_pdf_url = "https://repository.example/wrong-record.pdf"
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_landing_page": landing_url,
            "license": "cc-by",
        },
    }
    document = (
        '<meta name="citation_doi" content="10.9999/not-the-paper">'
        f'<meta name="citation_pdf_url" content="{wrong_pdf_url}">'
    )
    fake = FakeHttp(
        {
            api_url: [
                _fetch(
                    api_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            landing_url: [_fetch(landing_url, document.encode())],
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, b"<html></html>")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(unpaywall_email="researcher@example.org"),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_found"
    assert wrong_pdf_url not in fake.calls
    assert any(
        attempt.get("resolver") == "unpaywall-landing"
        and attempt.get("error_code") == "identity_mismatch"
        for attempt in manifest["attempts"]
    )


def test_oa_evidence_survives_landing_without_pdf_candidate(
    tmp_path: Path,
) -> None:
    doi = "10.3745/JIPS.04.0245"
    api_url = (
        f"https://api.unpaywall.org/v2/{doi}?"
        "email=researcher%40example.org"
    )
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    doi_url = f"https://doi.org/{doi}"
    landing_url = "https://repository.example/item/without-metadata"
    generic_download = "https://repository.example/files/supplement.pdf"
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_landing_page": landing_url,
            "license": "cc-by",
        },
    }
    fake = FakeHttp(
        {
            api_url: [
                _fetch(
                    api_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            landing_url: [
                _fetch(
                    landing_url,
                    (
                        f'<a href="{generic_download}">'
                        "Download article PDF</a>"
                    ).encode(),
                )
            ],
            crossref_url: [
                _fetch(
                    crossref_url,
                    b'{"message":{"link":[],"license":[]}}',
                    content_type="application/json",
                )
            ],
            doi_url: [_fetch(doi_url, b"<html></html>")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            unpaywall_email="researcher@example.org",
            require_open_access_evidence=True,
        ),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_found"
    assert generic_download not in fake.calls
    assert doi_url in fake.calls


@pytest.mark.parametrize("resolver_name", ["unpaywall-title", "openalex-title"])
def test_preverified_title_candidate_is_first_and_current_for_original_row(
    tmp_path: Path,
    resolver_name: str,
) -> None:
    original = _paper(None)
    doi = "10.3745/JIPS.04.0245"
    resolved = replace(
        original,
        doi_original=doi,
        doi=doi,
        doi_source=resolver_name,
        identifier_resolution={
            "resolver": resolver_name,
            "status": "resolved",
            "reason": "exact_title_and_year_match",
            "doi": doi,
            "matched_title": original.title,
            "matched_year": original.publication_year,
        },
    )
    pdf_url = f"https://repository.example/{resolver_name}.pdf"
    candidate = ResolverCandidate(
        url=pdf_url,
        resolver=resolver_name,
        landing_url="https://repository.example/article",
        license="cc-by",
        version="acceptedVersion",
        is_open_access=True,
        verified_doi=doi,
    )
    fake = FakeHttp(
        {
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Scanned title-resolved article"),
                    content_type="application/pdf",
                )
            ]
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, original.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            preverified_candidates={original.source_id: (candidate,)},
        ),
        http=fake,  # type: ignore[arg-type]
    ).download(resolved, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == resolver_name
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert fake.calls == [pdf_url]
    assert download_artifact_is_current(original, paths, manifest)
    mismatched_resolution = dict(manifest)
    mismatched_resolution["paper"] = dict(manifest["paper"])
    mismatched_resolution["paper"]["identifier_resolution"] = dict(
        manifest["paper"]["identifier_resolution"]
    )
    mismatched_resolution["paper"]["identifier_resolution"]["matched_year"] = 2019
    assert not download_artifact_is_current(
        original,
        paths,
        mismatched_resolution,
    )


def test_preverified_candidate_with_wrong_doi_is_not_requested() -> None:
    candidate = ResolverCandidate(
        url="https://repository.example/wrong.pdf",
        resolver="unpaywall-title",
        is_open_access=True,
        verified_doi="10.9999/wrong",
    )
    fake = FakeHttp({})
    attempts: list[dict[str, Any]] = []
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]

    result = downloader._fetch_pdf_candidate(candidate, _paper(), attempts)

    assert result is None
    assert not fake.calls
    assert attempts[0]["error_code"] == "identity_mismatch"


def test_strict_oa_mode_skips_preverified_candidate_without_oa_evidence(
    tmp_path: Path,
) -> None:
    candidate_url = "https://repository.example/unknown-license.pdf"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    candidate = ResolverCandidate(
        url=candidate_url,
        resolver="openalex-title",
        is_open_access=None,
        verified_doi=_paper().doi,
    )
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            require_open_access_evidence=True,
            preverified_candidates={_paper().source_id: (candidate,)},
        ),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_open_access"
    assert candidate_url not in fake.calls
    assert any(
        attempt.get("resolver") == "openalex-title"
        and attempt.get("error_code") == "oa_evidence_required"
        for attempt in manifest["attempts"]
    )


def test_candidate_urls_are_deduplicated_across_resolver_batches(
    tmp_path: Path,
) -> None:
    doi = _paper().doi
    assert doi is not None
    duplicate_url = "https://repository.example/shared.pdf"
    unpaywall_url = (
        f"https://api.unpaywall.org/v2/{doi}?"
        "email=researcher%40example.org"
    )
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    doi_url = f"https://doi.org/{doi}"
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": duplicate_url,
            "url_for_landing_page": "https://repository.example/item",
            "license": "cc-by",
        },
    }
    prepared = ResolverCandidate(
        url=duplicate_url,
        resolver="unpaywall-title",
        is_open_access=True,
        verified_doi=doi,
    )
    fake = FakeHttp(
        {
            duplicate_url: [
                _fetch(duplicate_url, b"", status=404, content_type="text/plain")
            ],
            unpaywall_url: [
                _fetch(
                    unpaywall_url,
                    json.dumps(payload).encode(),
                    content_type="application/json",
                )
            ],
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [_fetch(doi_url, b"<html></html>")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, _paper().source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            unpaywall_email="researcher@example.org",
            preverified_candidates={_paper().source_id: (prepared,)},
        ),
        http=fake,  # type: ignore[arg-type]
    ).download(_paper(), paths)

    assert manifest["status"] == "not_found"
    assert fake.calls.count(duplicate_url) == 1


def test_attempt_error_redacts_secrets_embedded_in_exception_message() -> None:
    secret_url = (
        "https://api.example/lookup?"
        "email=researcher%40example.org&api_key=super-secret"
    )
    error = RuntimeError(
        f"request failed for {secret_url}; "
        "email=second@example.org api_key=another-secret"
    )

    attempt = _attempt_error("metadata", secret_url, error)

    serialized = json.dumps(attempt)
    assert "researcher" not in serialized
    assert "second@example.org" not in serialized
    assert "super-secret" not in serialized
    assert "another-secret" not in serialized
    assert "REDACTED" in serialized


def test_strict_oa_mode_skips_public_sitemap_without_license(
    tmp_path: Path,
) -> None:
    pdf_url = (
        "https://journal.kci.go.kr/example/archive/"
        "articlePdf?artiId=ART002856685"
    )
    fake = FakeHttp({})
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(
        DownloadConfig(
            kci_journal_pdf_urls={"ART002856685": pdf_url},
            require_open_access_evidence=True,
        ),
        http=fake,  # type: ignore[arg-type]
    )

    manifest = downloader.download(_paper(None), paths)

    assert manifest["status"] == "identifier_unresolved"
    assert not fake.calls
    assert manifest["attempts"][0]["error_code"] == "oa_evidence_required"


def test_title_resolved_doi_artifact_is_current_for_original_db_row(
    tmp_path: Path,
) -> None:
    original = _paper(None)
    doi = "10.3745/JIPS.04.0245"
    resolved = replace(
        original,
        doi_original=doi,
        doi=doi,
        doi_source="crossref-title",
        identifier_resolution={
            "resolver": "crossref-title",
            "status": "resolved",
            "reason": "exact_title_and_year_match",
            "doi": doi,
            "matched_title": original.title,
            "matched_year": original.publication_year,
        },
    )
    doi_url = f"https://doi.org/{doi}"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    pdf_url = "https://publisher.example/paper.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode(),
                )
            ],
            pdf_url: [
                _fetch(pdf_url, _blank_pdf(original.title or ""), content_type="application/pdf")
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, original.source_id)

    manifest = PdfDownloader(DownloadConfig(), http=fake).download(resolved, paths)  # type: ignore[arg-type]

    assert manifest["status"] == "downloaded"
    assert download_artifact_is_current(original, paths, manifest)
    alternate_title_row = replace(
        original,
        title="한국어 데이터베이스 제목",
        alternate_titles=(original.title or "",),
    )
    assert download_artifact_is_current(
        alternate_title_row,
        paths,
        manifest,
    )
    changed_title = replace(original, title="A different database title")
    assert not download_artifact_is_current(changed_title, paths, manifest)
    ambiguous = replace(
        original,
        identifier_resolution={
            "resolver": "public-title-consensus",
            "status": "ambiguous",
            "reason": "multiple_distinct_dois",
        },
    )
    conflicting = replace(
        original,
        identifier_resolution={
            "resolver": "public-title-consensus",
            "status": "no_match",
            "reason": "cross_source_doi_conflict",
        },
    )
    assert not download_artifact_is_current(ambiguous, paths, manifest)
    assert not download_artifact_is_current(conflicting, paths, manifest)


def test_title_resolution_provenance_normalizes_html_entities(
    tmp_path: Path,
) -> None:
    original = replace(
        _paper(None),
        title="Detection &amp; Analysis of Timestamps",
    )
    doi = "10.3745/JIPS.04.0245"
    resolved = replace(
        original,
        doi_original=doi,
        doi=doi,
        doi_source="unpaywall-title",
        identifier_resolution={
            "resolver": "unpaywall-title",
            "status": "resolved",
            "doi": doi,
            "matched_title": "Detection & Analysis of Timestamps",
            "matched_year": original.publication_year,
        },
    )
    pdf_url = "https://repository.example/entity-title.pdf"
    candidate = ResolverCandidate(
        url=pdf_url,
        resolver="unpaywall-title",
        is_open_access=True,
        verified_doi=doi,
    )
    fake = FakeHttp(
        {
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Scanned article"),
                    content_type="application/pdf",
                )
            ]
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, original.source_id)

    manifest = PdfDownloader(
        DownloadConfig(
            preverified_candidates={original.source_id: (candidate,)},
        ),
        http=fake,  # type: ignore[arg-type]
    ).download(resolved, paths)

    assert manifest["status"] == "downloaded"
    assert download_artifact_is_current(original, paths, manifest)


def test_resume_reuses_hash_matching_pdf_without_network(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    pdf_url = "https://example.org/paper.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode(),
                )
            ],
            pdf_url: [_fetch(pdf_url, _blank_pdf(), content_type="application/pdf")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]
    downloader.download(_paper(), paths)
    first_call_count = len(fake.calls)
    legacy = load_json_object(paths.download_manifest)
    assert legacy is not None
    legacy["schema_version"] = "1.0"
    del legacy["paper"]["doi_source"]
    del legacy["paper"]["identifier_resolution"]
    del legacy["paper"]["alternate_titles"]
    del legacy["pdf"]["matched_source_id"]
    write_json_atomic(paths.download_manifest, legacy)

    reused = downloader.download(_paper(), paths)

    assert reused["_runtime"]["action"] == "reused"
    assert reused["schema_version"] == "1.1"
    assert "doi_source" in reused["paper"]
    assert "identifier_resolution" in reused["paper"]
    assert reused["paper"]["alternate_titles"] == []
    assert reused["pdf"]["matched_source_id"] is None
    assert len(fake.calls) == first_call_count


def test_recovers_pdf_committed_before_final_manifest_status(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    pdf_url = "https://example.org/paper.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode(),
                )
            ],
            pdf_url: [_fetch(pdf_url, _blank_pdf(), content_type="application/pdf")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )
    manifest = load_json_object(paths.download_manifest)
    assert manifest is not None
    manifest["status"] = "committing"
    write_json_atomic(paths.download_manifest, manifest)

    recovered = PdfDownloader(DownloadConfig(), http=FakeHttp({})).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert recovered["status"] == "downloaded"
    assert recovered["_runtime"]["action"] == "recovered"


def test_missing_and_invalid_doi_are_persisted_without_http(tmp_path: Path) -> None:
    fake = FakeHttp({})
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]
    missing = _paper(None)
    missing_paths = ArtifactPaths.for_source(tmp_path, missing.source_id)

    result = downloader.download(missing, missing_paths)

    assert result["status"] == "identifier_unresolved"
    assert not fake.calls


def test_missing_doi_preserves_retryable_identifier_status(tmp_path: Path) -> None:
    paper = replace(
        _paper(None),
        identifier_resolution={
            "resolver": "public-title-consensus",
            "status": "retryable_error",
            "reason": "metadata_service_unavailable",
        },
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    fake = FakeHttp({})

    result = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        paper,
        paths,
    )

    assert result["status"] == "retryable_error"
    assert result["last_error"]["code"] == "retryable_error"
    assert not fake.calls


def test_strict_oa_mode_does_not_scrape_unlicensed_landing(tmp_path: Path) -> None:
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    fake = FakeHttp(
        {
            crossref_url: [
                _fetch(
                    crossref_url,
                    b'{"status":"ok","message":{"link":[],"license":[]}}',
                    content_type="application/json",
                )
            ],
        }
    )
    downloader = PdfDownloader(
        DownloadConfig(require_open_access_evidence=True),
        http=fake,  # type: ignore[arg-type]
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    result = downloader.download(_paper(), paths)

    assert result["status"] == "not_open_access"
    assert all(not call.startswith("https://doi.org/") for call in fake.calls)


def test_unmanaged_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    paths.pdf.write_bytes(b"user file")
    downloader = PdfDownloader(DownloadConfig(), http=FakeHttp({}))  # type: ignore[arg-type]

    result = downloader.download(_paper(), paths)

    assert result["status"] == "unmanaged_existing_file"
    assert paths.pdf.read_bytes() == b"user file"


def test_rejects_structurally_valid_pdf_with_wrong_identity(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    pdf_url = "https://example.org/supplement.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode(),
                )
            ],
            pdf_url: [
                _fetch(
                    pdf_url,
                    _blank_pdf("Unrelated supplementary material"),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    result = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert result["status"] == "invalid_pdf"
    assert not paths.pdf.exists()


def test_current_artifact_requires_matching_manifest_doi(tmp_path: Path) -> None:
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    pdf_url = "https://example.org/paper.pdf"
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, b"", status=404)],
            doi_url: [
                _fetch(
                    doi_url,
                    f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode(),
                )
            ],
            pdf_url: [_fetch(pdf_url, _blank_pdf(), content_type="application/pdf")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")
    downloader = PdfDownloader(DownloadConfig(), http=fake)  # type: ignore[arg-type]
    downloader.download(_paper(), paths)
    corrected = PaperCandidate(
        database_id=7,
        source="kci",
        source_id="ART002856685",
        title="Timestamp study",
        doi_original="https://doi.org/10.9999/corrected",
        doi="10.9999/corrected",
        landing_url=None,
        abstract=None,
        publication_year=2022,
        first_page="1",
        final_page="2",
    )

    assert download_artifact_is_current(_paper(), paths)
    assert not download_artifact_is_current(corrected, paths)


def test_crossref_tdm_pdf_without_open_license_is_not_requested(tmp_path: Path) -> None:
    crossref_url = "https://api.crossref.org/works/10.3745%2FJIPS.04.0245"
    doi_url = "https://doi.org/10.3745/JIPS.04.0245"
    tdm_url = "https://publisher.example.org/tdm.pdf"
    payload = json.dumps(
        {
            "message": {
                "license": [],
                "link": [{"URL": tdm_url, "content-type": "application/pdf"}],
            }
        }
    ).encode()
    fake = FakeHttp(
        {
            crossref_url: [_fetch(crossref_url, payload, content_type="application/json")],
            doi_url: [_fetch(doi_url, b"<html></html>")],
        }
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    result = PdfDownloader(DownloadConfig(), http=fake).download(  # type: ignore[arg-type]
        _paper(),
        paths,
    )

    assert result["status"] == "not_found"
    assert tdm_url not in fake.calls


def test_kci_opt_in_uses_only_explicit_matching_download_button(tmp_path: Path) -> None:
    landing_url = (
        "https://www.kci.go.kr/kciportal/landing/article.kci?"
        "arti_id=ART002856685"
    )
    statistics_url = (
        "https://www.kci.go.kr/kciportal/landing/updateArticleStatistics.kci"
    )
    download_url = (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
        "ciSereArtiOrteServHistIFrame.kci?"
        "sereArticleSearchBean.artiId=ART002856685&"
        "sereArticleSearchBean.orteFileId=KCI_FI002856685"
    )
    html = b"""
    <meta name="citation_doi" content="10.3745/JIPS.04.0245"/>
    <button onclick="fncDown('ART002856685', 'KCI_FI002856685');">
      PDF download
    </button>
    """
    fake = FakeHttp(
        {
            landing_url: [_fetch(landing_url, html)],
            statistics_url: [_fetch(statistics_url, b"", content_type="text/plain")],
            download_url: [_fetch(download_url, _blank_pdf(), content_type="application/pdf")],
        }
    )
    downloader = PdfDownloader(
        DownloadConfig(enable_kci_ui_download=True),
        http=fake,  # type: ignore[arg-type]
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART002856685")

    result = downloader.download(_paper(), paths)

    assert result["status"] == "downloaded"
    assert result["resolver"]["name"] == "kci-ui"
    assert fake.calls == [landing_url, statistics_url, download_url]


def test_kci_exact_source_binding_accepts_audited_printed_doi_conflict(
    tmp_path: Path,
) -> None:
    landing_url = (
        "https://www.kci.go.kr/kciportal/landing/article.kci?"
        "arti_id=ART002856685"
    )
    statistics_url = (
        "https://www.kci.go.kr/kciportal/landing/updateArticleStatistics.kci"
    )
    download_url = (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
        "ciSereArtiOrteServHistIFrame.kci?"
        "sereArticleSearchBean.artiId=ART002856685&"
        "sereArticleSearchBean.orteFileId=KCI_FI002856685"
    )
    final_url = (
        "https://www.kci.go.kr/kciportal/co/download/popup/poDownload.kci?"
        "storFileBean.orteFileId=KCI_FI002856685"
    )
    html = b"""
    <meta name="citation_doi" content="10.3745/JIPS.04.0245"/>
    <button onclick="fncDown('ART002856685', 'KCI_FI002856685');">
      PDF download
    </button>
    """
    pdf = _text_pdf(
        [
            "Timestamp study Alice 2022 402 410 "
            "https://doi.org/10.9999/printed.402"
        ]
    )
    fake = FakeHttp(
        {
            landing_url: [_fetch(landing_url, html)],
            statistics_url: [
                _fetch(statistics_url, b"", content_type="text/plain")
            ],
            download_url: [
                _fetch(
                    download_url,
                    pdf,
                    content_type="application/pdf",
                    final_url=final_url,
                )
            ],
        }
    )
    paper = replace(_paper(), authors=("Alice (Example Lab)",))
    downloader = PdfDownloader(
        DownloadConfig(enable_kci_ui_download=True),
        http=fake,  # type: ignore[arg-type]
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    result = downloader.download(paper, paths)

    assert result["status"] == "downloaded"
    assert (
        result["pdf"]["identity_status"]
        == KCI_DOI_CONFLICT_IDENTITY_STATUS
    )
    assert result["pdf"]["matched_source_id"] == paper.source_id
    kci_attempt = next(
        attempt
        for attempt in result["attempts"]
        if attempt.get("resolver") == "kci-ui"
        and attempt.get("status") == "downloaded"
    )
    assert kci_attempt["conflicting_printed_dois"] == [
        "10.9999/printed.402"
    ]
    assert {
        "kci_landing_exact_database_doi",
        "kci_explicit_source_file_binding",
        "pdf_normalized_title_match",
        "pdf_publication_year_match",
        "pdf_author_match",
        "pdf_page_range_match",
    }.issubset(kci_attempt["identity_provenance"])
    serialized = json.dumps(result)
    assert "KCI_FI002856685" not in serialized
    assert "%5BREDACTED%5D" in serialized
    assert download_artifact_is_current(paper, paths, result)
    assert downloader.download(paper, paths)["_runtime"]["action"] == "reused"

    forged = json.loads(json.dumps(result))
    forged["attempts"][-1]["identity_provenance"] = [
        "pdf_normalized_title_match"
    ]
    assert not download_artifact_is_current(paper, paths, forged)


def test_kci_doi_conflict_exception_requires_exact_exposed_file_binding() -> None:
    landing_url = (
        "https://www.kci.go.kr/kciportal/landing/article.kci?"
        "arti_id=ART002856685"
    )
    statistics_url = (
        "https://www.kci.go.kr/kciportal/landing/updateArticleStatistics.kci"
    )
    download_url = (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
        "ciSereArtiOrteServHistIFrame.kci?"
        "sereArticleSearchBean.artiId=ART002856685&"
        "sereArticleSearchBean.orteFileId=KCI_FI999999999"
    )
    html = b"""
    <meta name="citation_doi" content="10.3745/JIPS.04.0245"/>
    <button onclick="fncDown('ART002856685', 'KCI_FI999999999');">
      PDF download
    </button>
    """
    fake = FakeHttp(
        {
            landing_url: [_fetch(landing_url, html)],
            statistics_url: [
                _fetch(statistics_url, b"", content_type="text/plain")
            ],
            download_url: [
                _fetch(
                    download_url,
                    _text_pdf(
                        [
                            "Timestamp study Alice 2022 "
                            "https://doi.org/10.9999/printed.402"
                        ]
                    ),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paper = replace(_paper(), authors=("Alice (Example Lab)",))
    attempts: list[dict[str, Any]] = []

    result = PdfDownloader(
        DownloadConfig(enable_kci_ui_download=True),
        http=fake,  # type: ignore[arg-type]
    )._try_kci_ui(paper, attempts)

    assert result is None
    assert attempts[-1]["error_code"] == "invalid_pdf"
    assert "KCI_FI999999999" not in json.dumps(attempts)


def test_kci_doi_conflict_exception_requires_pdf_title_match() -> None:
    landing_url = (
        "https://www.kci.go.kr/kciportal/landing/article.kci?"
        "arti_id=ART002856685"
    )
    statistics_url = (
        "https://www.kci.go.kr/kciportal/landing/updateArticleStatistics.kci"
    )
    download_url = (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
        "ciSereArtiOrteServHistIFrame.kci?"
        "sereArticleSearchBean.artiId=ART002856685&"
        "sereArticleSearchBean.orteFileId=KCI_FI002856685"
    )
    html = b"""
    <meta name="citation_doi" content="10.3745/JIPS.04.0245"/>
    <button onclick="fncDown('ART002856685', 'KCI_FI002856685');">
      PDF download
    </button>
    """
    fake = FakeHttp(
        {
            landing_url: [_fetch(landing_url, html)],
            statistics_url: [
                _fetch(statistics_url, b"", content_type="text/plain")
            ],
            download_url: [
                _fetch(
                    download_url,
                    _text_pdf(
                        [
                            "Unrelated paper Alice 2022 "
                            "https://doi.org/10.9999/printed.402"
                        ]
                    ),
                    content_type="application/pdf",
                )
            ],
        }
    )
    paper = replace(_paper(), authors=("Alice (Example Lab)",))
    attempts: list[dict[str, Any]] = []

    result = PdfDownloader(
        DownloadConfig(enable_kci_ui_download=True),
        http=fake,  # type: ignore[arg-type]
    )._try_kci_ui(paper, attempts)

    assert result is None
    assert attempts[-1]["error_code"] == "invalid_pdf"


def test_generic_pdf_validation_still_rejects_a_different_doi_with_same_title() -> None:
    pdf = _text_pdf(
        ["Timestamp study https://doi.org/10.9999/different"]
    )

    with pytest.raises(
        PdfValidationError,
        match="first pages contain a different DOI",
    ):
        validate_pdf(
            pdf,
            expected_doi="10.3745/JIPS.04.0245",
            expected_title="Timestamp study",
            require_identity=False,
        )
