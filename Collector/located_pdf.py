#!/usr/bin/env python3
"""Download PDFs from sources a person (or a research pass) already located.

The automatic providers search by title and give up when a platform files the
paper under another title, publisher spelling, or not at all.  For those papers
``Processor/refs/located_pdf_sources.json`` names the official page that hosts
the full text.  This module opens that page in the platform's own persistent
Chromium profile, uses the page's official download control (or the PDF address
the page itself publishes), verifies that the landing page and the PDF belong to
the paper, and stores the result exactly like every other provider does: the
PDF under ``Analyzer/Paper`` with its indexed title filename and a ``downloaded``
manifest under ``Analyzer/Manifest``.

Sign-in is always done by the operator in the browser window.  When a platform
needs it, the window is shown and this program only *watches* — it polls the
page for the signed-in state and continues on its own, so no password ever
passes through the program, the terminal, or the logs.

    python -m Collector.located_pdf                       # every missing paper in the table
    python -m Collector.located_pdf --platforms kiss      # one platform
    python -m Collector.located_pdf --only ART003061474
"""

from __future__ import annotations

import argparse
import io
import os
import json
import re
import sys
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sqlalchemy import create_engine  # noqa: E402

from Collector.kci_pdf import default_manifest_dir, default_papers_dir, download_artifact_is_intact  # noqa: E402
from PaperPipeline.db_source import iter_paper_candidates  # noqa: E402
from PaperPipeline.kyobo_browser import _configure_user_playwright_libraries, _load_sync_playwright  # noqa: E402
from PaperPipeline.paper_files import extend_paper_file_index_if_present  # noqa: E402
from PaperPipeline.records import PaperCandidate  # noqa: E402
from PaperPipeline.storage import ArtifactPaths, load_json_object, sha256_file, write_bytes_atomic, write_json_atomic  # noqa: E402
from config import load_settings  # noqa: E402

DEFAULT_SOURCES = REPOSITORY_ROOT / "Processor" / "refs" / "located_pdf_sources.json"
PROFILE_ROOT = Path.home() / ".local" / "share" / "forensics-paper"
KCI_ARTICLE_URL = "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId={source_id}"
MAX_PDF_BYTES = 80 * 1024 * 1024
LOGIN_WAIT_SECONDS = 20 * 60


@dataclass(frozen=True, slots=True)
class Platform:
    name: str
    label: str
    profile: str
    # Texts that only a signed-in / institution-authenticated page shows
    # (empty: the platform needs no sign-in).
    signed_in_markers: tuple[str, ...] = ()
    login_url: str | None = None
    # True: always show the window (bot checks, publisher log-ins).  False: work in
    # the background and show the window only when a sign-in turns out to be needed.
    headed: bool = False


PLATFORMS: dict[str, Platform] = {
    "koreascience": Platform("koreascience", "KoreaScience", "open-access-browser-profile"),
    "kricm": Platform("kricm", "KNU 기업경영연구소", "open-access-browser-profile"),
    "ksbe": Platform("ksbe", "한국방송·미디어공학회", "open-access-browser-profile"),
    "kci_portal": Platform("kci_portal", "KCI 원문", "kci-portal-browser-profile"),
    "dbpia": Platform("dbpia", "DBpia", "dbpia-browser-profile", signed_in_markers=("기관인증 완료", "로그아웃"), login_url="https://www.dbpia.co.kr/"),
    "kyobo": Platform("kyobo", "교보 스콜라", "kyobo-scholar-browser-profile", signed_in_markers=("로그아웃",), login_url="https://scholar.kyobobook.co.kr/"),
    "kiss": Platform("kiss", "KISS(한국학술정보)", "kiss-browser-profile", signed_in_markers=("로그아웃", "Logout"), login_url="https://kiss.kstudy.com/", headed=True),  # refuses headless browsers
}


class LocatedPdfError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] [LOCATED-PDF] {message}", flush=True)


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

