from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from PaperPipeline.text_extractor import (
    EncryptedPdfError,
    InsufficientTextError,
    InvalidPdfError,
    extract_pdf_text,
)


def _pdf_escape(text: str) -> bytes:
    return (
        text.encode("latin-1")
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
    )


def _write_text_pdf(path: Path, page_texts: list[str]) -> None:
    """Write a small, standards-compliant PDF with one text stream per page."""

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
            + _pdf_escape(text)
            + b") Tj\nET\n"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream"
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode("ascii")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (font_id + 1)
    for object_id in range(1, font_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {font_id + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, font_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(pdf)


def _write_blank_pdf(path: Path, *, encrypted: bool = False) -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if encrypted:
        writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)


def test_import_is_lazy_and_does_not_load_pypdf() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import PaperPipeline.text_extractor; "
                "raise SystemExit(1 if 'pypdf' in sys.modules else 0)"
            ),
        ],
        cwd=repository_root,
        check=False,
    )
    assert completed.returncode == 0


def test_extracts_page_marked_text_atomically_with_metadata(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    output_path = tmp_path / "paper.txt"
    _write_text_pdf(pdf_path, ["Alpha first page", "Beta second page"])

    result = extract_pdf_text(pdf_path, output_path, min_chars=5)
    output_bytes = output_path.read_bytes()
    output_text = output_bytes.decode("utf-8")

    assert output_text == (
        "=== PAGE 1 ===\nAlpha first page\n\n"
        "=== PAGE 2 ===\nBeta second page\n"
    )
    assert result.source_path == pdf_path
    assert result.output_path == output_path
    assert result.page_count == 2
    assert result.page_char_counts == (16, 16)
    assert result.raw_char_count == 32
    assert result.written_char_count == 32
    assert result.output_char_count == len(output_text)
    assert result.truncated is False
    assert result.sha256 == hashlib.sha256(output_bytes).hexdigest()
    assert not list(tmp_path.glob(f".{output_path.name}.*.part"))


def test_truncation_is_deterministic_and_keeps_every_page_marker(tmp_path: Path) -> None:
    pdf_path = tmp_path / "long.pdf"
    first_output = tmp_path / "first.txt"
    second_output = tmp_path / "second.txt"
    _write_text_pdf(pdf_path, ["A" * 80, "B" * 6, "C" * 80])

    first = extract_pdf_text(pdf_path, first_output, max_chars=90)
    second = extract_pdf_text(pdf_path, second_output, max_chars=90)
    text = first_output.read_text(encoding="utf-8")

    assert first.truncated is True
    assert first.output_char_count == 90
    assert first.output_char_count <= 90
    assert first.written_page_char_counts == second.written_page_char_counts
    assert all(count > 0 for count in first.written_page_char_counts)
    assert first_output.read_bytes() == second_output.read_bytes()
    assert first.sha256 == second.sha256
    for page_number in range(1, 4):
        assert f"=== PAGE {page_number} ===" in text


def test_rejects_encrypted_invalid_and_textless_pdfs_without_overwrite(
    tmp_path: Path,
) -> None:
    encrypted = tmp_path / "encrypted.pdf"
    invalid = tmp_path / "invalid.pdf"
    blank = tmp_path / "blank.pdf"
    output = tmp_path / "output.txt"
    _write_blank_pdf(encrypted, encrypted=True)
    _write_blank_pdf(blank)
    invalid.write_bytes(b"this is not a PDF")
    output.write_text("keep me", encoding="utf-8")

    with pytest.raises(EncryptedPdfError):
        extract_pdf_text(encrypted, output)
    assert output.read_text(encoding="utf-8") == "keep me"

    with pytest.raises(InvalidPdfError):
        extract_pdf_text(invalid, output)
    assert output.read_text(encoding="utf-8") == "keep me"

    with pytest.raises(InsufficientTextError):
        extract_pdf_text(blank, output)
    assert output.read_text(encoding="utf-8") == "keep me"


def test_rejects_budget_that_cannot_hold_all_page_markers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    _write_text_pdf(pdf_path, ["one", "two"])

    with pytest.raises(ValueError, match="page markers"):
        extract_pdf_text(pdf_path, tmp_path / "out.txt", max_chars=5)


def test_rejects_same_input_and_output_path(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_text_pdf(pdf_path, ["content"])

    with pytest.raises(ValueError, match="paths must differ"):
        extract_pdf_text(pdf_path, pdf_path)
