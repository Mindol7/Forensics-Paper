"""Hand the paper profiles to the DF-Insight web app.

DF-Insight (``web/DF-Insight``, its own git repository) keeps every reference
document under ``data/references_md/<id>/{ko,en}.md`` and builds its graphs,
manifest and trends from them with ``npm run build``.  The KCI paper profiles
this pipeline writes to ``Analyzer/Paper/MD/KNOW-DOMP-*/`` are one family of
those documents.  ``sync_profiles`` mirrors that family into the web app:

* a profile that is new or changed is copied over (``ko.md`` and ``en.md``);
* a ``KNOW-DOMP-*`` document whose paper is no longer in the corpus is removed;
* documents of every other family (``KNOW-INTP-…``, ``KNOW-BLOG-…`` …) and the
  target documents are never mirrored or removed; only a table row in them that
  names one of our papers by a number the paper no longer has gets the paper's
  new number (see "Renumbered papers" below).

The copy follows DF-Insight's document conventions (``export_profile_text``):
a missing value is an empty table cell rather than ``N/A`` (a table with no
rows has no placeholder row at all), and text is not HTML-escaped (``&``, ``<``, ``>`` stay literal; the escaped pipe ``&#124;`` must
stay, a pipe would break the table).  DF-Insight's own build then links
identifiers inside its copy (``npm run link:write``), which is why the web copy
may legitimately differ from the profile afterwards.  Committing and pushing
the web repository stays a human step.

Application rows: the profile's Level-5 ids, DF-Insight's links of the rest
---------------------------------------------------------------------------
Since 2026-10-04 a profile writes the Level-5 ids the pipeline mapped
(``Analyzer/Application/level5.py``) into its application table, one row per id
(``| EXAM-04-01-02-003 | Discord |``), and leaves the id blank only for an
application the system has not registered (``|  | KakaoMap |``).  DF-Insight's
build never resolves a row again once it carries an id; it may still link a
blank row by name, and when it does it rewrites the table with its node titles.
A fresh link of the same blank names can give other rows later (a node
registered since then wins over the alias that used to resolve the name).

The sync therefore keeps the rows of the web copy of the *same paper* — found
by its KCI id in the URL row, because ``KNOW-DOMP`` numbers can be re-sorted —
as long as the paper's own rows (ids and names) are the ones last exported, and
exports the profile's rows when they changed (a new Level-5 mapping, a newly
registered application, another name) or when the web copy's table cannot be
used (it is gone, empty or unreadable).  ``Analyzer/Manifest/web_sync_state.json``
(``state``) is what tells a changed table from a change on the web side: per KCI
id it holds the names (``applications``) and, when any row has an id, the
``(id, name)`` rows (``rows``) the web table was last exported from, and the
fingerprints of the tables a fresh link replaced (``replaced``), so that a web
repository put back to an older commit is recognised and the rows are exported
again.  A record written before ids existed (names only) stands for rows with
blank ids, so the first sync after the Level-5 mapping exports every paper whose
table now carries ids.  When nothing is recorded for a paper its web table is
kept and the current rows recorded; without a ``state`` path nothing is read or
written and every table is kept.

Since 2026-10-26: the profile's rows win (``profile_rows_win=True``)
-------------------------------------------------------------------
The profiles now show the Jev AI classification (chapter III '논문 분류'), in
which a blank id is Jev's decision that no Level-5 node fits the application in
that paper, not an application the system lacks.  The pipeline's graphs stage
therefore runs DF-Insight's build without the pass that links blank rows by name
(``Scheduler/pipeline.py``, ``WEB_LINK_PASSES``) and syncs with
``profile_rows_win=True``: every web copy gets the profile's application table
as it is, also when an earlier build had linked it by name (the paper is then
listed in ``relinked``).  The state is still written (the rows exported), so the
default mode can take over again later.

Renumbered papers in DF-Insight's own documents
-----------------------------------------------
DF-Insight's linker writes our numbers into documents of its own: a target
document's '관련 참고문헌' table gets a row per paper whose application links that
target (``| KNOW-DOMP-0000501 | <title> |``, pass D), and a reference row of
another family's document that cites one of our papers gets its number (pass B).
Neither pass looks at a row again once it carries an id, so after a renumbering
such a row would name the paper that now holds the old number.  The sync
therefore gives such a row the paper's new number (an empty id cell when the
paper has no profile any more) — first, before it writes anything else — but only
when the rest of the row names that paper (its Korean or English title or its
DOI, as its web copy had them).  A row that names another paper is left alone,
which also makes a run repeated after an interruption harmless; a row that names
no paper it can recognise is reported (``unverified_rows``).  A paper's old number
is the directory of its web copy, found by KCI id like the application tables.

The build also fills the id of a reference row whose cited work it finds among
its documents.  Such an id in a cell the export leaves blank is not a
difference (``_same_document``), unless it is a ``KNOW-DOMP`` number, which a
renumbering can make stale and which is therefore resolved again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES = REPOSITORY_ROOT / "Analyzer" / "Paper" / "MD"
DEFAULT_WEB = REPOSITORY_ROOT / "web" / "DF-Insight"
DEFAULT_STATE = REPOSITORY_ROOT / "Analyzer" / "Manifest" / "web_sync_state.json"
STATE_VERSION = 1
REPLACED_TABLES_KEPT = 8
PROFILE_PREFIX = "KNOW-DOMP-"
LANGUAGE_FILES = ("ko.md", "en.md")
PRIMARY = "ko.md"   # DF-Insight links both files from the Korean application table

# Why a paper's application table was exported again instead of kept (shown to the operator).
NAMES_CHANGED = "애플리케이션 목록이 바뀜"
IDS_CHANGED = "애플리케이션의 Level-5 ID가 바뀜(새 매핑·새로 등록된 애플리케이션)"
REPLACED_TABLE_IS_BACK = "웹 사본이 다시 연결하기 전의 표로 되돌아가 있음(웹 저장소를 이전 커밋·다른 브랜치로 되돌린 경우)"
WEB_COPY_GONE = "웹 사본이 없어짐"
EMPTY_WEB_TABLE = "웹 사본의 표가 비어 있음"
UNREADABLE_WEB_TABLE = "웹 사본의 대상 애플리케이션 표를 읽을 수 없음"
UNIDENTIFIED_WEB_COPY = "웹 사본이 어느 논문의 것인지 확인할 수 없음(파일 누락·KCI 논문 ID 불일치)"
SEVERAL_WEB_COPIES = "같은 논문의 웹 사본이 여러 폴더에 있음"
# profile_rows_win: the web copy shows other rows than the profile although the profile did not change
# (an earlier DF-Insight build linked a blank row by name, or the table was edited on the web side).
WEB_ROWS_DIFFER = "웹 사본의 표가 프로필(Jev AI 분류)과 다름(이전 웹 빌드가 이름으로 붙인 Level-5 ID 등)"

# DF-Insight documents outside our family that may name our papers by number (see the module docstring).
NUMBERED_DOCUMENT_ROOTS = ("targets_md", "references_md")
_NUMBER_CELL_RE = re.compile(r"^(\s*\|\s*)(KNOW-DOMP-[0-9]{7})(\s*\|)")   # a table row whose first cell is our number
_SUBJECT_RE = re.compile(r'^subject:\s*"(.*)"', re.MULTILINE)            # the title, as DF-Insight's linker reads it
_DOI_ROW_RE = re.compile(r"^\|\s*DOI\s*\|([^|\n]*)\|", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MIN_TITLE = 12   # DF-Insight's linker does not resolve a shorter title either


class WebSyncError(RuntimeError):
    """The web app checkout is missing or is not a DF-Insight repository, or the profiles cannot be mirrored."""


@dataclass(slots=True)
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    # Text of every document that was overwritten, so that a copy DF-Insight's build turns back
    # into what it was (it links the identifiers our export leaves blank) can be restored afterwards.
    previous: dict[Path, str] = field(default_factory=dict)
    restored: int = 0
    # Application tables of papers that name at least one application: kept because the paper names
    # the recorded applications / kept although nothing was recorded for the paper / exported again,
    # with the reason.
    links_kept: int = 0
    links_adopted: int = 0
    relinked: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)   # web copies whose application table could not be used
    state: Path | None = None
    state_found: bool = False
    # KCI id -> Korean web copy of every paper whose plain rows are waiting for a link while a replaced
    # table is remembered for it; ``restore_unchanged_after_build`` looks at what the build made of them.
    raw_exports: dict[str, Path] = field(default_factory=dict)
    # Rows of DF-Insight's own documents that named a renumbered paper by its old number (module docstring):
    # given the new number / emptied (the paper has no profile any more) / left alone although the number
    # moved, because the row does not name the paper ("<document>: <number>").
    renumbered: dict[str, str | None] = field(default_factory=dict)   # old number -> new number (None: no profile)
    renumbered_rows: int = 0
    unlinked_rows: int = 0
    renumbered_documents: list[str] = field(default_factory=list)
    unverified_rows: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed)

    def as_dict(self) -> dict[str, object]:
        return {"added": len(self.added), "updated": len(self.updated), "removed": len(self.removed), "unchanged": self.unchanged,
                "restored_after_build": self.restored, "removed_ids": self.removed[:20], "added_ids": self.added[:20],
                "application_links_kept": self.links_kept, "application_links_adopted": self.links_adopted,
                "application_links_rebuilt": len(self.relinked), "application_links_rebuilt_reasons": dict(list(self.relinked.items())[:20]),
                "application_tables_unreadable": self.unreadable[:20], "state_found": self.state_found,
                "renumbered_papers": len(self.renumbered), "renumbered_rows": self.renumbered_rows, "unlinked_rows": self.unlinked_rows,
                "renumbered_documents": len(self.renumbered_documents), "renumbered_documents_first": self.renumbered_documents[:20],
                "unverified_rows": self.unverified_rows[:20], "unverified_row_count": len(self.unverified_rows)}


def references_dir(web: Path = DEFAULT_WEB) -> Path:
    if not (web / "scripts" / "build-graphs.mjs").is_file() or not (web / "data" / "references_md").is_dir():
        raise WebSyncError(
            f"{web} 는 DF-Insight 저장소가 아니거나 아직 받지 않았습니다 (scripts/build-graphs.mjs, data/references_md 필요)"
        )
    return web / "data" / "references_md"


_MISSING_CELL_RE = re.compile(r"\| N/A \|")
_EMPTY_ROW_RE = re.compile(r"^\|(?: *\|)+ *$")
_DATE_LINE_RE = re.compile(r"^last_updated: .*$", re.MULTILINE)
_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"))
# The paper's own KCI id: the URL row of the information table (reference rows may link other KCI articles).
_PAPER_KEY_RE = re.compile(r"^\|\s*URL\s*\|[^|\n]*?artiId=(ART[0-9]{9})(?![0-9])", re.MULTILINE)
_APPLICATION_HEADING_RE = re.compile(r"대상 애플리케이션|Target Applications?")   # the test DF-Insight's linker uses
_TABLE_RULE_RE = re.compile(r"[|\-: ]+")
# A reference id DF-Insight's build filled in (documents of the other families; see the module docstring).
_FILLED_REFERENCE_ID_RE = re.compile(r"\| KNOW-(?!DOMP-)[A-Z]{4}-[0-9]{7} \|")


def export_profile_text(text: str) -> str:
    """A profile in DF-Insight's document conventions (see the module docstring)."""

    exported = _MISSING_CELL_RE.sub("|  |", text)
    for entity, literal in _ENTITIES:
        exported = exported.replace(entity, literal)
    lines = [line for line in exported.split("\n") if not _EMPTY_ROW_RE.match(line)]
    return "\n".join(lines)


