"""Stable artifact paths and crash-safe small-file writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    paper_filename_for_source,
    validate_pdf_filename,
)


_SAFE_SOURCE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_source_stem(source_id: str) -> str:
    """Return a filename-safe stable identifier, never a user-provided title."""

    original = source_id.strip()
    cleaned = _SAFE_SOURCE_ID.sub("_", original).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unsafe or empty source_id: {source_id!r}")
    if cleaned == original and len(cleaned) <= 160:
        return cleaned
    suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:148]}--{suffix}"


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """Paths for one paper with binary and state artifacts kept separate.

    ``root`` is the PDF directory. ``manifest_root`` stores manifests, locks,
    extracted text, and analysis state. When ``manifest_root`` is omitted the
    historical co-located layout is retained for backward compatibility.
    """

    root: Path
    stem: str
    manifest_root: Path | None = None
    pdf_filename: str | None = None

    @classmethod
    def for_source(
        cls,
        root: Path,
        source_id: str,
        *,
        manifest_root: Path | None = None,
        pdf_filename: str | None = None,
        allow_unindexed_pdf: bool = False,
    ) -> "ArtifactPaths":
        root = Path(root)
        state_root = Path(manifest_root) if manifest_root is not None else root
        if pdf_filename is not None:
            validate_pdf_filename(pdf_filename)
            resolved_pdf_filename = pdf_filename
        else:
            resolved_pdf_filename = paper_filename_for_source(
                state_root / PAPER_FILE_INDEX_NAME,
                source_id.strip(),
                allow_missing=allow_unindexed_pdf,
            )
        return cls(
            root=root,
            stem=safe_source_stem(source_id),
            manifest_root=Path(manifest_root) if manifest_root is not None else None,
            pdf_filename=resolved_pdf_filename,
        )

    @property
    def state_root(self) -> Path:
        return self.manifest_root or self.root

    @property
    def pdf(self) -> Path:
        return self.root / (self.pdf_filename or f"{self.stem}.pdf")

    @property
    def download_manifest(self) -> Path:
        return self.state_root / f"{self.stem}.download.json"

    @property
    def text(self) -> Path:
        return self.state_root / f"{self.stem}.txt"

    @property
    def analysis(self) -> Path:
        return self.state_root / f"{self.stem}.json"

    @property
    def analysis_error(self) -> Path:
        return self.state_root / f"{self.stem}.analysis-error.json"

    @property
    def lock(self) -> Path:
        return self.state_root / f".{self.stem}.lock"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else None
    temporary_token = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".write-{temporary_token}-",
        suffix=".part",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, content: str) -> None:
    write_bytes_atomic(path, content.encode("utf-8"))


def write_json_atomic(path: Path, value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    write_text_atomic(path, serialized)


def load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
