from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from Analyzer.Ref.ref_collect import (
    KciReferenceClient,
    parse_reference_info_xml,
    write_reference_manifest,
    CollectionRun,
    FatalReferenceError,
    InvalidReferenceResponse,
    KciPaper,
    PaperReferenceResult,
    ParsedArticleDetail,
    ReferenceCache,
    ReferenceCollectionError,
    ReferenceRecord,
    RetryableReferenceError,
    _RedactedQueryParams,
    _parse_retry_after,
    _partial_output_path,
    collect_references,
    export_reference_workbook,
    load_kci_papers,
    parse_article_detail_xml,
)


SEOUL = ZoneInfo("Asia/Seoul")
SOURCE_ID = "ART000000001"
SECRET = "test-secret-key"


def _xml(reference_info: str = "", *, source_id: str = SOURCE_ID, total: int = 1) -> bytes:
    record = ""
    if total:
        record = f"""
        <record>
          <journalInfo journal-id="JOURNAL-1"><pub-year>2026</pub-year></journalInfo>
          <articleInfo article-id="{source_id}">
            <title-group><article-title lang="original">시험 논문</article-title></title-group>
          </articleInfo>
          {reference_info}
        </record>
        """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <MetaData>
      <inputData><apiCode>articleDetail</apiCode><key>{SECRET}</key></inputData>
      <outputData>
        <result><total>{total}</total><resultMsg>정상 처리되었습니다.</resultMsg></result>
        {record}
      </outputData>
    </MetaData>
    """.encode()


def _paper(index: int = 1, source_id: str = SOURCE_ID, title: str = "시험 논문") -> KciPaper:
    return KciPaper(
        paper_index=index,
        database_id=index,
        source_id=source_id,
        title=title,
        title_kor=title,
        title_eng="Test paper",
        doi_raw="https://doi.org/10.1234/PAPER",
        doi="10.1234/paper",
        publication_year=2026,
        journal_id="JOURNAL-1",
        institution_id="INST-1",
    )


def test_parse_article_detail_preserves_types_typos_order_and_unknown_fields() -> None:
    reference_info = """
    <referenceInfo>
      <reference refebibl-id="REF0001" arti-id="ART000000111"
                 type-code="01" type-name="학술지(정기간행물)">
        <title>IaaS 디지털 포렌식</title><author>홍길동 ; 김철수</author>
        <journal-name>정보보호학회논문지</journal-name><pubi-year>2025</pubi-year>
        <volume>35</volume><isseue>2</isseue><serno>03</serno><page>1-20</page>
        <pubilisher>한국정보보호학회</pubilisher>
        <doi>https://doi.org/10.1000/ABC</doi><future-field>향후 필드</future-field>
      </reference>
      <reference refebibl-id="REF0002" type-code="05" type-name="학위논문">
        <title>학위논문 제목</title><author>이영희</author><pubi-year>2024</pubi-year>
        <degree>박사</degree><university>고려대학교</university><department>정보보호학과</department>
      </reference>
    </referenceInfo>
    """

    parsed = parse_article_detail_xml(
        _xml(reference_info), expected_source_id=SOURCE_ID
    )

    assert parsed.status == "collected"
    assert parsed.reference_info_present is True
    assert [reference.reference_order for reference in parsed.references] == [1, 2]
    first, second = parsed.references
    assert first.reference_id == "REF0001"
    assert first.referenced_kci_source_id == "ART000000111"
    assert first.reference_publication_year == "2025"
    assert first.reference_issue == "2"
    assert first.reference_publisher == "한국정보보호학회"
    assert first.reference_doi == "10.1000/ABC"
    assert first.reference_doi_status == "normalized"
    assert json.loads(first.additional_fields_json or "{}") == {
        "future-field": "향후 필드"
    }
    assert second.reference_degree == "박사"
    assert second.reference_university == "고려대학교"
    assert second.reference_department == "정보보호학과"
    assert SECRET not in repr(parsed)


def test_parse_all_non_journal_reference_type_fields() -> None:
    reference_info = """
    <referenceInfo>
      <reference refebibl-id="R2" type-code="02" type-name="학술대회논문">
        <title>Conference paper</title><conference-name>DFRWS</conference-name>
        <pubi-year>2025</pubi-year><pubilisher>A</pubilisher><publisher>B</publisher>
      </reference>
      <reference refebibl-id="R3" type-code="03" type-name="단행본">
        <title>Book</title><pubilisher>Publisher</pubilisher><page>10-20</page>
      </reference>
      <reference refebibl-id="R4" type-code="04" type-name="보고서">
        <title>Report</title><author>Institute</author><pubi-year>2024</pubi-year>
      </reference>
      <reference refebibl-id="R6" type-code="06" type-name="인터넷자원">
        <title>Web page</title><url>https://example.org/page</url>
        <citation-day>2026-08-01</citation-day><site-name>Example</site-name>
        <registration-day>2025-01-01</registration-day>
      </reference>
      <reference refebibl-id="R7" type-code="07" type-name="기타자료">
        <title>Other</title><author>Author</author>
      </reference>
    </referenceInfo>
    """
    parsed = parse_article_detail_xml(
        _xml(reference_info), expected_source_id=SOURCE_ID
    )
    by_code = {reference.reference_type_code: reference for reference in parsed.references}
    assert set(by_code) == {"02", "03", "04", "06", "07"}
    assert by_code["02"].reference_conference_name == "DFRWS"
    aliases = json.loads(by_code["02"].additional_fields_json or "{}")
    assert aliases["publisher_aliases"] == [
        {"tag": "pubilisher", "value": "A"},
        {"tag": "publisher", "value": "B"},
    ]
    assert by_code["03"].reference_publisher == "Publisher"
    assert by_code["04"].reference_author == "Institute"
    assert by_code["06"].reference_url == "https://example.org/page"
    assert by_code["06"].reference_citation_date == "2026-08-01"
    assert by_code["06"].reference_site_name == "Example"
    assert by_code["06"].reference_registration_date == "2025-01-01"
    assert by_code["07"].reference_title == "Other"


@pytest.mark.parametrize(
    ("reference_info", "present"),
    [("", False), ("<referenceInfo />", True)],
)
def test_missing_or_empty_reference_info_is_confirmed_zero_data(
    reference_info: str,
    present: bool,
) -> None:
    parsed = parse_article_detail_xml(
        _xml(reference_info), expected_source_id=SOURCE_ID
    )
    assert parsed.status == "no_reference_data"
    assert parsed.reference_info_present is present
    assert parsed.references == ()


def test_no_record_is_distinct_from_zero_reference_data() -> None:
    parsed = parse_article_detail_xml(_xml(total=0), expected_source_id=SOURCE_ID)
    assert parsed.status == "no_record"
    assert parsed.reference_info_present is None

    live_shape = b"""<MetaData><outputData><result>
    <resultMsg>No Data</resultMsg></result></outputData></MetaData>"""
    parsed_without_total = parse_article_detail_xml(
        live_shape, expected_source_id=SOURCE_ID
    )
    assert parsed_without_total.status == "no_record"


def test_parser_rejects_identity_mismatch_and_unsafe_xml() -> None:
    with pytest.raises(InvalidReferenceResponse, match="article_identity_mismatch"):
        parse_article_detail_xml(
            _xml(source_id="ART000000002"), expected_source_id=SOURCE_ID
        )


def test_api_authentication_message_is_fatal_without_echoing_message() -> None:
    body = b"""<MetaData><outputData><result><total>0</total>
    <resultMsg>\xeb\x93\xb1\xeb\xa1\x9d\xeb\x90\x98\xec\xa7\x80 \xec\x95\x8a\xec\x9d\x80 key</resultMsg>
    <message>No Data</message>
    </result></outputData></MetaData>"""
    with pytest.raises(FatalReferenceError, match="api_authentication_error") as raised:
        parse_article_detail_xml(body, expected_source_id=SOURCE_ID)
    assert SECRET not in str(raised.value)


def test_unknown_or_search_style_api_error_is_run_wide_fatal() -> None:
    body = """<MetaData><outputData><result>
    <resultMsg>검색 조건이 없습니다</resultMsg>
    </result></outputData></MetaData>""".encode()
    with pytest.raises(FatalReferenceError, match="api_reported_error"):
        parse_article_detail_xml(body, expected_source_id=SOURCE_ID)


def test_retry_after_supports_seconds_and_http_date() -> None:
    assert _parse_retry_after("12") == 12
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert _parse_retry_after("not-a-date") is None
    with pytest.raises(InvalidReferenceResponse, match="unsafe_xml"):
        parse_article_detail_xml(
            b'<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>',
            expected_source_id=SOURCE_ID,
        )


def test_duplicate_references_are_marked_but_not_removed() -> None:
    reference_info = """
    <referenceInfo>
      <reference refebibl-id="REF1" type-code="01"><title>A</title><doi>https://doi.org/10.1000/x</doi></reference>
      <reference refebibl-id="REF2" type-code="01"><title>A copy</title><doi>10.1000/X</doi></reference>
    </referenceInfo>
    """
    parsed = parse_article_detail_xml(
        _xml(reference_info), expected_source_id=SOURCE_ID
    )
    assert len(parsed.references) == 2
    assert parsed.references[0].is_duplicate_within_paper is False
    assert parsed.references[0].duplicate_key_type is None
    assert parsed.references[1].is_duplicate_within_paper is True
    assert parsed.references[1].duplicate_key_type == "doi"
    assert parsed.references[1].duplicate_of_order == 1


def test_query_parameter_repr_redacts_key() -> None:
    params = _RedactedQueryParams(api_key=SECRET, source_id=SOURCE_ID)
    assert params["key"] == SECRET
    assert SECRET not in repr(params)
    assert "<redacted>" in repr(params)


def test_load_kci_papers_uses_all_rows_in_database_id_order(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE kci_papers (
        id INTEGER PRIMARY KEY, source_id TEXT, title TEXT, title_kor TEXT,
        title_eng TEXT, doi TEXT, publication_year INTEGER, journal_id TEXT,
        institution_id TEXT)"""
    )
    connection.executemany(
        "INSERT INTO kci_papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (9, "ART000000009", "제목 9", None, None, None, 2025, None, None),
            (3, "ART000000003", "제목 3", None, None, "10.1000/ABC", 2024, "J", "I"),
        ],
    )
    connection.commit()
    connection.close()

    papers = load_kci_papers(database)

    assert [paper.database_id for paper in papers] == [3, 9]
    assert [paper.paper_index for paper in papers] == [1, 2]
    assert papers[0].doi == "10.1000/ABC"


