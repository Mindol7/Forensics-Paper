from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from Analyzer.Graph.build_author_graph import (
    CorpusValidationError,
    compile_graph as compile_author_graph,
    normalize_identity,
    write_outputs as write_author_outputs,
)
from Analyzer.Graph.build_institution_graph import (
    DEFAULT_INSTITUTION_OVERRIDES,
    build,
    compile_institution_graph,
    institution_id,
    load_institution_catalog,
    normalize_institution_identity,
)
from Analyzer.Graph.tests.test_build_author_graph import markdown, record_id_for



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


class InstitutionGraphFixtureTests(unittest.TestCase):
    def _write_records(self, root: Path, records: list[dict[str, object]]) -> tuple[Path, Path]:
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
            connection.execute(
                "CREATE TABLE kci_papers ("
                "source TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE, "
                "publication_year INTEGER, registered_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO kci_papers VALUES (?, ?, ?, ?)",
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
        return source, database

    @staticmethod
    def _write_catalog(path: Path) -> Path:
        payload = {
            "schema_version": 1,
            "canonicalizations": [
                {
                    "canonical_name": "고려대학교",
                    "aliases": [
                        "고려대학교 정보보호대학원",
                        "Department of Information Security, Korea University, Republic of Korea",
                    ],
                    "rationale": "Fixture-reviewed parent institution.",
                },
                {
                    "canonical_name": "경찰대학",
                    "aliases": ["경찰대학 치안정책연구소"],
                    "rationale": "Fixture-reviewed parent institution.",
                },
                {
                    "canonical_name": "순천향대학교",
                    "aliases": ["순천향대학교 대학원"],
                    "rationale": "Fixture-reviewed parent institution.",
                },
            ],
            "splits": [
                {
                    "raw_name": "경찰대학 치안정책연구소, 순천향대학교 대학원",
                    "institutions": ["경찰대학 치안정책연구소", "순천향대학교 대학원"],
                    "rationale": "Fixture-reviewed comma split.",
                }
            ],
            "exclusions": [
                {"raw_name": "판사", "rationale": "Fixture job title."}
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        records = [
            {
                "identifier": "ART000000001",
                "ko_title": "기관 그래프 첫 논문",
                "en_title": "Institution Graph Paper One",
                "doi": "10.1234/institution.1",
                "authors": "홍길동; Alice",
                "affiliations": (
                    "홍길동: 고려대학교 정보보호대학원<br>"
                    "Alice: Department of Information Security, Korea University, Republic of Korea"
                ),
                "year": 2022,
            },
            {
                "identifier": "ART000000002",
                "ko_title": "기관 그래프 둘째 논문",
                "en_title": "Institution Graph Paper Two",
                "authors": "Bob; Carol",
                "affiliations": (
                    "Bob: 경찰대학 치안정책연구소, 순천향대학교 대학원<br>Carol: 판사"
                ),
                "year": 2024,
                "refs": ["H. Author. DOI: https://doi.org/10.1234/INSTITUTION.1."],
            },
            {
                "identifier": "ART000000003",
                "ko_title": "기관 불명 논문",
                "en_title": "Paper Without an Institution",
                "authors": "Judge",
                "affiliations": "Judge: 판사",
                "year": 2025,
            },
        ]
        source, database = self._write_records(root, records)
        return source, database, self._write_catalog(root / "institution-overrides.json")

    def test_canonical_membership_split_exclusion_and_citation_invariance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, database, catalog = self._fixture(Path(temporary))
            index, audit = compile_institution_graph(
                source,
                database_path=database,
                expected_count=3,
                institution_overrides_path=catalog,
            )

        self.assertEqual(index["schema_version"], 1)
        self.assertEqual([item["display_name"] for item in index["institutions"]], [
            "경찰대학", "고려대학교", "순천향대학교"
        ])
        korea = next(item for item in index["institutions"] if item["display_name"] == "고려대학교")
        self.assertEqual(korea["paper_ids"], ["ART000000001"])
        self.assertEqual(korea["paper_count"], 1)
        self.assertEqual(korea["author_count"], 2)
        self.assertEqual(korea["authorship_count"], 2)
        self.assertEqual(korea["first_publication_date"], "2022-01")
        self.assertEqual(korea["last_publication_date"], "2022-01")
        self.assertIn("고려대학교 정보보호대학원", korea["aliases"])
        self.assertEqual(index["papers"]["ART000000001"]["institution_ids"], [korea["id"]])
        self.assertEqual(len(index["papers"]["ART000000002"]["institution_ids"]), 2)
        self.assertEqual(index["papers"]["ART000000003"]["institution_ids"], [])
        self.assertEqual(len(index["citation_edges"]), 1)
        self.assertEqual(index["citation_edges"][0]["source"], "ART000000002")
        self.assertEqual(index["citation_edges"][0]["target"], "ART000000001")
        self.assertEqual(index["corpus"]["paper_count"], 3)
        self.assertEqual(index["corpus"]["institution_count"], 3)
        self.assertEqual(index["corpus"]["institution_paper_link_count"], 3)
        self.assertEqual(index["corpus"]["raw_affiliation_record_count"], 5)
        self.assertEqual(index["corpus"]["raw_paper_affiliation_link_count"], 5)
        self.assertEqual(index["corpus"]["canonical_link_reduction_count"], 2)
        self.assertEqual(audit["validation"]["citation_edges_unchanged"], "passed")
        actions = {item["raw_name"]: item["action"] for item in audit["affiliation_decisions"]}
        self.assertEqual(actions["경찰대학 치안정책연구소, 순천향대학교 대학원"], "split")
        self.assertEqual(actions["판사"], "excluded")

        legacy = institution_id("고려대학교 정보보호대학원")
        self.assertEqual(index["institution_id_aliases"][legacy], korea["id"])
        self.assertEqual(
            normalize_institution_identity("  Korea University! "),
            normalize_institution_identity("Korea University"),
        )

    def test_catalog_rejects_duplicate_json_keys_and_conflicting_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":1,"schema_version":1}', encoding="utf-8"
            )
            with self.assertRaisesRegex(CorpusValidationError, "duplicate JSON object key"):
                load_institution_catalog(duplicate)

            conflict = root / "conflict.json"
            conflict.write_text(json.dumps({
                "schema_version": 1,
                "canonicalizations": [
                    {"canonical_name": "Alpha", "aliases": ["Shared"], "rationale": "A"},
                    {"canonical_name": "Beta", "aliases": ["Shared"], "rationale": "B"},
                ],
                "splits": [],
                "exclusions": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(CorpusValidationError, "maps to both"):
                load_institution_catalog(conflict)

    def test_bilingual_affiliation_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, database, catalog = self._fixture(Path(temporary))
            path = source / "KNOW-DOMP-0000001" / "en.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "고려대학교 정보보호대학원", "고려대학교 정보보호 대학원"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CorpusValidationError, "affiliation mismatch"):
                compile_institution_graph(
                    source,
                    database_path=database,
                    expected_count=3,
                    institution_overrides_path=catalog,
                )

    def test_build_does_not_replace_author_graph_or_shared_paper_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, database, catalog = self._fixture(root)
            graph_dir = root / "graph"
            paper_dir = graph_dir / "papers" / "ko"
            paper_dir.mkdir(parents=True)
            author_graph = graph_dir / "author-graph.json"
            author_graph.write_text("author sentinel", encoding="utf-8")
            paper_sentinel = paper_dir / "sentinel.md"
            paper_sentinel.write_text("paper sentinel", encoding="utf-8")
            output = graph_dir / "institution-graph.json"
            audit = root / "audit.json"
            build(
                source,
                output,
                audit,
                database_path=database,
                expected_count=3,
                institution_overrides_path=catalog,
            )
            self.assertEqual(author_graph.read_text(encoding="utf-8"), "author sentinel")
            self.assertEqual(paper_sentinel.read_text(encoding="utf-8"), "paper sentinel")
            self.assertTrue(output.is_file())
            self.assertTrue(audit.is_file())

    def test_author_rebuild_preserves_only_a_fresh_institution_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, database, catalog = self._fixture(root)
            author_index, author_audit, corpus = compile_author_graph(
                source, database_path=database, expected_count=3
            )
            institution_index, _ = compile_institution_graph(
                source,
                database_path=database,
                expected_count=3,
                institution_overrides_path=catalog,
            )
            graph_dir = root / "graph"
            graph_dir.mkdir()
            institution_path = graph_dir / "institution-graph.json"
            institution_path.write_text(
                json.dumps(institution_index, ensure_ascii=False), encoding="utf-8"
            )
            write_author_outputs(
                author_index,
                author_audit,
                corpus,
                output_dir=graph_dir,
                audit_path=root / "author-audit.json",
            )
            self.assertTrue(institution_path.is_file(), "matching institution graph must survive")

            stale = json.loads(institution_path.read_text(encoding="utf-8"))
            stale["source_digest"] = "0" * 64
            institution_path.write_text(json.dumps(stale), encoding="utf-8")
            write_author_outputs(
                author_index,
                author_audit,
                corpus,
                output_dir=graph_dir,
                audit_path=root / "author-audit.json",
            )
            self.assertFalse(institution_path.exists(), "stale institution graph must fail closed")


class InstitutionGraphCorpusTests(unittest.TestCase):
    def test_real_corpus_frozen_normalization_and_membership_counts(self) -> None:
        index, audit = compile_institution_graph(
            institution_overrides_path=DEFAULT_INSTITUTION_OVERRIDES
        )
        corpus = index["corpus"]
        # The reviewed 527-paper corpus is the floor; monthly increments add to it.
        self.assertGreaterEqual(corpus["paper_count"], _reviewed_corpus_floor())
        self.assertGreaterEqual(corpus["raw_affiliation_record_count"], 1256)
        self.assertGreaterEqual(corpus["raw_affiliation_value_count"], 337)
        self.assertGreaterEqual(corpus["raw_paper_affiliation_link_count"], 812)
        # Counts observed on the reviewed 527-paper corpus are floors: every
        # monthly increment can only add authorships, papers and institutions.
        self.assertGreaterEqual(corpus["raw_affiliated_authorship_count"], 1254)
        self.assertGreaterEqual(corpus["authorship_without_affiliation_count"], 71)
        self.assertGreaterEqual(corpus["papers_with_complete_raw_affiliations"], 479)
        self.assertGreaterEqual(corpus["papers_with_partial_raw_affiliations"], 16)
        self.assertGreaterEqual(corpus["papers_without_raw_affiliations"], 16)
        self.assertGreaterEqual(corpus["same_paper_raw_affiliation_duplicate_count"], 441)
        self.assertGreaterEqual(corpus["institution_count"], 190)
        self.assertGreaterEqual(corpus["institution_paper_link_count"], 650)
        self.assertGreaterEqual(corpus["canonical_link_reduction_count"], 162)
        self.assertEqual(
            corpus["papers_with_complete_raw_affiliations"]
            + corpus["papers_with_partial_raw_affiliations"]
            + corpus["papers_without_raw_affiliations"],
            corpus["paper_count"],
        )
        self.assertLess(corpus["institution_paper_link_count"], corpus["raw_paper_affiliation_link_count"])
        self.assertGreaterEqual(corpus["citation_edge_count"], 436)
        self.assertEqual(index["citation_edges"], sorted(
            index["citation_edges"], key=lambda edge: (edge["source"], edge["target"])
        ))
        self.assertEqual(audit["validation"]["bilingual_affiliation_clean_equality"], "passed")

        expected_rows = {
            "고려대학교": 171,
            "성균관대학교": 125,
            "국립한밭대학교": 27,
            "대검찰청": 46,
            "국가보안기술연구소": 20,
        }
        by_name = {item["display_name"]: item for item in index["institutions"]}
        self.assertNotIn("고등법원", by_name)
        self.assertEqual(
            index["papers"]["ART003210512"]["institution_names"],
            ["특허법원"],
        )
        for display_name, record_count in expected_rows.items():
            # Reviewed floors; later increments add affiliation records.
            self.assertGreaterEqual(by_name[display_name]["affiliation_record_count"], record_count)
        self.assertTrue(all(
            set(paper["institution_ids"]) == {
                institution["id"]
                for institution in index["institutions"]
                if paper_id in institution["paper_ids"]
            }
            for paper_id, paper in index["papers"].items()
        ))
        self.assertTrue(all(
            item["aliases"] == sorted(
                item["aliases"], key=lambda value: (normalize_identity(value), value)
            )
            for item in index["institutions"]
        ))


if __name__ == "__main__":
    unittest.main()
