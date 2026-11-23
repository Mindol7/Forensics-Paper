from __future__ import annotations

from argparse import Namespace
from collections import Counter
from dataclasses import replace
from pathlib import Path
import threading

import pytest

import scripts.process_papers as process_papers
from PaperPipeline.dbpia_browser import initialize_dbpia_profile_dir
from PaperPipeline.openalex_identifier import (
    OpenAlexIdentifierResolution,
    OpenAlexLocation,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.semantic_scholar_title import (
    SemanticScholarTitleOpenAccessPdf,
    SemanticScholarTitleResolution,
)
from PaperPipeline.unpaywall_identifier import (
    UnpaywallIdentifierResolution,
    UnpaywallOaLocation,
)


def _paper(source_id: str = "ART0001") -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=source_id,
        title="An Exact Forensic Paper Title",
        doi_original=None,
        doi=None,
        landing_url=None,
        abstract=None,
        publication_year=2024,
        first_page=None,
        final_page=None,
    )


def _args() -> Namespace:
    return Namespace(
        per_host_rps=1000.0,
        kci_detail_rps=1000.0,
        connect_timeout=1.0,
        read_timeout=1.0,
        http_retries=0,
        identifier_workers=2,
        refresh_resolver_cache=False,
        enable_kci_detail_resolution=True,
        enable_unpaywall_title_resolution=True,
        enable_openalex_title_resolution=True,
        enable_semantic_scholar=False,
        kci_open_api_key="test-kci-key",
        unpaywall_email="researcher@example.org",
        openalex_api_key="test-key",
        semantic_scholar_api_key=None,
        semantic_scholar_rps=0.8,
        require_open_access_evidence=False,
    )


def test_dbpia_node_argument_is_strict_and_canonicalized() -> None:
    parsed = process_papers._parse_dbpia_node_args(
        ["ART003071074=NODE11750870"]
    )

    assert parsed == {"ART003071074": "NODE11750870"}
    assert process_papers._dbpia_article_url(parsed["ART003071074"]) == (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE11750870"
    )

    with pytest.raises(ValueError):
        process_papers._parse_dbpia_node_args(
            ["ART003071074=https://evil.example/paper.pdf"]
        )
    with pytest.raises(ValueError):
        process_papers._parse_dbpia_node_args(
            [
                "ART003071074=NODE11750870",
                "ART003071074=NODE11750871",
            ]
        )


def test_dbpia_discovery_requires_key_and_platform_permission_assertion() -> None:
    parser = process_papers.build_parser()
    missing_key = parser.parse_args(
        ["download", "--enable-dbpia-openapi-discovery"]
    )
    missing_key.dbpia_api_key = None

    with pytest.raises(ValueError, match="DBPIA_API_KEY"):
        process_papers._validate_args(missing_key)

    missing_permission = parser.parse_args(
        [
            "download",
            "--enable-dbpia-openapi-discovery",
        ]
    )
    missing_permission.dbpia_api_key = "test-key"
    with pytest.raises(ValueError, match="enable-platform-download"):
        process_papers._validate_args(missing_permission)


def test_explicit_dbpia_node_requires_platform_permission_assertion() -> None:
    args = process_papers.build_parser().parse_args(
        ["download", "--dbpia-node", "ART003071074=NODE11750870"]
    )

    with pytest.raises(ValueError, match="enable-platform-download"):
        process_papers._validate_args(args)


def test_dbpia_browser_requires_one_worker_and_initialized_private_profile(
    tmp_path: Path,
) -> None:
    parser = process_papers.build_parser()
    too_many_workers = parser.parse_args(
        [
            "download",
            "--dbpia-node",
            "ART003071074=NODE11750870",
            "--enable-dbpia-browser-download",
        ]
    )
    with pytest.raises(ValueError, match="download-workers 1"):
        process_papers._validate_args(too_many_workers)

    missing_profile = parser.parse_args(
        [
            "download",
            "--dbpia-node",
            "ART003071074=NODE11750870",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--dbpia-browser-profile-dir",
            str(tmp_path / "missing"),
        ]
    )
    with pytest.raises(ValueError, match="has not been initialized"):
        process_papers._validate_args(missing_profile)

    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    valid = parser.parse_args(
        [
            "download",
            "--dbpia-node",
            "ART003071074=NODE11750870",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )

    process_papers._validate_args(valid)

    assert valid.dbpia_browser_profile_path == profile


def test_dbpia_browser_title_search_needs_no_api_key_or_node_mapping(
    tmp_path: Path,
) -> None:
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--source-id",
            "ART003071074",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )
    args.dbpia_api_key = None
    args.dbpia_login_id = None
    args.dbpia_login_password = None

    process_papers._validate_args(args)

    assert args.dbpia_browser_profile_path == profile


def test_dbpia_browser_credentials_must_be_configured_as_a_pair(
    tmp_path: Path,
) -> None:
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )
    args.dbpia_login_id = "dbpia-user"
    args.dbpia_login_password = None

    with pytest.raises(ValueError, match="둘 다 설정"):
        process_papers._validate_args(args)


def test_dbpia_subscription_backend_cannot_claim_open_access(
    tmp_path: Path,
) -> None:
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--dbpia-node",
            "ART003071074=NODE11750870",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--require-open-access-evidence",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )

    with pytest.raises(ValueError, match="OA"):
        process_papers._validate_args(args)