# Middle-dot look-alikes; ``ㆍ`` (U+318D) is a *letter* to ``str.isalnum`` although
# Korean titles use it as punctuation, so it has to be dropped explicitly.
_DOT_LIKE = str.maketrans("", "", "ㆍ\u119e·・‧∙⋅•")  # U+119E is what NFKC turns ㆍ into


def compact(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", (value or "").translate(_DOT_LIKE)).translate(_DOT_LIKE).casefold()
    return "".join(character for character in text if character.isalnum())


def paper_titles(paper: PaperCandidate) -> list[str]:
    titles = [paper.title, *getattr(paper, "alternate_titles", ())]
    return [title for title in titles if title and len(compact(title)) >= 8]


def title_matches(paper: PaperCandidate, observed: Sequence[str]) -> str | None:
    """The observed page title that names this paper (compact-exact, or one contains the other)."""

    expected = [compact(title) for title in paper_titles(paper)]
    for raw in observed:
        value = compact(raw)
        if len(value) < 8:
            continue
        for title in expected:
            shorter, longer = sorted((value, title), key=len)
            if shorter == longer or (len(shorter) >= 16 and shorter in longer and len(shorter) / len(longer) >= 0.6):
                return raw
    return None


def pdf_page_count(content: bytes) -> int:
    from pypdf import PdfReader

    if not content.startswith(b"%PDF-"):
        raise LocatedPdfError("invalid_pdf", "받은 파일이 PDF가 아닙니다 (not a PDF)")
    try:
        return len(PdfReader(io.BytesIO(content), strict=False).pages)
    except Exception as exc:  # noqa: BLE001 - any parser failure is an invalid PDF
        raise LocatedPdfError("invalid_pdf", f"unreadable PDF: {type(exc).__name__}") from exc


def pdf_names_the_paper(content: bytes, paper: PaperCandidate) -> bool | None:
    """True/False when the first pages carry text; None for image-only PDFs."""

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        text = "".join((page.extract_text() or "") for page in list(reader.pages)[:3])
    except Exception:  # noqa: BLE001
        return None
    body = compact(text)
    if len(body) < 200:
        return None
    for title in paper_titles(paper):
        value = compact(title)
        # PDF text extraction breaks long titles across lines and glyph tables:
        # accept a long contiguous run of the title instead of the whole string.
        window = max(12, min(len(value), 24))
        if any(value[start:start + window] in body for start in range(0, max(1, len(value) - window + 1), 4)):
            return True
    return False


# --------------------------------------------------------------------------- #
# browser session
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self, platform: Platform, *, headed: bool, login_wait: int) -> None:
        self.platform = platform
        self.headed = headed or platform.headed
        self.login_wait = login_wait
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self) -> "Session":
        _configure_user_playwright_libraries()
        profile = PROFILE_ROOT / self.platform.profile
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = _load_sync_playwright()().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=not self.headed, accept_downloads=True,
            locale="ko-KR", viewport={"width": 1280, "height": 900},
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog", lambda dialog: dialog.accept())
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    # -- helpers ---------------------------------------------------------------
    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 - slow trackers must not fail the visit
            pass

    def observed_titles(self) -> list[str]:
        script = """() => {
            const pick = (selector, attribute) => Array.from(document.querySelectorAll(selector)).map(e => attribute ? e.getAttribute(attribute) : e.textContent);
            return [
              ...pick('meta[name="citation_title"]', 'content'), ...pick('meta[property="og:title"]', 'content'),
              ...pick('meta[name="dc.title"]', 'content'), ...pick('meta[name="DC.title"]', 'content'),
              document.title, ...pick('h1'), ...pick('h2'), ...pick('h3'), ...pick('.title'), ...pick('#pubTitle'), ...pick('.tit')
            ].filter(Boolean).map(s => s.trim()).filter(s => s.length > 0).slice(0, 60);
        }"""
        try:
            return list(self.page.evaluate(script))
        except Exception:  # noqa: BLE001
            return []

    def signed_in(self) -> bool:
        markers = self.platform.signed_in_markers
        if not markers:
            return True
        # An institution-authenticated page names the subscribing institution.
        institution = (os.getenv("DBPIA_REQUIRED_INSTITUTION_NAME") or "").strip()
        if institution:
            markers = (*markers, institution)
        for page in self.context.pages:
            try:
                body = page.inner_text("body", timeout=5_000)
                if any(marker in body for marker in markers):
                    return True
            except Exception:  # noqa: BLE001 - a navigating page has no body yet
                continue
        return False

    def wait_for_sign_in(self, reason: str) -> None:
        """Show the window and watch until the operator has signed in."""

        if self.signed_in():
            return
        if not self.headed:
            raise LocatedPdfError("login_required", f"{self.platform.label}: sign-in is required ({reason})")
        if self.platform.login_url and self.platform.login_url not in self.page.url:
            try:
                self.goto(self.platform.login_url)
            except Exception:  # noqa: BLE001
                pass
        log(
            f"▶ {self.platform.label} 로그인이 필요합니다 ({reason}). 열려 있는 브라우저 창에서 직접 로그인해 주세요. "
            f"로그인이 확인되면 자동으로 이어집니다 (최대 {self.login_wait // 60}분 대기)."
        )
        deadline = time.monotonic() + self.login_wait
        while time.monotonic() < deadline:
            time.sleep(3)
            if self.signed_in():
                log(f"{self.platform.label} 로그인 확인 — 다운로드를 이어갑니다.")
                return
        raise LocatedPdfError("login_timeout", f"{self.platform.label}: no sign-in within {self.login_wait // 60} minutes")

    def fetch(self, url: str, *, referer: str | None = None) -> bytes | None:
        """GET with the browser's own cookies; PDF bytes or None."""

        try:
            response = self.context.request.get(url, headers={"Referer": referer} if referer else None, timeout=120_000, max_redirects=10)
        except Exception:  # noqa: BLE001
            return None
        if response.status != 200:
            return None
        body = response.body()
        return body if body.startswith(b"%PDF-") and len(body) <= MAX_PDF_BYTES else None

    def click_and_download(self, locator, *, timeout_ms: int = 90_000) -> bytes | None:
        """Click an official download control; capture a download, a popup PDF, or a PDF response."""

        captured: list[bytes] = []

        def on_response(response) -> None:
            try:
                if "pdf" in (response.headers.get("content-type") or "").casefold():
                    body = response.body()
                    if body.startswith(b"%PDF-"):
                        captured.append(body)
            except Exception:  # noqa: BLE001
                pass

        self.context.on("response", on_response)
        try:
            try:
                with self.page.expect_download(timeout=timeout_ms) as holder:
                    locator.click(timeout=15_000)
                path = holder.value.path()
                if path:
                    content = Path(path).read_bytes()
                    if content.startswith(b"%PDF-"):
                        return content
            except Exception:  # noqa: BLE001 - no download event: look at what the click produced instead
                pass
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not captured:
                time.sleep(1)
            if captured:
                return max(captured, key=len)
            for page in self.context.pages:
                if page is not self.page and page.url.casefold().split("?")[0].endswith(".pdf"):
                    content = self.fetch(page.url, referer=self.page.url)
                    if content:
                        return content
            return None
        finally:
            self.context.remove_listener("response", on_response)


