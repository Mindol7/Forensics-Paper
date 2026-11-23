#!/usr/bin/env python3
"""Internal audit/report engine for PDF collection statistics.

The report treats ``kci_papers`` as the authoritative target list and joins
``Analyzer/Manifest/<source_id>.download.json`` to the corresponding
the title-based PDF resolved through ``Analyzer/Manifest/paper_files.json``.
An item is counted as collected only when all of the following hold:

* the manifest status is ``downloaded``;
* the PDF is a regular, non-symlink file with a PDF header;
* its byte length equals the manifest value; and
* its SHA-256 equals the manifest value.

This makes the society-level totals reproducible from the paper-level detail
sheet instead of relying on a directory file count alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
from openpyxl import load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from PaperPipeline.paper_files import PAPER_FILE_INDEX_NAME, load_paper_file_index
from PaperPipeline.storage import ArtifactPaths


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SEOUL = ZoneInfo("Asia/Seoul")

SHEET_OVERVIEW = "요약"
SHEET_SOCIETY = "학회별 통계"
SHEET_SOCIETY_ROUTE = "학회×수집경로"
SHEET_ROUTE = "수집경로 요약"
SHEET_DETAIL = "논문별 상세"
SHEET_MISSING = "미수집 목록"

SUCCESS_STATUS = "downloaded"
SENSITIVE_QUERY_PARTS = (
    "token",
    "jwt",
    "key",
    "code",
    "signature",
    "credential",
    "auth",
    "cookie",
    "email",
    "mailto",
)

ROUTE_LABELS = {
    "kci-ui": "KCI 공개 상세페이지 다운로드",
    "publisher-metadata": "출판사 표준 PDF 메타데이터",
    "spo-public-board": "대검찰청 공개 게시판 첨부",
    "authorized-platform-public-ui:dbpia": "DBpia 공식 다운로드 UI",
    "authorized-platform-public-ui:earticle": "eArticle 공식 다운로드 UI",
    "authorized-platform-public-ui:kiss": "KISS 공식 다운로드 UI",
    "authorized-platform-public-ui:kyobo": "교보 스콜라 공식 다운로드 UI",
    "kyobo-authenticated-browser": "교보 Scholar 인증 브라우저",
    "kdfs-jams-authenticated-browser": "한국디지털포렌식학회 JAMS 인증 브라우저",
    "elsevier-article-retrieval": "ScienceDirect (Elsevier Article Retrieval API)",
    "kci-scienceon": "KCI 원문찾기 → ScienceON",
    "kicj-public-archive": "한국형사·법무정책연구원 공개 아카이브",
    "google-drive-explicit-download": "공식 페이지 명시 Google Drive",
    "police-university-public-board": "경찰대학 공개 게시판 첨부",
    "publisher-explicit-pdf-link": "출판사 페이지 명시 PDF 링크",
    "dcs-public-reader": "디지털콘텐츠학회 공개 Reader",
    "kci-journal-sitemap": "KCI Journal 공개 sitemap",
    "doi-direct": "DOI가 직접 가리킨 PDF",
    "jiis-public-search": "지능정보시스템학회 공개 검색",
    "crossref": "Crossref PDF 메타데이터",
}

STATUS_LABELS = {
    "downloaded": "수집 완료",
    "not_found": "공개 원문 미발견",
    "identifier_unresolved": "식별자/원문 경로 미해결",
    "permission_required": "자동 수집 허가 필요",
    "access_denied": "접근 거부",
    "authentication_required": "로그인 세션 갱신 필요",
    "not_entitled": "기관 구독 권한 없음",
    "invalid_pdf": "PDF 검증 실패",
    "manifest_missing": "다운로드 기록 없음",
    "artifact_invalid": "수집 파일 무결성 불일치",
}


@dataclass(frozen=True, slots=True)
class ArtifactAudit:
    collected: bool
    integrity_status: str
    actual_bytes: int | None
    actual_sha256: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="학회별 PDF 수집 현황과 수집 경로를 Excel로 내보냅니다.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=REPOSITORY_ROOT / "kci_pipeline.db",
        help="kci_papers 테이블이 있는 SQLite DB",
    )
    parser.add_argument(
        "--papers-dir",
        type=Path,
        default=REPOSITORY_ROOT / "Analyzer" / "Paper",
        help="PDF 파일이 있는 디렉터리",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=REPOSITORY_ROOT / "Analyzer" / "Manifest",
        help="*.download.json과 lock이 있는 디렉터리",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "reports"
            / f"pdf_collection_by_society_{datetime.now(SEOUL):%Y%m%d_%H%M%S}.xlsx"
        ),
        help="생성할 .xlsx 파일",
    )
    return parser.parse_args(argv)


def _require_inputs(
    database: Path,
    papers_dir: Path,
    output: Path,
    *,
    manifest_dir: Path | None = None,
) -> None:
    state_dir = manifest_dir or papers_dir
    if not database.is_file():
        raise ValueError(f"SQLite DB가 존재하지 않습니다: {database}")
    if not papers_dir.is_dir():
        raise ValueError(f"PDF 디렉터리가 존재하지 않습니다: {papers_dir}")
    if not state_dir.is_dir():
        raise ValueError(f"manifest 디렉터리가 존재하지 않습니다: {state_dir}")
    if output.suffix.casefold() != ".xlsx":
        raise ValueError("출력 파일 확장자는 .xlsx여야 합니다")
    if database.is_symlink() or papers_dir.is_symlink() or state_dir.is_symlink():
        raise ValueError("입력 DB/PDF/manifest 경로는 심볼릭 링크일 수 없습니다")


def _load_papers(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kci_papers'"
        ).fetchone()
        if table is None:
            raise ValueError("DB에 kci_papers 테이블이 없습니다")
        rows = connection.execute(
            """
            SELECT
                id,
                source,
                source_id,
                title,
                title_kor,
                title_eng,
                doi,
                url,
                publication_year,
                first_page,
                final_page,
                journal_id,
                institution_id,
                issue_id,
                authors_json
            FROM kci_papers
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    papers = [dict(row) for row in rows]
    source_ids = [str(row.get("source_id") or "").strip() for row in papers]
    if any(not source_id for source_id in source_ids):
        raise ValueError("kci_papers에 빈 source_id가 있습니다")
    duplicates = sorted(
        source_id
        for source_id, count in Counter(source_ids).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"kci_papers source_id 중복: {', '.join(duplicates[:10])}")
    return papers


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file() or path.is_symlink():
        return None, "manifest_missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "manifest_invalid"
    if not isinstance(value, dict):
        return None, "manifest_invalid"
    return value, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_pdf(
    pdf_path: Path,
    manifest: dict[str, Any] | None,
) -> ArtifactAudit:
    if manifest is None or manifest.get("status") != SUCCESS_STATUS:
        return ArtifactAudit(False, "미수집", None, None)
    if not pdf_path.is_file() or pdf_path.is_symlink():
        return ArtifactAudit(False, "PDF 파일 없음", None, None)
    try:
        actual_bytes = pdf_path.stat().st_size
        with pdf_path.open("rb") as handle:
            magic = handle.read(5)
        if magic != b"%PDF-":
            return ArtifactAudit(False, "PDF 헤더 불일치", actual_bytes, None)
        actual_sha256 = _sha256(pdf_path)
    except OSError:
        return ArtifactAudit(False, "PDF 읽기 실패", None, None)

    pdf_meta = manifest.get("pdf")
    if not isinstance(pdf_meta, dict):
        return ArtifactAudit(
            False,
            "manifest PDF 메타데이터 없음",
            actual_bytes,
            actual_sha256,
        )
    if pdf_meta.get("path") != pdf_path.name:
        return ArtifactAudit(
            False,
            "manifest 파일명 불일치",
            actual_bytes,
            actual_sha256,
        )
    if pdf_meta.get("bytes") != actual_bytes:
        return ArtifactAudit(
            False,
            "파일 크기 불일치",
            actual_bytes,
            actual_sha256,
        )
    expected_sha256 = str(pdf_meta.get("sha256") or "").casefold()
    if expected_sha256 != actual_sha256.casefold():
        return ArtifactAudit(
            False,
            "SHA-256 불일치",
            actual_bytes,
            actual_sha256,
        )
    return ArtifactAudit(True, "PDF·크기·SHA-256 일치", actual_bytes, actual_sha256)