def test_dbpia_browser_permits_repair_force_and_validates_attempt_budget(
    tmp_path: Path,
) -> None:
    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    parser = process_papers.build_parser()
    forced = parser.parse_args(
        [
            "download",
            "--source-id",
            "ART003071081",
            "--dbpia-node",
            "ART003071081=NODE11750871",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--force",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )
    process_papers._validate_args(forced)

    invalid_budget = parser.parse_args(
        [
            "download",
            "--source-id",
            "ART003071081",
            "--dbpia-node",
            "ART003071081=NODE11750871",
            "--enable-dbpia-browser-download",
            "--download-workers",
            "1",
            "--dbpia-browser-max-attempts",
            "0",
            "--dbpia-browser-profile-dir",
            str(profile),
        ]
    )
    with pytest.raises(ValueError, match="1~1000"):
        process_papers._validate_args(invalid_budget)


def test_dbpia_redownload_campaign_is_explicit_and_cannot_use_force_or_api(
    tmp_path: Path,
) -> None:
    parser = process_papers.build_parser()
    missing_browser = parser.parse_args(
        ["download", "--dbpia-browser-redownload-campaign", "refresh-2026"]
    )
    with pytest.raises(ValueError, match="enable-dbpia-browser-download"):
        process_papers._validate_args(missing_browser)

    profile = initialize_dbpia_profile_dir(
        tmp_path / "profile",
        repository_root=process_papers.REPOSITORY_ROOT,
    )
    base = [
        "download",
        "--enable-dbpia-browser-download",
        "--download-workers",
        "1",
        "--dbpia-browser-profile-dir",
        str(profile),
        "--dbpia-browser-redownload-campaign",
        "refresh-2026",
    ]
    valid = parser.parse_args(base)
    process_papers._validate_args(valid)
    assert valid.dbpia_browser_redownload_campaign == "refresh-2026"

    forced = parser.parse_args([*base, "--force"])
    with pytest.raises(ValueError, match="--force"):
        process_papers._validate_args(forced)

    with_openapi = parser.parse_args(
        [*base, "--enable-dbpia-openapi-discovery"]
    )
    with_openapi.dbpia_api_key = "test-key"
    with pytest.raises(ValueError, match="웹 제목 검색"):
        process_papers._validate_args(with_openapi)

    invalid_name = parser.parse_args(
        [
            *base[:-1],
            "contains/slash",
        ]
    )
    with pytest.raises(ValueError, match="1~64"):
        process_papers._validate_args(invalid_name)


def test_dbpia_browser_batch_stays_on_main_thread_and_closes_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main_thread = threading.get_ident()
    resolver = Namespace(close_calls=0)

    def close() -> None:
        resolver.close_calls += 1

    resolver.close = close
    runtime = Namespace(
        config=Namespace(enable_dbpia_browser_download=True),
        dbpia_browser_resolver=resolver,
    )
    observed: list[tuple[str, int, object]] = []

    def fake_download_one(
        paper,
        _papers_dir,
        _manifest_dir,
        active_runtime,
        force,
    ):
        assert not force
        observed.append(
            (paper.source_id, threading.get_ident(), active_runtime)
        )
        return paper.source_id, {"status": "downloaded"}

    monkeypatch.setattr(process_papers, "_download_one", fake_download_one)
    monkeypatch.setattr(
        process_papers,
        "_download_result_is_durable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        process_papers,
        "ThreadPoolExecutor",
        lambda *_args, **_kwargs: pytest.fail("executor must not be created"),
    )

    result = process_papers.download_many(
        [_paper("ART0001"), _paper("ART0002")],
        papers_dir=tmp_path,
        args=Namespace(download_workers=1, force=False),
        runtime=runtime,
    )

    assert result.counts == Counter({"downloaded": 2})
    assert observed == [
        ("ART0001", main_thread, runtime),
        ("ART0002", main_thread, runtime),
    ]
    assert resolver.close_calls == 1


def test_dbpia_redownload_batch_counts_only_campaign_successes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resolver = Namespace(close=lambda: None)
    runtime = Namespace(
        config=Namespace(enable_dbpia_browser_download=True),
        dbpia_browser_resolver=resolver,
    )
    actions = {
        "ART4001": "redownloaded",
        "ART4002": "redownload_reused",
        "ART4003": "redownload_failed",
        "ART4004": "redownload_deferred",
    }

    def fake_download_one(
        paper,
        _papers_dir,
        _manifest_dir,
        _runtime,
        force,
    ):
        assert not force
        return paper.source_id, {
            "status": "downloaded",
            "_runtime": {
                "action": actions[paper.source_id],
                "browser_attempted": actions[paper.source_id]
                in {"redownloaded", "redownload_failed"},
            },
        }

    monkeypatch.setattr(process_papers, "_download_one", fake_download_one)
    monkeypatch.setattr(
        process_papers,
        "_download_result_is_durable",
        lambda *_args, **_kwargs: True,
    )
    papers = [_paper(source_id) for source_id in actions]
    result = process_papers.download_many(
        papers,
        papers_dir=tmp_path,
        args=Namespace(
            download_workers=1,
            force=False,
            dbpia_browser_redownload_campaign="refresh-2026",
        ),
        runtime=runtime,
    )

    assert result.counts == Counter(actions.values())
    assert result.successful_source_ids == frozenset(
        {"ART4001", "ART4002"}
    )


def test_download_batch_never_counts_uncommitted_success_as_durable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resolver = Namespace(close=lambda: None)
    runtime = Namespace(
        config=Namespace(enable_dbpia_browser_download=True),
        dbpia_browser_resolver=resolver,
    )
    monkeypatch.setattr(
        process_papers,
        "_download_one",
        lambda paper, *_args, **_kwargs: (
            paper.source_id,
            {"status": "downloaded"},
        ),
    )
    monkeypatch.setattr(
        process_papers,
        "_download_result_is_durable",
        lambda *_args, **_kwargs: False,
    )

    result = process_papers.download_many(
        [_paper("ART4999")],
        papers_dir=tmp_path,
        args=Namespace(download_workers=1, force=False),
        runtime=runtime,
    )

    assert result.counts == Counter({"artifact_validation_failed": 1})
    assert result.successful_source_ids == frozenset()


def test_dbpia_browser_resume_uses_durable_cursor_after_manifest_rewrite(
    tmp_path: Path,
) -> None:
    papers = [
        replace(_paper("ART3001"), database_id=1),
        replace(_paper("ART3002"), database_id=2),
        replace(_paper("ART3003"), database_id=3),
    ]
    resolver = process_papers.DBPIA_BROWSER_RESOLVER_NAME
    actual_manifest = {
        "status": "retryable_error",
        "attempts": [
            {
                "resolver": f"{resolver}:dbpia-browser-landing",
                "status": "failed",
            }
        ],
    }

    assert process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        papers[1],
        actual_manifest,
    )
    assert not process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        replace(_paper("ART3006"), database_id=6),
        {
            "status": "not_found",
            "attempts": [
                {
                    "resolver": f"{resolver}:browser-precheck",
                    "status": "failed",
                }
            ],
        },
    )
    assert process_papers._load_dbpia_browser_resume_cursor(tmp_path) == 2

    # A normal per-paper manifest can be replaced by a later budget deferral;
    # the separate cursor must still remember the last real browser attempt.
    process_papers.write_json_atomic(
        process_papers.ArtifactPaths.for_source(
            tmp_path,
            papers[1].source_id,
        ).download_manifest,
        {
            "status": "retryable_error",
            "attempts": [
                {
                    "resolver": f"{resolver}:browser-budget",
                    "status": "failed",
                }
            ],
        },
    )

    ordered = process_papers._rotate_dbpia_browser_candidates(
        papers,
        papers_dir=tmp_path,
        candidate_source_ids={paper.source_id for paper in papers},
    )

    assert [paper.source_id for paper in ordered] == [
        "ART3003",
        "ART3001",
        "ART3002",
    ]
    assert process_papers._load_dbpia_browser_resume_cursor(tmp_path) == 2


def test_dbpia_browser_resume_ignores_precheck_budget_and_reused_manifests(
    tmp_path: Path,
) -> None:
    paper = replace(_paper("ART3010"), database_id=10)
    resolver = process_papers.DBPIA_BROWSER_RESOLVER_NAME
    budget_manifest = {
        "status": "retryable_error",
        "attempts": [
            {
                "resolver": f"{resolver}:browser-budget",
                "status": "failed",
            }
        ],
    }
    precheck_manifest = {
        "status": "not_found",
        "attempts": [
            {
                "resolver": f"{resolver}:browser-precheck",
                "status": "failed",
            }
        ],
    }
    reused_manifest = {
        "status": "downloaded",
        "_runtime": {"action": "reused"},
        "attempts": [
            {
                "resolver": f"{resolver}:dbpia-browser-download",
                "status": "downloaded",
            }
        ],
    }

    assert not process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        paper,
        budget_manifest,
    )
    assert not process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        paper,
        precheck_manifest,
    )
    assert not process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        paper,
        reused_manifest,
    )
    assert not process_papers._advance_dbpia_browser_resume_cursor(
        tmp_path,
        paper,
        {
            "status": "downloaded",
            "_runtime": {
                "action": "redownload_deferred",
                "browser_attempted": False,
            },
            # Historical attempts from the preserved successful manifest must
            # not move the cursor for this deferred campaign turn.
            "attempts": [
                {
                    "resolver": f"{resolver}:dbpia-browser-download",
                    "status": "downloaded",
                }
            ],
        },
    )
    assert process_papers._load_dbpia_browser_resume_cursor(tmp_path) is None


