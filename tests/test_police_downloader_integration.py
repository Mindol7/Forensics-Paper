from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

from pypdf import PdfWriter

import PaperPipeline.downloader as downloader_module
import scripts.process_papers as process_papers
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.http_client import HttpFetch, redact_url
from PaperPipeline.police_resolver import (
    POLICE_BOARD_URL,
    POLICE_DOWNLOAD_URL,
    POLICE_INTERNAL_USE_NOTICE,
    POLICE_RESOLVER_NAME,
    PoliceResolution,
    PoliceResolverAttempt,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import ArtifactPaths


TITLE = "영상처리 디지털포렌식 기술의 혁신 분석"


def _paper(
    *,
    source_id: str = "ART003198151",
    journal_id: str = "범죄수사학연구",
    institution_id: str = "범죄수사연구원",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=361,
        source="kci",
        source_id=source_id,
        title=TITLE,
        doi_original=None,
        doi=None,
        landing_url=f"https://www.kci.go.kr/article/{source_id}",
        abstract=None,
        publication_year=2025,
        first_page="5",
        final_page="31",
        alternate_titles=(
            "Analysing Innovations in Image Processing Digital Forensics",
        ),
        journal_id=journal_id,
        institution_id=institution_id,
        issue_id="11_1",
        authors=(
            "심지연(연세대학교)",
            "나승균(연세대학교)",
            "이봉규(연세대학교)",
        ),
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
        self.calls: list[tuple[str, str]] = []

    def get_bytes(self, url: str, **_kwargs) -> HttpFetch:
        self.calls.append(("GET", url))
        raise AssertionError(f"unexpected downloader GET: {url}")

    def post_form_bytes(self, url: str, **_kwargs) -> HttpFetch:
        self.calls.append(("POST", url))
        raise AssertionError(f"unexpected downloader POST: {url}")


class ResolvedPolice:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[str] = []

    def resolve(self, paper: PaperCandidate) -> PoliceResolution:
        self.calls.append(paper.source_id)
        fetch = HttpFetch(
            requested_url=POLICE_DOWNLOAD_URL,
            final_url=POLICE_DOWNLOAD_URL,
            status_code=200,
            headers={
                "Content-Type": "application/pdf",
                "Set-Cookie": "JSESSIONID=response-secret",
            },
            body=self.body,
            redirects=(),
        )
        return PoliceResolution(
            status="resolved",
            reason="official_police_board_exact_pdf_identity",
            paper=paper,
            candidate=ResolverCandidate(
                url=POLICE_DOWNLOAD_URL,
                resolver=POLICE_RESOLVER_NAME,
                landing_url=f"{POLICE_BOARD_URL}&token=landing-secret",
                # The downloader must not persist a licence assertion from an
                # injected resolver; the official board declares none.
                license="https://example.invalid/injected-license",
                is_open_access=True,
            ),
            fetch=fetch,
            matched_title=TITLE,
            matched_authors=("심지연", "나승균", "이봉규"),
            attachment_filename="server-form-secret.pdf",
            article_seq="article-form-secret",
            attempts=(
                PoliceResolverAttempt(
                    stage="police-title-search",
                    url=f"{POLICE_BOARD_URL}&token=stage-secret",
                    status="succeeded",
                    http_status=200,
                    final_url=(
                        f"{POLICE_BOARD_URL}&token=final-stage-secret"
                    ),
                ),
            ),
            http_status=200,
        )


class NotFoundPolice:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, paper: PaperCandidate) -> PoliceResolution:
        self.calls.append(paper.source_id)
        return PoliceResolution(
            status="not_found",
            reason="exact_police_article_not_found",
            paper=paper,
            error_code="not_found",
            attempts=(
                PoliceResolverAttempt(
                    stage="police-title-search",
                    url=POLICE_BOARD_URL,
                    status="succeeded",
                    http_status=200,
                ),
            ),
        )


def _summary_attempt(manifest: dict) -> dict:
    return next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == POLICE_RESOLVER_NAME
    )


