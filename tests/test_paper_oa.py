from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

import Analyzer.OA.paper_oa as paper_oa_module
from Analyzer.OA.paper_oa import (
    CACHE_SCHEMA_VERSION,
    KCI_ARTICLE_SEARCH_URL,
    KCI_OAI_PRIMARY_PREFIX,
    KCI_OAI_SET,
    OA_COLLECTION_STRATEGY,
    FatalOaError,
    InvalidOaResponse,
    KciArticleSearchHttpClient,
    KciOaClient,
    KciPaper,
    OaCache,
    OaCollectionError,
    OaCollectionRun,
    OaiDcRecord,
    OaiHarvestCache,
    OaiHarvestState,
    OaiListPage,
    PaperOaResult,
    ParsedOaValue,
    RetryableOaError,
    _RedactedQueryParams,
    collect_open_access,
    collect_open_access_oai,
    export_oa_workbook,
    load_kci_papers,
    parse_article_search_xml,
)
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
)


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=SEOUL)
SOURCE_ID = "ART000000001"
SECRET = "oa-test-secret-key"


def _paper(
    index: int = 1,
    *,
    source_id: str | None = None,
    title: str = "시험 논문",
) -> KciPaper:
    resolved_source_id = source_id or f"ART{index:09d}"
    return KciPaper(
        paper_index=index,
        database_id=index,
        source_id=resolved_source_id,
        title=title,
        title_kor=title,
        title_eng="Test paper",
        doi_raw="https://doi.org/10.1234/PAPER",
        doi="10.1234/PAPER",
        publication_year=2026,
        journal_id="JOURNAL-1",
        institution_id="INST-1",
    )