class _FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.request_count = 0

    def fetch(self, _source_id: str) -> ParsedArticleDetail:
        self.request_count += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, ParsedArticleDetail)
        return outcome


ONE_REFERENCE_XML = (
    "<referenceInfo>\r\n        <reference refebibl-id=\"REF1\"><title>참고문헌</title>"
    "</reference>\r\n      </referenceInfo>"
)


def test_collection_retries_and_resume_cache_contains_no_xml_secret(tmp_path: Path) -> None:
    reference = ReferenceRecord(reference_order=1, reference_id="REF1", reference_title="참고문헌")
    client = _FakeClient(
        [
            RetryableReferenceError("http_429", retry_after=0),
            ParsedArticleDetail("collected", True, (reference,), ONE_REFERENCE_XML),
        ]
    )
    now = lambda: datetime(2026, 8, 21, 12, 0, tzinfo=SEOUL)
    cache = ReferenceCache(tmp_path / "cache", now=now)
    sleeps: list[float] = []

    first = collect_references(
        [_paper()], client, cache=cache, sleeper=sleeps.append, now=now
    )
    assert client.request_count == 2
    assert sleeps == [1.0]
    assert first.results[0].attempts == 2
    cache_text = (tmp_path / "cache" / f"{SOURCE_ID}.json").read_text()
    assert SECRET not in cache_text
    assert "<MetaData" not in cache_text
    # The verbatim element lands beside the cache, byte for byte (CRLF kept).
    xml_path = tmp_path / "api-response" / "referenceInfo" / f"{SOURCE_ID}.xml"
    assert xml_path.read_bytes() == ONE_REFERENCE_XML.encode("utf-8")

    second_client = _FakeClient([])
    second = collect_references([_paper()], second_client, cache=cache, now=now)
    assert second_client.request_count == 0
    assert second.cache_hits == 1
    assert second.results[0].cache_hit is True
    assert second.results[0].reference_info_xml == ONE_REFERENCE_XML

    # A missing or altered file is not a cache hit: the paper is fetched again.
    xml_path.write_bytes(b"<referenceInfo />")
    third_client = _FakeClient([ParsedArticleDetail("collected", True, (reference,), ONE_REFERENCE_XML)])
    third = collect_references([_paper()], third_client, cache=cache, now=now)
    assert third_client.request_count == 1 and third.cache_hits == 0
    xml_path.unlink()
    fourth_client = _FakeClient([ParsedArticleDetail("collected", True, (reference,), ONE_REFERENCE_XML)])
    assert collect_references([_paper()], fourth_client, cache=cache, now=now).cache_hits == 0
    assert xml_path.read_bytes() == ONE_REFERENCE_XML.encode("utf-8")


