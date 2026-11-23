"""Strict loading of the Level-3 digital-forensics taxonomy."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


# A technique code may be Level 2 or deeper, but every suffix is a two-digit
# component. Only codes with exactly two separators are selected as Level 3.
_TECHNIQUE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d{2}(?:-\d{2})*$")


class TaxonomyError(ValueError):
    """Base class for taxonomy validation failures."""


class MalformedTaxonomyError(TaxonomyError):
    """Raised when the taxonomy JSON does not have the expected structure."""


class DuplicateTaxonomyCodeError(MalformedTaxonomyError):
    """Raised when a JSON key or technique code is duplicated."""


class EmptyTaxonomyError(MalformedTaxonomyError):
    """Raised when no exact Level-3 technique is available."""


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True, slots=True)
class TaxonomyEntry:
    """One exact Level-3 taxonomy technique."""

    code: str
    name: str
    group: str

    @property
    def level(self) -> int:
        return self.code.count("-") + 1


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """Validated taxonomy plus a digest of the exact source bytes."""

    entries: tuple[TaxonomyEntry, ...]
    sha256: str
    source_path: Path

    def __iter__(self) -> Iterator[TaxonomyEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def by_code(self) -> Mapping[str, TaxonomyEntry]:
        return MappingProxyType({entry.code: entry for entry in self.entries})

    @property
    def code_to_name(self) -> Mapping[str, str]:
        return MappingProxyType({entry.code: entry.name for entry in self.entries})

    @property
    def version_hash(self) -> str:
        """Compatibility name for the source-file SHA-256."""

        return self.sha256

    def get(self, code: str) -> TaxonomyEntry | None:
        return self.by_code.get(code)

    def require(self, code: str) -> TaxonomyEntry:
        try:
            return self.by_code[code]
        except KeyError as exc:
            raise KeyError(f"unknown Level-3 taxonomy code: {code}") from exc

    def to_prompt_block(self) -> str:
        """Render entries in source order while retaining their group headings."""

        lines: list[str] = []
        previous_group: str | None = None
        for entry in self.entries:
            if entry.group != previous_group:
                if lines:
                    lines.append("")
                lines.append(f"# {entry.group}")
                previous_group = entry.group
            lines.append(f"{entry.code}\t{entry.name}")
        return "\n".join(lines)


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _decode_taxonomy(raw_bytes: bytes, path: Path) -> dict[str, object]:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedTaxonomyError(f"taxonomy is not valid UTF-8: {path}") from exc

    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except _DuplicateJsonKey as exc:
        raise DuplicateTaxonomyCodeError(
            f"duplicate JSON key {exc.key!r} in taxonomy: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MalformedTaxonomyError(f"invalid taxonomy JSON: {path}: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise MalformedTaxonomyError(f"taxonomy root must be a JSON object: {path}")
    return payload


def load_taxonomy(path: str | Path) -> Taxonomy:
    """Load exact Level-3 codes and validate the complete taxonomy structure.

    Valid Level-2 and Level-4/5 codes are accepted but not returned. A code is
    exact Level 3 only when it has exactly two ``-`` separators. The SHA-256 is
    calculated from the exact source-file bytes so it can be used as a
    provenance/version identifier.
    """

    source_path = Path(path)
    try:
        raw_bytes = source_path.read_bytes()
    except OSError as exc:
        raise TaxonomyError(f"unable to read taxonomy: {source_path}: {exc}") from exc

    payload = _decode_taxonomy(raw_bytes, source_path)
    entries: list[TaxonomyEntry] = []
    seen_codes: dict[str, str] = {}

    for group, techniques in payload.items():
        if group.startswith("_"):
            continue
        if not group.strip() or _has_prompt_control(group):
            raise MalformedTaxonomyError("taxonomy group name must not be empty")
        if not isinstance(techniques, dict):
            raise MalformedTaxonomyError(
                f"taxonomy group {group!r} must contain a JSON object"
            )

        for code, name in techniques.items():
            if not _TECHNIQUE_CODE_RE.fullmatch(code):
                raise MalformedTaxonomyError(
                    f"malformed taxonomy code {code!r} in group {group!r}"
                )
            if (
                not isinstance(name, str)
                or not name.strip()
                or _has_prompt_control(name)
            ):
                raise MalformedTaxonomyError(
                    f"taxonomy name for {code!r} must be a non-empty string"
                )
            previous_group = seen_codes.get(code)
            if previous_group is not None:
                raise DuplicateTaxonomyCodeError(
                    f"duplicate taxonomy code {code!r} in groups "
                    f"{previous_group!r} and {group!r}"
                )
            seen_codes[code] = group

            if code.count("-") == 2:
                entries.append(TaxonomyEntry(code=code, name=name, group=group))

    if not entries:
        raise EmptyTaxonomyError(
            f"taxonomy contains no exact Level-3 technique codes: {source_path}"
        )

    return Taxonomy(
        entries=tuple(entries),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        source_path=source_path,
    )


def load_level3_taxonomy(path: str | Path) -> Taxonomy:
    """Explicit alias for :func:`load_taxonomy`."""

    return load_taxonomy(path)


def _has_prompt_control(value: str) -> bool:
    return any(character in "\r\n\t" or ord(character) < 32 for character in value)