def _xml(
    field: str | None = "<orte-open-yn>Y</orte-open-yn>",
    *,
    source_id: str = SOURCE_ID,
    total: int = 1,
    result_message: str = "정상 처리되었습니다.",
    records: str | None = None,
) -> bytes:
    if records is None:
        records = "" if total == 0 else f"""
        <record>
          <journalInfo journal-id="JOURNAL-1" />
          <articleInfo article-id="{source_id}">
            <title-group><article-title lang="original">시험 논문</article-title></title-group>
            {field or ""}
          </articleInfo>
        </record>
        """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <MetaData>
      <inputData><apiCode>articleSearch</apiCode><key>{SECRET}</key></inputData>
      <outputData>
        <result><total>{total}</total><resultMsg>{result_message}</resultMsg></result>
        {records}
      </outputData>
    </MetaData>
    """.encode()


def _result(
    paper: KciPaper,
    status: str,
    *,
    token: str | None = None,
    observed: str | None = None,
) -> PaperOaResult:
    if status == "classified":
        return PaperOaResult(
            paper=paper,
            status=status,
            returned_source_id=paper.source_id,
            orte_open_yn=token,
            field_present=True,
            observed_value=None,
            collected_at=NOW.isoformat(),
            attempts=1,
        )
    if status == "field_missing":
        return PaperOaResult(
            paper=paper,
            status=status,
            returned_source_id=paper.source_id,
            orte_open_yn=None,
            field_present=False,
            observed_value=None,
            collected_at=NOW.isoformat(),
            attempts=1,
            error_code="orte_open_yn_missing",
        )
    if status == "invalid_value":
        return PaperOaResult(
            paper=paper,
            status=status,
            returned_source_id=paper.source_id,
            orte_open_yn=None,
            field_present=True,
            observed_value=observed,
            collected_at=NOW.isoformat(),
            attempts=1,
            error_code="orte_open_yn_invalid",
        )
    raise AssertionError(f"unsupported fixture status: {status}")


def _oai_result(paper: KciPaper, token: str = "Y") -> PaperOaResult:
    return PaperOaResult(
        paper=paper,
        status="classified",
        returned_source_id=paper.source_id,
        orte_open_yn=token,
        field_present=True,
        observed_value=None,
        collected_at=NOW.isoformat(),
        attempts=1,
        evidence_strategy=OA_COLLECTION_STRATEGY,
        oai_identifier=f"oai:kci.go.kr:ARTI/{paper.database_id}",
        dc_rights=token,
        dc_rights_field_present=True,
        observed_dc_rights=None,
        kci_orte_open_yn=token,
        kci_orte_open_yn_field_present=True,
        observed_kci_orte_open_yn=None,
    )


def _hold_harvest_lock(
    directory: str,
    source_ids: tuple[str, ...],
    from_date: str,
    until_date: str,
    entered: Any,
    release: Any,
) -> None:
    cache = OaiHarvestCache(Path(directory), now=lambda: NOW)
    with cache.window_lock(
        set(source_ids), from_date=from_date, until_date=until_date
    ):
        entered.set()
        if not release.wait(15):
            raise RuntimeError("test lock holder timed out")


def _probe_harvest_lock(
    directory: str,
    source_ids: tuple[str, ...],
    from_date: str,
    until_date: str,
    attempting: Any,
    acquired: Any,
) -> None:
    cache = OaiHarvestCache(Path(directory), now=lambda: NOW)
    attempting.set()
    with cache.window_lock(
        set(source_ids), from_date=from_date, until_date=until_date
    ):
        acquired.set()


def _hold_paper_lock(
    directory: str,
    source_id: str,
    entered: Any,
    release: Any,
) -> None:
    cache = OaCache(Path(directory), strategy=OA_COLLECTION_STRATEGY)
    with cache._paper_lock(source_id):
        entered.set()
        if not release.wait(15):
            raise RuntimeError("test paper lock holder timed out")


def _save_oai_result_in_process(
    directory: str,
    attempting: Any,
    finished: Any,
) -> None:
    attempting.set()
    cache = OaCache(Path(directory), strategy=OA_COLLECTION_STRATEGY)
    cache.save(_oai_result(_paper()))
    finished.set()


def _invalidate_oai_result_in_process(
    directory: str,
    attempting: Any,
    finished: Any,
) -> None:
    attempting.set()
    cache = OaCache(Path(directory), strategy=OA_COLLECTION_STRATEGY)
    cache.invalidate(_paper())
    finished.set()


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("Y", "Y"),
        ("N", "N"),
        (" y ", "Y"),
        ("\n n\t", "N"),
    ],
)
def test_parse_y_and_n_values_are_the_only_classified_outcomes(
    raw_value: str,
    expected: str,
) -> None:
    parsed = parse_article_search_xml(
        _xml(f"<orte-open-yn>{raw_value}</orte-open-yn>"),
        expected_source_id=SOURCE_ID,
    )

    assert parsed == ParsedOaValue(
        status="classified",
        returned_source_id=SOURCE_ID,
        orte_open_yn=expected,
        field_present=True,
    )
    assert SECRET not in repr(parsed)


def test_missing_and_blank_fields_remain_unresolved_and_are_distinguishable() -> None:
    missing = parse_article_search_xml(
        _xml(field=None), expected_source_id=SOURCE_ID
    )
    blank = parse_article_search_xml(
        _xml("<orte-open-yn>  \n </orte-open-yn>"),
        expected_source_id=SOURCE_ID,
    )

    assert missing.status == blank.status == "field_missing"
    assert missing.orte_open_yn is blank.orte_open_yn is None
    assert missing.field_present is False
    assert blank.field_present is True


def test_unexpected_oa_token_is_retained_for_diagnosis_but_not_classified() -> None:
    parsed = parse_article_search_xml(
        _xml("<orte-open-yn>UNKNOWN</orte-open-yn>"),
        expected_source_id=SOURCE_ID,
    )

    assert parsed.status == "invalid_value"
    assert parsed.orte_open_yn is None
    assert parsed.observed_value == "UNKNOWN"
    assert parsed.field_present is True


@pytest.mark.parametrize(
    "body",
    [
        _xml(field=None, total=0),
        b"<MetaData><outputData><result><resultMsg>No Data</resultMsg></result></outputData></MetaData>",
    ],
)
def test_no_record_is_not_confused_with_non_open_access(body: bytes) -> None:
    parsed = parse_article_search_xml(body, expected_source_id=SOURCE_ID)

    assert parsed == ParsedOaValue("no_record", None, None, None)


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        (b"", "empty_xml"),
        (b"<MetaData>", "malformed_xml"),
        (b'<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>', "unsafe_xml"),
        (_xml(field=None, total=1, records=""), "record_count_mismatch"),
        (
            b"<evil><outputData><result><total>1</total></result>"
            b'<record><articleInfo article-id="ART000000001">'
            b"<orte-open-yn>Y</orte-open-yn></articleInfo></record>"
            b"</outputData></evil>",
            "invalid_envelope",
        ),
        (
            _xml(
                "<orte-open-yn>Y</orte-open-yn><orte-open-yn>N</orte-open-yn>"
            ),
            "multiple_orte_open_yn_fields",
        ),
    ],
)
def test_parser_rejects_malformed_or_contract_violating_responses(
    body: bytes,
    error_code: str,
) -> None:
    with pytest.raises(InvalidOaResponse, match=error_code):
        parse_article_search_xml(body, expected_source_id=SOURCE_ID)


def test_parser_rejects_returned_article_id_mismatch() -> None:
    with pytest.raises(InvalidOaResponse, match="article_identity_mismatch"):
        parse_article_search_xml(
            _xml(source_id="ART000000002"),
            expected_source_id=SOURCE_ID,
        )


def test_nonzero_api_result_code_is_a_fatal_contract_error() -> None:
    body = (
        b"<MetaData><outputData><result><resultCode>99</resultCode>"
        b"<total>1</total><resultMsg>success</resultMsg></result>"
        b'<record><articleInfo article-id="ART000000001">'
        b"<orte-open-yn>Y</orte-open-yn></articleInfo></record>"
        b"</outputData></MetaData>"
    )

    with pytest.raises(FatalOaError, match="api_reported_error"):
        parse_article_search_xml(body, expected_source_id=SOURCE_ID)


def test_api_reported_error_is_fatal_and_does_not_echo_server_text_or_key() -> None:
    body = _xml(
        field=None,
        total=0,
        result_message=f"authentication failed for {SECRET}",
    )

    with pytest.raises(FatalOaError, match="api_authentication_error") as raised:
        parse_article_search_xml(body, expected_source_id=SOURCE_ID)

    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


class _FakeHttpClient:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.params_repr: str | None = None
        self.params: Any = None

    def get_bytes(self, url: str, **kwargs: Any) -> HttpFetch:
        assert url == KCI_ARTICLE_SEARCH_URL
        self.params = kwargs["params"]
        self.params_repr = repr(self.params)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, HttpFetch)
        return self.outcome


def _fetch(status: int, body: bytes = b"", **headers: str) -> HttpFetch:
    return HttpFetch(
        requested_url=KCI_ARTICLE_SEARCH_URL,
        final_url=KCI_ARTICLE_SEARCH_URL,
        status_code=status,
        headers=headers,
        body=body,
        redirects=(),
    )


@pytest.mark.parametrize(
    ("outcome", "exception_type", "error_code"),
    [
        (_fetch(429, **{"Retry-After": "7"}), RetryableOaError, "http_429"),
        (_fetch(503), RetryableOaError, "http_503"),
        (_fetch(401), FatalOaError, "authentication_failed"),
        (_fetch(403), FatalOaError, "authentication_failed"),
        (_fetch(400), FatalOaError, "http_400"),
        (RetryableHttpError("secret-bearing transport detail"), RetryableOaError, "network_error"),
        (ResponseTooLargeError("too large"), InvalidOaResponse, "response_too_large"),
        (HttpClientError("bad client"), FatalOaError, "http_client_error"),
    ],
)
def test_client_maps_http_and_transport_errors_to_stable_safe_codes(
    outcome: object,
    exception_type: type[Exception],
    error_code: str,
) -> None:
    http = _FakeHttpClient(outcome)
    client = KciOaClient(SECRET, http_client=http)

    with pytest.raises(exception_type, match=error_code) as raised:
        client.fetch(SOURCE_ID)

    assert client.request_count == 1
    assert SECRET not in str(raised.value)
    assert SECRET not in (http.params_repr or "")


def test_successful_client_request_uses_redacted_parameters_and_discards_raw_xml() -> None:
    http = _FakeHttpClient(_fetch(200, _xml("<orte-open-yn>Y</orte-open-yn>")))
    client = KciOaClient(SECRET, http_client=http)

    parsed = client.fetch(SOURCE_ID)

    assert parsed.orte_open_yn == "Y"
    assert http.params["key"] == SECRET
    assert SECRET not in (http.params_repr or "")
    assert SECRET not in repr(parsed)


def test_query_mapping_redacts_key_from_both_string_representations() -> None:
    params = _RedactedQueryParams(api_key=SECRET, source_id=SOURCE_ID)

    assert params["key"] == SECRET
    assert SECRET not in repr(params)
    assert SECRET not in str(params)
    assert "<redacted>" in repr(params)


class _FakeArticleSearchClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.request_count = 0

    def fetch(self, _source_id: str) -> ParsedOaValue:
        self.request_count += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ParsedOaValue)
        return outcome


def test_collection_retries_then_resumes_from_secret_free_cache(tmp_path: Path) -> None:
    now = lambda: NOW
    sleeps: list[float] = []
    cache = OaCache(tmp_path / "cache", now=now)
    client = _FakeArticleSearchClient(
        [
            RetryableOaError("http_429", retry_after=2.5),
            ParsedOaValue("classified", SOURCE_ID, "Y", True),
        ]
    )

    first = collect_open_access(
        [_paper()],
        client,
        cache=cache,
        retries=2,
        sleeper=sleeps.append,
        now=now,
    )

    assert client.request_count == 2
    assert sleeps == [2.5]
    assert first.results[0].attempts == 2
    assert first.results[0].is_open_access is True
    cache_path = tmp_path / "cache" / "_article_search" / f"{SOURCE_ID}.json"
    cache_text = cache_path.read_text(encoding="utf-8")
    assert SECRET not in cache_text
    assert "<MetaData" not in cache_text

    second_client = _FakeArticleSearchClient([])
    second = collect_open_access(
        [_paper()], second_client, cache=cache, now=now
    )
    assert second_client.request_count == 0
    assert second.cache_hits == 1
    assert second.results[0].cache_hit is True
    assert second.results[0].attempts == 0


def test_cache_identity_mismatch_is_treated_as_a_cache_miss(tmp_path: Path) -> None:
    cache = OaCache(tmp_path / "cache", now=lambda: NOW)
    paper = _paper()
    cache.save(_result(paper, "classified", token="Y"))
    path = tmp_path / "cache" / "_article_search" / f"{paper.source_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["returned_source_id"] = "ART000000002"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load(paper) is None


def test_negative_oa_cache_expires_before_positive_oa_cache(tmp_path: Path) -> None:
    clock = [NOW]
    cache = OaCache(
        tmp_path / "cache",
        open_access_ttl_seconds=30 * 24 * 60 * 60,
        not_open_access_ttl_seconds=7 * 24 * 60 * 60,
        now=lambda: clock[0],
    )
    y_paper = _paper(1)
    n_paper = _paper(2)
    cache.save(_result(y_paper, "classified", token="Y"))
    cache.save(_result(n_paper, "classified", token="N"))

    clock[0] = NOW + timedelta(days=8)

    assert cache.load(y_paper) is not None
    assert cache.load(n_paper) is None


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_http_transport_rejects_non_finite_or_non_positive_timeouts(
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="timeouts"):
        KciArticleSearchHttpClient(connect_timeout=timeout)


def test_exhausted_retry_becomes_failed_without_leaking_exception_text() -> None:
    client = _FakeArticleSearchClient(
        [
            RetryableOaError("network_error"),
            RetryableOaError("network_error"),
        ]
    )

    run = collect_open_access(
        [_paper()],
        client,
        retries=1,
        sleeper=lambda _seconds: None,
        now=lambda: NOW,
    )

    result = run.results[0]
    assert result.status == "failed"
    assert result.error_code == "network_error"
    assert result.attempts == 2
    assert result.is_open_access is None


def test_fatal_error_aborts_run_instead_of_turning_auth_failure_into_unknown() -> None:
    client = _FakeArticleSearchClient([FatalOaError("authentication_failed")])

    with pytest.raises(FatalOaError, match="authentication_failed"):
        collect_open_access([_paper()], client, now=lambda: NOW)


def test_load_kci_papers_reads_every_row_in_database_id_order(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE kci_papers (
        id INTEGER PRIMARY KEY, source_id TEXT, source TEXT, title TEXT,
        title_kor TEXT, title_eng TEXT, doi TEXT, publication_year INTEGER,
        journal_id TEXT, institution_id TEXT)"""
    )
    connection.executemany(
        "INSERT INTO kci_papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (537, "ART000000537", "kci", "마지막", None, None, None, 2026, None, None),
            (1, "ART000000001", "kci", "첫 번째", None, None, None, 2022, "J1", "I1"),
            (103, "ART000000103", "kci", "중간", None, None, "10.1000/MID", 2024, None, None),
        ],
    )
    connection.commit()
    connection.close()

    papers = load_kci_papers(database)

    assert [paper.database_id for paper in papers] == [1, 103, 537]
    assert [paper.paper_index for paper in papers] == [1, 2, 3]
    assert [paper.source_id for paper in papers] == [
        "ART000000001",
        "ART000000103",
        "ART000000537",
    ]


