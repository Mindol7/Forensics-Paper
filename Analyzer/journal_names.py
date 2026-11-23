"""Journal names for rows where the KCI API returned a serial id instead of a name.

A handful of early records carry ``SER000003109``-style serial ids in
``journal_id``.  ``Processor/refs/journal_serials.json`` is the reviewed name
table for them; anything not listed stays unresolved (``None``) so that a
caller shows it as missing rather than inventing a name.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

JOURNAL_SERIALS = Path(__file__).resolve().parents[1] / "Processor" / "refs" / "journal_serials.json"
SERIAL_RE = re.compile(r"SER[0-9]+\Z")


@lru_cache(maxsize=4)
def _table(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    journals = payload.get("journals", {}) if isinstance(payload, dict) else {}
    return {
        serial: str(entry["name"]).strip()
        for serial, entry in journals.items()
        if SERIAL_RE.fullmatch(str(serial)) and isinstance(entry, dict) and str(entry.get("name") or "").strip()
    }


def is_serial(value: str | None) -> bool:
    return bool(value) and SERIAL_RE.fullmatch(value.strip()) is not None


def resolve_journal_name(value: str | None, *, path: Path | None = None) -> str | None:
    """The journal's name: ``value`` itself, the reviewed name of a serial id, or ``None`` if unknown."""

    text = (value or "").strip()
    if not text:
        return None
    if not SERIAL_RE.fullmatch(text):
        return text
    return _table(path or JOURNAL_SERIALS).get(text)
