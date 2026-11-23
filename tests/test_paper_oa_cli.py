from __future__ import annotations

import pytest

from Analyzer.OA.paper_oa import (
    DEFAULT_NOT_OPEN_ACCESS_CACHE_TTL_SECONDS,
    DEFAULT_OPEN_ACCESS_CACHE_TTL_SECONDS,
    DEFAULT_UNRESOLVED_CACHE_TTL_SECONDS,
    _build_parser,
    cache_ttl_seconds,
)


def test_cache_ttl_flags_default_to_the_collector_constants() -> None:
    args = _build_parser().parse_args([])
    assert cache_ttl_seconds(args) == {
        "open_access_ttl_seconds": DEFAULT_OPEN_ACCESS_CACHE_TTL_SECONDS,
        "not_open_access_ttl_seconds": DEFAULT_NOT_OPEN_ACCESS_CACHE_TTL_SECONDS,
        "unresolved_ttl_seconds": DEFAULT_UNRESOLVED_CACHE_TTL_SECONDS,
    }
    assert args.canonical_when_unresolved is False


def test_cache_ttl_flags_translate_days_and_hours_to_seconds() -> None:
    args = _build_parser().parse_args(
        ["--open-access-ttl-days", "180", "--not-open-access-ttl-days", "60", "--unresolved-ttl-hours", "12"]
    )
    assert cache_ttl_seconds(args) == {
        "open_access_ttl_seconds": 180 * 86_400,
        "not_open_access_ttl_seconds": 60 * 86_400,
        "unresolved_ttl_seconds": 12 * 3_600,
    }
    with pytest.raises(ValueError, match="must be positive"):
        cache_ttl_seconds(_build_parser().parse_args(["--not-open-access-ttl-days", "0"]))