def test_workbook_preserves_row_order_counts_and_formula_safe_strings(
    tmp_path: Path,
) -> None:
    y_paper = _paper(1, title='=HYPERLINK("https://evil.example")')
    n_paper = _paper(2, title="+cmd|' /C calc'!A0")
    invalid_paper = _paper(3, title="@SUM(1,1)")
    results = (
        _result(y_paper, "classified", token="Y"),
        _result(n_paper, "classified", token="N"),
        _result(invalid_paper, "invalid_value", observed="=UNKNOWN()"),
    )
    requested_output = tmp_path / "oa_list.xlsx"

    exported = export_oa_workbook(
        OaCollectionRun(results, network_requests=3, cache_hits=0),
        requested_output,
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert exported.paper_count == 3
    assert exported.open_access_count == 1
    assert exported.not_open_access_count == 1
    assert exported.unresolved_count == 1
    assert exported.path == tmp_path / "oa_list.partial.xlsx"
    assert exported.path.is_file()
    assert not requested_output.exists()

    workbook = load_workbook(exported.path, data_only=False)
    try:
        assert workbook.sheetnames == ["OA 목록", "요약", "검증"]
        sheet = workbook["OA 목록"]
        assert sheet.max_row == 4
        headers = [cell.value for cell in sheet[1]]
        by_name = {name: index + 1 for index, name in enumerate(headers)}
        assert [
            sheet.cell(row, by_name["source_id"]).value for row in range(2, 5)
        ] == [paper.source_id for paper in (y_paper, n_paper, invalid_paper)]
        assert [
            sheet.cell(row, by_name["is_open_access"]).value for row in range(2, 5)
        ] == [True, False, None]
        assert [
            sheet.cell(row, by_name["oa_classification"]).value for row in range(2, 5)
        ] == ["Open Access", "Not Open Access", None]
        assert sheet.cell(2, by_name["title"]).data_type == "s"
        assert sheet.cell(3, by_name["title"]).data_type == "s"
        assert sheet.cell(4, by_name["observed_orte_open_yn"]).data_type == "s"
        assert not any(
            cell.data_type == "f"
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
        )

        summary = {
            row[0].value: row[1].value
            for row in workbook["요약"].iter_rows(min_row=2)
        }
        assert summary["paper_count"] == 3
        assert summary["open_access_y"] == 1
        assert summary["not_open_access_n"] == 1
        assert summary["unresolved_total"] == 1
        assert summary["classification_complete"] is False

        validation = {
            row[0].value: (row[1].value, row[3].value)
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["report_kind"] == ("partial", "FAIL")
        assert validation["classification_complete"] == (False, "FAIL")
        assert len(validation["database_snapshot_sha256"][0]) == 64
    finally:
        workbook.close()


def test_complete_workbook_uses_canonical_path(tmp_path: Path) -> None:
    results = (
        _result(_paper(1), "classified", token="Y"),
        _result(_paper(2), "classified", token="N"),
    )
    output = tmp_path / "oa_list.xlsx"

    exported = export_oa_workbook(
        OaCollectionRun(results, network_requests=2, cache_hits=0),
        output,
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert exported.path == output
    assert output.is_file()
    workbook = load_workbook(output, read_only=True)
    try:
        validation = {
            row[0].value: (row[1].value, row[3].value)
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["report_kind"] == ("complete", "PASS")
        assert validation["classification_complete"] == (True, "PASS")
    finally:
        workbook.close()


def test_result_constructor_forbids_calling_unresolved_false() -> None:
    result = _result(_paper(), "field_missing")
    assert result.orte_open_yn is None
    assert result.is_open_access is None
    assert result.oa_classification is None

    with pytest.raises(ValueError, match="unresolved OA value"):
        PaperOaResult(
            paper=_paper(),
            status="field_missing",
            returned_source_id=SOURCE_ID,
            orte_open_yn="N",
            field_present=False,
            observed_value=None,
            collected_at=NOW.isoformat(),
            attempts=1,
            error_code="orte_open_yn_missing",
        )


def test_collection_rejects_duplicate_input_identity() -> None:
    papers = [_paper(1), _paper(2, source_id=SOURCE_ID)]
    client = _FakeArticleSearchClient([])

    with pytest.raises(OaCollectionError, match="duplicate_input_source_id"):
        collect_open_access(papers, client)


class _UnavailableOaiClient:
    def __init__(self) -> None:
        self.request_count = 0

    def fetch_dc_page(self, **_kwargs: Any) -> OaiListPage:
        self.request_count += 1
        raise RetryableOaError("network_error")

    def fetch_kci_record(self, **_kwargs: Any) -> ParsedOaValue:
        raise AssertionError("no record can be verified after an incomplete harvest")


class _CapturingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def emit(self, _stage: str, message: str, **_kwargs: Any) -> None:
        self.messages.append(message)


def test_incomplete_harvest_failures_are_kept_but_not_logged_per_paper() -> None:
    papers = [_paper(index) for index in range(1, 6)]
    client = _UnavailableOaiClient()
    logger = _CapturingLogger()

    run = collect_open_access_oai(
        papers,
        client,
        retries=0,
        logger=logger,  # type: ignore[arg-type]
        log_every=1,
        now=lambda: NOW,
        from_date="2026-08-21",
        until_date="2026-08-21",
    )

    assert client.request_count == 1
    assert [result.status for result in run.results] == ["failed"] * len(papers)
    assert [result.error_code for result in run.results] == [
        "oai_harvest_incomplete"
    ] * len(papers)
    assert logger.messages == []


def _harvest_payload(
    source_ids: set[str],
    *,
    from_date: str,
    until_date: str,
    state: OaiHarvestState,
) -> dict[str, Any]:
    snapshot = hashlib.sha256(
        json.dumps(sorted(source_ids), separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "strategy": OA_COLLECTION_STRATEGY,
        "metadata_prefix": KCI_OAI_PRIMARY_PREFIX,
        "set": KCI_OAI_SET,
        "target_source_ids_sha256": snapshot,
        "from": from_date,
        "until": until_date,
        "next_token": state.next_token,
        "exhausted": state.exhausted,
        "pages": state.pages,
        "records_scanned": state.records_scanned,
        "mappings": {
            source_id: {
                "oai_identifier": record.oai_identifier,
                "rights": record.rights,
                "field_present": record.field_present,
                "observed_value": record.observed_value,
            }
            for source_id, record in state.mappings.items()
        },
        "updated_at": NOW.isoformat(),
    }


def test_harvest_cache_migrates_legacy_date_only_checkpoint_without_deleting_it(
    tmp_path: Path,
) -> None:
    source_ids = {SOURCE_ID}
    from_date = "2026-08-01"
    until_date = "2026-08-21"
    record = OaiDcRecord(
        source_id=SOURCE_ID,
        oai_identifier="oai:kci.go.kr:ARTI/1",
        rights="Y",
        field_present=True,
        observed_value=None,
    )
    expected = OaiHarvestState(
        mappings={SOURCE_ID: record},
        next_token="legacy-token",
        exhausted=False,
        pages=37,
        records_scanned=3700,
    )
    cache = OaiHarvestCache(tmp_path / "cache", now=lambda: NOW)
    legacy_path = cache._legacy_path(from_date, until_date)
    legacy_path.parent.mkdir(parents=True)
    legacy_payload = _harvest_payload(
        source_ids,
        from_date=from_date,
        until_date=until_date,
        state=expected,
    )
    legacy_path.write_text(
        json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    legacy_before = legacy_path.read_bytes()

    with cache.window_lock(
        source_ids, from_date=from_date, until_date=until_date
    ):
        loaded = cache.load(
            source_ids, from_date=from_date, until_date=until_date
        )

    new_path = cache._path(source_ids, from_date, until_date)
    assert loaded == expected
    assert new_path.is_file()
    assert json.loads(new_path.read_text(encoding="utf-8")) == legacy_payload
    assert legacy_path.read_bytes() == legacy_before
    assert cache._path({"ART000000002"}, from_date, until_date) != new_path


@pytest.mark.skipif(not paper_oa_module.sys.platform.startswith("linux"), reason="Linux flock")
def test_harvest_window_flock_serializes_same_identity_but_not_other_windows(
    tmp_path: Path,
) -> None:
    context = mp.get_context("spawn")
    directory = str(tmp_path / "cache")
    source_ids = (SOURCE_ID,)
    holder_entered = context.Event()
    holder_release = context.Event()
    holder = context.Process(
        target=_hold_harvest_lock,
        args=(
            directory,
            source_ids,
            "2026-08-01",
            "2026-08-31",
            holder_entered,
            holder_release,
        ),
    )
    processes = [holder]
    try:
        holder.start()
        assert holder_entered.wait(10)

        same_attempting, same_acquired = context.Event(), context.Event()
        same = context.Process(
            target=_probe_harvest_lock,
            args=(
                directory,
                source_ids,
                "2026-08-01",
                "2026-08-31",
                same_attempting,
                same_acquired,
            ),
        )
        processes.append(same)
        same.start()
        assert same_attempting.wait(10)

        other_window_attempting, other_window_acquired = (
            context.Event(),
            context.Event(),
        )
        other_window = context.Process(
            target=_probe_harvest_lock,
            args=(
                directory,
                source_ids,
                "2026-07-01",
                "2026-07-31",
                other_window_attempting,
                other_window_acquired,
            ),
        )
        processes.append(other_window)
        other_window.start()
        assert other_window_attempting.wait(10)
        assert other_window_acquired.wait(10)

        other_target_attempting, other_target_acquired = (
            context.Event(),
            context.Event(),
        )
        other_target = context.Process(
            target=_probe_harvest_lock,
            args=(
                directory,
                ("ART000000002",),
                "2026-08-01",
                "2026-08-31",
                other_target_attempting,
                other_target_acquired,
            ),
        )
        processes.append(other_target)
        other_target.start()
        assert other_target_attempting.wait(10)
        assert other_target_acquired.wait(10)

        assert not same_acquired.wait(0.25)
        holder_release.set()
        assert same_acquired.wait(10)

        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        holder_release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)


@pytest.mark.skipif(not paper_oa_module.sys.platform.startswith("linux"), reason="Linux flock")
def test_oai_base_and_evidence_sidecar_save_waits_for_per_paper_lock(
    tmp_path: Path,
) -> None:
    context = mp.get_context("spawn")
    directory = str(tmp_path / "cache")
    holder_entered, holder_release = context.Event(), context.Event()
    holder = context.Process(
        target=_hold_paper_lock,
        args=(directory, SOURCE_ID, holder_entered, holder_release),
    )
    saver_attempting, saver_finished = context.Event(), context.Event()
    saver = context.Process(
        target=_save_oai_result_in_process,
        args=(directory, saver_attempting, saver_finished),
    )
    processes = [holder, saver]
    try:
        holder.start()
        assert holder_entered.wait(10)
        saver.start()
        assert saver_attempting.wait(10)
        assert not saver_finished.wait(0.25)
        holder_release.set()
        assert saver_finished.wait(10)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        holder_release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)

    cache = OaCache(
        Path(directory), strategy=OA_COLLECTION_STRATEGY, now=lambda: NOW
    )
    loaded = cache.load(_paper())
    assert loaded is not None
    assert loaded.orte_open_yn == "Y"
    base = json.loads(cache._path(SOURCE_ID).read_text(encoding="utf-8"))
    sidecar = json.loads(
        cache._evidence_path(SOURCE_ID).read_text(encoding="utf-8")
    )
    canonical = json.dumps(
        base,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert sidecar["base_payload_sha256"] == hashlib.sha256(canonical).hexdigest()


@pytest.mark.skipif(not paper_oa_module.sys.platform.startswith("linux"), reason="Linux flock")
def test_oai_base_and_sidecar_invalidation_waits_for_per_paper_lock(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "cache"
    cache = OaCache(directory, strategy=OA_COLLECTION_STRATEGY, now=lambda: NOW)
    cache.save(_oai_result(_paper()))
    assert cache._path(SOURCE_ID).is_file()
    assert cache._evidence_path(SOURCE_ID).is_file()

    context = mp.get_context("spawn")
    holder_entered, holder_release = context.Event(), context.Event()
    holder = context.Process(
        target=_hold_paper_lock,
        args=(str(directory), SOURCE_ID, holder_entered, holder_release),
    )
    invalidator_attempting, invalidator_finished = context.Event(), context.Event()
    invalidator = context.Process(
        target=_invalidate_oai_result_in_process,
        args=(str(directory), invalidator_attempting, invalidator_finished),
    )
    processes = [holder, invalidator]
    try:
        holder.start()
        assert holder_entered.wait(10)
        invalidator.start()
        assert invalidator_attempting.wait(10)
        assert not invalidator_finished.wait(0.25)
        holder_release.set()
        assert invalidator_finished.wait(10)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        holder_release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)

    assert not cache._path(SOURCE_ID).exists()
    assert not cache._evidence_path(SOURCE_ID).exists()


def test_cache_lock_rejects_symlink_leaf(tmp_path: Path) -> None:
    cache = OaiHarvestCache(tmp_path / "cache", now=lambda: NOW)
    source_ids = {SOURCE_ID}
    lock_path = cache._lock_path(source_ids, "2026-08-01", "2026-08-31")
    lock_path.parent.mkdir(parents=True)
    target = tmp_path / "unrelated.lock"
    target.write_text("do not lock", encoding="utf-8")
    lock_path.symlink_to(target)

    with pytest.raises(
        OaCollectionError, match="unsafe_or_unavailable_cache_lock"
    ):
        with cache.window_lock(
            source_ids, from_date="2026-08-01", until_date="2026-08-31"
        ):
            raise AssertionError("symlink lock must not be entered")


def test_cache_lock_has_explicit_fail_safe_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper_oa_module, "_fcntl", None)
    cache = OaiHarvestCache(tmp_path / "cache", now=lambda: NOW)

    with pytest.raises(
        OaCollectionError, match="interprocess_cache_lock_unsupported"
    ):
        with cache.window_lock(
            {SOURCE_ID}, from_date="2026-08-01", until_date="2026-08-31"
        ):
            raise AssertionError("unsupported lock must not be entered")


class _NoRecordsOaiClient:
    def __init__(self) -> None:
        self.request_count = 0

    def fetch_dc_page(self, **_kwargs: Any) -> OaiListPage:
        self.request_count += 1
        return OaiListPage((), None, 0, no_records_match=True)

    def fetch_kci_record(self, **_kwargs: Any) -> ParsedOaValue:
        raise AssertionError("noRecordsMatch cannot be verified")


def test_force_resets_only_current_target_scoped_window_and_preserves_legacy(
    tmp_path: Path,
) -> None:
    source_ids = {SOURCE_ID}
    cache = OaiHarvestCache(tmp_path / "cache", now=lambda: NOW)
    old_state = OaiHarvestState({}, "old-token", False, 9, 900)
    january = ("2026-01-01", "2026-01-31")
    february = ("2026-02-01", "2026-02-28")
    for start, end in (january, february):
        with cache.window_lock(source_ids, from_date=start, until_date=end):
            cache.save(
                old_state,
                source_ids,
                from_date=start,
                until_date=end,
                updated_at=NOW,
            )
    january_path = cache._path(source_ids, *january)
    january_before = january_path.read_bytes()
    legacy_path = cache._legacy_path(*february)
    legacy_path.write_text(
        json.dumps(
            _harvest_payload(
                source_ids,
                from_date=february[0],
                until_date=february[1],
                state=OaiHarvestState({}, None, True, 99, 9900),
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    legacy_before = legacy_path.read_bytes()
    client = _NoRecordsOaiClient()

    run = collect_open_access_oai(
        [_paper()],
        client,
        harvest_cache=cache,
        force=True,
        retries=0,
        now=lambda: NOW,
        from_date=february[0],
        until_date=february[1],
    )

    assert run.results[0].error_code == "oai_harvest_incomplete"
    assert client.request_count == 1
    assert january_path.read_bytes() == january_before
    assert legacy_path.read_bytes() == legacy_before
    with cache.window_lock(
        source_ids, from_date=february[0], until_date=february[1]
    ):
        refreshed = cache.load(
            source_ids, from_date=february[0], until_date=february[1]
        )
    assert refreshed.exhausted is True
    assert refreshed.pages == 1


def test_cache_identity_survives_the_removal_of_earlier_papers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``paper_index`` is a row position: removing a paper shifts it, the evidence stays valid."""

    from dataclasses import asdict, replace

    written_as = _paper(40)  # the paper when its evidence was collected
    positional = hashlib.sha256(
        json.dumps(asdict(written_as), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    today = replace(written_as, paper_index=38)  # two earlier papers were removed since

    assert today.fingerprint == written_as.fingerprint
    assert replace(today, database_id=9_999).fingerprint == written_as.fingerprint  # a full re-match re-inserts rows
    monkeypatch.setattr(paper_oa_module, "_removed_paper_count", lambda: 2)
    assert today.matches_fingerprint(today.fingerprint)
    assert today.matches_fingerprint(positional)
    # The shift is bounded by the exclusion list, and a changed identity never matches.
    monkeypatch.setattr(paper_oa_module, "_removed_paper_count", lambda: 1)
    assert not today.matches_fingerprint(positional)
    monkeypatch.setattr(paper_oa_module, "_removed_paper_count", lambda: 2)
    assert not replace(today, doi="10.1234/OTHER").matches_fingerprint(positional)
    assert not today.matches_fingerprint(None)