def _parse_authors(raw: Any) -> str:
    if raw is None:
        return ""
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return str(raw)
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value)


def _route_label(resolver_name: str, host: str) -> str:
    if resolver_name == "publisher-metadata":
        host_labels = {
            "koreascience.kr": "KoreaScience 표준 PDF 메타데이터",
            "www.koreascience.kr": "KoreaScience 표준 PDF 메타데이터",
            "koreascience.or.kr": "KoreaScience 표준 PDF 메타데이터",
            "www.koreascience.or.kr": "KoreaScience 표준 PDF 메타데이터",
            "itiis.org": "TIIS 출판사 표준 PDF 메타데이터",
            "www.itiis.org": "TIIS 출판사 표준 PDF 메타데이터",
            "accesson.kr": "AccessON 표준 PDF 메타데이터",
            "www.accesson.kr": "AccessON 표준 PDF 메타데이터",
            "jips-k.org": "JIPS-K 표준 PDF 메타데이터",
            "www.jips-k.org": "JIPS-K 표준 PDF 메타데이터",
            "jicce.org": "JICCE 표준 PDF 메타데이터",
            "www.jicce.org": "JICCE 표준 PDF 메타데이터",
        }
        return host_labels.get(host, ROUTE_LABELS[resolver_name])
    return ROUTE_LABELS.get(resolver_name, resolver_name or "경로 정보 없음")