def test_force_failure_invalidates_old_success_cache(tmp_path: Path) -> None:
    now = lambda: datetime(2026, 8, 21, 12, 0, tzinfo=SEOUL)
    cache = ReferenceCache(tmp_path / "cache", now=now)
    success_client = _FakeClient(
        [ParsedArticleDetail("collected", True, (ReferenceRecord(1),), ONE_REFERENCE_XML)]
    )
    collect_references([_paper()], success_client, cache=cache, now=now)
    cache_path = tmp_path / "cache" / f"{SOURCE_ID}.json"
    xml_path = tmp_path / "api-response" / "referenceInfo" / f"{SOURCE_ID}.xml"
    assert cache_path.is_file() and xml_path.is_file()

    failed_client = _FakeClient([RetryableReferenceError("network_error")])
    forced = collect_references(
        [_paper()],
        failed_client,
        cache=cache,
        force=True,
        retries=0,
        now=now,
    )
    assert forced.results[0].status == "failed"
    assert not cache_path.exists()
    assert not xml_path.exists()

    fresh_client = _FakeClient(
        [ParsedArticleDetail("no_reference_data", False, ())]
    )
    resumed = collect_references([_paper()], fresh_client, cache=cache, now=now)
    assert fresh_client.request_count == 1
    assert resumed.cache_hits == 0