def _same_content(left: str, right: str) -> bool:
    """Equal apart from the generation date: a document nobody changed is not "updated"."""

    return _DATE_LINE_RE.sub("", left) == _DATE_LINE_RE.sub("", right)


def _same_document(copy: str, exported: str) -> bool:
    """The web copy is the export, apart from the generation date and from reference ids DF-Insight's
    build filled into cells the export leaves blank (``|  | citation |`` became ``| KNOW-INTP-0000012 | citation |``)."""

    if _same_content(copy, exported):
        return True
    ours, theirs = exported.split("\n"), copy.split("\n")
    if len(ours) != len(theirs):
        return False
    for mine, other in zip(ours, theirs):
        if mine == other or (_DATE_LINE_RE.fullmatch(mine) and _DATE_LINE_RE.fullmatch(other)):
            continue
        filled = _FILLED_REFERENCE_ID_RE.match(other)
        if not (filled and mine.startswith("|  |") and other[filled.end():] == mine[4:]):
            return False
    return True


def _write(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, destination)


def paper_key(text: str) -> str | None:
    """KCI id (``ART#########``) of a profile or web copy, from its URL row; ``None`` unless there is exactly one."""

    found = set(_PAPER_KEY_RE.findall(text))
    return found.pop() if len(found) == 1 else None