def test_institution_selection_is_trimmed_and_deduplicated() -> None:
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--dry-run",
            "--institution-id",
            " 한국디지털포렌식학회 ",
            "--institution-id",
            "한국디지털포렌식학회",
        ]
    )

    process_papers._validate_args(args)

    assert args.institution_id == ["한국디지털포렌식학회"]


def test_dbpia_redownload_preparation_uses_only_web_candidates_and_campaign_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    completed = replace(
        _paper("ART5001"),
        doi_original="10.1000/completed",
        doi="10.1000/completed",
    )
    pending = replace(
        _paper("ART5002"),
        database_id=2,
        doi_original="10.1000/pending",
        doi="10.1000/pending",
    )
    monkeypatch.setattr(
        process_papers,
        "_dbpia_redownload_campaign_completed",
        lambda paper, _paths, campaign: (
            campaign == "refresh-2026" and paper.source_id == "ART5001"
        ),
    )
    args = Namespace(
        dbpia_browser_redownload_campaign="refresh-2026",
        dbpia_node_map={},
        dbpia_browser_max_attempts=25,
    )

    preparation = process_papers._prepare_dbpia_redownload_campaign(
        [completed, pending],
        papers_dir=tmp_path,
        args=args,
    )

    assert {paper.source_id for paper in preparation.papers} == {
        "ART5001",
        "ART5002",
    }
    assert preparation.kci_journal_pdf_urls == {}
    assert preparation.preverified_candidates == {}
    assert preparation.semantic_scholar_candidates == {}
    assert preparation.platform_landing_urls == {}
    assert preparation.identifier_counts == Counter(
        {
            "dbpia-redownload:completed": 1,
            "dbpia-redownload:pending": 1,
        }
    )


def test_dbpia_title_search_precheck_requires_parsed_authors() -> None:
    paper = replace(
        _paper("ART003346756"),
        institution_id="한국지능시스템학회",
        journal_id="한국지능시스템학회 논문지",
        issue_id="36_3",
        first_page="204",
        final_page="211",
        authors=(),
    )

    assert not process_papers._dbpia_browser_title_search_ready(paper)
    assert process_papers._dbpia_browser_title_search_ready(
        replace(paper, authors=("정재", "최석환"))
    )


def test_campaign_adopts_doi_only_from_a_fully_valid_current_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = _paper("ART5003")
    paths = process_papers.ArtifactPaths.for_source(tmp_path, paper.source_id)
    tmp_path.mkdir(parents=True, exist_ok=True)
    process_papers.write_json_atomic(
        paths.download_manifest,
        {
            "status": "downloaded",
            "paper": {
                "source_id": paper.source_id,
                "doi_original": "https://doi.org/10.1000/Validated.One",
                "doi": "10.1000/Validated.One",
                "doi_source": "crossref-title",
                "identifier_resolution": {
                    "resolver": "crossref-title",
                    "status": "resolved",
                },
            },
        },
    )
    monkeypatch.setattr(
        process_papers,
        "download_artifact_is_current",
        lambda candidate, candidate_paths, manifest=None: (
            candidate == paper and candidate_paths == paths
        ),
    )

    adopted = process_papers._adopt_current_artifact_identifier(paper, paths)

    assert adopted.doi == "10.1000/Validated.One"
    assert adopted.doi_original == "https://doi.org/10.1000/Validated.One"
    assert adopted.doi_source == "crossref-title"
    assert paper.doi is None

    monkeypatch.setattr(
        process_papers,
        "download_artifact_is_current",
        lambda *_args, **_kwargs: False,
    )
    assert process_papers._adopt_current_artifact_identifier(paper, paths) == paper


def test_campaign_enriches_a_missing_author_row_once_before_dbpia_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = replace(
        _paper("ART5004"),
        institution_id="Test Publisher",
        journal_id="Test Journal",
        issue_id="36_3",
        first_page="204",
        final_page="211",
        authors=(),
    )
    calls: list[list[PaperCandidate]] = []

    def enrich(targets, **_kwargs):
        calls.append(list(targets))
        return (
            [replace(targets[0], authors=("정재", "최석환"))],
            Counter({"kci-openapi-detail:no_match": 1}),
        )

    monkeypatch.setattr(
        process_papers,
        "_resolve_kci_detail_identifiers",
        enrich,
    )
    args = Namespace(
        dbpia_browser_redownload_campaign="refresh-2026",
        dbpia_node_map={},
        dbpia_browser_max_attempts=25,
    )

    preparation = process_papers._prepare_dbpia_redownload_campaign(
        [paper],
        papers_dir=tmp_path,
        args=args,
    )

    assert calls == [[paper]]
    assert preparation.papers[0].authors == ("정재", "최석환")
    assert process_papers._dbpia_browser_title_search_ready(
        preparation.papers[0]
    )