def test_zero_reference_cache_expires_after_seven_days(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 21, 12, 0, tzinfo=SEOUL)]
    cache = ReferenceCache(tmp_path / "cache", now=lambda: clock[0])
    result = PaperReferenceResult(
        paper=_paper(),
        status="no_reference_data",
        reference_info_present=False,
        references=(),
        collected_at=clock[0].isoformat(),
        attempts=1,
    )
    cache.save(result)
    assert cache.load(_paper()) is not None
    clock[0] = datetime(2026, 8, 29, 12, 0, tzinfo=SEOUL)
    assert cache.load(_paper()) is None


def test_workbook_has_complete_status_and_formula_safe_strings(tmp_path: Path) -> None:
    dangerous = _paper(title='=HYPERLINK("https://evil.example")')
    reference = ReferenceRecord(
        reference_order=1,
        reference_id="REF1",
        reference_title="+cmd|' /C calc'!A0",
        reference_doi_raw="not a doi",
        reference_doi_status="invalid",
    )
    first = PaperReferenceResult(
        paper=dangerous,
        status="collected",
        reference_info_present=True,
        references=(reference,),
        collected_at="2026-08-21T12:00:00+09:00",
        attempts=1,
        reference_info_xml=ONE_REFERENCE_XML,
    )
    second_paper = _paper(2, "ART000000002", "참고문헌 데이터 없음")
    second = PaperReferenceResult(
        paper=second_paper,
        status="no_reference_data",
        reference_info_present=False,
        references=(),
        collected_at="2026-08-21T12:00:01+09:00",
        attempts=1,
    )
    output = tmp_path / "ref_list.xlsx"

    exported = export_reference_workbook(
        CollectionRun((first, second), network_requests=2, cache_hits=0),
        output,
        database=tmp_path / "source.db",
        generated_at=datetime(2026, 8, 21, 12, 1, tzinfo=SEOUL),
    )

    assert exported.paper_count == 2
    assert exported.reference_count == 1
    workbook = load_workbook(output, data_only=False)
    try:
        assert workbook.sheetnames == ["논문별 요약", "참고문헌 상세", "검증"]
        summary = workbook["논문별 요약"]
        details = workbook["참고문헌 상세"]
        assert summary.max_row == 3
        assert details.max_row == 2
        title_column = [cell.value for cell in summary[1]].index("title") + 1
        reference_title_column = [cell.value for cell in details[1]].index("reference_title") + 1
        assert summary.cell(2, title_column).data_type == "s"
        assert details.cell(2, reference_title_column).data_type == "s"
        assert not any(
            cell.data_type == "f"
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
        )
        validation = {
            row[0].value: row[1].value
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["report_kind"] == "complete"
        assert validation["collection_complete"] is True
        assert len(validation["database_snapshot_sha256"]) == 64
    finally:
        workbook.close()


def test_partial_output_path_never_equals_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "ref_list.xlsx"
    partial = _partial_output_path(canonical)
    assert partial == tmp_path / "ref_list.partial.xlsx"
    assert partial != canonical


def test_exporter_itself_refuses_to_publish_unresolved_run_as_canonical(
    tmp_path: Path,
) -> None:
    failed = PaperReferenceResult(
        paper=_paper(),
        status="failed",
        reference_info_present=None,
        references=(),
        collected_at="2026-08-21T12:00:00+09:00",
        attempts=3,
        error_code="network_error",
    )
    canonical = tmp_path / "ref_list.xlsx"
    exported = export_reference_workbook(
        CollectionRun((failed,), network_requests=3, cache_hits=0),
        canonical,
        database=tmp_path / "source.db",
    )
    assert exported.failed_count == 1
    assert exported.path == tmp_path / "ref_list.partial.xlsx"
    assert not canonical.exists()
    workbook = load_workbook(exported.path, read_only=True)
    try:
        validation = {
            row[0].value: (row[1].value, row[3].value)
            for row in workbook["검증"].iter_rows(min_row=2)
        }
        assert validation["report_kind"] == ("partial", "FAIL")
        assert validation["collection_complete"] == (False, "FAIL")
    finally:
        workbook.close()


CDATA_ENVELOPE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\r\n<MetaData>\r\n'
    b"  <inputData><apiCode>articleDetail</apiCode><key>" + SECRET.encode() + b"</key></inputData>\r\n"
    b"  <outputData>\r\n    <result><total>1</total><resultMsg>\xec\xa0\x95\xec\x83\x81 \xec\xb2\x98\xeb\xa6\xac\xeb\x90\x98\xec\x97\x88\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4.</resultMsg></result>\r\n"
    b"    <record>\r\n      <journalInfo journal-id=\"JOURNAL-1\"><pub-year>2026</pub-year></journalInfo>\r\n"
    b"      <articleInfo article-id=\"" + SOURCE_ID.encode() + b"\">\r\n"
    b"        <title-group><article-title lang=\"original\">CDATA</article-title></title-group>\r\n"
    b"      </articleInfo>\r\n"
    b"      <referenceInfo>\r\n"
    b"        <reference refebibl-id=\"REF1\" type-name=\"\xed\x95\x99\xec\x88\xa0\xec\xa7\x80\"><![CDATA[Agrawal, N.M. 2000. A <b>&amp;</b> study. Lancet 343: 769\xe2\x80\x93772.]]></reference>\r\n"
    b"        <reference refebibl-id=\"REF2\" arti-id=\"ART000000111\" type-code=\"01\">\r\n"
    b"          <title>Structured</title><author>Kim</author><doi><![CDATA[https://doi.org/10.1000/ABC]]></doi>\r\n"
    b"        </reference>\r\n"
    b"        <reference refebibl-id=\"REF3\"><![CDATA[Text mentioning </referenceInfo> and <referenceInfo> tags]]></reference>\r\n"
    b"      </referenceInfo>\r\n"
    b"    </record>\r\n  </outputData>\r\n</MetaData>\r\n"
)


