from __future__ import annotations

from dataclasses import dataclass

from Core import collect_builder
from Core import query_builder


def test_legacy_query_builder_reexports_new_query_types() -> None:
    assert query_builder.KciQuery is collect_builder.KciQuery
    assert query_builder.get_kci_monthly_ranges is collect_builder.get_kci_monthly_ranges


@dataclass
class _Paper:
    raw_payload: dict[str, object]


def test_article_detail_targets_group_each_exact_art_id_once() -> None:
    first = _Paper({"arti_id": "ART0001"})
    duplicate = _Paper({"arti_id": "ART0001"})
    second = _Paper({"arti_id": " ART0002 "})
    missing = _Paper({})

    targets = collect_builder.build_article_detail_targets(
        [first, duplicate, second, missing]
    )

    assert targets == {
        "ART0001": [first, duplicate],
        "ART0002": [second],
    }


class _MonthProbe:
    def probe_total(self, query: collect_builder.KciQuery) -> int:
        if query.date_from == "202401" and query.date_to == "202402":
            return 20_001
        return 1 if query.date_from == "202401" else 2


def test_oversized_publication_range_is_split_into_months() -> None:
    messages: list[str] = []
    slices = collect_builder.build_kci_query_slices(
        _MonthProbe(),
        collect_builder.KciQuery(
            title="*",
            date_from="202401",
            date_to="202402",
        ),
        on_split=messages.append,
    )

    assert [(item.query.date_from, item.total_count) for item in slices] == [
        ("202401", 1),
        ("202402", 2),
    ]
    assert messages and "publication month" in messages[0]


class _RegistrationProbe:
    def probe_total(self, query: collect_builder.KciQuery) -> int:
        if query.reg_date_from in (None, collect_builder.KCI_REG_DATE_MIN) and query.reg_date_to in (
            None,
            collect_builder.KCI_REG_DATE_MAX,
        ):
            return collect_builder.KCI_MAX_RESULTS_PER_QUERY + 1
        return 5


def test_oversized_month_is_split_by_registration_date() -> None:
    slices = collect_builder.build_kci_query_slices(
        _RegistrationProbe(),
        collect_builder.KciQuery(
            title="*",
            date_from="202401",
            date_to="202401",
        ),
    )

    assert len(slices) == 2
    assert all(item.total_count == 5 for item in slices)
    assert slices[0].query.reg_date_from == collect_builder.KCI_REG_DATE_MIN
    assert slices[-1].query.reg_date_to == collect_builder.KCI_REG_DATE_MAX