def _application_table(lines: list[str]) -> tuple[int, int, list[str]] | None:
    """The application table, read the way DF-Insight's linker reads it: every line of the section
    that starts with ``|`` once trimmed is a table line, rule rows are skipped, the first remaining
    line is the header and the others are the data rows.

    Returns the line span that holds the data rows (from the line after the header and its rule row
    to the last table line of the section; an empty span for an empty table) and the data rows;
    ``None`` when the document has no application section or the section has no table.
    """

    heading = next((i for i, line in enumerate(lines) if line.startswith("## ") and _APPLICATION_HEADING_RE.search(line)), None)
    if heading is None:
        return None
    end = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    table = [i for i in range(heading + 1, end) if lines[i].strip().startswith("|")]
    rows = [i for i in table if not _TABLE_RULE_RE.fullmatch(lines[i].strip())]
    if not rows:
        return None
    start = rows[0] + 1
    while start in table and _TABLE_RULE_RE.fullmatch(lines[start].strip()):
        start += 1
    return start, max(start, table[-1] + 1), [lines[i] for i in rows[1:]]


def _table(text: str) -> list[str] | None:
    """Data rows of the application table; ``None`` without such a table."""

    found = _application_table(text.split("\n"))
    return None if found is None else found[2]


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") else row
    return [cell.strip() for cell in row.split("|")]