def test_cdata_reference_keeps_citation_text_and_segment_is_verbatim() -> None:
    parsed = parse_article_detail_xml(CDATA_ENVELOPE, expected_source_id=SOURCE_ID)

    assert parsed.status == "collected"
    first, second, third = parsed.references
    # A CDATA-only reference is one citation string, entities and tags inside
    # the CDATA kept literally; a structured one still yields its fields.
    assert first.reference_citation_text == "Agrawal, N.M. 2000. A <b>&amp;</b> study. Lancet 343: 769\u2013772."
    assert first.reference_title is None and first.reference_type == "학술지"
    assert second.reference_citation_text is None
    assert second.reference_title == "Structured" and second.reference_doi == "10.1000/ABC"
    assert second.referenced_kci_source_id == "ART000000111"
    assert third.reference_citation_text == "Text mentioning </referenceInfo> and <referenceInfo> tags"
    # The element is sliced out of the response exactly, CRLF and indentation
    # included, and it never carries the echoed key.
    text = CDATA_ENVELOPE.decode("utf-8")
    start = text.index("<referenceInfo>")
    # The closing tag inside REF3's CDATA is text, so the real end is the last one.
    end = text.rindex("</referenceInfo>") + len("</referenceInfo>")
    assert parsed.reference_info_xml == text[start:end]
    assert parsed.reference_info_xml.startswith("<referenceInfo>\r\n        <reference")
    assert SECRET not in parsed.reference_info_xml
    # The saved file parses on its own to the same references.
    assert parse_reference_info_xml(parsed.reference_info_xml) == parsed.references
    assert parse_reference_info_xml(parsed.reference_info_xml.encode("utf-8")) == parsed.references