def test_dbpia_search_resolution_is_wired_into_platform_landings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = replace(
        _paper("ART003071074"),
        doi_original="10.22798/KDFS.2024.18.1.11",
        doi="10.22798/KDFS.2024.18.1.11",
        institution_id="한국디지털포렌식학회",
        journal_id="디지털포렌식연구",
        first_page="11",
        final_page="23",
    )
    calls: list[tuple[str, ...]] = []

    class FakeDbpiaResolver:
        def __init__(self, _config, *, http) -> None:
            self.http = http

        def resolve(self, candidates: list[PaperCandidate]):
            calls.append(tuple(candidate.source_id for candidate in candidates))
            resolution = Namespace(
                status="resolved",
                resolved=True,
                landing_url=(
                    "https://www.dbpia.co.kr/journal/articleDetail?"
                    "nodeId=NODE11750870"
                ),
            )
            return Namespace(
                resolutions={candidates[0].source_id: resolution},
                pages_fetched=1,
                returned_items=1,
            )

    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiResolver",
        FakeDbpiaResolver,
    )
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiHttpClient",
        lambda **_kwargs: object(),
    )
    args = Namespace(
        enable_platform_download=True,
        enable_dbpia_openapi_discovery=True,
        force=True,
        dbpia_api_key="private-key",
        dbpia_search_rps=1000.0,
        refresh_resolver_cache=False,
        connect_timeout=1.0,
        read_timeout=1.0,
        http_retries=0,
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        [paper],
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert calls == [(paper.source_id,)]
    assert urls == {
        paper.source_id: (
            "https://www.dbpia.co.kr/journal/articleDetail?"
            "nodeId=NODE11750870"
        )
    }
    assert counts == Counter(
        {"resolved": 1, "existing_artifact_reused": 0}
    )


def test_dbpia_search_without_key_is_network_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = replace(
        _paper("ART003071074"),
        doi_original="10.22798/KDFS.2024.18.1.11",
        doi="10.22798/KDFS.2024.18.1.11",
        institution_id="한국디지털포렌식학회",
    )
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiResolver",
        lambda *_args, **_kwargs: pytest.fail("resolver must not be created"),
    )
    args = Namespace(
        enable_platform_download=True,
        enable_dbpia_openapi_discovery=True,
        force=True,
        dbpia_api_key=None,
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        [paper],
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert urls == {}
    assert counts == Counter(
        {"configuration_missing": 1, "existing_artifact_reused": 0}
    )


def test_dbpia_browser_force_skips_current_artifact_during_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = replace(
        _paper("ART003071074"),
        doi="10.22798/KDFS.2024.18.1.11",
        journal_id="디지털포렌식연구",
        institution_id="한국디지털포렌식학회",
    )
    monkeypatch.setattr(
        process_papers,
        "download_artifact_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiResolver",
        lambda *_args, **_kwargs: pytest.fail("resolver must not be created"),
    )
    args = Namespace(
        enable_dbpia_openapi_discovery=True,
        enable_dbpia_browser_download=True,
        dbpia_openapi_scope="kdfs",
        force=True,
        dbpia_api_key="private-key",
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        [paper],
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert urls == {}
    assert counts == Counter({"existing_artifact_reused": 1})


def test_selected_dbpia_scope_partitions_exact_journal_publishers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    papers = [
        replace(
            _paper("ART1001"),
            journal_id="경희법학",
            institution_id="법학연구소",
        ),
        replace(
            _paper("ART1002"),
            journal_id="인터넷정보학회논문지",
            institution_id="한국인터넷정보학회",
        ),
    ]
    observed: list[tuple[str, str, tuple[str, ...], object]] = []

    class FakeDbpiaResolver:
        def __init__(self, config, *, http) -> None:
            self.config = config
            self.http = http

        def resolve(self, candidates: list[PaperCandidate]):
            observed.append(
                (
                    self.config.journal,
                    self.config.publisher,
                    tuple(candidate.source_id for candidate in candidates),
                    self.http,
                )
            )
            resolutions = {
                candidate.source_id: Namespace(
                    status="resolved",
                    resolved=True,
                    landing_url=(
                        "https://www.dbpia.co.kr/journal/articleDetail?"
                        "nodeId=NODE12345678"
                    ),
                )
                for candidate in candidates
            }
            return Namespace(
                resolutions=resolutions,
                pages_fetched=1,
                returned_items=len(candidates),
            )

    monkeypatch.setattr(process_papers, "DbpiaOpenApiResolver", FakeDbpiaResolver)
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiHttpClient",
        lambda **kwargs: Namespace(kwargs=kwargs),
    )
    args = Namespace(
        enable_dbpia_openapi_discovery=True,
        dbpia_openapi_scope="selected",
        force=False,
        dbpia_api_key="private-key",
        dbpia_search_rps=1000.0,
        connect_timeout=1.0,
        read_timeout=1.0,
        http_retries=0,
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        papers,
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert {(journal, publisher) for journal, publisher, _, _ in observed} == {
        ("경희법학", "법학연구소"),
        ("인터넷정보학회논문지", "한국인터넷정보학회"),
    }
    assert {source for _, _, sources, _ in observed for source in sources} == {
        "ART1001",
        "ART1002",
    }
    assert all(
        http.kwargs["search_journal"] == journal
        and http.kwargs["search_publisher"] == publisher
        for journal, publisher, _, http in observed
    )
    assert set(urls) == {"ART1001", "ART1002"}
    assert counts["resolved"] == 2


def test_selected_dbpia_scope_never_guesses_opaque_kci_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = replace(
        _paper("ART1003"),
        journal_id="SER000009999",
        institution_id="어떤 학회",
    )
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiResolver",
        lambda *_args, **_kwargs: pytest.fail("resolver must not be created"),
    )
    args = Namespace(
        enable_dbpia_openapi_discovery=True,
        dbpia_openapi_scope="selected",
        force=False,
        dbpia_api_key="private-key",
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        [paper],
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert urls == {}
    assert counts["scope_metadata_missing"] == 1


def test_selected_dbpia_scope_stops_after_provider_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    papers = [
        replace(
            _paper("ART2001"),
            journal_id="A Journal",
            institution_id="A Publisher",
        ),
        replace(
            _paper("ART2002"),
            journal_id="B Journal",
            institution_id="B Publisher",
        ),
    ]
    calls: list[str] = []

    class FailingResolver:
        def __init__(self, config, *, http) -> None:
            self.config = config

        def resolve(self, candidates: list[PaperCandidate]):
            calls.append(self.config.journal)
            resolution = Namespace(
                status="invalid_response",
                resolved=False,
                landing_url=None,
            )
            return Namespace(
                resolutions={candidates[0].source_id: resolution},
                pages_fetched=1,
                returned_items=0,
                stop_provider=True,
            )

    monkeypatch.setattr(process_papers, "DbpiaOpenApiResolver", FailingResolver)
    monkeypatch.setattr(
        process_papers,
        "DbpiaOpenApiHttpClient",
        lambda **_kwargs: object(),
    )
    args = Namespace(
        enable_dbpia_openapi_discovery=True,
        dbpia_openapi_scope="selected",
        force=False,
        dbpia_api_key="private-key",
        dbpia_search_rps=1000.0,
        connect_timeout=1.0,
        read_timeout=1.0,
        http_retries=0,
    )

    urls, counts = process_papers._resolve_dbpia_landing_urls(
        papers,
        papers_dir=tmp_path,
        args=args,
        explicit_urls={},
    )

    assert urls == {}
    assert calls == ["A Journal"]
    assert counts["invalid_response"] == 1
    assert counts["provider_circuit_deferred"] == 1


def test_dbpia_openapi_run_gate_counts_retries_and_stops_before_request() -> None:
    passed: list[str] = []
    limiter = Namespace(wait=lambda url: passed.append(url))
    gate = process_papers._DbpiaOpenApiRunGate(
        max_requests=2,
        limiter=limiter,
    )

    gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")
    gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")
    with pytest.raises(process_papers.HttpClientError, match="budget exhausted"):
        gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    assert gate.requests_started == 2
    assert len(passed) == 2


def test_dbpia_openapi_daily_gate_is_shared_across_process_instances(
    tmp_path: Path,
) -> None:
    passed: list[str] = []
    limiter = Namespace(wait=lambda url: passed.append(url))
    state_path = tmp_path / ".dbpia-openapi-daily-quota.json"
    first = process_papers._DbpiaOpenApiRunGate(
        max_requests=10,
        limiter=limiter,
        daily_state_path=state_path,
        daily_limit=2,
        day_provider=lambda: "2026-08-03",
    )
    second = process_papers._DbpiaOpenApiRunGate(
        max_requests=10,
        limiter=limiter,
        daily_state_path=state_path,
        daily_limit=2,
        day_provider=lambda: "2026-08-03",
    )

    first.wait("https://api.dbpia.co.kr/v2/search/search.xml")
    second.wait("https://api.dbpia.co.kr/v2/search/search.xml")
    with pytest.raises(process_papers.HttpClientError, match="daily"):
        first.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    state = process_papers.load_json_object(state_path)
    assert state is not None
    assert state["schema_version"] == 1
    assert state["date"] == "2026-08-03"
    assert state["requests_started"] == 2
    assert state["daily_limit"] == 2
    assert isinstance(state["updated_at"], str)
    # The third call is paced, then rejected immediately before HTTP send.
    assert len(passed) == 3


def test_dbpia_openapi_daily_gate_resets_on_new_korea_day(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".dbpia-openapi-daily-quota.json"
    process_papers.write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "date": "2026-08-02",
            "requests_started": 2500,
            "daily_limit": 2500,
        },
    )
    gate = process_papers._DbpiaOpenApiRunGate(
        max_requests=1,
        limiter=Namespace(wait=lambda _url: None),
        daily_state_path=state_path,
        day_provider=lambda: "2026-08-03",
    )

    gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    assert process_papers.load_json_object(state_path)["requests_started"] == 1


def test_dbpia_openapi_daily_gate_charges_after_rate_limit_wait(
    tmp_path: Path,
) -> None:
    current_day = ["2026-08-02"]

    def cross_midnight(_url: str) -> None:
        current_day[0] = "2026-08-03"

    state_path = tmp_path / ".dbpia-openapi-daily-quota.json"
    gate = process_papers._DbpiaOpenApiRunGate(
        max_requests=1,
        limiter=Namespace(wait=cross_midnight),
        daily_state_path=state_path,
        day_provider=lambda: current_day[0],
    )

    gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    assert process_papers.load_json_object(state_path)["date"] == "2026-08-03"


def test_dbpia_openapi_daily_gate_fails_closed_on_corrupt_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".dbpia-openapi-daily-quota.json"
    state_path.write_text("not-json", encoding="utf-8")
    gate = process_papers._DbpiaOpenApiRunGate(
        max_requests=1,
        limiter=Namespace(wait=lambda _url: None),
        daily_state_path=state_path,
        day_provider=lambda: "2026-08-03",
    )

    with pytest.raises(process_papers.HttpClientError, match="invalid"):
        gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    assert state_path.read_text(encoding="utf-8") == "not-json"


def test_dbpia_openapi_daily_gate_fails_closed_on_future_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".dbpia-openapi-daily-quota.json"
    process_papers.write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "date": "2026-08-04",
            "requests_started": 1,
            "daily_limit": 2500,
        },
    )
    gate = process_papers._DbpiaOpenApiRunGate(
        max_requests=1,
        limiter=Namespace(wait=lambda _url: None),
        daily_state_path=state_path,
        day_provider=lambda: "2026-08-03",
    )

    with pytest.raises(process_papers.HttpClientError, match="future"):
        gate.wait("https://api.dbpia.co.kr/v2/search/search.xml")

    assert process_papers.load_json_object(state_path)["requests_started"] == 1


