from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from Analyzer.Graph.build_author_graph import (
    CorpusValidationError,
    PUBLICATION_DATE_RE,
    _collect_author_occurrences,
    _occurrence_evidence_sha256,
    author_id,
    build,
    compile_graph,
    load_corpus,
    normalize_identity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]

def record_id_for(identifier: str, corpus: list[str]) -> str:
    """Mirror the generator: number ascending KCI ids from one."""
    return f"KNOW-DOMP-{sorted(corpus).index(identifier) + 1:07d}"


def markdown(
    identifier: str,
    *,
    record_id: str,
    language: str,
    title: str,
    doi: str,
    authors: str,
    affiliations: str,
    year: int,
    references: list[object],
    record_ids: dict[str, str],
    legacy_date: bool = False,
) -> str:
    ko = language == "ko"
    labels = {
        "heading": "기본 정보" if ko else "Basic Information",
        "header": ("항목", "내용") if ko else ("Item", "Value"),
        "type": "유형" if ko else "Type",
        "journal": "논문지" if ko else "Journal",
        "volume": "권호" if ko else "Vol. and Issue",
        "publisher": "출판 기관" if ko else "Publisher",
        "date": "출판 날짜" if ko else "Publication Date",
        "oa": "OA 여부" if ko else "OA",
        "authors": "저자" if ko else "Authors",
        "affiliations": "소속 기관" if ko else "Affiliations",
        "references": "참고문헌" if ko else "References",
        "reference_title": "REF",
        "applications": "대상 애플리케이션" if ko else "Target Applications",
        "application_name": "이름" if ko else "Name",
        "technique": "애플리케이션 아티팩트 조사" if ko else "Application Artifact Examination",
    }
    reference_rows = []
    for value in references:
        # A reference is either plain text or (text, cited KCI id), where the
        # cited id becomes the table's ID cell.
        text, cited = value if isinstance(value, tuple) else (value, None)
        reference_rows.append(f"| {record_ids[cited] if cited else ''} | {text} |")
    reference_table = "\n".join(reference_rows) or "|  | N/A |"
    if legacy_date:  # profiles written before 2026-10-14
        date_rows = (
            f"| {'출판 연도' if ko else 'Publication Year'} | {year} |\n"
            f"| {'발행일' if ko else 'Published'} | {year}-03 |"
        )
    else:
        date_rows = f"| {labels['date']} | {year}. 03. |"
    return f'''---
subject: {json.dumps(title, ensure_ascii=False)}
identifier: "{record_id}"
classification:
  - EXAM-04-01 # {labels["technique"]}
tags:
  - OS-WIN # Windows
last_updated: "2026-08-31"
author: "Fixture"
---

# {title}

## 1. {labels["heading"]}
| {labels["header"][0]} | {labels["header"][1]} |
|---|---|
| DOI | {doi} |
| URL | https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId={identifier} |
| {labels["type"]} | Article |
| {labels["journal"]} | Test Journal |
| {labels["volume"]} | 1_1 |
| {labels["publisher"]} | Test Publisher |
{date_rows}
| {labels["oa"]} | O |
| {labels["authors"]} | {authors} |
| {labels["affiliations"]} | {affiliations} |

## 2. Abstract
Fixture.

## 3. Keywords
Fixture

## 4. {labels["applications"]}
| ID | {labels["application_name"]} |
|---|---|
|  | N/A |

## 5. {labels["references"]}
| ID | {labels["reference_title"]} |
|---|---|
{reference_table}
'''



def _reviewed_corpus_floor() -> int:
    """The reviewed 527 papers minus those later removed after review (their numbers are retired)."""

    import json as _json

    path = Path(__file__).resolve().parents[3] / "Processor" / "refs" / "excluded_papers.json"
    if not path.is_file():
        return 527
    papers = _json.loads(path.read_text(encoding="utf-8")).get("papers", [])
    # The number a removed paper had while it was in the corpus (numbers are re-sorted afterwards).
    former = [str(item.get("former_record_id") or item.get("record_id") or "0-0").rsplit("-", 1)[1] for item in papers]
    return 527 - sum(1 for number in former if number.isdigit() and 0 < int(number) <= 527)