@pytest.mark.parametrize(
    "reference_info",
    [
        "<referenceInfo><reference><![CDATA[x]]></reference>",
        "<referenceInfo><referenceInfo /></referenceInfo>",
        "</referenceInfo>",
    ],
)
def test_malformed_reference_info_container_is_rejected(reference_info: str) -> None:
    with pytest.raises(InvalidReferenceResponse):
        parse_article_detail_xml(_xml(reference_info), expected_source_id=SOURCE_ID)


def test_empty_reference_info_element_is_saved_verbatim() -> None:
    parsed = parse_article_detail_xml(_xml("<referenceInfo />"), expected_source_id=SOURCE_ID)
    assert parsed.status == "no_reference_data"
    assert parsed.reference_info_present is True
    assert parsed.reference_info_xml == "<referenceInfo />"
    absent = parse_article_detail_xml(_xml(""), expected_source_id=SOURCE_ID)
    assert absent.reference_info_present is False and absent.reference_info_xml is None
    with pytest.raises(InvalidReferenceResponse):
        parse_reference_info_xml("<record><referenceInfo /></record>")


class _FakeHttp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get_bytes(self, url: str, **_: object):
        class _Fetched:
            status_code = 200
            headers: dict[str, str] = {}

        fetched = _Fetched()
        fetched.body = self.body  # type: ignore[attr-defined]
        return fetched