def application_names(text: str) -> tuple[str, ...] | None:
    """Application names of an exported profile, sorted and without repeats (a link depends on
    neither order nor repeats); ``None`` when the document has no application table."""

    rows = _table(text)
    if rows is None:
        return None
    names: set[str] = set()
    for row in rows:
        cells = _cells(row)
        name = " ".join(cells[1].split()) if len(cells) > 1 else ""
        if name and name != "N/A":
            names.add(name)
    return tuple(sorted(names))


def application_rows(text: str) -> tuple[tuple[str, str], ...] | None:
    """``(Level-5 id or "", name)`` rows of an exported profile, sorted and without repeats;
    ``None`` when the document has no application table."""

    rows = _table(text)
    if rows is None:
        return None
    found: set[tuple[str, str]] = set()
    for row in rows:
        cells = _cells(row)
        name = " ".join(cells[1].split()) if len(cells) > 1 else ""
        if name and name != "N/A":
            found.add((" ".join(cells[0].split()), name))
    return tuple(sorted(found, key=lambda item: (item[1], item[0])))


def _names(rows: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(sorted({name for _identifier, name in rows}))


def _fingerprint(rows: list[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _with_web_application_rows(exported: dict[str, str], copy: dict[str, str]) -> dict[str, str] | None:
    """The export with the data rows of the web copy's application table, each language from its own
    file, in both languages or not at all (DF-Insight links both files from the Korean table, so one
    linked table alone would stay alone)."""

    carried: dict[str, str] = {}
    for name in LANGUAGE_FILES:
        if name not in copy:
            return None
        ours, theirs = exported[name].split("\n"), copy[name].split("\n")
        here, there = _application_table(ours), _application_table(theirs)
        if here is None or there is None:
            return None
        carried[name] = "\n".join(ours[:here[0]] + theirs[there[0]:there[1]] + ours[here[1]:])
    return carried


@dataclass(frozen=True, slots=True)
class _WebCopies:
    """Every ``KNOW-DOMP`` web copy, read once before anything is written (numbers may have moved)."""

    by_directory: dict[str, dict[str, str]]   # directory -> {file name: text}
    by_paper: dict[str, list[str]]            # KCI id -> directories whose two language files carry it

    def identified(self, directory: str) -> bool:
        return any(directory in directories for directories in self.by_paper.values())


def _web_copies(target_root: Path) -> _WebCopies:
    by_directory: dict[str, dict[str, str]] = {}
    by_paper: dict[str, list[str]] = {}
    for path in sorted(target_root.iterdir()):
        if not path.is_dir() or path.is_symlink() or not path.name.startswith(PROFILE_PREFIX):
            continue
        texts: dict[str, str] = {}
        for name in LANGUAGE_FILES:
            try:
                texts[name] = (path / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        by_directory[path.name] = texts
        key = paper_key(texts.get(PRIMARY, ""))
        if key is not None and all(paper_key(texts.get(name, "")) == key for name in LANGUAGE_FILES):
            by_paper.setdefault(key, []).append(path.name)
    return _WebCopies(by_directory, by_paper)


def load_state(path: Path) -> dict[str, dict[str, object]] | None:
    """``{KCI id: {"applications": (...), "rows": ((id, name), ...), "replaced": [...]}}``; ``None`` when no
    usable record exists.  An entry that cannot be read is no entry (the paper's web table is then kept).
    An entry without ``rows`` (names only) stands for rows whose ids are blank."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    papers = payload.get("papers") if isinstance(payload, dict) and payload.get("version") == STATE_VERSION else None
    if not isinstance(papers, dict):
        return None
    state: dict[str, dict[str, object]] = {}
    for key, entry in papers.items():
        names = entry.get("applications") if isinstance(entry, dict) else None
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            continue
        replaced = entry.get("replaced", [])
        stored_rows = entry.get("rows")
        if isinstance(stored_rows, list) and all(
            isinstance(item, list) and len(item) == 2 and all(isinstance(value, str) for value in item) for item in stored_rows
        ):
            rows = tuple(sorted({(identifier, name) for identifier, name in stored_rows}, key=lambda item: (item[1], item[0])))
        else:
            rows = tuple(sorted({("", name) for name in names}, key=lambda item: (item[1], item[0])))
        state[str(key)] = {
            "applications": tuple(sorted(set(names))),
            "rows": rows,
            "replaced": [item for item in replaced if isinstance(item, str)] if isinstance(replaced, list) else [],
        }
    return state


def _save_state(path: Path, papers: dict[str, dict[str, object]]) -> None:
    payload = {
        "version": STATE_VERSION,
        "about": "Analyzer/web_sync.py: per KCI paper, the application names (and, when any row has a Level-5 id, the "
                 "(id, name) rows) its DF-Insight copy's application table was last exported from, and fingerprints "
                 "of the tables a fresh link replaced.",
        "papers": {
            key: {
                "applications": list(_names(entry["rows"])),
                **({"rows": [list(row) for row in entry["rows"]]} if any(identifier for identifier, _ in entry["rows"]) else {}),
                **({"replaced": list(entry["replaced"])} if entry["replaced"] else {}),
            }
            for key, entry in sorted(papers.items())
        },
    }
    _write(Path(path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _application_table_for(
    record_id: str, key: str, rows_now: tuple[tuple[str, str], ...], exported: dict[str, str], entry: dict[str, object] | None,
    web: _WebCopies, report: SyncReport,
) -> tuple[dict[str, str], dict[str, object], bool]:
    """Decide one paper's application table: the export with the web copy's rows (kept) or as it is
    (plain rows for DF-Insight's build to link), the record to keep for the paper, and whether the
    build's result for it has to be looked at afterwards."""

    replaced = list(entry["replaced"]) if entry else []
    directories = web.by_paper.get(key, [])
    # The copy in the paper's own directory wins; otherwise the one directory that holds the paper (it was renumbered).
    home = record_id if record_id in directories else directories[0] if len(directories) == 1 else None
    copy = web.by_directory[home] if home is not None else None
    plain = _table(exported[PRIMARY]) or []
    kept: dict[str, str] | None = None
    reason: str | None = None
    damaged = False
    if copy is None:
        if directories:
            reason, damaged = SEVERAL_WEB_COPIES, True
        elif record_id in web.by_directory and not web.identified(record_id):
            reason, damaged = UNIDENTIFIED_WEB_COPY, True
        elif entry is not None:
            reason = WEB_COPY_GONE
        # otherwise a new paper: there is nothing to keep
    else:
        rows = _table(copy.get(PRIMARY, ""))
        carried = _with_web_application_rows(exported, copy)
        if rows is None or carried is None:
            reason, damaged = UNREADABLE_WEB_TABLE, True
        elif rows_now and not rows:
            reason = EMPTY_WEB_TABLE          # an empty table is nobody's link decision
        elif entry is None:
            kept = carried
            report.links_adopted += bool(rows_now)
        elif tuple(entry["rows"]) != rows_now:
            reason = NAMES_CHANGED if tuple(entry["applications"]) != _names(rows_now) else IDS_CHANGED
            fingerprint = _fingerprint(rows)
            if fingerprint not in replaced and rows != plain:
                replaced = (replaced + [fingerprint])[-REPLACED_TABLES_KEPT:]
        elif _fingerprint(rows) in replaced:
            reason = REPLACED_TABLE_IS_BACK
        else:
            kept = carried
            report.links_kept += bool(rows_now)
    if damaged:
        report.unreadable.append(record_id)
    if reason is not None and rows_now:
        report.relinked[record_id] = reason
    # Plain rows on their way to a link (exported now, or exported by an earlier run whose build did not link them yet).
    waiting = bool(replaced) and (kept is None or _table(kept[PRIMARY]) == plain)
    return kept or exported, {"applications": _names(rows_now), "rows": rows_now, "replaced": replaced}, waiting


def _profile_table_for(
    record_id: str, key: str, rows_now: tuple[tuple[str, str], ...], exported: dict[str, str], entry: dict[str, object] | None,
    web: _WebCopies, report: SyncReport,
) -> tuple[dict[str, str], dict[str, object], bool]:
    """``profile_rows_win``: the export as it is (the profile's rows), always.  The web copy is only
    looked at to tell the operator whose table changes and why; nothing waits for a link."""

    directories = web.by_paper.get(key, [])
    home = record_id if record_id in directories else directories[0] if len(directories) == 1 else None
    copy = web.by_directory[home] if home is not None else None
    reason: str | None = None
    damaged = False
    theirs: tuple[tuple[str, str], ...] = ()
    if copy is None:
        if directories:
            reason, damaged = SEVERAL_WEB_COPIES, True
        elif record_id in web.by_directory and not web.identified(record_id):
            reason, damaged = UNIDENTIFIED_WEB_COPY, True
        elif entry is not None:
            reason = WEB_COPY_GONE
    else:
        found = application_rows(copy.get(PRIMARY, ""))
        if found is None or _with_web_application_rows(exported, copy) is None:
            reason, damaged = UNREADABLE_WEB_TABLE, True
        else:
            theirs = found
            if theirs != rows_now:
                if rows_now and not theirs:
                    reason = EMPTY_WEB_TABLE
                elif entry is not None and tuple(entry["rows"]) != rows_now:
                    reason = NAMES_CHANGED if tuple(entry["applications"]) != _names(rows_now) else IDS_CHANGED
                else:
                    reason = WEB_ROWS_DIFFER
            else:
                report.links_kept += bool(rows_now)
    if damaged:
        report.unreadable.append(record_id)
    if reason is not None and (rows_now or theirs):
        report.relinked[record_id] = reason
    return exported, {"applications": _names(rows_now), "rows": rows_now, "replaced": []}, False


def _loose(text: str) -> str:
    """Text compared the way a citation is matched: links as their label, table escapes undone,
    whitespace collapsed, case folded, a final full stop dropped."""

    text = _MARKDOWN_LINK_RE.sub(r"\1", text).replace("\\|", "|").replace('\\"', '"')
    return " ".join(text.split()).casefold().rstrip(".")


@dataclass(frozen=True, slots=True)
class _Move:
    """A paper whose web copy is under another number than its profile now has (``new`` is ``None``:
    the paper has no profile any more), with what names it in a row: its titles and DOI."""

    new: str | None
    marks: tuple[str, ...]

    def named_in(self, text: str) -> bool:
        return any(mark in text for mark in self.marks)


def _marks(texts: dict[str, str]) -> tuple[str, ...]:
    marks: set[str] = set()
    for text in texts.values():
        subject = _SUBJECT_RE.search(text)
        title = _loose(subject.group(1)) if subject else ""
        if len(title) >= MIN_TITLE:
            marks.add(title)
        doi = _DOI_ROW_RE.search(text)
        value = _loose(doi.group(1)) if doi else ""
        if value.startswith("10."):
            marks.add(value)
    return tuple(sorted(marks))


def _moves(copies: _WebCopies, numbers: dict[str, str], complete: bool) -> dict[str, _Move]:
    """Old number -> move for every paper (KCI id -> its profile's number in ``numbers``) whose one web
    copy sits under another number.  A paper without a profile counts as gone only when every profile's
    KCI id could be read (``complete``)."""

    moves: dict[str, _Move] = {}
    for key, directories in copies.by_paper.items():
        if len(directories) != 1:
            continue                                    # reported by the application tables (SEVERAL_WEB_COPIES)
        old, new = directories[0], numbers.get(key)
        if new == old or (new is None and not complete):
            continue
        moves[old] = _Move(new, _marks(copies.by_directory[old]))
    return moves


def _numbered_documents(web: Path) -> list[Path]:
    """``ko.md``/``en.md`` of the target documents and of the other families' reference documents."""

    paths: list[Path] = []
    for family in NUMBERED_DOCUMENT_ROOTS:
        root = web / "data" / family
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir() or directory.name.startswith(PROFILE_PREFIX):
                continue
            paths.extend(path for name in LANGUAGE_FILES if (path := directory / name).is_file() and not path.is_symlink())
    return paths


def _renumber_rows(text: str, moves: dict[str, _Move], incoming: dict[str, _Move], document: str, report: SyncReport) -> str:
    """``text`` with every row that names a moved paper by its old number given the new one
    (``incoming``: new number -> the move of the paper that takes it)."""

    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = _NUMBER_CELL_RE.match(line)
        if match is None or match.group(2) not in moves:
            continue
        move, rest = moves[match.group(2)], _loose(line[match.end():])
        settled = incoming.get(match.group(2))
        named_old, named_new = move.named_in(rest), settled is not None and settled.named_in(rest)
        if named_new and not named_old:
            continue                                     # already the paper that takes this number (a repeated run)
        if named_old == named_new:                       # names neither paper, or both (one title inside the other)
            report.unverified_rows.append(f"{document}: {match.group(2)}")
            continue
        lines[index] = f"{match.group(1)}{move.new or ''}{match.group(3)}{line[match.end():]}"
        if move.new is None:
            report.unlinked_rows += 1
        else:
            report.renumbered_rows += 1
    return "\n".join(lines)


def _renumbered_documents(web: Path, moves: dict[str, _Move], report: SyncReport) -> list[tuple[Path, str]]:
    """The documents to rewrite (path, new text) and the counts in ``report``; nothing is written here."""

    writes: list[tuple[Path, str]] = []
    if not moves:
        return writes
    incoming = {move.new: move for move in moves.values() if move.new is not None}
    data = web / "data"
    for path in _numbered_documents(web):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "KNOW-DOMP-" not in text:
            continue
        document = path.relative_to(data).as_posix()
        updated = _renumber_rows(text, moves, incoming, document, report)
        if updated != text:
            writes.append((path, updated))
            report.renumbered_documents.append(document)
    return writes


def _profiles(profiles: Path) -> dict[str, Path]:
    """The profile directories to mirror; a half-written or linked one is refused, because mirroring
    without it would delete its paper's web copy."""

    current: dict[str, Path] = {}
    incomplete: list[str] = []
    for path in sorted(profiles.iterdir()) if profiles.is_dir() else []:
        if not path.name.startswith(PROFILE_PREFIX) or not (path.is_dir() or path.is_symlink()):
            continue
        if path.is_symlink():
            raise WebSyncError(f"symlink refused: {path}")
        if all((path / name).is_file() for name in LANGUAGE_FILES):
            current[path.name] = path
        else:
            incomplete.append(path.name)
    if incomplete:
        raise WebSyncError(
            f"프로필 폴더가 완전하지 않습니다(ko.md·en.md 둘 다 필요): {', '.join(incomplete[:5])}"
            f"{' 외 ' + str(len(incomplete) - 5) + '개' if len(incomplete) > 5 else ''} — MD 단계를 다시 실행하세요"
        )
    return current


def sync_profiles(*, profiles: Path = DEFAULT_PROFILES, web: Path = DEFAULT_WEB, dry_run: bool = False,
                  state: Path | None = None, profile_rows_win: bool = False) -> SyncReport:
    """Mirror the profiles into the web app (module docstring).  ``profile_rows_win``: every web copy
    gets the profile's application table as it is (the Jev AI classification; the pipeline's mode)."""

    target_root = references_dir(web)
    report = SyncReport(state=Path(state) if state is not None else None)
    current = _profiles(profiles)
    if not current:
        raise WebSyncError(f"복사할 프로필이 없습니다: {profiles} (MD 단계를 먼저 실행하세요)")

    exports: dict[str, dict[str, str]] = {}
    tracked: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}   # record id -> (KCI id, application rows)
    holders: dict[str, str] = {}
    numbers: dict[str, str] = {}   # KCI id -> number of its profile (every profile whose KCI id can be read)
    for record_id, source_dir in current.items():
        if (target_root / record_id).is_symlink():
            raise WebSyncError(f"symlink refused: {target_root / record_id}")
        exported = {name: export_profile_text((source_dir / name).read_text(encoding="utf-8")) for name in LANGUAGE_FILES}
        exports[record_id] = exported
        key, rows = paper_key(exported[PRIMARY]), application_rows(exported[PRIMARY])
        if key is not None and all(paper_key(exported[name]) == key for name in LANGUAGE_FILES):
            numbers.setdefault(key, record_id)
        if key is None or rows is None or any(paper_key(exported[name]) != key or _table(exported[name]) is None for name in LANGUAGE_FILES):
            continue   # not a paper profile whose application table can be followed: mirrored as it is
        if key in holders:
            raise WebSyncError(f"같은 KCI 논문({key})의 프로필이 둘입니다: {holders[key]}, {record_id}")
        holders[key] = record_id
        tracked[record_id] = (key, rows)

    loaded = load_state(report.state) if report.state is not None else None
    report.state_found = loaded is not None
    recorded = loaded or {}
    copies = _web_copies(target_root)   # before anything is written: numbers may have moved
    decide = _profile_table_for if profile_rows_win else _application_table_for
    next_state: dict[str, dict[str, object]] = {}
    writes: list[tuple[str, dict[str, str]]] = []
    for record_id in current:
        exported = exports[record_id]
        if record_id in tracked:
            key, rows = tracked[record_id]
            exported, next_state[key], waiting = decide(record_id, key, rows, exported, recorded.get(key), copies, report)
            if waiting:
                report.raw_exports[key] = target_root / record_id / PRIMARY
        web_here = copies.by_directory.get(record_id, {})
        if all(name in web_here and _same_document(web_here[name], exported[name]) for name in LANGUAGE_FILES):
            report.unchanged += 1
            continue
        (report.updated if (target_root / record_id).is_dir() else report.added).append(record_id)
        writes.append((record_id, exported))
    report.removed = sorted(name for name in copies.by_directory if name not in current)
    moves = _moves(copies, numbers, complete=len(numbers) == len(current))
    report.renumbered = {old: move.new for old, move in sorted(moves.items())}
    renumbered = _renumbered_documents(web, moves, report)
    if dry_run:
        return report

    # DF-Insight's documents that name a moved paper first: until the profiles are written, the web copies
    # still tell the old numbers, so a run that dies in between finds the same moves again (rows already
    # renumbered name the paper that holds their number now and are left alone).
    for path, text in renumbered:
        _write(path, text)
    # The record next: if the run dies before the documents are written, the next sync finds the new
    # names recorded beside the table they were meant to replace, and exports the plain rows again.
    # For the same reason the Korean file, whose table decides, is written last.
    if report.state is not None:
        _save_state(report.state, next_state)
    for record_id, exported in writes:
        web_here = copies.by_directory.get(record_id, {})
        for name in sorted(LANGUAGE_FILES, key=lambda item: item == PRIMARY):
            if name in web_here:
                report.previous[target_root / record_id / name] = web_here[name]
            _write(target_root / record_id / name, exported[name])
    for name in report.removed:
        shutil.rmtree(target_root / name)
    return report


def unknown_application_ids(web: Path = DEFAULT_WEB) -> dict[str, list[str]]:
    """``KNOW-DOMP`` web copies whose application table names a node DF-Insight does not have
    (no ``data/techniques/<id>.json``).  DF-Insight's own validation does not look at these ids, and
    a kept row whose node was removed upstream would silently drop the application from the site."""

    target_root = references_dir(web)
    nodes = {path.stem for path in (web / "data" / "techniques").glob("*.json")}
    unknown: dict[str, list[str]] = {}
    for path in sorted(target_root.iterdir()) if nodes else []:
        if not path.is_dir() or path.is_symlink() or not path.name.startswith(PROFILE_PREFIX):
            continue
        try:
            rows = _table((path / PRIMARY).read_text(encoding="utf-8")) or []
        except (OSError, UnicodeDecodeError):
            continue
        missing = sorted({identifier for identifier in (_cells(row)[0] for row in rows) if identifier and identifier not in nodes})
        if missing:
            unknown[path.name] = missing
    return unknown


def restore_unchanged_after_build(report: SyncReport) -> int:
    """After DF-Insight's build: put back every overwritten copy that the build brought back to its old content.

    DF-Insight links the identifiers an export leaves blank (reference ids, and
    the application rows of a paper whose list changed), so an overwritten copy
    can equal its previous content again after the build — apart from our newer
    generation date.  Such a document did not change for anybody; restoring its
    previous bytes keeps the web repository's diff to the papers that really
    changed.  Application tables that were kept are not linked again at all.
    """

    restored = 0
    for path, previous in report.previous.items():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if current != previous and _same_content(current, previous):
            _write(path, previous)
            restored += 1
    report.restored = restored
    _forget_reproduced_tables(report)
    return restored


def _forget_reproduced_tables(report: SyncReport) -> None:
    """A fresh link that gave the very table it replaced replaced nothing: forget that table, or the
    paper's plain rows would be exported again at every sync.  (Only a build that runs between this
    sync and ``restore_unchanged_after_build`` is seen; after a build done by hand the paper is
    exported once more by the next run and settled then.)"""

    if report.state is None or not report.raw_exports:
        return
    state = load_state(report.state)
    if state is None:
        return
    settled = False
    for key, path in report.raw_exports.items():
        entry = state.get(key)
        try:
            rows = _table(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if entry is None or rows is None or _fingerprint(rows) not in entry["replaced"]:
            continue
        entry["replaced"] = [item for item in entry["replaced"] if item != _fingerprint(rows)]
        settled = True
    if settled:
        _save_state(report.state, state)
