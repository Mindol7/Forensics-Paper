"""Verify a profile-layout migration against an immutable pre-migration archive.

This audit does not trust the renderer or infer bibliography.  It compares every
archived document to its new counterpart, permitting only the explicitly listed
format/identifier changes.  It never extracts the archive or changes Markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MD = ROOT / "Analyzer" / "Paper" / "MD"
DEFAULT_GRAPH = ROOT / "web" / "DF-Insight" / "data" / "graph"
OLD_MEMBER = re.compile(r"Analyzer/Paper/MD/(kor|eng)/(REP-DP-\d{6})\.md\Z")
NEW_ID = re.compile(r"KNOW-DOMP-\d{7}\Z")
OLD_ID = re.compile(r"REP-DP-(\d{6})(?!\d)")
SOURCE_ID = re.compile(r"artiId=(ART\d{9})(?!\d)")
PREFIXES = {
    "PRE": "PREP", "IDC": "IDEC", "PCP": "PRCP", "COL": "COLL",
    "PSP": "PSCP", "EXM": "EXAM", "ANL": "ANYS", "RPT": "REPT",
    "CMN": "CMNF", "CVG": "CVGF",
}
LANGUAGES = {"kor": "ko", "eng": "en"}


class MigrationAuditError(ValueError):
    """A document or graph violates the lossless migration contract."""


def new_record_id(old: str) -> str:
    match = re.fullmatch(r"REP-DP-(\d{6})", old)
    if not match:
        raise MigrationAuditError(f"Unsupported legacy record ID: {old}")
    return f"KNOW-DOMP-{int(match.group(1)):07d}"


def _frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    if not match:
        raise MigrationAuditError("Missing or malformed YAML front matter")
    return match.group(1), match.group(2)


def _scalar(front: str, name: str) -> str:
    matches = re.findall(rf"^{re.escape(name)}: (.+)$", front, re.MULTILINE)
    if len(matches) != 1:
        raise MigrationAuditError(f"Expected exactly one front-matter {name}")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise MigrationAuditError(f"Expected quoted string for {name}") from exc
    if not isinstance(value, str) or not value:
        raise MigrationAuditError(f"Empty or invalid {name}")
    return value


def _sequence(front: str, name: str) -> list[str]:
    match = re.search(rf"^{re.escape(name)}:(.*?)(?=^\w|\Z)", front, re.MULTILINE | re.DOTALL)
    if not match:
        raise MigrationAuditError(f"Missing sequence {name}")
    block = match.group(1).strip()
    if block == "[]":
        return []
    lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines or any(not line.startswith("- ") for line in lines):
        raise MigrationAuditError(f"Malformed sequence {name}")
    values = [line[2:].split(" #", 1)[0].strip() for line in lines]
    if len(set(values)) != len(values):
        raise MigrationAuditError(f"Duplicate sequence entries in {name}")
    return values


def _sections(body: str) -> dict[int, str]:
    matches = list(re.finditer(r"^## ([1-5])\. [^\n]+\n", body, re.MULTILINE))
    if [int(match.group(1)) for match in matches] != [1, 2, 3, 4, 5]:
        raise MigrationAuditError("Expected the five template sections, in order")
    return {
        int(match.group(1)): body[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(body)]
        for index, match in enumerate(matches)
    }


def _rows(section: str) -> list[tuple[str, str]]:
    lines = [line for line in section.splitlines() if line.startswith("|")]
    if len(lines) < 3:
        raise MigrationAuditError("A required table is empty or missing")
    result = []
    for line in lines[2:]:
        parts = line.split("|")
        if len(parts) != 4 or parts[0] or parts[-1]:
            raise MigrationAuditError("Expected exactly two escaped table columns")
        result.append((parts[1].strip(), parts[2].strip()))
    return result


def _normalize(text: str, language: str, *, legacy: bool) -> str:
    """Canonical text, with only approved presentation changes normalized."""

    front, body = _frontmatter(text)
    updated = _scalar(front, "last_updated")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated):
        raise MigrationAuditError("last_updated must use YYYY-MM-DD")
    date.fromisoformat(updated)
    front = re.sub(r'^last_updated: .+$', 'last_updated: "<date>"', front, flags=re.MULTILINE)
    # Technique comments are labels, not the classification identity.  Their
    # wording comes from the bilingual templates and may differ in translation.
    front = re.sub(r"^(  - [A-Z]{3,4}-\d{2}-\d{2})\s+#.*$", r"\1", front, flags=re.MULTILINE)
    if legacy:
        front = OLD_ID.sub(lambda m: f"KNOW-DOMP-{int(m.group(1)):07d}", front)
        body = OLD_ID.sub(lambda m: f"KNOW-DOMP-{int(m.group(1)):07d}", body)
        for old, new in PREFIXES.items():
            front = re.sub(rf"(?m)^(  - ){old}(-\d{{2}}-\d{{2}})$", rf"\g<1>{new}\2", front)
        front = front.replace("CLOUD-SaaS:MSG", "CLOUD-SaaS:MES").replace("CLOUD-SaaS:STO", "CLOUD-SaaS:CST")
        body = body.replace("## 1. 논문 정보\n", "## 1. 기본 정보\n")
        body = body.replace("## 1. Paper Information\n", "## 1. Basic Information\n")
        body = body.replace("| ID | NAME |", "| ID | Name |").replace("| ID | TITLE |", "| ID | Title |")
        for label in ("저자", "Authors"):
            body = re.sub(rf"(?m)^\| {label} \| ([^\n]*) \|$", lambda m: f"| {label} | {m.group(1).replace('; ', ', ')} |", body)
        for label in ("OA 여부", "OA"):
            body = body.replace(f"| {label} | Open Access |", f"| {label} | O |")
            body = body.replace(f"| {label} | Not Open Access |", f"| {label} | X |")
        if language == "ko":
            body = re.sub(r"(?m)^\| 권호 \| (\d+)_(\d+) \|$", r"| 권호 | \1권 \2호 |", body)
        else:
            body = re.sub(r"(?m)^\| Vol\. and Issue \| (\d+)_(\d+) \|$", r"| Vol. and Issue | Vol. \1, No. \2 |", body)
    return f"---\n{front}\n---\n{body}"


def compare_document(old: str, new: str, old_id: str, language: str) -> dict[str, Any]:
    """Reject any content changes beyond the documented migration whitelist."""

    new_id = new_record_id(old_id)
    old_front, old_body = _frontmatter(old)
    new_front, new_body = _frontmatter(new)
    if _scalar(old_front, "identifier") != old_id or _scalar(new_front, "identifier") != new_id:
        raise MigrationAuditError(f"Document identifier disagrees with its path: {new_id}/{language}")
    for name in ("subject", "author"):
        if _scalar(old_front, name) != _scalar(new_front, name):
            raise MigrationAuditError(f"{new_id}/{language}: changed {name}")
    old_sections, new_sections = _sections(old_body), _sections(new_body)
    info = dict(_rows(new_sections[1]))
    source = SOURCE_ID.findall(info.get("URL", ""))
    if len(source) != 1:
        raise MigrationAuditError(f"{new_id}/{language}: missing unique KCI source in URL")
    if _normalize(old, language, legacy=True) != _normalize(new, language, legacy=False):
        raise MigrationAuditError(f"{new_id}/{language}: unapproved content or formatting change")
    app_rows = _rows(new_sections[4])
    reference_rows = _rows(new_sections[5])
    for identifier, _ in reference_rows:
        if identifier and not NEW_ID.fullmatch(identifier):
            raise MigrationAuditError(f"{new_id}/{language}: invalid reference identifier {identifier}")
    classifications = _sequence(new_front, "classification")
    tags = _sequence(new_front, "tags")
    return {
        "source_id": source[0], "record_id": new_id,
        "classification": classifications, "tags": tags,
        "classification_count": len(classifications),
        "reference_count": sum(title != "N/A" for _, title in reference_rows),
        "internal_reference_count": sum(bool(identifier) for identifier, _ in reference_rows),
        "internal_reference_ids": [identifier for identifier, _ in reference_rows if identifier],
        "application_count": sum(name != "N/A" for _, name in app_rows),
        "registered_application_count": sum(bool(identifier) for identifier, _ in app_rows),
        "unknown_reference_data": reference_rows == [("", "N/A")],
        "unknown_oa": info.get("OA 여부", info.get("OA")) == "N/A",
        "unknown_title": _scalar(new_front, "subject") == "N/A",
        "unknown_abstract": bool(re.search(r">\s*N/A\s*</div>", new_sections[2])),
        "unknown_keyword_data": new_sections[3].strip() == "N/A",
        "metadata_na_fields": [key for key, value in info.items() if value == "N/A"],
        "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(new.encode()).hexdigest(),
    }


def _archive_documents(archive: tarfile.TarFile) -> dict[tuple[str, str], str]:
    documents = {}
    for member in archive.getmembers():
        name = member.name.removeprefix("./")
        match = OLD_MEMBER.fullmatch(name)
        if not match:
            continue
        if not member.isfile():
            raise MigrationAuditError(f"Legacy archive document is not a regular file: {name}")
        language, old_id = match.groups()
        key = (old_id, LANGUAGES[language])
        if key in documents:
            raise MigrationAuditError(f"Duplicate archive document: {name}")
        stream = archive.extractfile(member)
        if stream is None:
            raise MigrationAuditError(f"Unreadable archive document: {name}")
        documents[key] = stream.read().decode("utf-8")
    return documents


def _archive_json(archive: tarfile.TarFile, name: str) -> dict[str, Any]:
    members = [member for member in archive.getmembers() if member.name.removeprefix("./") == name]
    if len(members) != 1 or not members[0].isfile():
        raise MigrationAuditError(f"Missing or duplicate archived graph: {name}")
    stream = archive.extractfile(members[0])
    if stream is None:
        raise MigrationAuditError(f"Unreadable graph: {name}")
    return json.load(stream)


def _memberships(payload: dict[str, Any], key: str) -> dict[str, tuple[str, ...]]:
    nodes = payload.get(key, {})
    if isinstance(nodes, list):
        pairs = [(node["id"], node) for node in nodes]
    elif isinstance(nodes, dict):
        pairs = list(nodes.items())
    else:
        raise MigrationAuditError(f"Invalid {key} graph collection")
    if len({identifier for identifier, _ in pairs}) != len(pairs):
        raise MigrationAuditError(f"Duplicate {key} identity")
    return {identifier: tuple(sorted(node.get("paper_ids", []))) for identifier, node in pairs}


def _edges(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sorted((edge["source"], edge["target"], edge.get("method"), tuple(edge.get("reference_numbers", []))) for edge in payload["citation_edges"])


def audit_graphs(archive: tarfile.TarFile, graph_dir: Path, md_dir: Path, papers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Check memberships, citations, local MD URLs and exact bilingual copies."""

    report = {}
    web_root = graph_dir.resolve().parent.parent
    for graph_name in ("author", "institution", "reference"):
        filename = f"{graph_name}-graph.json"
        old = _archive_json(archive, f"web/DF-Insight/data/graph/{filename}")
        new = json.loads((graph_dir / filename).read_text(encoding="utf-8"))
        if set(old["papers"]) != set(new["papers"]) or set(new["papers"]) != set(papers):
            raise MigrationAuditError(f"{filename}: changed paper coverage")
        if _edges(old) != _edges(new):
            raise MigrationAuditError(f"{filename}: changed citation relations or evidence")
        for key in ("authors", "institutions"):
            if _memberships(old, key) != _memberships(new, key):
                raise MigrationAuditError(f"{filename}: changed {key} membership")
        for source_id, paper in new["papers"].items():
            old_paper = old["papers"][source_id]
            for key in ("author_ids", "institution_ids", "author_names", "affiliations"):
                if old_paper.get(key) != paper.get(key):
                    raise MigrationAuditError(f"{filename}/{source_id}: changed {key}")
            record_id = papers[source_id]["record_id"]
            if paper.get("record_id") != record_id:
                raise MigrationAuditError(f"{filename}/{source_id}: wrong record ID")
            for language in ("ko", "en"):
                expected_path = f"./data/graph/papers/{record_id}/{language}.md"
                if paper["md"][language] != expected_path:
                    raise MigrationAuditError(f"{filename}/{source_id}: expected {expected_path}")
                path = web_root / expected_path
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(web_root):
                    raise MigrationAuditError(f"Unsafe or missing web Markdown: {path}")
                raw = path.read_bytes()
                if raw != (md_dir / record_id / f"{language}.md").read_bytes():
                    raise MigrationAuditError(f"{filename}/{source_id}/{language}: MD copy differs from source")
                if hashlib.sha256(raw).hexdigest() != paper["md_sha256"][language]:
                    raise MigrationAuditError(f"{filename}/{source_id}/{language}: MD hash mismatch")
        report[graph_name] = {"papers": len(papers), "citation_edges": len(new["citation_edges"]), "bilingual_copies_verified": len(papers) * 2}
    return report