def test_dbpia_openapi_daily_ledger_is_independent_of_papers_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    first = process_papers.default_dbpia_openapi_daily_state_path()
    second = process_papers.default_dbpia_openapi_daily_state_path()

    assert first == second
    assert tmp_path / "papers-a" not in first.parents
    assert tmp_path / "papers-b" not in second.parents


def test_relative_xdg_state_home_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")

    state_path = process_papers.default_dbpia_openapi_daily_state_path()

    assert state_path == (
        Path.home()
        / ".local"
        / "state"
        / "forensics-paper"
        / process_papers.DBPIA_OPENAPI_DAILY_STATE_FILE
    ).resolve()


def test_kci_detail_identifier_is_adopted_before_title_providers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    doi = "10.1234/kci-detail"

    class FakeKciResolver:
        def __init__(self, _config, *, http) -> None:
            self.http = http

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> process_papers.IdentifierResolution:
            resolved = replace(
                paper,
                doi_original=f"https://doi.org/{doi}",
                doi=doi,
                doi_source="kci-openapi-detail",
            )
            return process_papers.IdentifierResolution(
                status="resolved",
                reason="exact_article_id_title_and_year_match",
                paper=resolved,
                resolver="kci-openapi-detail",
                doi_original=f"https://doi.org/{doi}",
                matched_title=paper.title,
                matched_year=paper.publication_year,
                returned_items=1,
                exact_title_matches=1,
                title_year_matches=1,
                http_status=200,
            )

    monkeypatch.setattr(
        process_papers,
        "KciOpenApiIdentifierResolver",
        FakeKciResolver,
    )

    prepared, counts = process_papers._resolve_kci_detail_identifiers(
        [_paper()],
        papers_dir=tmp_path,
        args=_args(),
    )

    assert prepared[0].doi == doi
    assert prepared[0].doi_source == "kci-openapi-detail"
    assert prepared[0].identifier_resolution["resolver"] == (
        "kci-openapi-detail"
    )
    assert prepared[0].identifier_resolution["status"] == "resolved"
    assert counts == Counter({"kci-openapi-detail:resolved": 1})


def test_kci_detail_identifier_circuit_breaker_defers_remaining_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class RetryableKciResolver:
        def __init__(self, _config, *, http) -> None:
            self.http = http

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> process_papers.IdentifierResolution:
            calls.append(paper.source_id)
            return process_papers.IdentifierResolution(
                status="retryable_error",
                reason="kci_openapi_rate_limited",
                paper=paper,
                resolver="kci-openapi-detail",
                http_status=429,
                error_code="retryable_error",
            )

    monkeypatch.setattr(
        process_papers,
        "KciOpenApiIdentifierResolver",
        RetryableKciResolver,
    )
    papers = [_paper(f"ART{index:09d}") for index in range(1, 7)]

    prepared, counts = process_papers._resolve_kci_detail_identifiers(
        papers,
        papers_dir=tmp_path,
        args=_args(),
    )

    assert calls == [paper.source_id for paper in papers[:3]]
    assert all(
        paper.identifier_resolution["status"] == "retryable_error"
        for paper in prepared
    )
    assert counts == Counter({"kci-openapi-detail:retryable_error": 6})


def test_kci_detail_auth_failure_stops_after_one_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class DeniedKciResolver:
        def __init__(self, _config, *, http) -> None:
            self.http = http

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> process_papers.IdentifierResolution:
            calls.append(paper.source_id)
            return process_papers.IdentifierResolution(
                status="configuration_error",
                reason="kci_openapi_access_denied",
                paper=paper,
                resolver="kci-openapi-detail",
                http_status=403,
                error_code="access_denied",
            )

    monkeypatch.setattr(
        process_papers,
        "KciOpenApiIdentifierResolver",
        DeniedKciResolver,
    )
    papers = [_paper(f"ART{index:09d}") for index in range(1, 7)]

    prepared, counts = process_papers._resolve_kci_detail_identifiers(
        papers,
        papers_dir=tmp_path,
        args=_args(),
    )

    assert calls == [papers[0].source_id]
    assert all(
        paper.identifier_resolution["status"] == "configuration_error"
        for paper in prepared
    )
    assert counts == Counter(
        {"kci-openapi-detail:configuration_error": 6}
    )