# --------------------------------------------------------------------------- #
# platform handlers: return (pdf bytes, landing url, evidence)
# --------------------------------------------------------------------------- #

def _require_landing_identity(session: Session, paper: PaperCandidate) -> str:
    matched = title_matches(paper, session.observed_titles())
    if matched is None:
        raise LocatedPdfError("identity_mismatch", "제공처 페이지의 제목이 이 논문과 일치하지 않습니다")
    return matched


def _first_visible(session: Session, selectors: Sequence[str]):
    for selector in selectors:
        try:
            locator = session.page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def handle(session: Session, paper: PaperCandidate, source: Mapping[str, Any]) -> tuple[bytes, str, list[str]]:
    platform = session.platform.name
    landing = source.get("landing_url") or KCI_ARTICLE_URL.format(source_id=paper.source_id)
    session.goto(landing)
    evidence = [f"landing_title:{_require_landing_identity(session, paper)[:80]}"]

    if platform == "kci_portal":
        control = _first_visible(session, ["a:has-text('KCI 원문 내려받기')", "a[onclick*='fncDown']"])
        if control is None:
            raise LocatedPdfError("no_download_control", "KCI가 이 논문의 원문 내려받기를 제공하지 않습니다(미리보기만 제공)")
        content = session.click_and_download(control)
        evidence.append("kci_portal_official_download_control")
    elif platform == "kiss":
        session.wait_for_sign_in("원문 다운로드는 기관·개인 로그인 후 가능")
        session.goto(landing)
        _require_landing_identity(session, paper)
        control = _first_visible(session, ["button[onclick*='pdfDown']", "button:has-text('다운로드')"])
        if control is None:
            raise LocatedPdfError("no_download_control", "KISS 페이지에 다운로드 버튼이 없습니다")
        content = session.click_and_download(control)
        evidence.append("kiss_official_download_control")
    elif platform == "dbpia":
        session.wait_for_sign_in("기관 인증 또는 로그인 필요")
        session.goto(landing)
        later = _first_visible(session, ["#orgAuthCompleteModal_later"])
        if later is not None:
            later.click()
        _require_landing_identity(session, paper)
        control = _first_visible(session, ["#downloadNodeButton_detail", "button:has-text('다운받기')", "a:has-text('다운받기')"])
        if control is None:
            raise LocatedPdfError("no_download_control", "DBpia 페이지에 다운로드 버튼이 없습니다(구독 범위 밖일 수 있음)")
        content = session.click_and_download(control)
        evidence.append("dbpia_official_download_control")
    elif platform == "kyobo":
        session.wait_for_sign_in("기관 인증 또는 로그인 필요")
        session.goto(landing)
        _require_landing_identity(session, paper)
        control = _first_visible(session, ["a.down:has-text('원문저장')", "a:has-text('원문저장')", "button:has-text('원문저장')"])
        if control is None:
            raise LocatedPdfError("no_download_control", "교보 스콜라 페이지에 원문저장 버튼이 없습니다")
        content = session.click_and_download(control)
        evidence.append("kyobo_official_download_control")
    else:
        # Platforms that publish the PDF address themselves (citation_pdf_url / documented link).
        pdf_url = source.get("pdf_url")
        if not pdf_url:
            try:
                pdf_url = session.page.evaluate("() => (document.querySelector('meta[name=citation_pdf_url]') || {}).content || null")
            except Exception:  # noqa: BLE001
                pdf_url = None
        content = session.fetch(pdf_url, referer=landing) if pdf_url else None
        if content is None and session.headed:
            # Subscription or bot-check: let the operator clear it in the window, keep polling quietly.
            log(f"▶ {session.platform.label}: 창에서 접근 확인(로그인·기관 인증·보안문자)을 완료해 주세요. PDF가 열리면 자동으로 이어집니다.")
            deadline = time.monotonic() + session.login_wait
            while content is None and time.monotonic() < deadline:
                time.sleep(8)
                if pdf_url:
                    content = session.fetch(pdf_url, referer=landing)
                if content is None:
                    for page in session.context.pages:
                        if ".pdf" in page.url.casefold() or "/pdfft" in page.url.casefold():
                            content = session.fetch(page.url, referer=landing)
                            if content:
                                break
        if content is None:
            control = _first_visible(session, ["a:has-text('PDF')", "a:has-text('원문')", "a[href$='.pdf']"])
            content = session.click_and_download(control) if control is not None else None
        evidence.append("publisher_published_pdf_address")
    if content is None:
        raise LocatedPdfError("download_failed", "공식 다운로드 버튼을 눌렀지만 PDF를 받지 못했습니다")
    return content, landing, evidence


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def store(paper: PaperCandidate, content: bytes, *, platform: Platform, landing: str, evidence: Sequence[str],
          open_access: bool, papers_dir: Path, manifest_dir: Path) -> ArtifactPaths:
    page_count = pdf_page_count(content)
    named = pdf_names_the_paper(content, paper)
    if named is False:
        raise LocatedPdfError("identity_mismatch", "받은 PDF 본문에 이 논문의 제목이 없습니다")
    index = extend_paper_file_index_if_present(manifest_dir / "paper_files.json", [(paper.source_id, paper.title)], papers_dir=papers_dir)
    filename = index[paper.source_id].filename
    paths = ArtifactPaths.for_source(papers_dir, paper.source_id, manifest_root=manifest_dir, pdf_filename=filename)
    write_bytes_atomic(paths.pdf, content)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = load_json_object(paths.download_manifest) or {"schema_version": "1.1", "attempts": []}
    attempts = list(manifest.get("attempts") or [])
    attempts.append({
        "resolver": f"located-source-browser:{platform.name}", "platform": platform.name, "status": "downloaded",
        "resolution_status": "resolved", "reason": "operator_located_official_page",
        "identity_provenance": [*evidence, "pdf_title_text_match" if named else "pdf_image_only_landing_identity"],
    })
    manifest.update({
        "schema_version": "1.1",
        "status": "downloaded",
        "resolver": {"name": f"located-source-browser:{platform.name}", "is_open_access": bool(open_access), "license": None,
                     "version": None, "landing_url": landing, "download_url": None},
        "pdf": {"path": paths.pdf.name, "sha256": sha256_file(paths.pdf), "bytes": len(content), "page_count": page_count,
                "identity_status": "located_source_landing_and_pdf_title_match" if named else "located_source_landing_title_match",
                "matched_doi": paper.doi, "matched_source_id": paper.source_id, "content_type": "application/pdf"},
        "attempts": attempts, "updated_at": now, "downloaded_at": now, "last_error": None, "provider_scope": "located",
    })
    manifest.setdefault("started_at", now)
    if not isinstance(manifest.get("paper"), dict) or manifest["paper"].get("source_id") != paper.source_id:
        manifest["paper"] = {"database_id": paper.database_id, "source": paper.source, "source_id": paper.source_id,
                             "title": paper.title, "doi": paper.doi, "publication_year": paper.publication_year}
    write_json_atomic(paths.download_manifest, manifest)
    if not download_artifact_is_intact(paper, paths):
        raise LocatedPdfError("storage_error", "the stored PDF does not satisfy the intact-artifact contract")
    return paths


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def located_source_ids(sources_path: Path = DEFAULT_SOURCES) -> set[str]:
    if not sources_path.is_file():
        return set()
    return {source["source_id"] for source in json.loads(sources_path.read_text(encoding="utf-8")).get("sources", [])}


