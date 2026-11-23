#!/usr/bin/env python3
"""Compatibility CLI for :mod:`Util.exporter`."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from Util.pdf_statistics import export_report, main, parse_args

__all__ = ["export_report", "main", "parse_args"]


if __name__ == "__main__":
    raise SystemExit(main())
