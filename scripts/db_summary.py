#!/usr/bin/env python3
"""Summarize the collected KCI dataset so experiments start on verified data.

Reads the SQLite DB directly (stdlib sqlite3 only — no project deps). Reports
total rows, year distribution, matched-field breakdown (제목/키워드/초록/journal),
society-journal count, and top categories. A near-empty or wildly skewed result
means the relevant=0 we saw during testing was a real matcher bug, not a sparse
month — fix that before building on the dataset.

Usage:
    python3 scripts/db_summary.py [path/to/db.sqlite]
    # default: parse DATABASE_URL from .env, else kci_pipeline.db
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def _resolve_db_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            m = re.match(r"\s*DATABASE_URL\s*=\s*sqlite:///(.+)\s*$", line)
            if m:
                return m.group(1).strip()
    return "kci_pipeline.db"


def main() -> int:
    db_path = _resolve_db_path()
    if not Path(db_path).exists():
        print(f"DB not found: {db_path}")
        return 1
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    total = cur.execute("SELECT COUNT(*) FROM kci_papers").fetchone()[0]
    print(f"DB: {db_path}")
    print(f"kci_papers total: {total}")
    if total == 0:
        print("\n>>> EMPTY: no relevant papers stored. The sweep matched 0 — matcher bug, "
              "not data. Do NOT start experiments; fix matching first.")
        return 0

    print("\n[by publication_year]")
    for year, n in cur.execute(
        "SELECT publication_year, COUNT(*) FROM kci_papers GROUP BY publication_year ORDER BY publication_year"
    ):
        print(f"  {year}: {n}")

    # matched-field breakdown + categories from JSON columns
    field_counter: Counter[str] = Counter()
    cat_counter: Counter[str] = Counter()
    society = 0
    for (reasons_json, cat_json) in cur.execute(
        "SELECT relevance_reasons_json, category FROM kci_papers"
    ):
        try:
            reasons = json.loads(reasons_json) if reasons_json else []
        except ValueError:
            reasons = []
        for r in reasons:
            if isinstance(r, str) and r.startswith("matched_in:"):
                field_counter[r.split(":", 1)[1]] += 1
                break
        try:
            cats = json.loads(cat_json) if cat_json else []
        except ValueError:
            cats = []
        for c in cats:
            cat_counter[c] += 1
            if "디지털포렌식학회" in c:
                society += 1

    print("\n[matched-in field] (제목→키워드→초록 우선순위 중 최초 매칭 필드)")
    for f in ("title", "keyword", "abstract", "journal"):
        print(f"  {f}: {field_counter.get(f, 0)}")

    print(f"\n[society fast-path rows] 한국디지털포렌식학회: {society}")

    print("\n[top 15 categories]")
    for cat, n in cat_counter.most_common(15):
        print(f"  {n:5d}  {cat}")

    print("\n[5 sample titles]")
    for (title,) in cur.execute("SELECT title FROM kci_papers LIMIT 5"):
        print(f"  - {(title or '')[:80]}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
