"""Portable title-based PDF filenames and their source-id index.

PDFs are pleasant to browse when their filenames are paper titles, while the
rest of the pipeline still needs a stable KCI ``source_id``.  This module keeps
those two concerns separate through ``Analyzer/Manifest/paper_files.json``.
State artifacts (download manifests, locks, extracted text) continue to use
the stable source id; only the PDF filename is title based.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


PAPER_FILE_INDEX_NAME = "paper_files.json"
PAPER_FILE_INDEX_SCHEMA = "1.0"
PAPER_FILENAME_POLICY = "title-nfc-portable-v1"
MAX_FILENAME_BYTES = 255

_PORTABLE_REPLACEMENTS = str.maketrans(
    {
        "<": "‹",
        ">": "›",
        ":": "∶",
        '"': "″",
        "/": "∕",
        "\\": "⧵",
        "|": "∣",
        "?": "❓",
        "*": "∗",
    }
)
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class PaperFileIndexError(ValueError):
    """Raised when a title filename or paper-file index is unsafe."""


class PaperFileIndexMissingSourceError(PaperFileIndexError):
    """Raised when an existing index has no entry for a requested source id."""


@dataclass(frozen=True, slots=True)
class PaperFileRecord:
    """Stable mapping; ``title`` is provenance from filename allocation time."""

    source_id: str
    title: str
    filename: str


def _filename_key(filename: str) -> str:
    """Return a conservative cross-platform collision key."""

    return unicodedata.normalize("NFKC", filename).casefold().rstrip(" .")


def _truncate_utf8(text: str, byte_limit: int) -> str:
    if byte_limit < 1:
        raise PaperFileIndexError("filename byte limit is too small")
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    candidate = encoded[:byte_limit]
    while candidate:
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            candidate = candidate[:-1]
    raise PaperFileIndexError("unable to truncate filename safely")


def portable_title(title: str) -> str:
    """Normalize a paper title for a portable filename without losing words."""

    if not isinstance(title, str) or not title.strip():
        raise PaperFileIndexError("paper title must be non-empty")
    normalized = unicodedata.normalize("NFC", title.strip())
    normalized = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in normalized
    )
    normalized = normalized.translate(_PORTABLE_REPLACEMENTS).rstrip(" .")
    if not normalized or normalized in {".", ".."}:
        raise PaperFileIndexError("paper title produces an empty filename")
    if normalized.startswith(".") or (
        normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
    ):
        normalized = f"＿{normalized}"
    return normalized


def title_pdf_filename(title: str, *, source_id: str | None = None) -> str:
    """Return a portable ``.pdf`` filename, truncating only when unavoidable."""

    stem = portable_title(title)
    filename = f"{stem}.pdf"
    if len(filename.encode("utf-8")) <= MAX_FILENAME_BYTES:
        return filename

    if not source_id or not source_id.strip():
        raise PaperFileIndexError(
            "source_id is required when a title exceeds the filename byte limit"
        )
    suffix = f"… [{source_id.strip()}].pdf"
    available = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    shortened = _truncate_utf8(stem, available).rstrip(" .")
    if not shortened:
        raise PaperFileIndexError("paper title cannot be shortened safely")
    return f"{shortened}{suffix}"


def validate_pdf_filename(filename: str) -> None:
    if not isinstance(filename, str) or not filename:
        raise PaperFileIndexError("PDF filename must be non-empty")
    if Path(filename).name != filename or filename in {".", ".."}:
        raise PaperFileIndexError(f"unsafe PDF filename: {filename!r}")
    if filename.startswith("."):
        raise PaperFileIndexError(f"hidden PDF filename is not allowed: {filename!r}")
    if not filename.endswith(".pdf"):
        raise PaperFileIndexError(f"PDF filename must end with .pdf: {filename!r}")
    if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise PaperFileIndexError(f"PDF filename exceeds 255 bytes: {filename!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise PaperFileIndexError(f"PDF filename contains a control character: {filename!r}")
    if any(character in '<>:"/\\|?*' for character in filename):
        raise PaperFileIndexError(f"PDF filename is not portable: {filename!r}")
    if filename.rstrip(" .") != filename:
        raise PaperFileIndexError(f"PDF filename has an unsafe suffix: {filename!r}")
    stem = filename[:-4]
    if stem.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise PaperFileIndexError(f"reserved PDF filename: {filename!r}")


def build_paper_file_records(
    papers: Iterable[tuple[str, str]],
) -> tuple[PaperFileRecord, ...]:
    """Build a deterministic, collision-free index for ``(source_id, title)``."""

    inputs: list[tuple[str, str, str]] = []
    seen_source_ids: set[str] = set()
    base_counts: dict[str, int] = {}
    for source_id, title in papers:
        if not isinstance(source_id, str) or not source_id.strip():
            raise PaperFileIndexError("source_id must be non-empty")
        source_id = source_id.strip()
        if source_id in seen_source_ids:
            raise PaperFileIndexError(f"duplicate source_id: {source_id}")
        seen_source_ids.add(source_id)
        base = title_pdf_filename(title, source_id=source_id)
        key = _filename_key(base)
        base_counts[key] = base_counts.get(key, 0) + 1
        inputs.append((source_id, title, base))

    records: list[PaperFileRecord] = []
    used: set[str] = set()
    for source_id, title, base in inputs:
        filename = base
        if base_counts[_filename_key(base)] > 1:
            suffix = f" [{source_id}].pdf"
            stem = base[:-4]
            available = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
            filename = f"{_truncate_utf8(stem, available).rstrip(' .')}{suffix}"
        validate_pdf_filename(filename)
        key = _filename_key(filename)
        if key in used:
            raise PaperFileIndexError(f"PDF filename collision: {filename!r}")
        used.add(key)
        records.append(PaperFileRecord(source_id, title, filename))
    return tuple(records)


def paper_file_index_document(
    records: Iterable[PaperFileRecord],
) -> dict[str, object]:
    ordered = tuple(records)
    return {
        "schema_version": PAPER_FILE_INDEX_SCHEMA,
        "filename_policy": PAPER_FILENAME_POLICY,
        "paper_count": len(ordered),
        "papers": [
            {
                "source_id": record.source_id,
                "title": record.title,
                "filename": record.filename,
            }
            for record in ordered
        ],
    }


def _duplicate_key_guard(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise PaperFileIndexError(f"duplicate JSON key in paper-file index: {key!r}")
        value[key] = item
    return value


def _parse_index(raw: bytes, path: Path) -> tuple[PaperFileRecord, ...]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_key_guard)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperFileIndexError(f"invalid paper-file index: {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "filename_policy",
        "paper_count",
        "papers",
    }:
        raise PaperFileIndexError(f"unexpected paper-file index schema: {path}")
    if value["schema_version"] != PAPER_FILE_INDEX_SCHEMA:
        raise PaperFileIndexError(f"unsupported paper-file index version: {path}")
    if value["filename_policy"] != PAPER_FILENAME_POLICY:
        raise PaperFileIndexError(f"unsupported paper filename policy: {path}")
    raw_papers = value["papers"]
    if not isinstance(raw_papers, list):
        raise PaperFileIndexError(f"paper-file index papers must be an array: {path}")
    if type(value["paper_count"]) is not int or value["paper_count"] != len(raw_papers):
        raise PaperFileIndexError(f"paper-file index count mismatch: {path}")

    records: list[PaperFileRecord] = []
    source_ids: set[str] = set()
    filenames: set[str] = set()
    for position, item in enumerate(raw_papers, start=1):
        if not isinstance(item, dict) or set(item) != {"source_id", "title", "filename"}:
            raise PaperFileIndexError(
                f"invalid paper-file record at position {position}: {path}"
            )
        source_id, title, filename = (
            item["source_id"],
            item["title"],
            item["filename"],
        )
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or source_id != source_id.strip()
        ):
            raise PaperFileIndexError(f"invalid source_id at position {position}: {path}")
        if not isinstance(title, str) or not title.strip():
            raise PaperFileIndexError(f"invalid title at position {position}: {path}")
        if not isinstance(filename, str):
            raise PaperFileIndexError(f"invalid filename at position {position}: {path}")
        validate_pdf_filename(filename)
        key = _filename_key(filename)
        if source_id in source_ids or key in filenames:
            raise PaperFileIndexError(f"duplicate source_id or filename in index: {path}")
        source_ids.add(source_id)
        filenames.add(key)
        records.append(PaperFileRecord(source_id, title, filename))
    return tuple(records)


@lru_cache(maxsize=64)
def _load_cached(
    path_text: str,
    mtime_ns: int,
    ctime_ns: int,
    inode: int,
    size: int,
) -> tuple[PaperFileRecord, ...]:
    del mtime_ns, ctime_ns, inode, size
    path = Path(path_text)
    return _parse_index(path.read_bytes(), path)


def load_paper_file_index(
    path: str | Path,
    *,
    required: bool = True,
) -> dict[str, PaperFileRecord]:
    source = Path(path)
    try:
        stat = source.stat()
    except FileNotFoundError:
        if required:
            raise PaperFileIndexError(f"paper-file index is missing: {source}")
        return {}
    except OSError as exc:
        raise PaperFileIndexError(f"unable to stat paper-file index: {source}: {exc}") from exc
    if source.is_symlink() or not source.is_file():
        raise PaperFileIndexError(f"paper-file index must be a regular file: {source}")
    records = _load_cached(
        str(source.resolve()),
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
        stat.st_size,
    )
    return {record.source_id: record for record in records}


def paper_filename_for_source(
    index_path: str | Path,
    source_id: str,
    *,
    allow_missing: bool = False,
) -> str | None:
    path = Path(index_path)
    if not path.exists():
        return None
    record = load_paper_file_index(path).get(source_id)
    if record is None:
        if allow_missing:
            return None
        raise PaperFileIndexMissingSourceError(
            f"source_id is missing from existing paper-file index: {source_id}"
        )
    return record.filename


def _write_index_atomic(path: Path, records: Iterable[PaperFileRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    content = (
        json.dumps(
            paper_file_index_document(records),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n"
    ).encode("utf-8")
    _parse_index(content, path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".paper-files-", suffix=".part", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _load_cached.cache_clear()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def extend_paper_file_index_if_present(
    index_path: str | Path,
    papers: Iterable[tuple[str, str]],
    *,
    papers_dir: str | Path,
) -> dict[str, PaperFileRecord]:
    """Append newly collected papers while preserving every existing filename.

    Legacy installations without an index retain source-id filenames.  Once a
    title index exists, incremental collection automatically follows the title
    naming policy.
    """

    path = Path(index_path)
    if not path.exists():
        return {}
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load_paper_file_index(path)
        incoming = tuple(papers)
        incoming_ids: set[str] = set()
        additions: list[tuple[str, str]] = []
        for source_id, title in incoming:
            if source_id in incoming_ids:
                raise PaperFileIndexError(
                    f"duplicate incoming source_id: {source_id}"
                )
            incoming_ids.add(source_id)
            if source_id not in current:
                additions.append((source_id, title))
        if not additions:
            return current

        records = list(current.values())
        used = {_filename_key(record.filename) for record in records}
        paper_root = Path(papers_dir)
        for source_id, title in additions:
            filename = title_pdf_filename(title, source_id=source_id)
            key = _filename_key(filename)
            if key in used or (paper_root / filename).exists():
                suffix = f" [{source_id}].pdf"
                stem = filename[:-4]
                available = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
                filename = f"{_truncate_utf8(stem, available).rstrip(' .')}{suffix}"
                key = _filename_key(filename)
            validate_pdf_filename(filename)
            if key in used or (paper_root / filename).exists():
                raise PaperFileIndexError(
                    f"unable to allocate unique PDF filename for {source_id}"
                )
            used.add(key)
            records.append(PaperFileRecord(source_id, title, filename))
        _write_index_atomic(path, records)
        return load_paper_file_index(path)


def remove_paper_file_index_entries(
    index_path: str | Path,
    source_ids: Iterable[str],
    *,
    papers_dir: str | Path,
) -> dict[str, PaperFileRecord]:
    """Drop index entries whose PDF never materialised.

    A filename is reserved before a download so the PDF can be written under
    its title name.  When every provider fails, that reservation must not stay
    in the index: downstream stages define the analysable corpus as "papers in
    the index", so a reservation without a PDF would look like a paper that
    lost its file.  Entries whose PDF exists on disk are always kept.
    """

    path = Path(index_path)
    if not path.exists():
        return {}
    requested = {source_id for source_id in source_ids if source_id}
    if not requested:
        return load_paper_file_index(path)
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load_paper_file_index(path)
        paper_root = Path(papers_dir)
        kept = [
            record
            for record in current.values()
            if record.source_id not in requested or (paper_root / record.filename).exists()
        ]
        if len(kept) == len(current):
            return current
        _write_index_atomic(path, kept)
        return load_paper_file_index(path)


__all__ = [
    "MAX_FILENAME_BYTES",
    "PAPER_FILE_INDEX_NAME",
    "PAPER_FILE_INDEX_SCHEMA",
    "PAPER_FILENAME_POLICY",
    "PaperFileIndexError",
    "PaperFileIndexMissingSourceError",
    "PaperFileRecord",
    "build_paper_file_records",
    "extend_paper_file_index_if_present",
    "load_paper_file_index",
    "paper_file_index_document",
    "paper_filename_for_source",
    "portable_title",
    "remove_paper_file_index_entries",
    "title_pdf_filename",
    "validate_pdf_filename",
]
