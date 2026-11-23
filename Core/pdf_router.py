"""Journal-aware routing policy for permission-gated PDF providers.

The ordered collector still hands every unresolved source ID to every stage.
This module decides which rows a site-specific runner may actually attempt.
That distinction matters for law journals: a generic publisher name such as
``법학연구소`` is not enough to identify the correct repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


ROUTED_PDF_PROVIDERS = frozenset({"kyobo", "jams", "sciencedirect"})
PDF_ROUTES_SCHEMA_VERSION = 1
DEFAULT_PDF_ROUTES_PATH = Path("Processor/refs/pdf_routes.json")
_SPACE_RE = re.compile(r"\s+")


class PdfRouteCandidate(Protocol):
    journal_id: str | None
    institution_id: str | None
    doi: str | None
    doi_original: str | None


@dataclass(frozen=True, slots=True)
class PdfRoute:
    """One AND-composed identity rule; rules for a provider are OR-composed."""

    provider: str
    journal: str | None = None
    institution: str | None = None
    doi_prefix: str | None = None

    def __post_init__(self) -> None:
        provider = _normalized_text(self.provider)
        if provider not in ROUTED_PDF_PROVIDERS:
            raise ValueError(f"unsupported routed PDF provider: {self.provider!r}")
        journal = _optional_identity(self.journal)
        institution = _optional_identity(self.institution)
        doi_prefix = _optional_doi_prefix(self.doi_prefix)
        if journal is None and institution is None and doi_prefix is None:
            raise ValueError("a PDF route must contain an identity condition")
        # Institution-only routing is deliberately forbidden.  Names such as
        # 법학연구소 occur at many universities and cannot identify a platform.
        if journal is None and institution is not None and doi_prefix is None:
            raise ValueError("institution-only PDF routes are ambiguous")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "journal", journal)
        object.__setattr__(self, "institution", institution)
        object.__setattr__(self, "doi_prefix", doi_prefix)

    def matches(self, paper: PdfRouteCandidate) -> bool:
        if self.journal is not None and _optional_identity(paper.journal_id) != self.journal:
            return False
        if (
            self.institution is not None
            and _optional_identity(paper.institution_id) != self.institution
        ):
            return False
        if self.doi_prefix is not None:
            doi = _candidate_doi(paper)
            if doi is None or not doi.startswith(self.doi_prefix):
                return False
        return True


def load_pdf_routes(path: Path = DEFAULT_PDF_ROUTES_PATH) -> tuple[PdfRoute, ...]:
    """Load and strictly validate the small operator-maintained route table."""

    if path.is_symlink():
        raise ValueError(f"PDF route file must not be a symlink: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read PDF route file: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PDF route JSON: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != PDF_ROUTES_SCHEMA_VERSION:
        raise ValueError("unsupported PDF route schema")
    if set(raw) != {"schema_version", "routes"} or not isinstance(raw.get("routes"), list):
        raise ValueError("PDF route document must contain only schema_version and routes")

    routes: list[PdfRoute] = []
    seen: set[PdfRoute] = set()
    for index, item in enumerate(raw["routes"]):
        if not isinstance(item, dict):
            raise ValueError(f"PDF route #{index + 1} must be an object")
        allowed = {"provider", "journal", "institution", "doi_prefix"}
        if set(item) - allowed or "provider" not in item:
            raise ValueError(f"PDF route #{index + 1} has invalid fields")
        route = PdfRoute(
            provider=item["provider"],
            journal=item.get("journal"),
            institution=item.get("institution"),
            doi_prefix=item.get("doi_prefix"),
        )
        if route in seen:
            raise ValueError(f"duplicate PDF route #{index + 1}")
        seen.add(route)
        routes.append(route)
    return tuple(routes)


def paper_is_eligible_for_provider(
    paper: PdfRouteCandidate,
    provider: str,
    routes: Iterable[PdfRoute],
) -> bool:
    """Return whether an unresolved row may be attempted by ``provider``."""

    normalized_provider = _normalized_text(provider)
    if normalized_provider not in ROUTED_PDF_PROVIDERS:
        return True
    return any(
        route.provider == normalized_provider and route.matches(paper)
        for route in routes
    )


def _normalized_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("route text values must be non-empty strings")
    return _SPACE_RE.sub(" ", value).strip().casefold()


def _optional_identity(value: Any) -> str | None:
    if value is None:
        return None
    return _normalized_text(value)


def _optional_doi_prefix(value: Any) -> str | None:
    if value is None:
        return None
    prefix = _normalized_text(value)
    for leading in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/"):
        if prefix.startswith(leading):
            prefix = prefix[len(leading) :]
            break
    if not prefix.startswith("10.") or "/" not in prefix:
        raise ValueError("doi_prefix must be a canonical DOI prefix")
    return prefix


def _candidate_doi(paper: PdfRouteCandidate) -> str | None:
    raw = paper.doi or paper.doi_original
    if not raw or not isinstance(raw, str):
        return None
    value = raw.strip().casefold()
    for leading in (
        "https://doi.org/",
        "http://doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if value.startswith(leading):
            value = value[len(leading) :].strip()
            break
    return value or None


__all__ = [
    "DEFAULT_PDF_ROUTES_PATH",
    "PDF_ROUTES_SCHEMA_VERSION",
    "ROUTED_PDF_PROVIDERS",
    "PdfRoute",
    "PdfRouteCandidate",
    "load_pdf_routes",
    "paper_is_eligible_for_provider",
]
