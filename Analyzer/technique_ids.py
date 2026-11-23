"""Stage codes of the technique taxonomy.

``Analyzer/LV/Techniques/tech_taxonomy.json`` names its stages with four-letter
codes (``EXAM-04-01``).  Evidence written before 2026-09-19 and the DF-Insight
manifest use the earlier three-letter codes (``EXM-04-01``); the two forms name
the same technique, so identifiers are bridged exactly by stage code.
"""

from __future__ import annotations

LEGACY_STAGE_CODES = {
    "PRE": "PREP", "IDC": "IDEC", "PCP": "PRCP", "COL": "COLL", "PSP": "PSCP",
    "EXM": "EXAM", "ANL": "ANYS", "RPT": "REPT", "CMN": "CMNF", "CVG": "CVGF",
}
_CURRENT_TO_LEGACY = {current: legacy for legacy, current in LEGACY_STAGE_CODES.items()}


def canonical_technique_id(identifier: str) -> str:
    """``EXM-04-01`` -> ``EXAM-04-01`` (already-current ids are returned unchanged)."""

    stage, separator, suffix = identifier.partition("-")
    return f"{LEGACY_STAGE_CODES.get(stage, stage)}{separator}{suffix}"


def legacy_technique_id(identifier: str) -> str:
    """``EXAM-04-01`` -> ``EXM-04-01`` (for sources that still use the old codes)."""

    stage, separator, suffix = identifier.partition("-")
    return f"{_CURRENT_TO_LEGACY.get(stage, stage)}{separator}{suffix}"


__all__ = ["LEGACY_STAGE_CODES", "canonical_technique_id", "legacy_technique_id"]