def test_post_fetch_is_revalidated_and_atomically_saved_without_get(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = _pdf_with_metadata_title(TITLE)
    resolver = ResolvedPolice(content)
    http = NoNetworkHttp()
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
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

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
        police_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert resolver.calls == [paper.source_id]
    # The resolver already POSTed the attachment.  The downloader must not
    # turn its fixed form endpoint into a second GET request.
    assert http.calls == []
    assert paths.pdf.read_bytes() == content
    assert manifest["pdf"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert manifest["resolver"] == {
        "name": POLICE_RESOLVER_NAME,
        "is_open_access": True,
        "license": None,
        "version": None,
        "landing_url": redact_url(
            f"{POLICE_BOARD_URL}&token=landing-secret"
        ),
        "download_url": POLICE_DOWNLOAD_URL,
    }
    assert validation_calls == [
        {
            "expected_doi": None,
            "expected_title": TITLE,
            "require_identity": True,
        }
    ]
    assert (
        json.loads(paths.download_manifest.read_text(encoding="utf-8"))
        == manifest
    )

    summary = _summary_attempt(manifest)
    assert summary["status"] == "downloaded"
    assert summary["resolution_status"] == "resolved"
    assert summary["form_provenance"] == "same-result-row-hidden-fields"
    assert summary["transport"] == "anonymous-form-post"
    assert summary["usage_notice"] == POLICE_INTERNAL_USE_NOTICE
    assert summary["matched_title"] == TITLE
    assert summary["matched_authors"] == ["심지연", "나승균", "이봉규"]
    serialized = json.dumps(manifest, ensure_ascii=False)
    for secret in (
        "stage-secret",
        "final-stage-secret",
        "landing-secret",
        "response-secret",
        "server-form-secret",
        "article-form-secret",
        "injected-license",
    ):
        assert secret not in serialized


def test_common_validation_rejects_wrong_pdf_claimed_resolved(
    tmp_path: Path,
) -> None:
    paper = _paper()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    resolver = ResolvedPolice(_pdf_with_metadata_title("다른 논문"))

    manifest = PdfDownloader(
        DownloadConfig(),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        police_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "invalid_pdf"
    assert manifest["last_error"]["code"] == "invalid_pdf"
    assert not paths.pdf.exists()
    summary = _summary_attempt(manifest)
    assert summary["status"] == "failed"
    assert summary["resolution_status"] == "resolved"
    assert summary["error_code"] == "invalid_pdf"


def test_official_board_absence_is_preserved_as_not_found(
    tmp_path: Path,
) -> None:
    paper = _paper(
        source_id="ART003330395",
        journal_id="범죄수사학연구",
    )
    paper = replace(
        paper,
        publication_year=2026,
        issue_id="12_1",
        first_page="283",
        final_page="306",
    )
    resolver = NotFoundPolice()

    manifest = PdfDownloader(
        DownloadConfig(),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        police_resolver=resolver,
    ).download(
        paper,
        ArtifactPaths.for_source(tmp_path, paper.source_id),
    )

    assert resolver.calls == [paper.source_id]
    assert manifest["status"] == "not_found"
    assert manifest["last_error"]["code"] == "not_found"
    assert _summary_attempt(manifest)["reason"] == (
        "exact_police_article_not_found"
    )


def test_non_police_row_never_enters_shared_police_resolver(
    tmp_path: Path,
) -> None:
    paper = _paper(institution_id="다른 기관")

    class UnexpectedPolice:
        def resolve(self, _paper: PaperCandidate) -> PoliceResolution:
            raise AssertionError("non-Police rows must not enter resolver")

    manifest = PdfDownloader(
        DownloadConfig(),
        http=NoNetworkHttp(),  # type: ignore[arg-type]
        police_resolver=UnexpectedPolice(),
    ).download(
        paper,
        ArtifactPaths.for_source(tmp_path, paper.source_id),
    )

    assert manifest["status"] == "identifier_unresolved"
    assert not any(
        str(attempt.get("resolver") or "").startswith(
            POLICE_RESOLVER_NAME
        )
        for attempt in manifest["attempts"]
    )


def test_cli_runtime_builds_one_synchronized_bounded_police_resolver() -> None:
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--dry-run",
            "--per-host-rps",
            "2",
            "--connect-timeout",
            "3",
            "--read-timeout",
            "4",
            "--max-pdf-mb",
            "17",
        ]
    )
    process_papers._validate_args(args)

    runtime = process_papers._download_runtime(args)

    synchronized = runtime.police_resolver
    assert isinstance(
        synchronized,
        process_papers._SynchronizedPoliceResolver,
    )
    resolver = synchronized._resolver
    assert resolver.config.requests_per_second == 0.5
    assert resolver.config.connect_timeout == 3
    assert resolver.config.read_timeout == 4
    assert resolver.config.max_pdf_bytes == 17 * 1024 * 1024