def test_kci_detail_identifier_skips_current_doi_less_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class MustNotRun:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("resolver must not be constructed")

    monkeypatch.setattr(
        process_papers,
        "KciOpenApiIdentifierResolver",
        MustNotRun,
    )
    paper = _paper()

    prepared, counts = process_papers._resolve_kci_detail_identifiers(
        [paper],
        papers_dir=tmp_path,
        args=_args(),
        skip_source_ids={paper.source_id},
    )

    assert prepared == [paper]
    assert counts == Counter(
        {"kci-openapi-detail:existing-artifact-reused": 1}
    )


@pytest.mark.parametrize(
    "record",
    [
        {
            "resolver": "kci-openapi-detail",
            "status": "identity_mismatch",
            "reason": "title_mismatch",
        },
        {
            "resolver": "kci-openapi-detail",
            "status": "ambiguous",
            "reason": "multiple_kci_article_records",
            "error_code": "ambiguous_response",
        },
        {
            "resolver": "kci-openapi-detail",
            "status": "invalid_response",
            "reason": "multiple_kci_article_records",
            "error_code": "ambiguous_response",
        },
    ],
)
def test_kci_identity_uncertainty_cannot_be_overridden_by_title_lookup(
    record: dict[str, object],
) -> None:
    assert process_papers._public_title_attempt_is_ambiguous(record)


class _FakeUnpaywallResolver:
    doi = "10.1234/same"

    def __init__(self, _config, *, http) -> None:
        self.http = http

    def resolve(self, paper: PaperCandidate) -> UnpaywallIdentifierResolution:
        resolved = replace(
            paper,
            doi_original=self.doi,
            doi=self.doi,
            doi_source="unpaywall-title",
        )
        return UnpaywallIdentifierResolution(
            status="resolved",
            reason="exact_title_and_year_match",
            paper=resolved,
            doi_original=self.doi,
            matched_title=paper.title,
            matched_year=paper.publication_year,
            oa_location=UnpaywallOaLocation(
                doi=self.doi,
                url_for_pdf="https://repository.example/paper.pdf",
                url_for_landing_page="https://repository.example/paper",
                license="cc-by",
                version="publishedVersion",
            ),
            returned_items=1,
            exact_title_matches=1,
            title_year_matches=1,
            http_status=200,
        )


class _FakeOpenAlexResolver:
    doi = "10.1234/same"

    def __init__(self, _config, *, http) -> None:
        self.http = http

    def resolve(self, paper: PaperCandidate) -> OpenAlexIdentifierResolution:
        resolved = replace(
            paper,
            doi_original=self.doi,
            doi=self.doi,
            doi_source="openalex-title",
        )
        return OpenAlexIdentifierResolution(
            status="resolved",
            reason="exact_title_and_year_match",
            paper=resolved,
            doi_original=self.doi,
            matched_title=paper.title,
            matched_year=paper.publication_year,
            locations=(
                OpenAlexLocation(
                    pdf_url="https://repository.example/paper.pdf",
                    landing_page_url="https://repository.example/paper",
                    license="cc-by",
                    version="publishedVersion",
                    is_open_access=True,
                    is_best=True,
                ),
            ),
            returned_items=1,
            exact_title_matches=1,
            title_year_matches=1,
            http_status=200,
        )


def test_public_title_sources_require_cross_provider_consensus(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        _FakeUnpaywallResolver,
    )
    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        _FakeOpenAlexResolver,
    )

    paper = _paper()
    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=_args(),
        )
    )

    assert prepared[0].doi == "10.1234/same"
    assert prepared[0].doi_source in {
        "unpaywall-title",
        "openalex-title",
    }
    assert prepared[0].identifier_resolution["status"] == "resolved"
    assert len(prepared[0].identifier_resolution["attempts"]) == 2
    assert len(candidates[paper.source_id]) == 1
    assert candidates[paper.source_id][0].verified_doi == "10.1234/same"
    assert counts["public-title:resolved"] == 1


def test_public_title_sources_reject_provider_doi_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class ConflictingOpenAlex(_FakeOpenAlexResolver):
        doi = "10.9999/different"

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        _FakeUnpaywallResolver,
    )
    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        ConflictingOpenAlex,
    )

    paper = _paper()
    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=_args(),
        )
    )

    assert prepared[0].doi is None
    assert candidates == {}
    resolution = prepared[0].identifier_resolution
    assert resolution["resolver"] == "public-title-consensus"
    assert resolution["status"] == "ambiguous"
    assert resolution["reason"] == "cross_source_doi_conflict"
    assert counts["public-title:ambiguous"] == 1


def test_public_title_sources_reject_one_provider_ambiguity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class AmbiguousUnpaywall(_FakeUnpaywallResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> UnpaywallIdentifierResolution:
            return UnpaywallIdentifierResolution(
                status="ambiguous",
                reason="multiple_distinct_dois",
                paper=paper,
                returned_items=2,
                exact_title_matches=2,
                title_year_matches=2,
                http_status=200,
            )

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        AmbiguousUnpaywall,
    )
    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        _FakeOpenAlexResolver,
    )

    paper = _paper()
    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=_args(),
        )
    )

    assert prepared[0].doi is None
    assert candidates == {}
    resolution = prepared[0].identifier_resolution
    assert resolution["status"] == "ambiguous"
    assert resolution["reason"] == "provider_returned_multiple_distinct_dois"
    assert counts["public-title:ambiguous"] == 1


def test_semantic_scholar_multiple_match_items_block_other_provider_doi(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class AmbiguousSemanticScholar:
        def __init__(self, _config, *, request_gate) -> None:
            self.request_gate = request_gate

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> SemanticScholarTitleResolution:
            return SemanticScholarTitleResolution(
                status="ambiguous",
                reason="multiple_match_items",
                paper=paper,
                returned_items=0,
                http_status=200,
                error_code="multiple_match_items",
            )

    monkeypatch.setattr(
        process_papers,
        "SemanticScholarTitleResolver",
        AmbiguousSemanticScholar,
    )
    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        _FakeUnpaywallResolver,
    )
    args = _args()
    args.enable_openalex_title_resolution = False
    args.enable_semantic_scholar = True
    args.semantic_scholar_api_key = "test-s2-key"

    paper = _paper()
    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=args,
            semantic_scholar_request_gate=lambda _url: None,
        )
    )

    assert prepared[0].doi is None
    assert candidates == {}
    resolution = prepared[0].identifier_resolution
    assert resolution["status"] == "ambiguous"
    assert resolution["reason"] == "provider_returned_multiple_distinct_dois"
    semantic_attempt = next(
        attempt
        for attempt in resolution["attempts"]
        if attempt["resolver"] == "semantic-scholar-title"
    )
    assert semantic_attempt["status"] == "ambiguous"
    assert semantic_attempt["reason"] == "multiple_match_items"
    assert semantic_attempt["error_code"] == "multiple_match_items"
    assert counts["semantic-scholar-title:ambiguous"] == 1
    assert counts["public-title:ambiguous"] == 1


