"""Crash-safe, deterministic PDF-to-text extraction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PaperPipeline.storage import write_text_atomic


class TextExtractionError(RuntimeError):
    """Base class for PDF text-extraction failures."""


class PdfDependencyError(TextExtractionError):
    """Raised when the optional pypdf dependency is unavailable."""


class InvalidPdfError(TextExtractionError):
    """Raised when a file is missing, corrupt, or cannot be parsed as a PDF."""


class EncryptedPdfError(TextExtractionError):
    """Raised when an encrypted PDF is intentionally rejected."""


class InsufficientTextError(TextExtractionError):
    """Raised when a PDF has too little machine-readable text."""


@dataclass(frozen=True, slots=True)
class TextExtractionResult:
    """Metadata for one atomically persisted text artifact.

    ``raw_char_count`` and ``written_char_count`` count normalized page content
    only; ``output_char_count`` additionally includes page markers and
    separators. ``sha256`` is the digest of the UTF-8 output bytes.
    """

    source_path: Path
    output_path: Path
    page_count: int
    raw_char_count: int
    written_char_count: int
    output_char_count: int
    page_char_counts: tuple[int, ...]
    written_page_char_counts: tuple[int, ...]
    truncated: bool
    sha256: str

    @property
    def extracted_char_count(self) -> int:
        return self.raw_char_count

    @property
    def char_count(self) -> int:
        return self.written_char_count

    @property
    def text_sha256(self) -> str:
        return self.sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path),
            "output_path": str(self.output_path),
            "page_count": self.page_count,
            "raw_char_count": self.raw_char_count,
            "written_char_count": self.written_char_count,
            "output_char_count": self.output_char_count,
            "page_char_counts": list(self.page_char_counts),
            "written_page_char_counts": list(self.written_page_char_counts),
            "truncated": self.truncated,
            "sha256": self.sha256,
        }


def _load_pdf_reader() -> Any:
    """Import pypdf only when extraction is actually requested."""

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfDependencyError(
            "PDF extraction requires the optional 'pypdf' package"
        ) from exc
    return PdfReader


def _normalize_page_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    # A paper is untrusted data. Prevent a literal line in its body from being
    # confused with the extractor's structural page delimiter.
    return re.sub(
        r"(?m)^=== PAGE ([1-9][0-9]*) ===$",
        r"=== PAPER-TEXT PAGE \1 ===",
        normalized,
    )


def _render_pages(page_texts: list[str]) -> str:
    blocks = [
        f"=== PAGE {page_number} ===\n{text}"
        for page_number, text in enumerate(page_texts, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def _balanced_page_budgets(lengths: list[int], total_budget: int) -> list[int]:
    """Water-fill a content budget across pages in stable page order."""

    budgets = [0] * len(lengths)
    active = [index for index, length in enumerate(lengths) if length > 0]
    remaining = min(total_budget, sum(lengths))

    while active and remaining > 0:
        share, extra = divmod(remaining, len(active))
        if share == 0:
            for index in active[:extra]:
                budgets[index] += 1
            remaining = 0
            break

        spent = 0
        still_active: list[int] = []
        for position, index in enumerate(active):
            requested = share + (1 if position < extra else 0)
            available = lengths[index] - budgets[index]
            granted = min(requested, available)
            budgets[index] += granted
            spent += granted
            if budgets[index] < lengths[index]:
                still_active.append(index)

        if spent == 0:
            break
        remaining -= spent
        active = still_active

    return budgets


def _truncate_pages(page_texts: list[str], max_chars: int | None) -> tuple[list[str], bool]:
    if max_chars is None:
        return list(page_texts), False
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise TypeError("max_chars must be an integer or None")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    marker_overhead = len(_render_pages([""] * len(page_texts)))
    if max_chars < marker_overhead:
        raise ValueError(
            f"max_chars={max_chars} cannot represent all {len(page_texts)} "
            f"page markers; at least {marker_overhead} is required"
        )

    rendered_length = marker_overhead + sum(len(page_text) for page_text in page_texts)
    if rendered_length <= max_chars:
        return list(page_texts), False

    content_budget = max_chars - marker_overhead
    budgets = _balanced_page_budgets(
        [len(page_text) for page_text in page_texts],
        content_budget,
    )
    truncated_pages = [
        page_text[:page_budget]
        for page_text, page_budget in zip(page_texts, budgets, strict=True)
    ]
    return truncated_pages, True


def extract_pdf_text(
    pdf_path: str | Path,
    output_path: str | Path,
    *,
    max_chars: int | None = None,
    min_chars: int = 1,
) -> TextExtractionResult:
    """Extract a PDF into a deterministic, page-marked UTF-8 text artifact.

    Encrypted documents are rejected even if they use an empty password.
    ``min_chars`` is measured after whitespace removal and before truncation.
    When ``max_chars`` is set, its budget includes markers and separators; a
    stable water-filling allocation truncates page content while retaining a
    marker for every source page.
    """

    source = Path(pdf_path)
    destination = Path(output_path)

    if isinstance(min_chars, bool) or not isinstance(min_chars, int):
        raise TypeError("min_chars must be an integer")
    if min_chars < 1:
        raise ValueError("min_chars must be at least 1")
    if not source.is_file():
        raise InvalidPdfError(f"PDF does not exist or is not a file: {source}")
    try:
        if source.resolve() == destination.resolve():
            raise ValueError("PDF input and text output paths must differ")
    except OSError:
        # Resolution can fail for a not-yet-created destination; absolute() is
        # still sufficient to prevent the obvious same-path overwrite.
        if source.absolute() == destination.absolute():
            raise ValueError("PDF input and text output paths must differ")

    PdfReader = _load_pdf_reader()
    try:
        reader = PdfReader(str(source), strict=False)
    except Exception as exc:
        raise InvalidPdfError(f"unable to parse PDF {source}: {exc}") from exc

    try:
        if bool(reader.is_encrypted):
            raise EncryptedPdfError(f"encrypted PDF is not supported: {source}")
        page_count = len(reader.pages)
    except EncryptedPdfError:
        raise
    except Exception as exc:
        raise InvalidPdfError(f"unable to inspect PDF {source}: {exc}") from exc

    if page_count == 0:
        raise InvalidPdfError(f"PDF contains no pages: {source}")

    page_texts: list[str] = []
    raw_page_char_counts: list[int] = []
    safety_truncated = False
    raw_char_count = 0
    per_page_safety_limit: int | None = None
    if max_chars is not None:
        total_safety_limit = max(2_000_000, max_chars * 4)
        per_page_safety_limit = max(1_000, total_safety_limit // page_count)
    for page_index in range(page_count):
        try:
            extracted = reader.pages[page_index].extract_text()
        except Exception as exc:
            raise InvalidPdfError(
                f"unable to extract page {page_index + 1} from {source}: {exc}"
            ) from exc
        normalized = _normalize_page_text(extracted)
        raw_char_count += len(normalized)
        raw_page_char_counts.append(len(normalized))
        if per_page_safety_limit is not None and len(normalized) > per_page_safety_limit:
            normalized = normalized[:per_page_safety_limit]
            safety_truncated = True
        page_texts.append(normalized)

    meaningful_char_count = sum(
        len("".join(page_text.split())) for page_text in page_texts
    )
    if meaningful_char_count < min_chars:
        raise InsufficientTextError(
            f"PDF has {meaningful_char_count} non-whitespace text characters; "
            f"at least {min_chars} required: {source}"
        )

    written_pages, output_truncated = _truncate_pages(page_texts, max_chars)
    output_text = _render_pages(written_pages)
    output_bytes = output_text.encode("utf-8")
    digest = hashlib.sha256(output_bytes).hexdigest()

    write_text_atomic(destination, output_text)

    page_char_counts = tuple(raw_page_char_counts)
    written_page_char_counts = tuple(len(page_text) for page_text in written_pages)
    return TextExtractionResult(
        source_path=source,
        output_path=destination,
        page_count=page_count,
        raw_char_count=raw_char_count,
        written_char_count=sum(written_page_char_counts),
        output_char_count=len(output_text),
        page_char_counts=page_char_counts,
        written_page_char_counts=written_page_char_counts,
        truncated=safety_truncated or output_truncated,
        sha256=digest,
    )


def extract_pdf_to_text(
    pdf_path: str | Path,
    output_path: str | Path,
    *,
    max_chars: int | None = None,
    min_chars: int = 1,
) -> TextExtractionResult:
    """Compatibility alias for :func:`extract_pdf_text`."""

    return extract_pdf_text(
        pdf_path,
        output_path,
        max_chars=max_chars,
        min_chars=min_chars,
    )


# Alternate exception names read naturally at call sites and retain backwards
# compatibility if a caller adopted these names during early pipeline work.
PdfEncryptedError = EncryptedPdfError
PdfInvalidError = InvalidPdfError