def test_client_refuses_a_segment_that_would_carry_the_key() -> None:
    leaking = CDATA_ENVELOPE.replace(b"Agrawal, N.M.", SECRET.encode())
    client = KciReferenceClient(SECRET, http_client=_FakeHttp(leaking))
    with pytest.raises(InvalidReferenceResponse, match="secret_in_reference_info"):
        client.fetch(SOURCE_ID)
    clean = KciReferenceClient(SECRET, http_client=_FakeHttp(CDATA_ENVELOPE))
    assert clean.fetch(SOURCE_ID).status == "collected"


def test_manifest_indexes_every_paper_and_detects_edited_files(tmp_path: Path) -> None:
    now = lambda: datetime(2026, 9, 28, 22, 0, tzinfo=SEOUL)
    cache = ReferenceCache(tmp_path / "cache", now=now)
    text_only = ReferenceRecord(reference_order=1, reference_id="R1", reference_citation_text="Cited. 2020.")
    papers = [_paper(), _paper(2, "ART000000002", "둘째"), _paper(3, "ART000000003", "셋째")]
    client = _FakeClient(
        [
            ParsedArticleDetail("collected", True, (text_only,), "<referenceInfo><reference refebibl-id=\"R1\"><![CDATA[Cited. 2020.]]></reference></referenceInfo>"),
            ParsedArticleDetail("no_reference_data", False, ()),
            RetryableReferenceError("network_error"),
        ]
    )
    run = collect_references(papers, client, cache=cache, retries=0, now=now)
    manifest_path = tmp_path / "api-response" / "manifest.json"

    written = write_reference_manifest(
        run, manifest_path, xml_directory=cache.xml_directory, database=tmp_path / "db.sqlite", generated_at=now()
    )

    assert (written.paper_count, written.file_count, written.text_only_reference_count, written.unresolved_count) == (3, 1, 1, 1)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0" and payload["xml_directory"] == "referenceInfo"
    assert [entry["status"] for entry in payload["papers"]] == ["collected", "no_reference_data", "failed"]
    first, second, third = payload["papers"]
    assert first["file"] == f"referenceInfo/{SOURCE_ID}.xml" and len(first["sha256"]) == 64
    assert first["reference_count"] == 1 and first["text_only_reference_count"] == 1
    assert second["file"] is None and second["reference_count"] == 0
    assert third["file"] is None and third["reference_count"] is None and third["error_code"] == "network_error"
    assert SECRET not in manifest_path.read_text(encoding="utf-8")

    (cache.xml_directory / f"{SOURCE_ID}.xml").write_bytes(b"<referenceInfo />")
    with pytest.raises(ReferenceCollectionError, match="reference_info_file_mismatch"):
        write_reference_manifest(run, manifest_path, xml_directory=cache.xml_directory, database=tmp_path / "db.sqlite")


def test_text_only_references_are_deduplicated_by_their_citation_text() -> None:
    reference_info = """
    <referenceInfo>
      <reference refebibl-id="R1"><![CDATA[Same citation. 2020.]]></reference>
      <reference refebibl-id="R2"><![CDATA[  Same   citation. 2020. ]]></reference>
      <reference refebibl-id="R3"><![CDATA[Another. 2021.]]></reference>
    </referenceInfo>
    """
    parsed = parse_article_detail_xml(_xml(reference_info), expected_source_id=SOURCE_ID)
    flags = [(r.is_duplicate_within_paper, r.duplicate_key_type, r.duplicate_of_order) for r in parsed.references]
    assert flags == [(False, None, None), (True, "citation_text", 1), (False, None, None)]