def test_public_title_sources_do_not_override_prior_crossref_ambiguity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    doi = "10.1234/semantic-title"

    class ResolvedSemanticScholar:
        def __init__(self, _config, *, request_gate) -> None:
            self.request_gate = request_gate

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> SemanticScholarTitleResolution:
            resolved = replace(
                paper,
                doi_original=doi,
                doi=doi,
                doi_source="semantic-scholar-title",
            )
            return SemanticScholarTitleResolution(
                status="resolved",
                reason="exact_title_year_single_doi",
                paper=resolved,
                doi_original=doi,
                matched_title=paper.title,
                matched_year=paper.publication_year,
                pdf=SemanticScholarTitleOpenAccessPdf(
                    doi=doi,
                    url="https://pdfs.semanticscholar.org/example.pdf",
                    title=paper.title or "",
                    year=paper.publication_year or 0,
                ),
            )

    monkeypatch.setattr(
        process_papers,
        "SemanticScholarTitleResolver",
        ResolvedSemanticScholar,
    )
    args = _args()
    args.enable_unpaywall_title_resolution = False
    args.enable_openalex_title_resolution = False
    args.enable_semantic_scholar = True
    args.semantic_scholar_api_key = "test-s2-key"
    paper = replace(
        _paper(),
        identifier_resolution={
            "resolver": "crossref-title",
            "status": "ambiguous",
            "reason": "multiple_distinct_dois",
        },
    )

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=args,
            semantic_scholar_request_gate=lambda _url: None,
        )
    )

    assert prepared[0].doi is None
    assert candidates == {}
    resolution = prepared[0].identifier_resolution
    assert resolution["resolver"] == "public-title-consensus"
    assert resolution["status"] == "ambiguous"
    assert resolution["reason"] == "prior_title_resolution_ambiguous"
    assert resolution["attempts"][0]["resolver"] == "crossref-title"
    assert counts["semantic-scholar-title:resolved"] == 1
    assert counts["public-title:ambiguous"] == 1


def test_public_title_sources_skip_current_doi_less_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class MustNotRun:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("resolver must not be constructed")

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        MustNotRun,
    )
    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        MustNotRun,
    )

    paper = _paper()
    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=_args(),
            skip_source_ids={paper.source_id},
        )
    )

    assert prepared == [paper]
    assert candidates == {}
    assert counts["public-title:existing-artifact-reused"] == 1


def test_public_title_sources_preserve_transient_lookup_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailedUnpaywall(_FakeUnpaywallResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> UnpaywallIdentifierResolution:
            return UnpaywallIdentifierResolution(
                status="request_error",
                reason="unpaywall_request_failed",
                paper=paper,
                error_code="timeout",
            )

    class NoMatchOpenAlex(_FakeOpenAlexResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> OpenAlexIdentifierResolution:
            return OpenAlexIdentifierResolution(
                status="no_match",
                reason="title_mismatch",
                paper=paper,
                http_status=200,
            )

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        FailedUnpaywall,
    )
    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        NoMatchOpenAlex,
    )

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [_paper()],
            papers_dir=tmp_path,
            args=_args(),
        )
    )

    assert candidates == {}
    assert prepared[0].identifier_resolution["status"] == "retryable_error"
    assert counts["public-title:retryable_error"] == 1


def test_public_title_provider_circuit_opens_after_consecutive_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FailedUnpaywall(_FakeUnpaywallResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> UnpaywallIdentifierResolution:
            calls.append(paper.source_id)
            return UnpaywallIdentifierResolution(
                status="http_error",
                reason="unpaywall_http_error",
                paper=paper,
                http_status=500,
            )

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        FailedUnpaywall,
    )
    args = _args()
    args.enable_openalex_title_resolution = False
    papers = [
        replace(_paper(), database_id=index, source_id=f"ART{index:04d}")
        for index in range(1, 6)
    ]

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            papers,
            papers_dir=tmp_path,
            args=args,
        )
    )

    assert len(calls) == process_papers.PROVIDER_CIRCUIT_BREAKER_THRESHOLD
    assert candidates == {}
    assert all(
        paper.identifier_resolution["status"] == "retryable_error"
        for paper in prepared
    )
    assert counts["public-title:retryable_error"] == len(papers)


def test_public_title_provider_circuit_also_opens_after_mid_run_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class DegradingUnpaywall(_FakeUnpaywallResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> UnpaywallIdentifierResolution:
            calls.append(paper.source_id)
            sequence = int(paper.source_id.removeprefix("ART"))
            if 5 <= sequence <= 7:
                return UnpaywallIdentifierResolution(
                    status="http_error",
                    reason="unpaywall_rate_limited",
                    paper=paper,
                    http_status=429,
                    error_code="rate_limited",
                )
            return UnpaywallIdentifierResolution(
                status="no_match",
                reason="title_mismatch",
                paper=paper,
                http_status=200,
            )

    monkeypatch.setattr(
        process_papers,
        "UnpaywallTitleResolver",
        DegradingUnpaywall,
    )
    args = _args()
    args.enable_openalex_title_resolution = False
    papers = [
        replace(_paper(), database_id=index, source_id=f"ART{index:04d}")
        for index in range(1, 11)
    ]

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            papers,
            papers_dir=tmp_path,
            args=args,
        )
    )

    assert calls == [f"ART{index:04d}" for index in range(1, 8)]
    assert candidates == {}
    assert [
        paper.identifier_resolution["status"]
        for paper in prepared
    ] == ["unresolved"] * 4 + ["retryable_error"] * 6
    assert counts["public-title:retryable_error"] == 6


def test_strict_oa_mode_drops_non_oa_preverified_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class NonOaOpenAlex(_FakeOpenAlexResolver):
        def resolve(
            self,
            paper: PaperCandidate,
        ) -> OpenAlexIdentifierResolution:
            result = super().resolve(paper)
            return replace(
                result,
                locations=(
                    replace(
                        result.locations[0],
                        is_open_access=False,
                    ),
                ),
            )

    monkeypatch.setattr(
        process_papers,
        "OpenAlexTitleResolver",
        NonOaOpenAlex,
    )
    args = _args()
    args.enable_unpaywall_title_resolution = False
    args.require_open_access_evidence = True

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [_paper()],
            papers_dir=tmp_path,
            args=args,
        )
    )

    assert prepared[0].doi == "10.1234/same"
    assert candidates == {}
    assert counts["public-title:resolved"] == 1


