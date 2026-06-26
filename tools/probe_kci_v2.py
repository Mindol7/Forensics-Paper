#!/usr/bin/env python3
"""Probe the new KCI Open API (open.kci.go.kr/articleSearch).

Verifies:
  - whether title/keyword/abstract params can each be used independently
  - whether combining them is AND (intersection) or OR (union)
  - actual response XML structure (keyword-group presence, etc.)
  - rough total counts per scenario

Usage:
    python tools/probe_kci_v2.py
    python tools/probe_kci_v2.py --term "디지털 포렌식"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from lxml import etree

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

API_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"


def _redact(params: dict) -> dict:
    return {k: ("***" if k == "key" else v) for k, v in params.items()}


def _call(key: str, params: dict, *, label: str) -> None:
    full_params = {"apiCode": "articleSearch", "key": key, "displayCount": 5, **params}
    print(f"\n{'=' * 72}")
    print(f"[{label}]")
    print(f"  params: {_redact(full_params)}")

    started = time.monotonic()
    try:
        response = requests.get(API_URL, params=full_params, timeout=30)
    except requests.RequestException as exc:
        print(f"  REQUEST ERROR: {exc}")
        return
    elapsed = time.monotonic() - started
    print(f"  HTTP {response.status_code}  elapsed: {elapsed:.2f}s  body: {len(response.content)}B")

    body = response.content
    if not body:
        print("  (empty body)")
        return

    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError as exc:
        print(f"  XML PARSE ERROR: {exc}")
        print(f"  raw (first 500B): {body[:500]!r}")
        return

    total = root.findtext(".//result/total")
    records = root.findall(".//record")
    has_keyword_group = root.find(".//keyword-group") is not None
    has_abstract_group = root.find(".//abstract-group") is not None or root.find(".//abstract") is not None

    print(f"  total: {total}  records_in_page: {len(records)}")
    print(f"  has <keyword-group>: {has_keyword_group}   has <abstract>/<abstract-group>: {has_abstract_group}")

    err = root.findtext(".//resultMsg") or root.findtext(".//errMsg") or root.findtext(".//message")
    if err:
        print(f"  serverMsg: {err}")

    for idx, rec in enumerate(records[:2]):
        journal_name = rec.findtext(".//journalInfo/journal-name")
        arti_info = rec.find(".//articleInfo")
        arti_id = arti_info.get("article-id") if arti_info is not None else None
        title_orig = rec.findtext('.//title-group/article-title[@lang="original"]')
        print(f"  sample[{idx}] arti_id={arti_id}  journal={journal_name}")
        print(f"             title={(title_orig or '')[:90]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe KCI Open API (new endpoint)")
    parser.add_argument("--term", default="디지털 포렌식", help="검색어 (default: '디지털 포렌식')")
    args = parser.parse_args()

    key = os.getenv("KCI_OPEN_API_KEY")
    if not key:
        print("ERROR: KCI_OPEN_API_KEY 환경변수가 .env에 없습니다.", file=sys.stderr)
        return 1

    term = args.term
    print(f"Endpoint: {API_URL}")
    print(f"Term:     {term!r}")

    _call(key, {"title": term},                                    label="A. title only")
    _call(key, {"keyword": term},                                  label="B. keyword only (no title)")
    _call(key, {"abstract": term},                                 label="C. abstract only (no title)")
    _call(key, {"title": term, "keyword": term},                   label="D. title + keyword")
    _call(key, {"title": term, "abstract": term},                  label="E. title + abstract")
    _call(key, {"title": term, "keyword": term, "abstract": term}, label="F. title + keyword + abstract")

    # Probe whether we can fetch broadly (without title/keyword) using date range or wildcard
    _call(key, {"regDateFrom": "20250101", "regDateTo": "20250131"}, label="G. date-range only (no title/keyword)")
    _call(key, {"title": "*"},                                       label="H. title='*' wildcard")
    _call(key, {"title": " "},                                       label="I. title=' ' single space")
    _call(key, {"title": "의"},                                      label="J. title='의' (common Korean particle)")
    _call(key, {"title": "the"},                                     label="K. title='the' (common English word)")
    _call(key, {"title": "*", "dateFrom": "202401", "dateTo": "202412"}, label="L. title='*' + 2024년 publication range")
    _call(key, {"title": "*", "dateFrom": "202501", "dateTo": "202503"}, label="M. title='*' + 2025년 1-3월")

    print(f"\n{'=' * 72}\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