def _host_from_urls(download_url: str, landing_url: str) -> str:
    for value in (download_url, landing_url):
        if not value:
            continue
        hostname = urlsplit(value).hostname
        if hostname:
            return hostname.casefold()
    return ""


def _safe_url(value: Any) -> str:
    """Mask sensitive query values even if an old manifest missed redaction."""

    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if any(part in lowered for part in SENSITIVE_QUERY_PARTS):
            query.append((key, "[REDACTED]"))
        else:
            query.append((key, item))
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path,
            urlencode(query),
            "",
        )
    )


def _manifest_status(
    manifest: dict[str, Any] | None,
    manifest_error: str | None,
    audit: ArtifactAudit,
) -> str:
    if manifest_error is not None:
        return manifest_error
    assert manifest is not None
    status = str(manifest.get("status") or "unknown")
    if status == SUCCESS_STATUS and not audit.collected:
        return "artifact_invalid"
    return status


def _artifact_state(
    papers_root: Path,
    manifest_root: Path,
    source_id: str,
) -> tuple[Path, Path, dict[str, Any] | None, str | None, ArtifactAudit, str]:
    paths = ArtifactPaths.for_source(
        papers_root,
        source_id,
        manifest_root=manifest_root,
        # Statistics include DB targets that have never produced a PDF.  This
        # explicit fallback is read-only; collectors remain fail-closed and
        # register every new title before choosing an artifact path.
        allow_unindexed_pdf=True,
    )
    manifest_path = paths.download_manifest
    pdf_path = paths.pdf
    manifest, manifest_error = _load_manifest(manifest_path)
    audit = _audit_pdf(pdf_path, manifest)
    status = _manifest_status(manifest, manifest_error, audit)
    if manifest is not None:
        manifest_paper = manifest.get("paper")
        if (
            isinstance(manifest_paper, dict)
            and manifest_paper.get("source_id") != source_id
        ):
            audit = ArtifactAudit(
                False,
                "manifest source_id 불일치",
                audit.actual_bytes,
                audit.actual_sha256,
            )
            status = "artifact_invalid"
    return manifest_path, pdf_path, manifest, manifest_error, audit, status


