"""Read-only, keyset-paginated paper projection for the artifact pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from PaperPipeline.records import PaperCandidate


ALLOWED_SOURCE_TABLES = frozenset({"kci_papers", "kci_corpus"})


class InvalidDoiError(ValueError):
    pass


def _normalize_title(value: str | None) -> str | None:
    """Trim a DB title and collapse whitespace without changing its wording."""

    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _project_titles(
    title: str | None,
    title_eng: str | None,
    title_other: str | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Return the compatible primary title and distinct additional titles."""

    primary = _normalize_title(title)
    seen = {primary.casefold()} if primary is not None else set()
    alternates: list[str] = []
    for value in (title_eng, title_other):
        normalized = _normalize_title(value)
        if normalized is None:
            continue
        identity = normalized.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        alternates.append(normalized)
    return primary, tuple(alternates)


def _project_authors(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    authors: list[str] = []
    seen: set[str] = set()
    for item in payload:
        normalized = _normalize_title(item) if isinstance(item, str) else None
        if normalized is None or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        authors.append(normalized)
    return tuple(authors)


def normalize_doi(value: str | None) -> str | None:
    """Convert resolver URLs and ``doi:`` values to a canonical DOI string.

    Case is preserved for provenance. Callers that need an identity key should
    use ``normalize_doi(value).casefold()``.
    """

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise InvalidDoiError("DOI contains a control character")

    lowered = candidate.casefold()
    if lowered.startswith("doi:"):
        candidate = candidate[4:].strip()
    else:
        parsed = urlsplit(candidate)
        if parsed.scheme.casefold() in {"http", "https"}:
            hostname = (parsed.hostname or "").casefold().rstrip(".")
            if hostname not in {"doi.org", "dx.doi.org"}:
                raise InvalidDoiError("URL is not a DOI resolver URL")
            candidate = parsed.path.lstrip("/")

    candidate = unquote(candidate).strip()
    if (
        not candidate
        or any(character.isspace() for character in candidate)
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise InvalidDoiError("DOI is empty or contains whitespace/control characters")
    if not candidate.casefold().startswith("10.") or "/" not in candidate:
        raise InvalidDoiError("DOI must start with 10. and contain a slash")

    prefix, suffix = candidate.split("/", 1)
    registrar = prefix[3:]
    if not registrar.isdigit() or not 4 <= len(registrar) <= 9 or not suffix:
        raise InvalidDoiError("DOI has an invalid registrant prefix or empty suffix")
    return candidate


def count_paper_candidates(
    engine: Engine,
    *,
    source_table: str = "kci_papers",
    include_missing_doi: bool = False,
) -> int:
    table = _validated_table(source_table)
    clauses = ["source = 'kci'"]
    if not include_missing_doi:
        clauses.extend(("doi IS NOT NULL", "trim(doi) <> ''"))
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE {' AND '.join(clauses)}")
            ).scalar_one()
        )


def iter_paper_candidates(
    engine: Engine,
    *,
    source_table: str = "kci_papers",
    include_missing_doi: bool = False,
    source_ids: Sequence[str] | None = None,
    after_id: int = 0,
    limit: int | None = None,
    chunk_size: int = 250,
) -> Iterator[PaperCandidate]:
    """Yield only the columns needed for downloading and analysis.

    The table name is selected from a fixed allowlist; all user values are
    bound parameters. Sessions are not kept open during network operations.
    """

    table = _validated_table(source_table)
    selected_ids = tuple(dict.fromkeys(value.strip() for value in (source_ids or ()) if value.strip()))
    if source_table == "kci_corpus" and limit is None and not selected_ids:
        raise ValueError(
            "kci_corpus contains hundreds of thousands of rows; provide an explicit "
            "limit/source_id selection"
        )
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if limit == 0:
        return

    yielded = 0
    last_id = max(0, int(after_id))
    while limit is None or yielded < limit:
        fetch_size = chunk_size if limit is None else min(chunk_size, limit - yielded)
        clauses = ["id > :last_id", "source = 'kci'"]
        if not include_missing_doi:
            clauses.append("doi IS NOT NULL")
            clauses.append("trim(doi) <> ''")
        if selected_ids:
            clauses.append("source_id IN :source_ids")
        statement = text(
            f"""
            SELECT id, source, source_id, title, title_eng, title_other,
                   doi, url, abstract, journal_id, institution_id, issue_id,
                   authors_json,
                   publication_year, first_page, final_page
            FROM {table}
            WHERE {" AND ".join(clauses)}
            ORDER BY id
            LIMIT :fetch_size
            """
        )
        if selected_ids:
            statement = statement.bindparams(bindparam("source_ids", expanding=True))
        parameters: dict[str, Any] = {"last_id": last_id, "fetch_size": fetch_size}
        if selected_ids:
            parameters["source_ids"] = selected_ids

        with engine.connect() as connection:
            rows = list(connection.execute(statement, parameters).mappings())
        if not rows:
            break
        for row in rows:
            title, alternate_titles = _project_titles(
                row["title"],
                row["title_eng"],
                row["title_other"],
            )
            try:
                normalized_doi = normalize_doi(row["doi"])
            except InvalidDoiError:
                normalized_doi = None
            yield PaperCandidate(
                database_id=int(row["id"]),
                source=str(row["source"]),
                source_id=str(row["source_id"]),
                title=title,
                doi_original=row["doi"],
                doi=normalized_doi,
                landing_url=row["url"],
                abstract=row["abstract"],
                publication_year=row["publication_year"],
                first_page=row["first_page"],
                final_page=row["final_page"],
                doi_source="database" if normalized_doi else None,
                alternate_titles=alternate_titles,
                journal_id=(
                    str(row["journal_id"]) if row["journal_id"] is not None else None
                ),
                institution_id=(
                    str(row["institution_id"])
                    if row["institution_id"] is not None
                    else None
                ),
                issue_id=(
                    str(row["issue_id"]) if row["issue_id"] is not None else None
                ),
                authors=_project_authors(row["authors_json"]),
            )
            yielded += 1
        last_id = int(rows[-1]["id"])


def _validated_table(source_table: str) -> str:
    if source_table not in ALLOWED_SOURCE_TABLES:
        choices = ", ".join(sorted(ALLOWED_SOURCE_TABLES))
        raise ValueError(f"unsupported source table {source_table!r}; choose one of {choices}")
    return source_table