def audit_migration(backup: Path, md_dir: Path = DEFAULT_MD, *, expected_papers: int = 527, graph_dir: Path | None = None, allow_legacy: bool = False) -> dict[str, Any]:
    """Read-only comparison; a caller may serialize the returned audit report."""

    with tarfile.open(backup, "r:gz") as archive:
        documents = _archive_documents(archive)
        old_ids = {identifier for identifier, _ in documents}
        expected_old_keys = {(identifier, language) for identifier in old_ids for language in ("ko", "en")}
        if len(old_ids) != expected_papers or set(documents) != expected_old_keys:
            raise MigrationAuditError(f"Archive bilingual coverage mismatch: {len(old_ids)} IDs / {len(documents)} docs")
        expected = {md_dir / new_record_id(identifier) / f"{language}.md" for identifier, language in documents}
        all_md = set(md_dir.rglob("*.md"))
        actual = {path for path in all_md if not (allow_legacy and path.relative_to(md_dir).parts[0] in {"kor", "eng"})}
        if actual != expected:
            raise MigrationAuditError(f"New Markdown file set mismatch: missing={len(expected - actual)}, unexpected={len(actual - expected)}")
        if not allow_legacy and any((md_dir / language).exists() for language in ("kor", "eng")):
            raise MigrationAuditError("Legacy kor/eng directories remain")
        expected_dirs = {path.parent for path in expected}
        actual_dirs = {path for path in md_dir.iterdir() if path.is_dir() and not (allow_legacy and path.name in {"kor", "eng"})}
        if actual_dirs != expected_dirs:
            raise MigrationAuditError("Unexpected or missing per-paper directories")
        for directory in expected_dirs:
            if set(directory.iterdir()) != {directory / "ko.md", directory / "en.md"}:
                raise MigrationAuditError(f"Unexpected file in paper directory: {directory}")
        details = {}
        by_source = {}
        language_counts: dict[str, Counter[str]] = {language: Counter() for language in ("ko", "en")}
        valid_record_ids = {new_record_id(identifier) for identifier in old_ids}
        for (old_id, language), old in sorted(documents.items()):
            path = md_dir / new_record_id(old_id) / f"{language}.md"
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                raise MigrationAuditError(f"Unsafe new document: {path}")
            result = compare_document(old, path.read_text(encoding="utf-8"), old_id, language)
            if not set(result["internal_reference_ids"]) <= valid_record_ids:
                raise MigrationAuditError(f"Dangling registered reference in {path}")
            record_id = result["record_id"]
            if record_id in details:
                other = next(iter(details[record_id].values()))
                for key in ("source_id", "record_id", "classification", "tags", "reference_count", "application_count", "internal_reference_ids"):
                    if other[key] != result[key]:
                        raise MigrationAuditError(f"{record_id}: bilingual mismatch in {key}")
            details.setdefault(record_id, {})[language] = result
            if language == "ko":
                if result["source_id"] in by_source:
                    raise MigrationAuditError(f"Duplicate KCI source: {result['source_id']}")
                by_source[result["source_id"]] = result
            counts = language_counts[language]
            counts["documents"] += 1
            counts["empty_classification"] += not result["classification"]
            for key in ("classification_count", "reference_count", "internal_reference_count", "application_count", "registered_application_count", "unknown_reference_data", "unknown_oa", "unknown_title", "unknown_abstract", "unknown_keyword_data"):
                counts[key] += result[key]
            for field in result["metadata_na_fields"]:
                counts[f"metadata_na:{field}"] += 1
        graph_report = audit_graphs(archive, graph_dir, md_dir, by_source) if graph_dir else None
    return {
        "schema_version": 1, "status": "passed", "archive": str(backup.resolve()),
        "output": str(md_dir.resolve()), "paper_count": expected_papers,
        "markdown_count": len(documents), "allowed_transformations_only": True,
        "legacy_directories_allowed_for_pre_removal_check": allow_legacy,
        "languages": {language: dict(counts) for language, counts in language_counts.items()},
        "graphs": graph_report, "papers": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True, help="Immutable before-migration.tar.gz")
    parser.add_argument("--md-dir", type=Path, default=DEFAULT_MD)
    parser.add_argument("--expected-papers", type=int, default=527)
    parser.add_argument("--graph-dir", type=Path, help="Also compare deployed graph assets and memberships")
    parser.add_argument("--allow-legacy", action="store_true", help="Pre-removal check only: permit existing kor/eng directories")
    parser.add_argument("--output", type=Path, help="Write the successful JSON audit; never modifies the archive/MD")
    args = parser.parse_args(argv)
    try:
        report = audit_migration(args.backup, args.md_dir, expected_papers=args.expected_papers, graph_dir=args.graph_dir, allow_legacy=args.allow_legacy)
        if args.output:
            if args.output.exists():
                raise MigrationAuditError(f"Refusing to overwrite existing audit: {args.output}")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        summary = {key: value for key, value in report.items() if key != "papers"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (MigrationAuditError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"Migration audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