def build_detail_rows(
    papers: list[dict[str, Any]],
    papers_dir: Path,
    compatibility_papers_dirs: Sequence[Path] = (),
    *,
    manifest_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    detail_rows: list[dict[str, Any]] = []
    source_ids = {str(paper["source_id"]) for paper in papers}
    integrity_failures: list[str] = []
    state_dir = manifest_dir or papers_dir
    locations = tuple(
        dict.fromkeys(
            (
                (papers_dir, state_dir),
                *((path, path) for path in compatibility_papers_dirs),
            )
        )
    )

    for paper in papers:
        source_id = str(paper["source_id"])
        states = [
            _artifact_state(papers_root, manifest_root, source_id)
            for papers_root, manifest_root in locations
        ]
        # Prefer the first fully valid artifact.  When none is valid, retain the
        # first manifest-bearing state so its latest failure reason is reported.
        selected = next((state for state in states if state[4].collected), None)
        if selected is None:
            selected = next(
                (state for state in states if state[3] != "manifest_missing"),
                states[0],
            )
        manifest_path, pdf_path, manifest, manifest_error, audit, status = selected
        if status == "artifact_invalid":
            integrity_failures.append(source_id)

        resolver = manifest.get("resolver") if isinstance(manifest, dict) else None
        resolver = resolver if isinstance(resolver, dict) else {}
        pdf_meta = manifest.get("pdf") if isinstance(manifest, dict) else None
        pdf_meta = pdf_meta if isinstance(pdf_meta, dict) else {}
        last_error = manifest.get("last_error") if isinstance(manifest, dict) else None
        last_error = last_error if isinstance(last_error, dict) else {}

        resolver_name = str(resolver.get("name") or "") if audit.collected else ""
        landing_url = _safe_url(resolver.get("landing_url")) if audit.collected else ""
        download_url = _safe_url(resolver.get("download_url")) if audit.collected else ""
        host = _host_from_urls(download_url, landing_url) if audit.collected else ""
        route_label = _route_label(resolver_name, host) if audit.collected else ""
        identity_status = str(pdf_meta.get("identity_status") or "") if audit.collected else ""
        review_note = ""
        if "doi_conflict" in identity_status:
            review_note = "PDF 본문 DOI 충돌 표기: source_id·제목 근거로 수집 인정"
        elif identity_status == "resolver_source_id_match":
            review_note = "source_id 근거로 수집 인정"

        failure_code = "" if audit.collected else str(last_error.get("code") or status)
        failure_message = "" if audit.collected else str(last_error.get("message") or "")
        title = paper.get("title") or paper.get("title_kor") or paper.get("title_eng") or ""
        society = str(paper.get("institution_id") or "").strip() or "(학회/발행기관 미상)"
        journal = str(paper.get("journal_id") or "").strip() or "(학술지 미상)"

        detail_rows.append(
            {
                "DB ID": paper.get("id"),
                "Source ID": source_id,
                "학회/발행기관": society,
                "학술지": journal,
                "논문명": title,
                "국문 논문명": paper.get("title_kor") or "",
                "영문 논문명": paper.get("title_eng") or "",
                "저자": _parse_authors(paper.get("authors_json")),
                "발행연도": paper.get("publication_year"),
                "권호 ID": paper.get("issue_id") or "",
                "시작 페이지": paper.get("first_page") or "",
                "종료 페이지": paper.get("final_page") or "",
                "DOI": paper.get("doi") or "",
                "수집 여부": "수집" if audit.collected else "미수집",
                "수집 상태 코드": status,
                "수집 상태": STATUS_LABELS.get(status, status),
                "수집 경로": route_label,
                "Resolver": resolver_name,
                "원문 호스트": host,
                "Landing URL": landing_url,
                "Download URL (민감값 마스킹)": download_url,
                "PDF 파일": pdf_path.name if audit.collected else "",
                "PDF 바이트": audit.actual_bytes if audit.collected else None,
                "PDF 페이지 수": pdf_meta.get("page_count") if audit.collected else None,
                "PDF SHA-256": audit.actual_sha256 if audit.collected else "",
                "PDF 식별 상태": identity_status,
                "응답 Content-Type": pdf_meta.get("content_type") if audit.collected else "",
                "다운로드 시각": manifest.get("downloaded_at") if audit.collected else "",
                "무결성 검증": audit.integrity_status,
                "검토 메모": review_note,
                "미수집 사유 코드": failure_code,
                "미수집 사유": failure_message,
            }
        )

    manifest_stems = {
        path.name.removesuffix(".download.json")
        for _papers_root, manifest_root in locations
        for path in manifest_root.glob("*.download.json")
        if path.is_file()
    }
    recognized_pdf_source_ids: set[str] = set()
    orphan_pdf_names: set[str] = set()
    actual_pdf_count = 0
    for papers_root, manifest_root in locations:
        indexed = load_paper_file_index(
            manifest_root / PAPER_FILE_INDEX_NAME,
            required=False,
        )
        source_by_filename = {
            record.filename: source_id for source_id, record in indexed.items()
        }
        pdf_paths = [path for path in papers_root.glob("*.pdf") if path.is_file()]
        actual_pdf_count += len(pdf_paths)
        for path in pdf_paths:
            source_id = source_by_filename.get(path.name)
            if not indexed and source_id is None and path.stem in source_ids:
                source_id = path.stem
            if (
                source_id is None
                or source_id not in source_ids
                or source_id in recognized_pdf_source_ids
            ):
                orphan_pdf_names.add(path.name)
            else:
                recognized_pdf_source_ids.add(source_id)
    audit_summary = {
        "manifest_files": len(manifest_stems),
        "pdf_files": actual_pdf_count,
        "orphan_manifests": sorted(manifest_stems - source_ids),
        "orphan_pdfs": sorted(orphan_pdf_names),
        "integrity_failures": sorted(integrity_failures),
    }
    return detail_rows, audit_summary


def _joined_counts(values: pd.Series) -> str:
    counts = values[values.astype(str).str.len() > 0].value_counts()
    if counts.empty:
        return "-"
    return "; ".join(f"{name} {count}건" for name, count in counts.items())


def build_frames(
    detail_rows: list[dict[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    detail = pd.DataFrame(detail_rows)
    if detail.empty:
        raise ValueError("보고서로 내보낼 논문이 없습니다")

    detail["수집 플래그"] = (detail["수집 여부"] == "수집").astype(int)
    detail["미수집 플래그"] = (detail["수집 여부"] == "미수집").astype(int)

    base = (
        detail.groupby("학회/발행기관", dropna=False)
        .agg(
            전체_논문=("Source ID", "size"),
            수집=("수집 플래그", "sum"),
            미수집=("미수집 플래그", "sum"),
            학술지_수=("학술지", "nunique"),
        )
        .reset_index()
    )
    base["수집률"] = base["수집"] / base["전체_논문"]

    collected = detail[detail["수집 플래그"] == 1].copy()
    route_text = (
        collected.assign(
            경로_표시=collected.apply(
                lambda row: (
                    f"{row['수집 경로']} [{row['원문 호스트']}]"
                    if row["원문 호스트"]
                    else row["수집 경로"]
                ),
                axis=1,
            )
        )
        .groupby("학회/발행기관")["경로_표시"]
        .apply(_joined_counts)
        .rename("수집 경로별 건수")
        .reset_index()
    )
    missing_text = (
        detail[detail["미수집 플래그"] == 1]
        .groupby("학회/발행기관")["수집 상태"]
        .apply(_joined_counts)
        .rename("미수집 상태별 건수")
        .reset_index()
    )
    society = base.merge(route_text, on="학회/발행기관", how="left").merge(
        missing_text,
        on="학회/발행기관",
        how="left",
    )
    society["수집 경로별 건수"] = society["수집 경로별 건수"].fillna("-")
    society["미수집 상태별 건수"] = society["미수집 상태별 건수"].fillna("-")
    society = society.rename(
        columns={
            "전체_논문": "전체 논문",
            "학술지_수": "학술지 수",
        }
    ).sort_values(
        ["전체 논문", "학회/발행기관"],
        ascending=[False, True],
        kind="stable",
    )
    society.insert(0, "순위", range(1, len(society) + 1))

    if collected.empty:
        society_route = pd.DataFrame(
            columns=[
                "학회/발행기관",
                "수집 경로",
                "Resolver",
                "원문 호스트",
                "수집 건수",
                "학회 내 수집 비율",
                "학회 전체 논문 중 비율",
            ]
        )
        route = pd.DataFrame(
            columns=[
                "수집 경로",
                "Resolver",
                "원문 호스트",
                "수집 건수",
                "전체 수집본 중 비율",
                "사용 학회/발행기관 수",
            ]
        )
    else:
        society_route = (
            collected.groupby(
                ["학회/발행기관", "수집 경로", "Resolver", "원문 호스트"],
                dropna=False,
            )
            .size()
            .rename("수집 건수")
            .reset_index()
        )
        society_collected = society.set_index("학회/발행기관")["수집"]
        society_route["학회 내 수집 비율"] = society_route.apply(
            lambda row: row["수집 건수"] / society_collected[row["학회/발행기관"]],
            axis=1,
        )
        society_total = society.set_index("학회/발행기관")["전체 논문"]
        society_route["학회 전체 논문 중 비율"] = society_route.apply(
            lambda row: row["수집 건수"] / society_total[row["학회/발행기관"]],
            axis=1,
        )
        society_route = society_route.sort_values(
            ["학회/발행기관", "수집 건수", "수집 경로", "원문 호스트"],
            ascending=[True, False, True, True],
            kind="stable",
        )

        route = (
            collected.groupby(["수집 경로", "Resolver", "원문 호스트"], dropna=False)
            .agg(
                수집_건수=("Source ID", "size"),
                사용_학회_발행기관_수=("학회/발행기관", "nunique"),
            )
            .reset_index()
            .rename(
                columns={
                    "수집_건수": "수집 건수",
                    "사용_학회_발행기관_수": "사용 학회/발행기관 수",
                }
            )
        )
        route["전체 수집본 중 비율"] = route["수집 건수"] / len(collected)
        route = route[
            [
                "수집 경로",
                "Resolver",
                "원문 호스트",
                "수집 건수",
                "전체 수집본 중 비율",
                "사용 학회/발행기관 수",
            ]
        ].sort_values(
            ["수집 건수", "수집 경로", "원문 호스트"],
            ascending=[False, True, True],
            kind="stable",
        )

    missing = detail[detail["미수집 플래그"] == 1].copy()
    missing = missing[
        [
            "학회/발행기관",
            "학술지",
            "Source ID",
            "논문명",
            "저자",
            "발행연도",
            "DOI",
            "수집 상태 코드",
            "수집 상태",
            "미수집 사유 코드",
            "미수집 사유",
        ]
    ].sort_values(
        ["학회/발행기관", "발행연도", "Source ID"],
        ascending=[True, True, True],
        kind="stable",
    )

    detail = detail.drop(columns=["수집 플래그", "미수집 플래그"])
    return society, society_route, route, detail, missing


def _overview_rows(
    detail: pd.DataFrame,
    society: pd.DataFrame,
    route: pd.DataFrame,
    audit_summary: dict[str, Any],
    *,
    database: Path,
    papers_dir: Path,
    manifest_dir: Path,
    generated_at: datetime,
    compatibility_papers_dirs: Sequence[Path] = (),
) -> list[tuple[str, Any, str]]:
    total = len(detail)
    collected = int((detail["수집 여부"] == "수집").sum())
    missing = total - collected
    total_pdf_bytes = int(detail["PDF 바이트"].fillna(0).sum())
    total_pdf_pages = int(detail["PDF 페이지 수"].fillna(0).sum())
    missing_statuses = _joined_counts(
        detail.loc[detail["수집 여부"] == "미수집", "수집 상태"]
    )
    return [
        ("보고서 생성 시각", generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"), "Asia/Seoul"),
        ("기준 DB", str(database.resolve()), "kci_papers 전체 행"),
        (
            "기준 PDF 경로",
            "; ".join(
                str(path.resolve())
                for path in (papers_dir, *compatibility_papers_dirs)
            ),
            "표준 PDF 경로 우선, 호환 경로의 검증된 산출물 포함",
        ),
        ("기준 manifest 경로", str(manifest_dir.resolve()), "download manifest·lock"),
        ("대상 논문", total, "DB의 고유 source_id 수"),
        ("수집 논문", collected, "manifest downloaded + PDF/크기/SHA-256 일치"),
        ("미수집 논문", missing, "대상 논문 - 수집 논문"),
        ("전체 수집률", collected / total if total else 0, "수집 논문 / 대상 논문"),
        ("학회/발행기관 수", len(society), "DB institution_id 기준"),
        (
            "미수집 보유 학회/발행기관",
            int((society["미수집"] > 0).sum()),
            "미수집 논문이 1편 이상인 기관",
        ),
        ("미수집 상태별", missing_statuses, "최종 manifest 상태 기준"),
        ("수집 경로 조합 수", len(route), "Resolver + 원문 호스트 기준"),
        ("수집 PDF 총 페이지", total_pdf_pages, "manifest page_count 합계"),
        (
            "수집 PDF 총 용량",
            total_pdf_bytes,
            f"{total_pdf_bytes / (1024 * 1024):,.2f} MiB",
        ),
        (
            "manifest 파일 수",
            audit_summary["manifest_files"],
            "Analyzer/Manifest/*.download.json",
        ),
        ("실제 PDF 파일 수", audit_summary["pdf_files"], "Analyzer/Paper/*.pdf"),
        ("무결성 불일치", len(audit_summary["integrity_failures"]), "0건이어야 정상"),
        ("DB 밖 manifest", len(audit_summary["orphan_manifests"]), "DB source_id와 미결합"),
        ("DB 밖 PDF", len(audit_summary["orphan_pdfs"]), "DB source_id와 미결합"),
        (
            "집계 단위",
            "학회/발행기관",
            "kci_papers.institution_id를 사용; 학술지는 journal_id로 별도 표기",
        ),
        (
            "경로 해석",
            "Resolver + 원문 호스트",
            "공개 접근성은 재배포 라이선스를 뜻하지 않음",
        ),
    ]


def _write_overview(workbook, rows: list[tuple[str, Any, str]]) -> None:
    worksheet = workbook.create_sheet(SHEET_OVERVIEW, 0)
    worksheet.append(["항목", "값", "정의/비고"])
    for row in rows:
        worksheet.append(list(row))
    worksheet.freeze_panes = "A2"
    worksheet.sheet_view.showGridLines = False
    worksheet.auto_filter.ref = f"A1:C{worksheet.max_row}"
    worksheet.column_dimensions["A"].width = 24
    worksheet.column_dimensions["B"].width = 70
    worksheet.column_dimensions["C"].width = 70
    for row in range(2, worksheet.max_row + 1):
        if worksheet.cell(row=row, column=1).value == "전체 수집률":
            worksheet.cell(row=row, column=2).number_format = "0.0%"
            break


def _add_table(worksheet, index: int) -> None:
    if worksheet.max_row < 2 or worksheet.max_column < 1:
        return
    table = Table(
        displayName=f"ReportTable{index}",
        ref=f"A1:{get_column_letter(worksheet.max_column)}{worksheet.max_row}",
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    worksheet.add_table(table)


def _style_workbook(output: Path) -> None:
    workbook = load_workbook(output)
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    green_fill = PatternFill(fill_type="solid", fgColor="E2F0D9")
    red_fill = PatternFill(fill_type="solid", fgColor="FCE4D6")

    for table_index, worksheet in enumerate(workbook.worksheets, start=1):
        worksheet.freeze_panes = "A2"
        worksheet.sheet_view.showGridLines = False
        worksheet.sheet_view.zoomScale = 85
        if worksheet.max_row >= 1:
            for cell in worksheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(
                    horizontal="center",
                    vertical="center",
                    wrap_text=True,
                )
                cell.border = border
            worksheet.row_dimensions[1].height = 30

        headers = {
            cell.column: str(cell.value or "")
            for cell in worksheet[1]
        }
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                header = headers.get(cell.column, "")
                cell.border = border
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=header
                    in {
                        "논문명",
                        "국문 논문명",
                        "영문 논문명",
                        "저자",
                        "수집 경로별 건수",
                        "미수집 상태별 건수",
                        "미수집 사유",
                        "정의/비고",
                        "값",
                    },
                )
                if header in {
                    "수집률",
                    "학회 내 수집 비율",
                    "학회 전체 논문 중 비율",
                    "전체 수집본 중 비율",
                }:
                    cell.number_format = "0.0%"
                if header == "PDF 바이트" and isinstance(cell.value, (int, float)):
                    cell.number_format = "#,##0"

        if worksheet.title != SHEET_OVERVIEW:
            _add_table(worksheet, table_index)

        for column_index in range(1, worksheet.max_column + 1):
            header = headers.get(column_index, "")
            values = [
                str(worksheet.cell(row=row, column=column_index).value or "")
                for row in range(1, min(worksheet.max_row, 200) + 1)
            ]
            width = min(max(max((len(value) for value in values), default=0) + 2, 10), 48)
            if header in {"논문명", "국문 논문명", "영문 논문명", "미수집 사유"}:
                width = 45
            elif header in {"Landing URL", "Download URL (민감값 마스킹)", "PDF SHA-256"}:
                width = 42
            elif header in {"수집 경로별 건수", "미수집 상태별 건수"}:
                width = 48
            worksheet.column_dimensions[get_column_letter(column_index)].width = width

        if "수집 여부" in headers.values():
            column = next(index for index, value in headers.items() if value == "수집 여부")
            area = f"{get_column_letter(column)}2:{get_column_letter(column)}{worksheet.max_row}"
            worksheet.conditional_formatting.add(
                area,
                CellIsRule(operator="equal", formula=['"수집"'], fill=green_fill),
            )
            worksheet.conditional_formatting.add(
                area,
                CellIsRule(operator="equal", formula=['"미수집"'], fill=red_fill),
            )

    workbook.active = 0
    workbook.save(output)


def export_report(
    database: Path,
    papers_dir: Path,
    output: Path,
    *,
    manifest_dir: Path | None = None,
    compatibility_papers_dirs: Sequence[Path] = (),
) -> dict[str, Any]:
    database = database.resolve()
    papers_dir = papers_dir.resolve()
    manifest_dir = (manifest_dir or papers_dir).resolve()
    compatibility_papers_dirs = tuple(
        path.resolve() for path in compatibility_papers_dirs
    )
    output = output.resolve()
    _require_inputs(
        database,
        papers_dir,
        output,
        manifest_dir=manifest_dir,
    )
    for compatibility_dir in compatibility_papers_dirs:
        _require_inputs(database, compatibility_dir, output)
    papers = _load_papers(database)
    detail_rows, audit_summary = build_detail_rows(
        papers,
        papers_dir,
        compatibility_papers_dirs,
        manifest_dir=manifest_dir,
    )
    society, society_route, route, detail, missing = build_frames(detail_rows)
    generated_at = datetime.now(SEOUL)
    overview = _overview_rows(
        detail,
        society,
        route,
        audit_summary,
        database=database,
        papers_dir=papers_dir,
        manifest_dir=manifest_dir,
        compatibility_papers_dirs=compatibility_papers_dirs,
        generated_at=generated_at,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        society.to_excel(writer, index=False, sheet_name=SHEET_SOCIETY)
        society_route.to_excel(writer, index=False, sheet_name=SHEET_SOCIETY_ROUTE)
        route.to_excel(writer, index=False, sheet_name=SHEET_ROUTE)
        detail.to_excel(writer, index=False, sheet_name=SHEET_DETAIL)
        missing.to_excel(writer, index=False, sheet_name=SHEET_MISSING)
    workbook = load_workbook(output)
    _write_overview(workbook, overview)
    workbook.save(output)
    _style_workbook(output)

    collected_count = int((detail["수집 여부"] == "수집").sum())
    return {
        "output": output,
        "target_count": len(detail),
        "collected_count": collected_count,
        "missing_count": len(detail) - collected_count,
        "society_count": len(society),
        "route_count": len(route),
        **audit_summary,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = export_report(
            args.database,
            args.papers_dir,
            args.output,
            manifest_dir=args.manifest_dir,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"Excel: {summary['output']}")
    print(
        "coverage: "
        f"total={summary['target_count']} "
        f"collected={summary['collected_count']} "
        f"missing={summary['missing_count']} "
        f"societies={summary['society_count']} "
        f"routes={summary['route_count']}"
    )
    return 0