def run(
    *, sources_path: Path = DEFAULT_SOURCES, only: set[str] | None = None, platforms: set[str] | None = None,
    headed: bool = False, login_wait: int = LOGIN_WAIT_SECONDS, allow_window: bool = True,
    papers_dir: Path | None = None, manifest_dir: Path | None = None, database_url: str | None = None,
) -> dict[str, Any]:
    """Download every still-missing paper of the table.

    ``allow_window=False`` (unattended runs) never opens a browser window: the
    platforms that need one are reported as ``window_required`` instead.
    """

    only, platforms = only or set(), platforms or set()
    settings = load_settings(validate=False)
    papers_dir, manifest_dir = papers_dir or default_papers_dir(), manifest_dir or default_manifest_dir()
    table = json.loads(sources_path.read_text(encoding="utf-8"))["sources"]
    wanted = [s for s in table if (not only or s["source_id"] in only) and (not platforms or s["platform"] in platforms)]
    engine = create_engine(database_url or settings.database_url)
    candidates = {p.source_id: p for p in iter_paper_candidates(engine, include_missing_doi=True, source_ids=[s["source_id"] for s in wanted])}
    report: dict[str, Any] = {"downloaded": [], "already": [], "failed": {}}
    by_platform: dict[str, list[Mapping[str, Any]]] = {}
    for source in wanted:
        paper = candidates.get(source["source_id"])
        if paper is None:
            report["failed"][source["source_id"]] = "not_in_database"
            continue
        index = load_json_object(manifest_dir / "paper_files.json") or {}
        filename = next((e["filename"] for e in index.get("papers", []) if e.get("source_id") == paper.source_id), None)
        if filename:
            paths = ArtifactPaths.for_source(papers_dir, paper.source_id, manifest_root=manifest_dir, pdf_filename=filename)
            if download_artifact_is_intact(paper, paths):
                report["already"].append(paper.source_id)
                continue
        by_platform.setdefault(source["platform"], []).append(source)
    queue = [(name, items, headed) for name, items in by_platform.items()]
    while queue:
        name, items, show_window = queue.pop(0)
        platform = PLATFORMS[name]
        show_window = show_window or platform.headed
        needs_window: list[Mapping[str, Any]] = []
        if show_window and not allow_window:
            for source in items:
                report["failed"][source["source_id"]] = (
                    f"window_required: {platform.label}은 브라우저 창이 필요합니다 "
                    "(대화형 터미널에서 `python3 main.py -pdf` 실행)"
                )
            continue
        log(f"{platform.label}: {len(items)}편 시작 ({'창 표시' if show_window else '백그라운드'})")
        try:
            with Session(platform, headed=show_window, login_wait=login_wait) as session:
                for source in items:
                    paper = candidates[source["source_id"]]
                    try:
                        content, landing, evidence = handle(session, paper, source)
                        paths = store(paper, content, platform=platform, landing=landing, evidence=evidence,
                                      open_access=bool(source.get("open_access")), papers_dir=papers_dir, manifest_dir=manifest_dir)
                        report["downloaded"].append(paper.source_id)
                        log(f"✔ {paper.source_id} 저장: {paths.pdf.name} ({len(content):,} bytes)")
                    except LocatedPdfError as exc:
                        if exc.code == "login_required" and not show_window:
                            needs_window.append(source)  # retry below with the window shown
                            continue
                        report["failed"][paper.source_id] = f"{exc.code}: {exc}"
                        log(f"✘ {paper.source_id} 실패: {exc.code}: {exc}")
                    except Exception as exc:  # noqa: BLE001 - one paper must not stop the platform
                        report["failed"][paper.source_id] = f"{type(exc).__name__}: {str(exc)[:200]}"
                        log(f"✘ {paper.source_id} 오류: {type(exc).__name__}: {str(exc)[:200]}")
                    time.sleep(3)
        except Exception as exc:  # noqa: BLE001 - browser could not start for this platform
            for source in items:
                report["failed"].setdefault(source["source_id"], f"browser_error: {type(exc).__name__}: {str(exc)[:160]}")
            log(f"{platform.label} 브라우저 오류: {type(exc).__name__}: {str(exc)[:200]}")
        if needs_window:
            queue.insert(0, (name, needs_window, True))
    log(f"완료: 받음 {len(report['downloaded'])} / 이미 있음 {len(report['already'])} / 실패 {len(report['failed'])}")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="위치가 확인된 제공처에서 원문 PDF를 내려받습니다")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--only", default="", help="쉼표로 구분한 source_id")
    parser.add_argument("--platforms", default="", help=f"쉼표로 구분한 플랫폼 ({', '.join(PLATFORMS)})")
    parser.add_argument("--headed", action="store_true", help="모든 플랫폼에서 브라우저 창을 표시")
    parser.add_argument("--login-wait", type=int, default=LOGIN_WAIT_SECONDS, help="로그인을 기다리는 최대 시간(초)")
    args = parser.parse_args(argv)
    platforms = {p.strip() for p in args.platforms.split(",") if p.strip()}
    unknown = platforms - set(PLATFORMS)
    if unknown:
        parser.error(f"unknown platforms: {sorted(unknown)}")
    report = run(sources_path=args.sources, only={s.strip() for s in args.only.split(",") if s.strip()},
                 platforms=platforms, headed=args.headed, login_wait=args.login_wait)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
