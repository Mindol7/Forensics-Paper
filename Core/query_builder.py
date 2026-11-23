"""Backward-compatible imports for the renamed collection builder.

New code should import query models and builders from
``Core.collect_builder``.  This module remains so existing integrations using
``Core.query_builder`` continue to work during the architecture migration.
"""

from Core.collect_builder import (
    KciQuery,
    get_kci_monthly_ranges,
    get_kci_target_years,
)

__all__ = [
    "KciQuery",
    "get_kci_monthly_ranges",
    "get_kci_target_years",
]