def test_semantic_scholar_title_uses_shared_gate_and_retains_oa_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    doi = "10.1234/semantic-title"
    gate_calls: list[str] = []
    observed: dict[str, object] = {}

    class FakeSemanticScholarTitleResolver:
        def __init__(self, config, *, request_gate) -> None:
            self.request_gate = request_gate
            observed["retries"] = config.retries

        def resolve(
            self,
            paper: PaperCandidate,
        ) -> SemanticScholarTitleResolution:
            self.request_gate("https://api.semanticscholar.org/test")
            resolved = replace(
                paper,
                doi_original=doi,
                doi=doi,
                doi_source="semantic-scholar-title",
            )
            return SemanticScholarTitleResolution(
                status="resolved",
                reason="exact_title_year_single_doi",
                paper=resolved,
                doi_original=doi,
                matched_title=paper.title,
                matched_year=paper.publication_year,
                pdf=SemanticScholarTitleOpenAccessPdf(
                    doi=doi,
                    url="https://pdfs.semanticscholar.org/example.pdf",
                    title=paper.title or "",
                    year=paper.publication_year or 0,
                    license="CC-BY",
                ),
                queried_titles=(paper.title or "",),
                query_count=1,
                returned_items=1,
                exact_title_matches=1,
                title_year_matches=1,
                http_status=200,
            )

    monkeypatch.setattr(
        process_papers,
        "SemanticScholarTitleResolver",
        FakeSemanticScholarTitleResolver,
    )
    args = _args()
    args.enable_unpaywall_title_resolution = False
    args.enable_openalex_title_resolution = False
    args.enable_semantic_scholar = True
    args.semantic_scholar_api_key = "test-s2-key"
    args.http_retries = 4
    paper = _paper()

    prepared, candidates, counts = (
        process_papers._resolve_public_title_sources(
            [paper],
            papers_dir=tmp_path,
            args=args,
            semantic_scholar_request_gate=gate_calls.append,
        )
    )

    assert gate_calls == ["https://api.semanticscholar.org/test"]
    assert prepared[0].doi == doi
    assert prepared[0].doi_source == "semantic-scholar-title"
    assert candidates[paper.source_id][0].verified_doi == doi
    assert candidates[paper.source_id][0].is_open_access is True
    attempts = prepared[0].identifier_resolution["attempts"]
    semantic_attempt = attempts[-1]
    assert semantic_attempt["resolver"] == "semantic-scholar-title"
    assert semantic_attempt["query_count"] == 1
    assert semantic_attempt["open_access_pdf"]["license"] == "CC-BY"
    assert counts["semantic-scholar-title:resolved"] == 1
    assert counts["public-title:resolved"] == 1
    assert observed["retries"] == 4


def test_prepare_downloads_shares_one_cross_process_s2_limiter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeInterProcessLimiter:
        def __init__(self, requests_per_second, state_path) -> None:
            observed["limiter"] = self
            observed["rps"] = requests_per_second
            observed["state_path"] = state_path

        def wait(self, _url: str) -> None:
            return None

        def defer(self, _seconds: float) -> None:
            return None

    def fake_public_title(
        papers,
        *,
        semantic_scholar_request_gate,
        semantic_scholar_defer_gate,
        **_kwargs,
    ):
        observed["title_wait_owner"] = semantic_scholar_request_gate.__self__
        observed["title_defer_owner"] = semantic_scholar_defer_gate.__self__
        return papers, {}, Counter()

    class FakeBatchResolver:
        def __init__(
            self,
            _config,
            *,
            request_gate,
            defer_gate,
        ) -> None:
            observed["batch_wait_owner"] = request_gate.__self__
            observed["batch_defer_owner"] = defer_gate.__self__

        def resolve(self, papers):
            return {
                paper.source_id: process_papers.SemanticScholarResolution(
                    source_id=paper.source_id,
                    doi=None,
                    status="missing_metadata",
                    reason="missing_doi",
                )
                for paper in papers
            }

    monkeypatch.setattr(
        process_papers,
        "InterProcessRateLimiter",
        FakeInterProcessLimiter,
    )
    monkeypatch.setattr(
        process_papers,
        "_resolve_public_title_sources",
        fake_public_title,
    )
    monkeypatch.setattr(
        process_papers,
        "SemanticScholarBatchResolver",
        FakeBatchResolver,
    )
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--semantic-scholar",
            "--semantic-scholar-api-key",
            "test-s2-key",
            "--semantic-scholar-rps",
            "0.5",
            "--no-kci-journal-sitemap",
            "--no-crossref-title-resolution",
        ]
    )
    process_papers._validate_args(args)

    process_papers._prepare_downloads(
        [_paper()],
        papers_dir=tmp_path,
        args=args,
        download_rate_limiter=process_papers.PerHostRateLimiter(1000),
    )

    limiter = observed["limiter"]
    assert observed["rps"] == 0.5
    assert observed["state_path"] == (
        process_papers.REPOSITORY_ROOT
        / "Analyzer"
        / "Manifest"
        / ".semantic-scholar-rate-limit"
    )
    assert observed["title_wait_owner"] is limiter
    assert observed["title_defer_owner"] is limiter
    assert observed["batch_wait_owner"] is limiter
    assert observed["batch_defer_owner"] is limiter


def test_semantic_scholar_is_opt_in_until_key_is_issued() -> None:
    args = process_papers.build_parser().parse_args(["download", "--dry-run"])

    assert args.enable_semantic_scholar is False


def test_standalone_cli_defaults_to_split_artifact_layout() -> None:
    args = process_papers.build_parser().parse_args(["download", "--dry-run"])
    status = process_papers.build_parser().parse_args(["status"])

    assert args.papers_dir == "Analyzer/Paper"
    assert args.manifest_dir == "Analyzer/Manifest"
    assert status.papers_dir == "Analyzer/Paper"
    assert status.manifest_dir == "Analyzer/Manifest"


def test_require_complete_is_opt_in_for_download_and_run() -> None:
    parser = process_papers.build_parser()

    assert parser.parse_args(["download", "--dry-run"]).require_complete is False
    assert (
        parser.parse_args(
            ["download", "--dry-run", "--require-complete"]
        ).require_complete
        is True
    )
    assert (
        parser.parse_args(
            ["run", "--dry-run", "--require-complete"]
        ).require_complete
        is True
    )


def test_require_complete_exit_policy_rejects_any_missing_selected_pdf() -> None:
    partial = process_papers.DownloadBatchResult(
        counts=Counter({"reused": 1, "not_found": 1}),
        successful_source_ids=frozenset({"ART0001"}),
    )
    selected = {"ART0001", "ART0002"}

    assert not process_papers._download_result_requires_failure(
        partial,
        selected_source_ids=selected,
        require_complete=False,
    )
    assert process_papers._download_result_requires_failure(
        partial,
        selected_source_ids=selected,
        require_complete=True,
    )

    complete = process_papers.DownloadBatchResult(
        counts=Counter({"reused": 2}),
        successful_source_ids=frozenset(selected),
    )
    assert not process_papers._download_result_requires_failure(
        complete,
        selected_source_ids=selected,
        require_complete=True,
    )


def test_missing_doi_only_is_mutually_exclusive_with_doi_only() -> None:
    parser = process_papers.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["download", "--doi-only", "--missing-doi-only"]
        )


def test_semantic_scholar_rejects_a_pending_note_as_api_key() -> None:
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--semantic-scholar",
            "--semantic-scholar-api-key",
            "발급 대기중 (신청완료)",
        ]
    )

    with pytest.raises(ValueError, match="발급된 ASCII 키"):
        process_papers._validate_args(args)


def test_elsevier_rejects_malformed_api_key_before_workers_start() -> None:
    args = process_papers.build_parser().parse_args(
        [
            "download",
            "--elsevier-api-key",
            "invalid key",
        ]
    )

    with pytest.raises(ValueError, match="ELSEVIER_API_KEY"):
        process_papers._validate_args(args)
