from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from Analyzer.Graph.build_author_graph import (
    CorpusValidationError,
    _reference_graph_matches_author_inputs,
    compile_graph as compile_author_graph,
    write_outputs as write_author_outputs,
)
from Analyzer.Graph.build_reference_graph import (
    SimilarityConfig,
    build,
    build_similarity_edges,
    canonical_reference,
    collect_reference_sets,
    compile_reference_graph,
    extract_text_features,
    extract_topic_features,
    normalize_app,
    parse_target_apps,
    parse_techniques,
    runtime_scoring_inputs,
    TOPIC_COMPONENTS,
    tfidf_vectors,
    tokenize,
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


class ReferenceSimilarityTests(unittest.TestCase):
    def test_runtime_export_preserves_full_precision_empty_evidence_and_canonical_order(self):
        vectors = {'B': {}, 'A': {'zeta': math.sqrt(.7), 'alpha': math.sqrt(.3)}}
        references = {'A': {'doi:10.1234/two', 'doi:10.1234/one'}, 'B': set()}
        labels = {'doi:10.1234/two': 'Second reference', 'doi:10.1234/one': 'First reference'}
        payload = runtime_scoring_inputs(vectors, references, labels)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(list(payload['text_vectors']), ['A', 'B'])
        self.assertEqual(list(payload['text_vectors']['A']), ['alpha', 'zeta'])
        self.assertEqual(payload['text_vectors'], vectors)
        self.assertEqual(payload['reference_keys'], {'A': sorted(labels), 'B': []})
        self.assertEqual(list(payload['reference_labels']), sorted(labels))
        reversed_payload = runtime_scoring_inputs(
            dict(reversed(list(vectors.items()))), dict(reversed(list(references.items()))),
            dict(reversed(list(labels.items()))),
        )
        self.assertEqual(json.dumps(payload), json.dumps(reversed_payload))
        roundtrip = json.loads(json.dumps(payload))
        self.assertEqual(roundtrip['text_vectors'], vectors)
        self.assertAlmostEqual(sum(weight ** 2 for weight in roundtrip['text_vectors']['A'].values()), 1, places=14)

    def test_runtime_export_rejects_incomplete_or_invalid_evidence(self):
        for vectors, references, labels in (
            ({'A': {}}, {}, {}),
            ({'A': {}}, {'A': {'missing'}}, {}),
            ({'A': {}}, {'A': set()}, {'unused': 'Unused label'}),
            ({'A': {'term': float('nan')}}, {'A': set()}, {}),
            ({'A': {'term': float('inf')}}, {'A': set()}, {}),
            ({'A': {'term': -1.0}}, {'A': set()}, {}),
            ({'A': {'term': 0.0}}, {'A': set()}, {}),
            ({'A': {'term': .5}}, {'A': set()}, {}),
        ):
            with self.subTest(vectors=vectors, references=references, labels=labels):
                with self.assertRaises(CorpusValidationError):
                    runtime_scoring_inputs(vectors, references, labels)

    def test_normalization_discards_template_and_generic_forensic_terms(self) -> None:
        self.assertEqual(tokenize('<div>N/A null none Abstract Keywords Digital Forensics 연구 방법 증거</div>'), [])
        self.assertEqual(tokenize('KakaoTalk Windows APFS Dropbox'), ['kakaotalk', 'windows', 'apfs', 'dropbox'])
        self.assertEqual(tokenize('카카오톡에서 암호화된 데이터베이스를'), ['카카오톡', '암호화된', '데이터베이스'])
        self.assertEqual(tokenize('der die und für zur'), [])

    def test_reference_identity_is_conservative_and_not_domain_matching(self) -> None:
        first = '[학술지(정기간행물)]. A. Kim. Distinctive KakaoTalk database decryption. Journal One. 2020.'
        self.assertEqual(canonical_reference(first)[0], canonical_reference(first.upper())[0])
        self.assertNotEqual(canonical_reference(first)[0], canonical_reference(first.replace('decryption', 'encryption'))[0])
        self.assertEqual(canonical_reference('DOI: https://doi.org/10.1234/EXAMPLE.')[0], 'doi:10.1234/example')
        self.assertEqual(canonical_reference('DOI: 10.1234/one. DOI: 10.1234/two.')[0], None)
        self.assertEqual(canonical_reference(first, 'ART000000001')[0], 'paper:ART000000001')
        for value in (
            '[인터넷자원]. Developer guide. URL: https://example.org/start. 접속일: 2024.',
            '[인터넷자원]. Another guide. URL: https://example.org/another. 등록일: 2024.',
            'https://example.org/2024/paper', 'N/A 2024',
            '[기타자료]. 대법원 2024. 7. 25. 선고 2021도1181 판결.',
        ):
            self.assertIsNone(canonical_reference(value)[0], value)

    def test_tfidf_cosine_against_independent_small_vector_calculation(self) -> None:
        documents = {'A': Counter({'kakaotalk': 3, 'macos': 1}), 'B': Counter({'kakaotalk': 3, 'ios': 1})}
        vectors, _ = tfidf_vectors(documents, SimilarityConfig())
        common_weight = 1 + math.log(3)
        unique_weight = 1 + math.log(3 / 2)
        expected = common_weight**2 / (common_weight**2 + unique_weight**2)
        actual = sum(value * vectors['B'].get(term, 0) for term, value in vectors['A'].items())
        self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(sum(value**2 for value in vectors['A'].values()), 1)

    def test_components_and_direction_are_independently_explainable(self) -> None:
        vectors = {'A': {'kakaotalk': .6, 'decryption': .8}, 'B': {'kakaotalk': .8, 'decryption': .6}, 'C': {'drone': 1}, 'D': {'firmware': 1}}
        references = {'A': {'x', 'y', 'z'}, 'B': {'x', 'y'}, 'C': set(), 'D': set()}
        citations = [{'source': source, 'target': target} for source in ('C', 'D') for target in ('A', 'B')]
        edges, _ = build_similarity_edges(vectors, references, {'x': 'First work', 'y': 'Second work', 'z': 'Third work'}, citations, SimilarityConfig())
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual((edge['source'], edge['target']), ('A', 'B'))
        self.assertAlmostEqual(edge['components']['text'], .96)
        self.assertAlmostEqual(edge['components']['bibliographic_coupling'], 2 / math.sqrt(6), places=6)
        self.assertEqual(edge['components']['co_citation'], 1)
        self.assertAlmostEqual(edge['score'], .96, places=6)
        self.assertEqual(edge['available_components'], ['text'])
        self.assertEqual(edge['coverage'], .4)
        self.assertEqual(edge['shared_citers'], ['C', 'D'])
        self.assertEqual([reference['key'] for reference in edge['shared_references']], ['x', 'y'])
        self.assertNotIn({'source': 'A', 'target': 'B'}, citations)

    def test_one_shared_term_or_reference_cannot_manufacture_similarity(self) -> None:
        vectors = {'A': {'kakaotalk': 1}, 'B': {'kakaotalk': 1}}
        edges, _ = build_similarity_edges(vectors, {'A': {'x'}, 'B': {'x'}}, {'x': 'Shared handbook'}, [], SimilarityConfig(min_score=.01))
        self.assertEqual(edges, [])

    def test_top_k_union_retains_an_edge_selected_by_either_endpoint(self) -> None:
        # A and B choose one another, while C chooses B. B therefore has degree
        # two with k=1, proving this is a union and not an accidental intersection.
        vectors = {'A': {'x': .8, 'y': .6}, 'B': {'x': .6, 'y': .8}, 'C': {'x': .1, 'y': math.sqrt(.99)}}
        config = SimilarityConfig(top_k=1, min_score=.01, min_text_cosine=.01)
        empty = {identifier: set() for identifier in vectors}
        edges, _ = build_similarity_edges(vectors, empty, {}, [], config)
        self.assertEqual([(edge['source'], edge['target']) for edge in edges], [('A', 'B'), ('B', 'C')])
        reversed_edges, _ = build_similarity_edges(dict(reversed(list(vectors.items()))), empty, {}, [], config)
        self.assertEqual(edges, reversed_edges)

    def test_invalid_parameters_are_rejected(self) -> None:
        for config in (SimilarityConfig(top_k=0), SimilarityConfig(top_k=True), SimilarityConfig(min_score=float('nan')), SimilarityConfig(text_weight=2)):
            with self.assertRaises(CorpusValidationError):
                config.validate()

    def test_weighted_set_arithmetic_is_cross_hash_seed_deterministic(self):
        code = '''from Analyzer.Graph.build_reference_graph import weighted_jaccard
values = {f"term{i}": (i + .1) / 71 for i in range(1000)}
print(repr(weighted_jaccard(set(values), {f"term{i}" for i in range(900)}, values)))
'''
        results = [subprocess.check_output([sys.executable, '-c', code], env={**os.environ, 'PYTHONHASHSEED': seed}, text=True) for seed in ('1', '2026')]
        self.assertEqual(results[0], results[1])


class StructuredTopicTests(unittest.TestCase):
    def feature(self, *, tags=(), apps=(), categories=(), paths=()):
        return {'tags': list(tags), 'target_apps': list(apps), 'app_categories': list(categories), 'techniques': [{'level2': parent, 'level3': child} for parent, child in paths]}

    def pair(self, left, right, vectors=None, **config):
        vectors = vectors or {'A': {'decryption': .6, 'sqlite': .8}, 'B': {'decryption': .8, 'sqlite': .6}}
        return build_similarity_edges(vectors, {'A': set(), 'B': set()}, {}, [], SimilarityConfig(**config), {'A': left, 'B': right})[0]

    def test_default_tags_outweigh_apps_without_changing_other_weights(self):
        config = SimilarityConfig()
        config.validate()
        self.assertEqual(
            {name: getattr(config, name + '_weight') for name in TOPIC_COMPONENTS},
            {'text': .4, 'tags': .2, 'applications': .15, 'techniques': .25},
        )
        source = self.feature(tags=['FS-NTFS'], apps=['KakaoTalk'], paths=[('A', 'Database decryption')])
        tags_match = self.feature(tags=['FS-NTFS'], apps=['WhatsApp'], paths=[('A', 'Database decryption')])
        apps_match = self.feature(tags=['FS-APFS'], apps=['KakaoTalk'], paths=[('A', 'Database decryption')])
        tag_edge = self.pair(source, tags_match)[0]
        app_edge = self.pair(source, apps_match)[0]
        self.assertEqual(tag_edge['score'], .834)
        self.assertEqual(app_edge['score'], .784)
        self.assertGreater(tag_edge['score'], app_edge['score'])

    def test_missing_values_do_not_match_or_penalize(self):
        edges = self.pair(self.feature(), self.feature(apps=['KakaoTalk'], paths=[('L2', 'L3')]))
        edge = edges[0]
        self.assertEqual(edge['available_components'], ['text'])
        self.assertEqual(edge['effective_weights'], {'text': 1, 'tags': 0, 'applications': 0, 'techniques': 0})
        self.assertEqual(edge['score'], .96)
        self.assertEqual(edge['coverage'], .4)
        self.assertFalse(edge['application_evidence']['applicable'])

    def test_apps_without_app_tags_still_compared_and_categories_are_weak(self):
        same = self.pair(self.feature(apps=['KakaoTalk'], categories=['메신저']), self.feature(apps=['KakaoTalk'], categories=['메신저']))[0]
        different = self.pair(self.feature(apps=['KakaoTalk'], categories=['메신저']), self.feature(apps=['WhatsApp'], categories=['메신저']))[0]
        self.assertEqual(same['components']['applications'], 1)
        self.assertEqual(different['components']['applications'], .2)
        self.assertGreater(same['score'], different['score'])
        self.assertEqual(different['application_evidence']['shared_apps'], [])

    def test_app_category_not_counted_twice(self):
        feature = self.feature(tags=['DEV-PC', 'APP-MES'], apps=['KakaoTalk'], categories=['APP-MES'])
        edge = self.pair(feature, feature)[0]
        self.assertTrue(edge['tag_evidence']['app_dimension_excluded'])
        self.assertEqual(edge['shared_tags'], ['DEV-PC'])
        self.assertEqual(edge['tag_evidence']['dimensions'], {'DEV': 1})

    def test_generic_platform_tags_and_technique_alone_cannot_create_edges(self):
        generic = self.feature(tags=['DEV-PC', 'OS-WIN', 'APP-MES'], paths=[('Application', 'Artifact')])
        vectors = {'A': {'windows': .6, 'pc': .8}, 'B': {'windows': .6, 'pc': .8}}
        self.assertEqual(self.pair(generic, generic, vectors), [])
        self.assertEqual(self.pair(self.feature(paths=[('L2', 'L3')]), self.feature(paths=[('L2', 'L3')]), {'A': {'one': 1}, 'B': {'other': 1}}), [])

    def test_reference_overlap_never_creates_topic_edges(self):
        vectors = {'A': {'drone': 1}, 'B': {'wallet': 1}, 'C': {}, 'D': {}}
        refs = {'A': {'x', 'y'}, 'B': {'x', 'y'}, 'C': set(), 'D': set()}
        citations = [{'source': citer, 'target': target} for citer in ('C', 'D') for target in ('A', 'B')]
        edges, _ = build_similarity_edges(vectors, refs, {'x': 'reference1', 'y': 'reference2'}, citations, SimilarityConfig(min_score=.01))
        self.assertEqual(edges, [])

    def test_full_level3_path_not_child_substring_and_parent_credit_capped(self):
        a = self.feature(paths=[('A', 'Recovery')])
        b = self.feature(paths=[('B', 'Recovery')])
        edge = self.pair(a, b)[0]
        self.assertEqual(edge['components']['techniques'], 0)
        self.assertEqual(edge['technique_evidence']['shared_level3'], [])
        parent = self.pair(a, self.feature(paths=[('A', 'Recovery of media')]))[0]
        self.assertEqual(parent['components']['techniques'], .15)
        self.assertEqual(parent['technique_evidence']['shared_level3'], [])

    def test_specific_independent_structure_can_connect_without_text_overlap(self):
        both = self.feature(apps=['KakaoTalk'], paths=[('A', 'Database decryption')])
        edge = self.pair(both, both, {'A': {'unique': 1}, 'B': {'distinct': 1}})[0]
        self.assertTrue(edge['evidence_gate']['independent_structure'])
        self.assertFalse(edge['evidence_gate']['lexical'])
        self.assertEqual(edge['components']['text'], 0)

    def test_normalization_decodes_entities_and_only_explicit_identity_aliases(self):
        self.assertEqual(normalize_app(' Chrome '), normalize_app('Ｇｏｏｇｌｅ Chrome'))
        self.assertEqual(normalize_app('KakaoTalk'), normalize_app('카카오톡'))
        self.assertEqual(normalize_app('Teams'), normalize_app('Microsoft Teams'))
        for left, right in [('Telegram X', 'Telegram'), ('Wickr Me', 'Wickr Pro'), ('Microsoft Word', 'Microsoft Office'), ('FaceSwap', 'FaceSwapper')]:
            self.assertNotEqual(normalize_app(left), normalize_app(right))
        # Apps now arrive as APPNAME- tags with whitespace hyphenated; other
        # tag axes and non-app text never become applications.
        self.assertEqual(
            parse_target_apps(
                '## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n'
                '| TGT-0003 | KakaoTalk |\n|  | 카카오톡 |\n|  | Tool &amp; Notes |\n'
                '## 5. 참고문헌\n| ID | REF |\n|---|---|\n|  | NotAnApp |\n'
            ),
            ['KakaoTalk', 'tool & notes'],
        )
        self.assertEqual(
            parse_target_apps('## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n|  | N/A |\n'),
            [],
        )

    def test_markdown_tables_ignore_na_deduplicate_and_keep_full_paths(self):
        # A Level-3 identifier names its own parent, so the compared path pair
        # is derived from the identifier and duplicates collapse.
        self.assertEqual(
            parse_techniques(['EXAM-04-01', 'EXAM-04-01', 'CMNF-01-02']),
            [{'level2': 'CMNF-01', 'level3': 'CMNF-01-02'}, {'level2': 'EXAM-04', 'level3': 'EXAM-04-01'}],
        )
        self.assertEqual(parse_techniques([]), [])
        with self.assertRaises(CorpusValidationError):
            parse_techniques(['not-an-id'])

    def test_tag_dimensions_are_balanced_and_unshared_dimensions_missing(self):
        a = self.feature(tags=['OS-WIN', 'OS-LNX', 'DEV-PC', 'FS-BLK'])
        b = self.feature(tags=['OS-WIN', 'DEV-PC'])
        edge = self.pair(a, b)[0]
        dimensions = edge['tag_evidence']['dimensions']
        self.assertEqual(set(dimensions), {'DEV', 'OS'})
        self.assertEqual(dimensions['DEV'], 1)
        self.assertAlmostEqual(edge['components']['tags'], (1 + dimensions['OS']) / 2, places=6)

    def test_common_technique_cannot_support_weak_text_without_specific_topic(self):
        vectors = {'A': {'signal': math.sqrt(.05), 'wavelet': math.sqrt(.05), 'left': math.sqrt(.9)}, 'B': {'signal': math.sqrt(.05), 'wavelet': math.sqrt(.05), 'right': math.sqrt(.9)}}
        vectors.update({f'X{i}': {f'unique{i}': 1} for i in range(8)})
        features = {identifier: self.feature(paths=[('Application', 'Artifact examination')]) for identifier in vectors}
        refs = {identifier: set() for identifier in vectors}
        edges, audit = build_similarity_edges(vectors, refs, {}, [], SimilarityConfig(min_score=.01), features)
        self.assertNotIn(('A', 'B'), [(edge['source'], edge['target']) for edge in edges])
        self.assertEqual(audit['common_technique_paths'], [{'level2': 'Application', 'level3': 'Artifact examination', 'paper_count': 10}])
        features['A'] = self.feature(paths=[('Rare', 'Signal recovery')])
        features['B'] = self.feature(paths=[('Rare', 'Signal recovery')])
        edges, _ = build_similarity_edges(vectors, refs, {}, [], SimilarityConfig(min_score=.01), features)
        self.assertIn(('A', 'B'), [(edge['source'], edge['target']) for edge in edges])


class ReferenceBuildFixtureTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        source = root / 'MD'
        records = [
            ('ART000000001', '카카오톡 데이터베이스 복호화', 'KakaoTalk database decryption', '10.1234/kakao', []),
            ('ART000000002', '카카오톡 데이터베이스 암호화', 'KakaoTalk database encryption', 'N/A', []),
            ('ART000000003', '항공기 프로펠러 강도', 'Aircraft propeller strength', 'N/A', ['DOI: 10.1234/kakao.', 'DOI: https://doi.org/10.1234/KAKAO.']),
        ]
        corpus = [identifier for identifier, *_ in records]
        record_ids = {identifier: record_id_for(identifier, corpus) for identifier in corpus}
        for language in ('ko', 'en'):
            for identifier, ko_title, en_title, doi, references in records:
                directory = source / record_ids[identifier]
                directory.mkdir(parents=True, exist_ok=True)
                content = markdown(identifier, record_id=record_ids[identifier], language=language, title=ko_title if language == 'ko' else en_title, doi=doi, authors='홍길동', affiliations='홍길동: 고려대학교', year=2024, references=references, record_ids=record_ids)
                content = content.replace('Fixture.\n', 'N/A\n').replace('Fixture\n', 'N/A\n')
                (directory / f'{language}.md').write_text(content, encoding='utf-8')
        database = root / 'kci_pipeline.db'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE kci_papers (source TEXT, source_id TEXT, publication_year INTEGER, registered_at TEXT)')
            connection.executemany('INSERT INTO kci_papers VALUES (?, ?, ?, ?)', [('kci', record[0], 2024, '2024-03') for record in records])
        return source, database

    def test_similarity_without_citation_and_identical_reviewed_citations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, database = self.fixture(Path(temporary))
            author, _, corpus = compile_author_graph(source, database_path=database, expected_count=3)
            index, audit = compile_reference_graph(source, database_path=database, expected_count=3)
            self.assertEqual(index['papers'], author['papers'])
            self.assertEqual(index['citation_edges'], [{'source': 'ART000000003', 'target': 'ART000000001', 'method': 'doi', 'reference_numbers': [1, 2]}])
            self.assertEqual(index['citation_edges'], author['citation_edges'])
            self.assertEqual([(edge['source'], edge['target']) for edge in index['similarity_edges']], [('ART000000001', 'ART000000002')])
            self.assertEqual(index['corpus']['similarity_without_direct_citation_count'], 1)
            refs, _, identity_audit = collect_reference_sets(corpus, index['citation_edges'])
            self.assertEqual(refs['ART000000003'], {'paper:ART000000001'})
            self.assertEqual(identity_audit['identity_counts']['duplicate_mentions_collapsed'], 1)
            documents, coverage = extract_text_features(corpus)
            self.assertTrue(all('fixture' not in document for document in documents.values()))
            self.assertEqual(coverage.get('en_abstract_paper_count', 0), 0)
            vectors, _ = tfidf_vectors(documents, SimilarityConfig())
            runtime = index['runtime_inputs']
            self.assertEqual(runtime['text_vectors'], vectors)
            self.assertEqual(set(runtime['reference_keys']), set(author['papers']))
            self.assertEqual(runtime['reference_keys']['ART000000001'], [])
            self.assertEqual(runtime['reference_keys']['ART000000003'], ['paper:ART000000001'])
            # The third paper has no similarity edge, but is still available
            # for a future browser-selected weighting configuration.
            self.assertIn('ART000000003', runtime['text_vectors'])
            runtime_digest = hashlib.sha256(json.dumps(runtime, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(index['build_inputs']['runtime_inputs_sha256'], runtime_digest)
            self.assertEqual(audit['runtime_inputs']['sha256'], runtime_digest)
            baseline_edges, _ = build_similarity_edges(vectors, refs, runtime['reference_labels'], author['citation_edges'], SimilarityConfig(), index['topic_features'])
            self.assertEqual(index['similarity_edges'], baseline_edges)
            self.assertTrue(all(audit['validation'].values()))

    def test_check_only_and_repeated_build_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, database = self.fixture(root)
            output, audit = root / 'graph.json', root / 'audit.json'
            args = dict(database_path=database, expected_count=3)
            build(source, output, audit, check_only=True, **args)
            self.assertFalse(output.exists())
            self.assertFalse(audit.exists())
            first, _ = build(source, output, audit, **args)
            first_bytes = output.read_bytes()
            second, _ = build(source, output, audit, **args)
            self.assertEqual(first, second)
            self.assertEqual(output.read_bytes(), first_bytes)
            different, _ = compile_reference_graph(source, config=SimilarityConfig(top_k=1), **args)
            self.assertNotEqual(first['build_input_digest'], different['build_input_digest'])
            self.assertEqual(first['source_digest'], different['source_digest'])

    def test_author_rebuild_preserves_only_fresh_reference_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, database = self.fixture(root)
            author, author_audit, corpus = compile_author_graph(source, database_path=database, expected_count=3)
            output = root / 'graph'
            reference = output / 'reference-graph.json'
            build(source, reference, root / 'reference-audit.json', database_path=database, expected_count=3)
            original = reference.read_bytes()
            self.assertTrue(_reference_graph_matches_author_inputs(reference, author))
            write_author_outputs(author, author_audit, corpus, output_dir=output, audit_path=root / 'author-audit.json')
            self.assertEqual(reference.read_bytes(), original)
            for mutation in ('source', 'papers', 'method', 'builder', 'taxonomy'):
                payload = json.loads(original)
                if mutation == 'source':
                    payload['source_digest'] = 'stale'
                elif mutation == 'papers':
                    payload['papers']['ART000000001']['year'] = 1900
                elif mutation == 'method':
                    payload['method']['top_k'] = 99
                elif mutation == 'builder':
                    payload['build_inputs']['reference_builder_sha256'] = 'old-builder'
                else:
                    payload['build_inputs']['application_taxonomy_sha256'] = 'old-taxonomy'
                reference.write_text(json.dumps(payload), encoding='utf-8')
                self.assertFalse(_reference_graph_matches_author_inputs(reference, author), mutation)
            write_author_outputs(author, author_audit, corpus, output_dir=output, audit_path=root / 'author-audit.json')
            self.assertFalse(reference.exists())

    def test_bilingual_structure_counted_once_with_canonical_korean_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, database = self.fixture(Path(temporary))
            # The section-4 application table is not cross-validated between
            # translations, so each language gets a different application and
            # only the canonical Korean source may count.
            for language, application in [('ko', 'KakaoTalk'), ('en', 'Telegram')]:
                path = source / 'KNOW-DOMP-0000001' / f'{language}.md'
                content = (
                    path.read_text(encoding='utf-8')
                    .replace('  - OS-WIN # Windows', '  - OS-WIN # Windows\n  - APP-MES # 메신저')
                    .replace('|  | N/A |', f'|  | {application} |')
                )
                path.write_text(content, encoding='utf-8')
            _, _, corpus = compile_author_graph(source, database_path=database, expected_count=3)
            features, coverage = extract_topic_features(corpus)
            feature = features['ART000000001']
            self.assertEqual(feature['target_apps'], ['KakaoTalk'])
            # Categories come from the paper's own APP- tags, and the technique
            # path is derived from the classification identifier.
            self.assertEqual(feature['app_categories'], ['APP-MES'])
            self.assertEqual(feature['techniques'], [{'level2': 'EXAM-04', 'level3': 'EXAM-04-01'}])
            self.assertEqual(coverage['target_apps_paper_count'], 1)


class ReferenceCorpusTests(unittest.TestCase):
    def test_full_corpus_coverage_and_evidence_invariants(self) -> None:
        index, audit = compile_reference_graph()
        paper_count = index['corpus']['paper_count']
        # The reviewed 527-paper corpus is the floor; monthly increments add to it.
        self.assertGreaterEqual(paper_count, _reviewed_corpus_floor())
        self.assertEqual(len(index['papers']), paper_count)
        self.assertGreaterEqual(len(index['citation_edges']), 436)
        self.assertGreater(index['corpus']['similarity_without_direct_citation_count'], 0)
        self.assertEqual(index['corpus']['connected_paper_count'] + index['corpus']['isolated_paper_count'], paper_count)
        incoming = {identifier: set() for identifier in index['papers']}
        for citation in index['citation_edges']:
            incoming[citation['target']].add(citation['source'])
        unique = set()
        for edge in index['similarity_edges']:
            self.assertLess(edge['source'], edge['target'])
            self.assertNotIn(edge['id'], unique)
            unique.add(edge['id'])
            self.assertEqual(edge['shared_citers'], sorted(incoming[edge['source']] & incoming[edge['target']]))
            self.assertTrue(edge['shared_terms'] or edge['shared_references'] or edge['shared_citers'])
            components = edge['components']
            self.assertAlmostEqual(edge['score'], sum(edge['effective_weights'][name] * components[name] for name in TOPIC_COMPONENTS), delta=0.000001)
            self.assertAlmostEqual(sum(edge['effective_weights'].values()), 1, places=7)
            self.assertTrue(all(math.isfinite(value) and 0 <= value <= 1 for value in components.values()))
            self.assertGreaterEqual(edge['score'], index['method']['min_score'])
        self.assertTrue(all(audit['validation'].values()))
        self.assertEqual(set(index['topic_features']), set(index['papers']))
        runtime = index['runtime_inputs']
        self.assertEqual(set(runtime['text_vectors']), set(index['papers']))
        self.assertEqual(set(runtime['reference_keys']), set(index['papers']))
        self.assertEqual(audit['runtime_inputs']['paper_count'], paper_count)
        used_reference_keys = set()
        for identifier in sorted(index['papers']):
            vector = runtime['text_vectors'][identifier]
            self.assertEqual(bool(vector), index['topic_features'][identifier]['text_available'])
            self.assertTrue(all(math.isfinite(value) and value > 0 for value in vector.values()))
            if vector:
                self.assertAlmostEqual(math.fsum(value ** 2 for value in vector.values()), 1.0, places=12)
            keys = runtime['reference_keys'][identifier]
            self.assertEqual(keys, sorted(set(keys)))
            used_reference_keys.update(keys)
        self.assertEqual(used_reference_keys, set(runtime['reference_labels']))
        # Floors from the reviewed 527-paper corpus; increments only add papers.
        self.assertGreaterEqual(index['corpus']['tags_paper_count'], 227)
        self.assertGreaterEqual(index['corpus']['target_apps_paper_count'], 165)
        self.assertGreaterEqual(index['corpus']['techniques_paper_count'], 442)
        # The 260909 APP axis has a code for every application category the
        # workbook records, so a paper with applications always has an APP tag.
        self.assertEqual(index['corpus']['target_apps_without_app_tag_paper_count'], 0)
        self.assertNotIn('unresolved_app_categories', audit['topic_features'])
        pairs = {(edge['source'], edge['target']) for edge in index['similarity_edges']}
        # Reviewed topic contrast: mobile app-artifact category alone does not
        # make an iOS call recorder and Android metaverse study the same topic.
        self.assertNotIn(('ART003101914', 'ART003243459'), pairs)
        for left, right in [('ART003243477', 'ART003256893'), ('ART002923500', 'ART003071074'), ('ART003233052', 'ART003226215'), ('ART003196124', 'ART003294465'), ('ART003095258', 'ART003243484')]:
            self.assertIn(tuple(sorted((left, right))), pairs)
        # The reviewed procedural-law pair below has no tags or applications, so
        # its structural signal is the shared technique.  The Level stage
        # (Analyzer/LV/levels.json) confirms the same Level-3 for both papers,
        # which restores the edge that the withdrawn ``증거 생애주기 관리`` class
        # had once removed.
        self.assertIn(('ART003045948', 'ART003059727'), pairs)


if __name__ == '__main__':
    unittest.main()
