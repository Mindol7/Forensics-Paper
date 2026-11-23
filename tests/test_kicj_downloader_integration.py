from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

from pypdf import PdfWriter

import scripts.process_papers as process_papers
from PaperPipeline.downloader import DownloadConfig, PdfDownloader
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.kicj_resolver import (
    KICJ_RESOLVER_NAME,
    KicjResolution,
    KicjResolverAttempt,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import ArtifactPaths


TITLE = "디지털 증거 압수수색에서 피의자의 참여권 보장 법리에 대한 비판적 고찰"
DETAIL_URL = (
    "https://www.kicj.re.kr/board.es"
    "?mid=a10103010000&bid=0003&act=view&list_no=13698"
)
PDF_URL = (
    "https://www.kicj.re.kr/boardDownload.es"
    "?bid=0003&list_no=13698&seq=7"
)


def _paper(source_id: str = "ART003007915") -> PaperCandidate:
    return PaperCandidate(
        database_id=240,
        source="kci",
        source_id=source_id,
        title=TITLE,
        doi_original=None,
        doi=None,
        landing_url="https://www.kci.go.kr/article/ART003007915",
        abstract=None,
        publication_year=2023,
        first_page="193",
        final_page="224",
        alternate_titles=(
            "A Critical Look at the Jurisprudence of Digital Evidence",
        ),
        journal_id="형사정책연구",
        institution_id="한국형사법무정책연구원",
        issue_id="34_3",
        authors=("이관희(고려대학교)",),
    )


def _pdf_with_title(title: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _PdfHttp:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[str] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers=None,
    ) -> HttpFetch:
        del max_bytes, max_html_bytes, headers
        self.calls.append(url)
        return HttpFetch(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            body=self.body,
            redirects=(),
        )


class _ResolvedKicj:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, paper: PaperCandidate) -> KicjResolution:
        self.calls.append(paper.source_id)
        return KicjResolution(
            status="resolved",
            reason="official_kicj_issue_exact_identity",
            paper=paper,
            candidate=ResolverCandidate(
                url=PDF_URL,
                resolver=KICJ_RESOLVER_NAME,
                landing_url=DETAIL_URL,
                is_open_access=True,
            ),
            detail_url=DETAIL_URL,
            matched_title=TITLE,
            matched_authors=("이관희", "이상진"),
            archive_pages_checked=1,
            exact_title_matches=1,
            author_matches=1,
            attempts=(
                KicjResolverAttempt(
                    stage="kicj-archive-1",
                    url="https://www.kicj.re.kr/board.es?mid=a10103010000&bid=0003",
                    status="ok",
                    http_status=200,
                    final_url=(
                        "https://www.kicj.re.kr/board.es"
                        "?mid=a10103010000&bid=0003"
                    ),
                ),
            ),
            http_status=200,
        )


def test_doi_less_kicj_candidate_is_downloaded_before_identifier_terminal(
    tmp_path: Path,
) -> None:
    paper = _paper()
    http = _PdfHttp(_pdf_with_title(TITLE))
    resolver = _ResolvedKicj()
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(),
        http=http,  # type: ignore[arg-type]
        kicj_resolver=resolver,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"] == KICJ_RESOLVER_NAME
    assert manifest["resolver"]["is_open_access"] is True
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert resolver.calls == [paper.source_id]
    assert http.calls == [PDF_URL]
    archive_attempt = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt["resolver"] == f"{KICJ_RESOLVER_NAME}:kicj-archive-1"
    )
    assert archive_attempt["http_status"] == 200
    resolution_attempt = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt["resolver"] == KICJ_RESOLVER_NAME
        and attempt.get("resolution_status") == "resolved"
    )
    assert resolution_attempt["matched_title"] == TITLE
    assert resolution_attempt["matched_authors"] == ["이관희", "이상진"]


def test_non_kicj_paper_does_not_enter_shared_kicj_resolver(
    tmp_path: Path,
) -> None:
    paper = replace(_paper(), institution_id="다른 기관")
    resolver = _ResolvedKicj()

    manifest = PdfDownloader(
        DownloadConfig(),
        http=_PdfHttp(_pdf_with_title(TITLE)),  # type: ignore[arg-type]
        kicj_resolver=resolver,
    ).download(
        paper,
        ArtifactPaths.for_source(tmp_path, paper.source_id),
    )

    assert manifest["status"] == "identifier_unresolved"
    assert resolver.calls == []


def test_process_runtime_passes_one_shared_kicj_resolver_to_all_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = process_papers.build_parser().parse_args(
        ["download", "--dry-run", "--per-host-rps", "1000"]
    )
    process_papers._validate_args(args)
    runtime = process_papers._download_runtime(args)
    observed: list[object] = []

    class _Downloader:
        def __init__(
            self,
            _config,
            *,
            http,
            kicj_resolver,
            police_resolver,
            spo_resolver,
            kdfs_resolver,
        ) -> None:
            del http
            observed.append(
                (
                    kicj_resolver,
                    police_resolver,
                    spo_resolver,
                    kdfs_resolver,
                )
            )

        def download(self, paper, _paths, *, force):
            del paper, force
            return {"status": "identifier_unresolved"}

    monkeypatch.setattr(process_papers, "PdfDownloader", _Downloader)

    process_papers._download_one(
        _paper("ART003007915"),
        tmp_path / "paper",
        tmp_path / "manifest",
        runtime,
        False,
    )
    process_papers._download_one(
        _paper("ART003007916"),
        tmp_path / "paper",
        tmp_path / "manifest",
        runtime,
        False,
    )

    assert observed == [
        (
            runtime.kicj_resolver,
            runtime.police_resolver,
            runtime.spo_resolver,
            runtime.kdfs_resolver,
        ),
        (
            runtime.kicj_resolver,
            runtime.police_resolver,
            runtime.spo_resolver,
            runtime.kdfs_resolver,
        ),
    ]
