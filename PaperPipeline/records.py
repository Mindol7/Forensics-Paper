"""Plain records shared by the PDF download and analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PaperCandidate:
    """The small DB projection needed by the artifact pipeline."""

    database_id: int
    source: str
    source_id: str
    title: str | None
    doi_original: str | None
    doi: str | None
    landing_url: str | None
    abstract: str | None
    publication_year: int | None
    first_page: str | None
    final_page: str | None
    doi_source: str | None = None
    identifier_resolution: dict[str, Any] | None = None
    # Additional, distinct DB titles (for example an English title).  A tuple
    # keeps the projection immutable while ``title`` remains the compatible
    # primary-title field.
    alternate_titles: tuple[str, ...] = ()
    journal_id: str | None = None
    institution_id: str | None = None
    issue_id: str | None = None
    authors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolverCandidate:
    """A public PDF URL proposed by one metadata resolver."""

    url: str
    resolver: str
    landing_url: str | None = None
    license: str | None = None
    version: str | None = None
    is_open_access: bool | None = None
    # Set only when the resolver page itself bound this PDF URL to an exact,
    # normalized DOI (for example, citation_doi + citation_pdf_url).
    verified_doi: str | None = None
    # Set only when an official source binds the URL to the exact KCI ART id.
    verified_source_id: str | None = None