class FixtureBuildTests(unittest.TestCase):
    def _write_records(self, root: Path, records: list[dict[str, object]]) -> Path:
        source = root / "MD"
        corpus = [record["identifier"] for record in records]
        record_ids = {
            identifier: record_id_for(identifier, corpus) for identifier in corpus
        }
        for record in records:
            directory = source / record_ids[record["identifier"]]
            directory.mkdir(parents=True)
            for language, title in (
                ("ko", record["ko_title"]),
                ("en", record["en_title"]),
            ):
                content = markdown(
                    record["identifier"],
                    record_id=record_ids[record["identifier"]],
                    language=language,
                    title=title,
                    doi=record.get("doi", "N/A"),
                    authors=record["authors"],
                    affiliations=record["affiliations"],
                    year=record["year"],
                    references=record.get("refs", []),
                    record_ids=record_ids,
                )
                (directory / f"{language}.md").write_text(
                    content, encoding="utf-8"
                )
        database = root / "kci_pipeline.db"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE IF EXISTS kci_papers")
            connection.execute(
                "CREATE TABLE kci_papers ("
                "source TEXT NOT NULL, "
                "source_id TEXT NOT NULL UNIQUE, "
                "publication_year INTEGER, "
                "registered_at TEXT"
                ")"
            )
            connection.executemany(
                "INSERT INTO kci_papers "
                "(source, source_id, publication_year, registered_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        "kci",
                        record["identifier"],
                        record["year"],
                        record.get("publication_date", f'{record["year"]}-01'),
                    )
                    for record in records
                ],
            )
        return source

    @staticmethod
    def _database(source: Path) -> Path:
        return source.parent / "kci_pipeline.db"

    def _compile(self, source: Path, **kwargs: object):
        return compile_graph(source, database_path=self._database(source), **kwargs)

    def _write_author_overrides(
        self,
        path: Path,
        source: Path,
        decisions: list[dict[str, object]],
    ) -> Path:
        corpus = load_corpus(source, expected_count=None)
        occurrences_by_name, _, _ = _collect_author_occurrences(corpus)
        payload_decisions = []
        for decision in decisions:
            lookup = {
                occurrence.paper_id: occurrence
                for occurrence in occurrences_by_name[decision["normalized_name"]]
            }
            selected = [lookup[paper_id] for paper_id in decision["paper_ids"]]
            payload_decisions.append({
                **decision,
                "evidence_sha256": _occurrence_evidence_sha256(selected),
                "rationale": decision.get("rationale", "Fixture identity review."),
            })
        path.write_text(
            json.dumps({"schema_version": 1, "decisions": payload_decisions}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _fixture(self, root: Path) -> Path:
        records = [
            {
                "identifier": "ART000000001",
                "ko_title": "고유한 첫 번째 디지털 포렌식 논문",
                "en_title": "A Unique First Digital Forensics Paper",
                "doi": "10.1234/first.1",
                "authors": "홍길동; Alice Kim",
                "affiliations": "홍길동: 테스트대학교<br>Alice Kim: Example Lab",
                "year": 2022,
                "refs": [],
            },
            {
                "identifier": "ART000000002",
                "ko_title": "고유한 두 번째 디지털 포렌식 논문",
                "en_title": "A Unique Second Digital Forensics Paper",
                "doi": "10.1234/second.2",
                "authors": "홍길동",
                "affiliations": "홍길동: 새 테스트대학교",
                "year": 2024,
                "refs": [
                    "A. Author. First. DOI: https://doi.org/10.1234/FIRST.1.",
                    ("홍길동. 고유한 첫 번째 디지털 포렌식 논문. Test Journal. 2022.", "ART000000001"),
                    "Unknown reference that must remain unresolved.",
                ],
            },
        ]
        return self._write_records(root, records)

    def test_deterministic_identity_and_citation_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            index, audit, _ = self._compile(source, expected_count=2)
        self.assertEqual(index["schema_version"], 2)
        self.assertEqual(index["papers"]["ART000000001"]["publication_date"], "2022-01")
        self.assertEqual(index["papers"]["ART000000001"]["publication_year"], 2022)
        self.assertEqual(index["papers"]["ART000000001"]["publication_month"], 1)
        self.assertEqual(index["papers"]["ART000000001"]["publication_date_precision"], "month")
        self.assertRegex(index["build_inputs"]["publication_dates_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            index["build_inputs"]["publication_dates_sha256"],
            audit["publication_dates"]["publication_dates_sha256"],
        )
        expected = "AUT-" + hashlib.sha256(normalize_identity("홍길동").encode()).hexdigest()[:12]
        self.assertEqual(author_id("  홍길동 "), expected)
        self.assertEqual(index["papers"]["ART000000001"]["internal_citation_count"], 1)
        self.assertEqual(len(index["citation_edges"]), 1)
        # Reference 1 resolves by DOI and reference 2 by KCI's recorded link;
        # they collapse into one edge labelled by the stronger evidence.
        self.assertEqual(index["citation_edges"][0]["method"], "kci_link")
        self.assertEqual(index["citation_edges"][0]["reference_numbers"], [1, 2])
        self.assertEqual(audit["citation_resolution"]["counts"]["method_doi"], 1)
        self.assertEqual(audit["citation_resolution"]["counts"]["method_kci_link"], 1)
        self.assertEqual(audit["citation_resolution"]["counts"]["unresolved"], 1)
        hong_ids = {
            index["papers"][paper_id]["author_ids"][0]
            for paper_id in ("ART000000001", "ART000000002")
        }
        self.assertEqual(len(hong_ids), 1, "compatible affiliation history should merge")
        hong = next(author for author in index["authors"] if author["id"] in hong_ids)
        self.assertEqual(hong["identity_confidence"], "evidence_based")

    def test_publication_date_digest_is_independent_of_database_path_and_row_order(self) -> None:
        records = [
            {
                "identifier": "ART000000061",
                "ko_title": "월 정보 첫 논문",
                "en_title": "First Month Paper",
                "authors": "월저자",
                "affiliations": "월저자: Test University",
                "year": 2022,
                "publication_date": "2022-03",
            },
            {
                "identifier": "ART000000062",
                "ko_title": "월 정보 둘째 논문",
                "en_title": "Second Month Paper",
                "authors": "월저자",
                "affiliations": "월저자: Test University",
                "year": 2024,
                "publication_date": "2024-11",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_source = self._write_records(root / "first", records)
            second_source = self._write_records(root / "second", list(reversed(records)))
            first, _, _ = self._compile(first_source, expected_count=2)
            second, _, _ = self._compile(second_source, expected_count=2)
        self.assertEqual(
            first["build_inputs"]["publication_dates_sha256"],
            second["build_inputs"]["publication_dates_sha256"],
        )
        self.assertEqual(first["build_input_digest"], second["build_input_digest"])

    def test_publication_date_join_fails_closed(self) -> None:
        cases = (
            (
                "missing corpus ID",
                "DELETE FROM kci_papers WHERE source_id = 'ART000000002'",
                "missing 1 corpus IDs",
            ),
            (
                "invalid month",
                "UPDATE kci_papers SET registered_at = '2022-13' "
                "WHERE source_id = 'ART000000001'",
                "must be exact KCI YYYY-MM",
            ),
            (
                "null publication date",
                "UPDATE kci_papers SET registered_at = NULL "
                "WHERE source_id = 'ART000000001'",
                "registered_at must contain KCI YYYY-MM",
            ),
            (
                "database year mismatch",
                "UPDATE kci_papers SET publication_year = 2021 "
                "WHERE source_id = 'ART000000001'",
                "publication year mismatch",
            ),
        )
        for label, statement, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                source = self._fixture(Path(temporary))
                with sqlite3.connect(self._database(source)) as connection:
                    connection.execute(statement)
                with self.assertRaisesRegex(CorpusValidationError, expected_error):
                    self._compile(source, expected_count=2)

    def test_build_replaces_stale_output_and_copies_bilingual_md(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._fixture(root)
            output = root / "web-data"
            audit = root / "audit" / "audit.json"
            output.mkdir()
            (output / "stale.txt").write_text("stale", encoding="utf-8")
            build(
                source,
                output,
                audit,
                database_path=self._database(source),
                expected_count=2,
            )
            self.assertFalse((output / "stale.txt").exists())
            self.assertEqual(len(list((output / "papers").glob("KNOW-DOMP-*/ko.md"))), 2)
            self.assertEqual(len(list((output / "papers").glob("KNOW-DOMP-*/en.md"))), 2)
            self.assertTrue((output / "author-graph.json").is_file())
            self.assertTrue(audit.is_file())
            self.assertFalse((output / "papers" / "ko").exists())
            self.assertFalse((output / "papers" / "en").exists())
            index = json.loads((output / "author-graph.json").read_text(encoding="utf-8"))
            for paper in index["papers"].values():
                self.assertRegex(paper["id"], r"^ART\d{9}$")
                self.assertRegex(paper["record_id"], r"^KNOW-DOMP-\d{7}$")
                for language in ("ko", "en"):
                    relative = f'papers/{paper["record_id"]}/{language}.md'
                    self.assertEqual(paper["md"][language], f"./data/graph/{relative}")
                    self.assertEqual(
                        (output / relative).read_bytes(),
                        (source / paper["record_id"] / f"{language}.md").read_bytes(),
                    )

    def test_new_template_authors_oa_and_four_letter_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            before, _, _ = self._compile(source, expected_count=2)
            for language in ("ko", "en"):
                path = source / "KNOW-DOMP-0000001" / f"{language}.md"
                path.write_text(
                    path.read_text(encoding="utf-8")
                    .replace("홍길동; Alice Kim", "홍길동, Alice Kim")
                    .replace("| O |", "| X |"),
                    encoding="utf-8",
                )
            after, _, corpus = self._compile(source, expected_count=2)
            paper = after["papers"]["ART000000001"]
            self.assertEqual(paper["author_names"], ["홍길동", "Alice Kim"])
            self.assertEqual(paper["author_ids"], before["papers"]["ART000000001"]["author_ids"])
            self.assertEqual(paper["oa"], "closed")
            self.assertEqual(corpus["ko"][paper["id"]].classification, ("EXAM-04-01",))
            self.assertEqual(before["citation_edges"], after["citation_edges"])

    def test_publication_date_row_and_legacy_year_rows_give_the_same_year(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            current, _, _ = self._compile(source, expected_count=2)
            for language in ("ko", "en"):
                path = source / "KNOW-DOMP-0000001" / f"{language}.md"
                text = path.read_text(encoding="utf-8")
                date_label = "출판 날짜" if language == "ko" else "Publication Date"
                year_label, month_label = ("출판 연도", "발행일") if language == "ko" else ("Publication Year", "Published")
                year = current["papers"]["ART000000001"]["year"]
                self.assertIn(f"| {date_label} | {year}. 03. |", text)
                path.write_text(
                    text.replace(f"| {date_label} | {year}. 03. |", f"| {year_label} | {year} |\n| {month_label} | {year}-03 |"),
                    encoding="utf-8",
                )
            legacy, _, _ = self._compile(source, expected_count=2)
            without_hash = lambda papers: {key: {k: v for k, v in paper.items() if k != "md_sha256"} for key, paper in papers.items()}
            self.assertEqual(without_hash(legacy["papers"]), without_hash(current["papers"]))
            path = source / "KNOW-DOMP-0000001" / "ko.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(f"| 출판 연도 | {year} |\n", f"| 출판 연도 | {year} |\n| 출판 날짜 | {year}. 03. |\n"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CorpusValidationError, "paper-information fields differ"):
                self._compile(source, expected_count=2)

    def test_missing_translation_and_legacy_classification_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            path = source / "KNOW-DOMP-0000001" / "en.md"
            path.rename(path.with_suffix(".missing"))
            with self.assertRaisesRegex(CorpusValidationError, "missing en source Markdown"):
                self._compile(source, expected_count=2)
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            path = source / "KNOW-DOMP-0000001" / "ko.md"
            path.write_text(path.read_text(encoding="utf-8").replace("EXAM-04-01", "EXM-04-01"), encoding="utf-8")
            with self.assertRaisesRegex(CorpusValidationError, "invalid classification id"):
                self._compile(source, expected_count=2)

    def test_persistent_profile_ids_do_not_depend_on_article_sort_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            before, _, _ = self._compile(source, expected_count=2)
            replacements = {
                "KNOW-DOMP-0000001": "KNOW-DOMP-0000900",
                "KNOW-DOMP-0000002": "KNOW-DOMP-0000040",
            }
            for profile in sorted(source.glob("KNOW-DOMP-*")):
                for language in ("ko", "en"):
                    path = profile / f"{language}.md"
                    content = path.read_text(encoding="utf-8")
                    for previous, current in replacements.items():
                        content = content.replace(previous, current)
                    path.write_text(content, encoding="utf-8")
                profile.rename(source / replacements[profile.name])
            after, _, _ = self._compile(source, expected_count=2)
            self.assertEqual(before["citation_edges"], after["citation_edges"])
            self.assertEqual(before["authors"], after["authors"])
            self.assertEqual(set(before["papers"]), set(after["papers"]))
            self.assertEqual(after["papers"]["ART000000001"]["record_id"], "KNOW-DOMP-0000900")
            self.assertEqual(after["papers"]["ART000000002"]["record_id"], "KNOW-DOMP-0000040")

    def test_identifier_mismatch_fails_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._fixture(Path(temporary))
            path = source / "KNOW-DOMP-0000001" / "ko.md"
            path.write_text(path.read_text(encoding="utf-8").replace(
                'identifier: "KNOW-DOMP-0000001"', 'identifier: "KNOW-DOMP-0000009"'
            ), encoding="utf-8")
            with self.assertRaises(CorpusValidationError):
                self._compile(source, expected_count=2)

    def test_unknown_affiliations_never_merge_namesakes(self) -> None:
        records = [
            {
                "identifier": "ART000000011",
                "ko_title": "동명이인 첫 논문",
                "en_title": "Namesake Paper One",
                "authors": "김동명",
                "affiliations": "김동명: N / A",
                "year": 2022,
            },
            {
                "identifier": "ART000000012",
                "ko_title": "동명이인 둘째 논문",
                "en_title": "Namesake Paper Two",
                "authors": "김동명",
                "affiliations": "김동명: 소속 미상",
                "year": 2023,
            },
            {
                "identifier": "ART000000013",
                "ko_title": "동명이인 셋째 논문",
                "en_title": "Namesake Paper Three",
                "authors": "김동명",
                "affiliations": "김동명: unknown",
                "year": 2024,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = self._write_records(Path(temporary), records)
            index, audit, _ = self._compile(source, expected_count=3)
        ids = [index["papers"][paper_id]["author_ids"][0] for paper_id in sorted(index["papers"])]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(not author["affiliations"] for author in index["authors"]))
        self.assertTrue(all(author["representative_affiliation"] is None for author in index["authors"]))
        self.assertTrue(all("소속 미상" not in author["disambiguation_label"] for author in index["authors"]))
        candidate = audit["author_identity"]["author_identity_candidates"][0]
        self.assertEqual(candidate["resolved_identity_count"], 3)

    def test_automatic_identity_id_survives_a_new_disconnected_namesake(self) -> None:
        first = {
            "identifier": "ART000000021",
            "ko_title": "기존 저자 논문",
            "en_title": "Existing Author Paper",
            "authors": "김안정",
            "affiliations": "김안정: Alpha University",
            "year": 2022,
        }
        second = {
            "identifier": "ART000000022",
            "ko_title": "새 동명이인 논문",
            "en_title": "New Namesake Paper",
            "authors": "김안정",
            "affiliations": "김안정: Beta Institute",
            "year": 2024,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_source = self._write_records(root / "one", [first])
            first_index, _, _ = self._compile(first_source, expected_count=1)
            existing_id = first_index["papers"][first["identifier"]]["author_ids"][0]
            legacy_id = author_id("김안정")
            self.assertEqual(first_index["author_id_aliases"][legacy_id], existing_id)

            second_source = self._write_records(root / "two", [first, second])
            second_index, _, _ = self._compile(second_source, expected_count=2)
        self.assertEqual(second_index["papers"][first["identifier"]]["author_ids"][0], existing_id)
        self.assertNotIn(legacy_id, second_index["author_id_aliases"])
        self.assertEqual(
            set(second_index["ambiguous_author_id_aliases"][legacy_id]),
            set(author["id"] for author in second_index["authors"]),
        )

    def test_new_bridge_preserves_every_former_component_id_as_an_alias(self) -> None:
        first = {
            "identifier": "ART000000023",
            "ko_title": "첫 연구팀 논문",
            "en_title": "First Team Paper",
            "authors": "김가교; Alice",
            "affiliations": "김가교: N/A<br>Alice: Alpha Lab",
            "year": 2022,
        }
        second = {
            "identifier": "ART000000024",
            "ko_title": "둘째 연구팀 논문",
            "en_title": "Second Team Paper",
            "authors": "김가교; Bob",
            "affiliations": "김가교: N/A<br>Bob: Beta Lab",
            "year": 2023,
        }
        bridge = {
            "identifier": "ART000000025",
            "ko_title": "연결 연구팀 논문",
            "en_title": "Bridge Team Paper",
            "authors": "김가교; Alice; Bob",
            "affiliations": "김가교: N/A<br>Alice: Alpha Lab<br>Bob: Beta Lab",
            "year": 2024,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before_source = self._write_records(root / "before", [first, second])
            before, _, _ = self._compile(before_source, expected_count=2)
            former_ids = {
                before["papers"][paper_id]["author_ids"][0]
                for paper_id in before["papers"]
            }
            self.assertEqual(len(former_ids), 2)

            after_source = self._write_records(root / "after", [first, second, bridge])
            after, _, _ = self._compile(after_source, expected_count=3)
        current_ids = {
            after["papers"][paper_id]["author_ids"][0]
            for paper_id in after["papers"]
        }
        self.assertEqual(len(current_ids), 1)
        current_id = next(iter(current_ids))
        self.assertIn(current_id, former_ids)
        former_secondary = next(identifier for identifier in former_ids if identifier != current_id)
        self.assertEqual(after["author_id_aliases"][former_secondary], current_id)

    def test_assignment_override_hard_splits_automatic_merge(self) -> None:
        records = [
            {
                "identifier": "ART000000031",
                "ko_title": "자동 병합 첫 논문",
                "en_title": "Automatic Merge One",
                "authors": "김민지; 공통공저자",
                "affiliations": "김민지: Same University<br>공통공저자: Same University",
                "year": 2022,
            },
            {
                "identifier": "ART000000032",
                "ko_title": "자동 병합 둘째 논문",
                "en_title": "Automatic Merge Two",
                "authors": "김민지; 공통공저자",
                "affiliations": "김민지: Same University<br>공통공저자: Same University",
                "year": 2024,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._write_records(root, records)
            without_override, _, _ = self._compile(source, expected_count=2)
            self.assertEqual(len({
                without_override["papers"][paper_id]["author_ids"][0]
                for paper_id in without_override["papers"]
            }), 1)
            overrides = self._write_author_overrides(root / "author-overrides.json", source, [
                {
                    "normalized_name": "김민지",
                    "action": "assign",
                    "identity_key": "paper-art000000031-1",
                    "paper_ids": ["ART000000031"],
                },
                {
                    "normalized_name": "김민지",
                    "action": "assign",
                    "identity_key": "reviewed-second-person",
                    "paper_ids": ["ART000000032"],
                },
            ])
            index, audit, _ = self._compile(
                source, expected_count=2, author_overrides_path=overrides
            )
        self.assertEqual(len({
            index["papers"][paper_id]["author_ids"][0] for paper_id in index["papers"]
        }), 2)
        candidate = next(
            item for item in audit["author_identity"]["author_identity_candidates"]
            if item["normalized_name"] == "김민지"
        )
        self.assertEqual(candidate["automatic_component_count"], 1)
        self.assertEqual(candidate["resolved_identity_count"], 2)
        self.assertTrue(any(
            edge["blocked_by_assignment_override"]
            for edge in candidate["automatic_evidence_edges"]
        ))

    def test_decision_emptied_by_a_removed_paper_is_retired(self) -> None:
        """A reviewed merge whose other paper was removed after review must not fail the build."""

        from unittest import mock

        from Analyzer.Graph import build_author_graph as builder

        records = [
            {
                "identifier": f"ART00000004{number}",
                "ko_title": f"남은 논문 {number}",
                "en_title": f"Remaining Paper {number}",
                "authors": "윤검토",
                "affiliations": f"윤검토: University {number}",
                "year": 2020 + number,
            }
            for number in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._write_records(root, records)
            overrides = self._write_author_overrides(root / "author-overrides.json", source, [{
                "normalized_name": "윤검토", "action": "merge", "identity_key": "reviewed-person",
                "paper_ids": ["ART000000041", "ART000000042"],
            }])
            payload = json.loads(overrides.read_text(encoding="utf-8"))
            # The decision was written when a third paper was still part of the corpus.
            payload["decisions"][0]["paper_ids"] = ["ART000000041", "ART000000049"]
            overrides.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(CorpusValidationError, "references a paper without that author"):
                self._compile(source, expected_count=2, author_overrides_path=overrides)
            with mock.patch.object(builder, "_removed_paper_ids", return_value=frozenset({"ART000000049"})):
                _, audit, _ = self._compile(source, expected_count=2, author_overrides_path=overrides)
            self.assertEqual(audit["author_identity"]["reviewed_decisions"], [])

            # Two papers still remain: the reviewer has to confirm the smaller merge, so it is reported.
            payload["decisions"][0]["paper_ids"] = ["ART000000041", "ART000000042", "ART000000049"]
            overrides.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(builder, "_removed_paper_ids", return_value=frozenset({"ART000000049"})):
                with self.assertRaisesRegex(CorpusValidationError, "lost .*ART000000049.* to the exclusion list"):
                    self._compile(source, expected_count=2, author_overrides_path=overrides)

    def test_identity_override_hash_ignores_content_but_rejects_identity_change(self) -> None:
        records = [
            {
                "identifier": "ART000000041",
                "ko_title": "검토 병합 첫 논문",
                "en_title": "Reviewed Merge One",
                "authors": "이검토",
                "affiliations": "이검토: Old University",
                "year": 2022,
            },
            {
                "identifier": "ART000000042",
                "ko_title": "검토 병합 둘째 논문",
                "en_title": "Reviewed Merge Two",
                "authors": "이검토",
                "affiliations": "이검토: New Institute",
                "year": 2024,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._write_records(root, records)
            unreviewed_index, _, _ = self._compile(source, expected_count=2)
            former_component_ids = {
                unreviewed_index["papers"][paper_id]["author_ids"][0]
                for paper_id in unreviewed_index["papers"]
            }
            self.assertEqual(len(former_component_ids), 2)
            overrides = self._write_author_overrides(root / "author-overrides.json", source, [{
                "normalized_name": "이검토",
                "action": "merge",
                "identity_key": "reviewed-career-history",
                "paper_ids": ["ART000000041", "ART000000042"],
            }])
            original_index, original_audit, _ = self._compile(
                source, expected_count=2, author_overrides_path=overrides
            )
            self.assertIsNone(original_index["build_inputs"]["citation_overrides_sha256"])
            self.assertRegex(
                original_index["build_inputs"]["author_identity_overrides_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(
                original_index["build_input_digest"], original_audit["build_input_digest"]
            )
            reviewed_author_id = original_index["papers"]["ART000000041"]["author_ids"][0]
            self.assertTrue(all(
                original_index["author_id_aliases"][former_id] == reviewed_author_id
                for former_id in former_component_ids
            ))

            # Override provenance participates in freshness independently of
            # the Markdown source digest.
            override_payload = json.loads(overrides.read_text(encoding="utf-8"))
            override_payload["decisions"][0]["rationale"] = "Re-reviewed fixture career history."
            overrides.write_text(
                json.dumps(override_payload, ensure_ascii=False), encoding="utf-8"
            )
            reviewed_index, _, _ = self._compile(
                source, expected_count=2, author_overrides_path=overrides
            )
            self.assertEqual(reviewed_index["source_digest"], original_index["source_digest"])
            self.assertNotEqual(
                reviewed_index["build_inputs"]["author_identity_overrides_sha256"],
                original_index["build_inputs"]["author_identity_overrides_sha256"],
            )
            self.assertNotEqual(reviewed_index["build_input_digest"], original_index["build_input_digest"])

            # Abstract/title edits change whole MD/source hashes, but not the
            # canonical identity evidence to which the decision is bound.
            for language, old_title, new_title in (
                ("ko", "검토 병합 첫 논문", "내용만 바뀐 첫 논문"),
                ("en", "Reviewed Merge One", "Content-Only Revised Paper"),
            ):
                path = source / "KNOW-DOMP-0000001" / f"{language}.md"
                path.write_text(
                    path.read_text(encoding="utf-8")
                    .replace(old_title, new_title)
                    .replace("Fixture.", "Changed abstract only."),
                    encoding="utf-8",
                )
            self._compile(source, expected_count=2, author_overrides_path=overrides)

            for language in ("ko", "en"):
                path = source / "KNOW-DOMP-0000002" / f"{language}.md"
                path.write_text(
                    path.read_text(encoding="utf-8").replace("New Institute", "Third Institute"),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(CorpusValidationError, "evidence hash is stale"):
                self._compile(source, expected_count=2, author_overrides_path=overrides)

    def test_citation_override_file_participates_in_build_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._fixture(root)
            overrides = root / "citation-overrides.json"
            overrides.write_text(
                json.dumps({"schema_version": 1, "overrides": []}), encoding="utf-8"
            )
            original, _, _ = self._compile(
                source, expected_count=2, overrides_path=overrides
            )
            overrides.write_text(
                json.dumps({
                    "schema_version": 1,
                    "policy": "review contract changed",
                    "overrides": [],
                }),
                encoding="utf-8",
            )
            changed, _, _ = self._compile(
                source, expected_count=2, overrides_path=overrides
            )
        self.assertEqual(original["source_digest"], changed["source_digest"])
        self.assertNotEqual(
            original["build_inputs"]["citation_overrides_sha256"],
            changed["build_inputs"]["citation_overrides_sha256"],
        )
        self.assertNotEqual(original["build_input_digest"], changed["build_input_digest"])


class RealCorpusValidationTests(unittest.TestCase):
    def test_real_pair_corpus_compiles_and_invariants_hold(self) -> None:
        source = PROJECT_ROOT / "Analyzer" / "Paper" / "MD"
        profile_count = sum(
            1 for path in source.iterdir() if path.is_dir() and path.name.startswith("KNOW-DOMP-")
        )
        index, audit, _ = compile_graph(
            source,
            database_path=PROJECT_ROOT / "kci_pipeline.db",
            expected_count=profile_count,
        )
        self.assertEqual(index["schema_version"], 2)
        # The corpus grows monthly; the reviewed 527 papers are its floor.
        self.assertEqual(index["corpus"]["paper_count"], profile_count)
        self.assertGreaterEqual(index["corpus"]["paper_count"], _reviewed_corpus_floor())
        self.assertGreaterEqual(index["corpus"]["authorship_count"], 1325)
        self.assertEqual(index["corpus"]["publication_date_coverage"], profile_count)
        self.assertEqual(index["corpus"]["publication_date_precision"], "month")
        self.assertEqual(index["corpus"]["publication_date_min"], "2020-03")
        self.assertGreaterEqual(index["corpus"]["publication_date_max"], "2026-06")
        self.assertEqual(sum(index["corpus"]["publication_month_counts"].values()), profile_count)
        self.assertRegex(index["source_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(index["build_input_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(index["build_inputs"]["markdown_source_sha256"], index["source_digest"])
        self.assertRegex(index["build_inputs"]["publication_dates_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            index["build_inputs"]["publication_dates_sha256"],
            index["corpus"]["publication_dates_sha256"],
        )
        self.assertEqual(index["build_input_digest"], audit["build_input_digest"])
        self.assertEqual(index["build_inputs"], audit["build_inputs"])
        self.assertEqual(
            index["build_inputs"]["citation_overrides_sha256"],
            hashlib.sha256((PROJECT_ROOT / "Analyzer/Graph/citation_overrides.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            index["build_inputs"]["author_identity_overrides_sha256"],
            hashlib.sha256(
                (PROJECT_ROOT / "Analyzer/Graph/author_identity_overrides.json").read_bytes()
            ).hexdigest(),
        )
        # Internal citations grow with every increment; 436 was the reviewed corpus.
        self.assertGreaterEqual(index["corpus"]["citation_edge_count"], 436)
        self.assertEqual(index["corpus"]["reviewed_override_count"], 2)
        # Every edge rests on recorded evidence: KCI's own referenced-article
        # link, a DOI match inside the corpus, or a reviewed override.
        # The counts grow with the corpus; what is fixed is how they relate: a DOI
        # match adds nothing KCI's own link had not found, and the two reviewed
        # overrides add exactly two edges.
        checkpoints = audit["citation_resolution"]["edge_count_checkpoints"]
        self.assertGreaterEqual(checkpoints["kci_link"], 434)
        self.assertEqual(checkpoints["doi"], checkpoints["kci_link"])
        self.assertEqual(checkpoints["reviewed"], checkpoints["kci_link"] + 2)
        self.assertEqual(checkpoints["reviewed"], index["corpus"]["citation_edge_count"])
        self.assertEqual(
            {item["resolution_strategy"] for item in audit["citation_resolution"]["resolved_references"]},
            {"kci_link", "reviewed"},
        )
        self.assertEqual(set(index["papers"]), {paper["id"] for paper in index["papers"].values()})
        self.assertEqual(index["corpus"]["author_count"], len(index["authors"]))
        self.assertEqual(
            index["corpus"]["authorship_count"],
            sum(len(paper["author_ids"]) for paper in index["papers"].values()),
        )
        author_ids = {author["id"] for author in index["authors"]}
        self.assertEqual(len(author_ids), len(index["authors"]))
        self.assertTrue(all(
            author["publication_date_min"]
            == min(index["papers"][paper_id]["publication_date"] for paper_id in author["paper_ids"])
            and author["publication_date_max"]
            == max(index["papers"][paper_id]["publication_date"] for paper_id in author["paper_ids"])
            and all(
                history["publication_date"]
                == index["papers"][history["paper_id"]]["publication_date"]
                for history in author["affiliation_history"]
            )
            for author in index["authors"]
        ))
        self.assertTrue(all(
            len(paper["author_ids"]) == len(paper["author_names"])
            and set(paper["author_ids"]) <= author_ids
            for paper in index["papers"].values()
        ))
        self.assertTrue(all(
            PUBLICATION_DATE_RE.fullmatch(paper["publication_date"])
            and paper["publication_year"] == paper["year"]
            and paper["publication_date"]
            == f'{paper["publication_year"]:04d}-{paper["publication_month"]:02d}'
            and paper["publication_date_precision"] == "month"
            for paper in index["papers"].values()
        ))
        identity_audit = audit["author_identity"]
        self.assertEqual(
            identity_audit["counts"]["candidate_name_count"],
            len(identity_audit["author_identity_candidates"]),
        )
        self.assertEqual(
            identity_audit["counts"]["resolved_author_count"],
            index["corpus"]["author_count"],
        )
        self.assertEqual(
            identity_audit["counts"]["authorship_count"],
            index["corpus"]["authorship_count"],
        )
        self.assertEqual(
            identity_audit["counts"]["candidate_name_count"],
            identity_audit["counts"]["repeated_name_count"],
        )
        reviewed = {
            (decision["normalized_name"], decision["action"])
            for decision in identity_audit["reviewed_decisions"]
        }
        self.assertTrue({
            ("최윤성", "merge"),
            ("서승희", "merge"),
            ("송종화", "merge"),
            ("김승규", "merge"),
            ("방제완", "merge"),
            ("박정흠", "merge"),
            ("나보미", "merge"),
            ("김민지", "assign"),
            ("김혜지", "merge"),
            ("정다은", "merge"),
            ("장진수", "merge"),
            ("홍준호", "merge"),
            ("유제혁", "merge"),
            ("성민규", "merge"),
        } <= reviewed)
        # 곽대훈's and 윤신규's merges each joined two papers, one of which was later
        # removed from the corpus after review; a merge of one paper has nothing
        # to do, so those decisions are retired rather than applied.
        self.assertTrue({("곽대훈", "merge"), ("윤신규", "merge")}.isdisjoint(reviewed))
        confirmed_names = {
            decision["normalized_name"]
            for decision in identity_audit["reviewed_decisions"]
            if decision["effect"] == "confirmed_automatic"
        }
        self.assertTrue({
            "김혜지", "정다은", "장진수", "홍준호", "유제혁", "성민규"
        } <= confirmed_names)
        kim_minji = next(
            item for item in identity_audit["author_identity_candidates"]
            if item["normalized_name"] == "김민지"
        )
        self.assertEqual(
            (kim_minji["automatic_component_count"], kim_minji["resolved_identity_count"]),
            (1, 2),
        )
        self.assertTrue(any(
            edge["blocked_by_assignment_override"]
            for edge in kim_minji["automatic_evidence_edges"]
        ))
        namesake_labels: dict[str, list[str]] = {}
        for author in index["authors"]:
            namesake_labels.setdefault(author["normalized_name"], []).append(
                author["disambiguation_label"]
            )
            self.assertTrue(all(
                normalize_identity(affiliation) not in {
                    "n/a", "na", "없음", "소속 미상", "unknown"
                }
                for affiliation in author["affiliations"]
            ))
        self.assertTrue(all(
            len(labels) == len(set(labels)) for labels in namesake_labels.values()
        ))
        self.assertFalse(set(index["author_id_aliases"]) & set(index["ambiguous_author_id_aliases"]))
        incoming = {paper_id: 0 for paper_id in index["papers"]}
        for edge in index["citation_edges"]:
            incoming[edge["target"]] += 1
        self.assertTrue(all(
            paper["internal_citation_count"] == incoming[paper_id]
            for paper_id, paper in index["papers"].items()
        ))
        counts = audit["citation_resolution"]["counts"]
        self.assertEqual(counts["total_references"], index["corpus"]["reference_count"])


if __name__ == "__main__":
    unittest.main()
