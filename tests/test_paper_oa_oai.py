from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from Analyzer.OA.paper_oa import (
    CACHE_SCHEMA_VERSION,
    KCI_OAI_EARLIEST_DATE,
    KCI_OAI_PMH_URL,
    KCI_OAI_PRIMARY_PREFIX,
    KCI_OAI_SET,
    KCI_OAI_VERIFY_PREFIX,
    OA_COLLECTION_STRATEGY,
    FatalOaError,
    InvalidOaResponse,
    KciOaiPmhClient,
    KciPaper,
    OaCache,
    OaCollectionError,
    OaCollectionRun,
    OaiDcRecord,
    OaiHarvestCache,
    OaiHarvestState,
    OaiListPage,
    OaiRecordDiagnostic,
    PaperOaResult,
    ParsedOaValue,
    collect_open_access_oai,
    export_oa_workbook,
    parse_oai_dc_list_records_xml,
    parse_oai_kci_get_record_xml,
    _oai_date_windows,
)
from PaperPipeline.http_client import HttpFetch


OAI_NS = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
KCI_NS = "http://www.kci.go.kr/kciportal/OAI/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
OAI_SCHEMA_LOCATION = f"{OAI_NS} {OAI_NS}OAI-PMH.xsd"
KCI_HTTP_ECHO = "http://open.kci.go.kr/oai/request"
NO_RECORDS_MESSAGE = (
    "The combination of the values of the from, until, set and "
    "metadataPrefix arguments results in an empty set."
)
SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=SEOUL)
SOURCE_ID = "ART000000001"
OAI_IDENTIFIER = "oai:kci.go.kr:ARTI/914"


def _paper(
    index: int = 1,
    *,
    source_id: str | None = None,
    title: str | None = None,
) -> KciPaper:
    resolved_source_id = source_id or f"ART{index:09d}"
    return KciPaper(
        paper_index=index,
        database_id=index,
        source_id=resolved_source_id,
        title=title or f"논문 {index}",
        title_kor=title or f"논문 {index}",
        title_eng=f"Paper {index}",
        doi_raw=f"10.1234/{index}",
        doi=f"10.1234/{index}",
        publication_year=2026,
        journal_id="JOURNAL-1",
        institution_id="INSTITUTION-1",
    )


def _oai_envelope(payload: str, *, verb: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="{OAI_NS}" xmlns:oai_dc="{OAI_DC_NS}"
         xmlns:dc="{DC_NS}" xmlns:kci="{KCI_NS}">
  <responseDate>2026-08-22T03:00:00Z</responseDate>
  <request verb="{verb}">{KCI_OAI_PMH_URL}</request>
  {payload}
</OAI-PMH>
'''.encode()


def _dc_record(
    *,
    source_id: str = SOURCE_ID,
    oai_identifier: str = OAI_IDENTIFIER,
    rights: str | None = "Y",
) -> str:
    rights_xml = "" if rights is None else f"<dc:rights>{escape(rights)}</dc:rights>"
    return f'''
<record>
  <header>
    <identifier>{escape(oai_identifier)}</identifier>
    <datestamp>2026-08-22</datestamp>
    <setSpec>ARTI</setSpec>
  </header>
  <metadata>
    <oai_dc:dc>
      <dc:title lang="original">시험 논문</dc:title>
      <dc:identifier type="artiId">{escape(source_id)}</dc:identifier>
      {rights_xml}
    </oai_dc:dc>
  </metadata>
</record>
'''


def _dc_page(records: list[str], *, token: str | None = None) -> bytes:
    token_xml = (
        "" if token is None else f"<resumptionToken>{escape(token)}</resumptionToken>"
    )
    return _oai_envelope(
        f"<ListRecords>{''.join(records)}{token_xml}</ListRecords>",
        verb="ListRecords",
    )


def _kci_get_record(
    *,
    source_id: str = SOURCE_ID,
    oai_identifier: str = OAI_IDENTIFIER,
    oa_value: str | None = "Y",
) -> bytes:
    oa_xml = (
        ""
        if oa_value is None
        else f"<kci:orte-open-yn>{escape(oa_value)}</kci:orte-open-yn>"
    )
    return _oai_envelope(
        f'''
<GetRecord>
  <record>
    <header>
      <identifier>{escape(oai_identifier)}</identifier>
      <datestamp>2026-08-22</datestamp>
      <setSpec>ARTI</setSpec>
    </header>
    <metadata>
      <kci:oai_kci>
        <kci:articleInfo article-id="{escape(source_id)}">
          <kci:title-group><kci:article-title lang="original">시험 논문</kci:article-title></kci:title-group>
          {oa_xml}
        </kci:articleInfo>
      </kci:oai_kci>
    </metadata>
  </record>
</GetRecord>
''',
        verb="GetRecord",
    )


def _oai_error(code: str, message: str = "server detail") -> bytes:
    return _oai_envelope(
        f'<error code="{escape(code)}">{escape(message)}</error>',
        verb="ListRecords",
    )


def _broken_root_no_records_match(
    *,
    error_code: str = "noRecordsMatch",
    request_verb: str = "ListRecords",
    extra_xml: str = "",
    error_body: str = NO_RECORDS_MESSAGE,
    root_attributes: str = "",
    metadata_prefix: str = "oai_dc",
    set_spec: str = "ARTI",
    from_date: str = "2018-10-01",
    until_date: str = "2018-10-31",
    extra_request_attributes: str = "",
    request_url: str = KCI_OAI_PMH_URL,
) -> bytes:
    request_attributes = (
        f'from="{escape(from_date)}" '
        f'metadataPrefix="{escape(metadata_prefix)}" '
        f'set="{escape(set_spec)}" '
        f'until="{escape(until_date)}" '
        f'verb="{escape(request_verb)}"{extra_request_attributes}'
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns:oai="{OAI_NS}"{root_attributes}>
  <oai:responseDate>2026-08-22T03:00:00Z</oai:responseDate>
  <oai:request {request_attributes}>{escape(request_url)}</oai:request>
  <oai:error code="{escape(error_code)}">{error_body}</oai:error>
  {extra_xml}
</OAI-PMH>
'''.encode()


@pytest.mark.parametrize(
    ("raw", "rights", "present", "observed"),
    [
        ("Y", "Y", True, None),
        (" n ", "N", True, None),
        (None, None, False, None),
        (" \n ", None, True, None),
        ("UNKNOWN", None, True, "UNKNOWN"),
    ],
)
def test_parse_oai_dc_rights_with_real_namespaces(
    raw: str | None,
    rights: str | None,
    present: bool,
    observed: str | None,
) -> None:
    page = parse_oai_dc_list_records_xml(
        _dc_page([_dc_record(rights=raw)]),
        target_source_ids={SOURCE_ID},
    )

    assert page.record_count == 1
    assert page.next_token is None
    assert page.matches == (
        OaiDcRecord(
            source_id=SOURCE_ID,
            oai_identifier=OAI_IDENTIFIER,
            rights=rights,
            field_present=present,
            observed_value=observed,
        ),
    )


def test_malformed_unrelated_record_is_quarantined_without_losing_page() -> None:
    malformed_identifier = "oai:kci.go.kr:ARTI/777"
    malformed = f'''
<record>
  <header>
    <identifier>{malformed_identifier}</identifier>
    <datestamp>2026-08-22</datestamp>
    <setSpec>ARTI</setSpec>
  </header>
  <metadata />
</record>
'''

    page = parse_oai_dc_list_records_xml(
        _dc_page([malformed, _dc_record()]),
        target_source_ids={SOURCE_ID},
    )

    assert page.record_count == 2
    assert page.matches == (
        OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, "Y", True),
    )
    assert page.diagnostics == (
        OaiRecordDiagnostic(
            "invalid_oai_record_metadata",
            oai_identifier=malformed_identifier,
        ),
    )


def test_malformed_target_record_stays_unresolved_and_diagnostic_is_cached(
    tmp_path: Path,
) -> None:
    malformed_target = f'''
<record>
  <header>
    <identifier>{OAI_IDENTIFIER}</identifier>
    <datestamp>2026-08-22</datestamp>
    <setSpec>ARTI</setSpec>
  </header>
  <metadata>
    <oai_dc:dc>
      <dc:identifier type="artiId">{SOURCE_ID}</dc:identifier>
      <dc:rights>Y</dc:rights>
    </oai_dc:dc>
    <oai_dc:dc />
  </metadata>
</record>
'''
    page = parse_oai_dc_list_records_xml(
        _dc_page([malformed_target]),
        target_source_ids={SOURCE_ID},
    )
    diagnostic = OaiRecordDiagnostic(
        "invalid_oai_record_metadata",
        source_id=SOURCE_ID,
        oai_identifier=OAI_IDENTIFIER,
    )
    assert page.matches == ()
    assert page.diagnostics == (diagnostic,)

    class _MalformedTargetClient:
        request_count = 0

        def fetch_dc_page(self, **_kwargs: Any) -> OaiListPage:
            self.request_count += 1
            return page

        def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
            raise AssertionError(f"malformed primary must not be verified: {kwargs}")

    harvest_cache = OaiHarvestCache(tmp_path / "harvest", now=lambda: NOW)
    run = collect_open_access_oai(
        [_paper()],
        _MalformedTargetClient(),
        harvest_cache=harvest_cache,
        from_date="2026-08-22",
        until_date="2026-08-22",
        retries=0,
        now=lambda: NOW,
    )
    result = run.results[0]
    assert result.status == "failed"
    assert result.error_code == "invalid_oai_record_metadata"
    assert result.oai_identifier == OAI_IDENTIFIER
    assert result.orte_open_yn is None
    assert result.is_open_access is None

    with harvest_cache.window_lock(
        {SOURCE_ID}, from_date="2026-08-22", until_date="2026-08-22"
    ):
        restored = harvest_cache.load(
            {SOURCE_ID}, from_date="2026-08-22", until_date="2026-08-22"
        )
    assert restored == OaiHarvestState(
        mappings={},
        next_token=None,
        exhausted=True,
        pages=1,
        records_scanned=1,
        diagnostics=(diagnostic,),
    )


@pytest.mark.parametrize(
    ("raw", "status", "token", "present", "observed"),
    [
        ("Y", "classified", "Y", True, None),
        (" n ", "classified", "N", True, None),
        (None, "field_missing", None, False, None),
        (" \n ", "field_missing", None, True, None),
        ("UNKNOWN", "invalid_value", None, True, "UNKNOWN"),
    ],
)
def test_parse_oai_kci_orte_open_yn_with_real_namespaces(
    raw: str | None,
    status: str,
    token: str | None,
    present: bool,
    observed: str | None,
) -> None:
    parsed = parse_oai_kci_get_record_xml(
        _kci_get_record(oa_value=raw),
        expected_oai_identifier=OAI_IDENTIFIER,
        expected_source_id=SOURCE_ID,
    )

    assert parsed == ParsedOaValue(
        status=status,
        returned_source_id=SOURCE_ID,
        orte_open_yn=token,
        field_present=present,
        observed_value=observed,
    )


def test_oai_and_embedded_article_identities_are_checked_independently() -> None:
    with pytest.raises(InvalidOaResponse, match="oai_identifier_mismatch"):
        parse_oai_kci_get_record_xml(
            _kci_get_record(oai_identifier="oai:kci.go.kr:ARTI/915"),
            expected_oai_identifier=OAI_IDENTIFIER,
            expected_source_id=SOURCE_ID,
        )

    with pytest.raises(InvalidOaResponse, match="article_identity_mismatch"):
        parse_oai_kci_get_record_xml(
            _kci_get_record(source_id="ART000000002"),
            expected_oai_identifier=OAI_IDENTIFIER,
            expected_source_id=SOURCE_ID,
        )


class _CaptureHttpClient:
    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = list(bodies)
        self.params: list[dict[str, str]] = []

    def get_bytes(self, url: str, **kwargs: Any) -> HttpFetch:
        assert url == KCI_OAI_PMH_URL
        self.params.append(dict(kwargs["params"]))
        return HttpFetch(
            requested_url=KCI_OAI_PMH_URL,
            final_url=KCI_OAI_PMH_URL,
            status_code=200,
            headers={"Content-Type": "text/xml"},
            body=self.bodies.pop(0),
            redirects=(),
        )


def test_resumption_token_follow_up_sends_token_only_parameters() -> None:
    token = "2026-08-01:2026-08-31:100:1234"
    http = _CaptureHttpClient(
        [
            _dc_page([_dc_record()], token=token),
            _dc_page([]),
        ]
    )
    client = KciOaiPmhClient(http_client=http)

    first = client.fetch_dc_page(
        target_source_ids={SOURCE_ID},
        from_date="2026-08-01",
        until_date="2026-08-31",
    )
    client.fetch_dc_page(
        target_source_ids={SOURCE_ID},
        resumption_token=first.next_token,
        # These must not leak into a token continuation request.
        from_date="2020-01-01",
        until_date="2020-12-31",
    )

    assert http.params == [
        {
            "verb": "ListRecords",
            "set": KCI_OAI_SET,
            "metadataPrefix": KCI_OAI_PRIMARY_PREFIX,
            "from": "2026-08-01",
            "until": "2026-08-31",
        },
        {"verb": "ListRecords", "resumptionToken": token},
    ]


def test_no_records_match_and_id_does_not_exist_are_unresolved_not_n() -> None:
    page = parse_oai_dc_list_records_xml(
        _oai_error("noRecordsMatch", "no result"),
        target_source_ids={SOURCE_ID},
    )
    missing = parse_oai_kci_get_record_xml(
        _oai_error("idDoesNotExist", "unknown internal identifier"),
        expected_oai_identifier=OAI_IDENTIFIER,
        expected_source_id=SOURCE_ID,
    )

    assert page == OaiListPage((), None, 0, no_records_match=True)
    assert missing == ParsedOaValue("no_record", None, None, None)


@pytest.mark.parametrize(
    ("root_attributes", "request_url"),
    [
        ("", KCI_OAI_PMH_URL),
        (
            (
                f' xmlns:xsi="{XSI_NS}"'
                f' xsi:schemaLocation="{OAI_SCHEMA_LOCATION}"'
            ),
            KCI_HTTP_ECHO,
        ),
    ],
)
def test_kci_unqualified_root_no_records_match_is_a_narrow_empty_page(
    root_attributes: str,
    request_url: str,
) -> None:
    page = parse_oai_dc_list_records_xml(
        _broken_root_no_records_match(
            root_attributes=root_attributes,
            request_url=request_url,
        ),
        target_source_ids={SOURCE_ID},
    )

    assert page == OaiListPage((), None, 0, no_records_match=True)


@pytest.mark.parametrize(
    "body",
    [
        _broken_root_no_records_match(error_code="badArgument"),
        _broken_root_no_records_match(request_verb="GetRecord"),
        _broken_root_no_records_match(
            extra_xml=f"<oai:ListRecords xmlns:oai=\"{OAI_NS}\" />"
        ),
        _broken_root_no_records_match(
            error_body=f"<oai:record xmlns:oai=\"{OAI_NS}\" />"
        ),
        _broken_root_no_records_match(root_attributes=' version="2.0"'),
        _broken_root_no_records_match(
            root_attributes=(
                f' xmlns:xsi="{XSI_NS}"'
                f' xsi:schemaLocation="{OAI_NS} wrong.xsd"'
            )
        ),
        _broken_root_no_records_match(metadata_prefix="oai_kci"),
        _broken_root_no_records_match(set_spec="JOURNAL"),
        _broken_root_no_records_match(from_date="2018-10-99"),
        _broken_root_no_records_match(
            from_date="2018-11-01", until_date="2018-10-31"
        ),
        _broken_root_no_records_match(
            extra_request_attributes=' resumptionToken="opaque"'
        ),
        _broken_root_no_records_match().replace(
            b' metadataPrefix="oai_dc"', b""
        ),
        _broken_root_no_records_match(
            request_url="http://attacker.invalid/oai/request"
        ),
        _broken_root_no_records_match(
            request_url="http://open.kci.go.kr/oai/other"
        ),
        _broken_root_no_records_match(request_url=f"{KCI_HTTP_ECHO}?x=1"),
        _broken_root_no_records_match(request_url=f"{KCI_HTTP_ECHO}#fragment"),
        _broken_root_no_records_match().replace(
            b"<oai:error code=", b"<error code="
        ).replace(b"</oai:error>", b"</error>"),
    ],
)
def test_other_unqualified_or_mixed_oai_envelopes_remain_rejected(
    body: bytes,
) -> None:
    with pytest.raises(InvalidOaResponse, match="invalid_oai_envelope"):
        parse_oai_dc_list_records_xml(body, target_source_ids={SOURCE_ID})


def test_broken_root_exception_is_not_accepted_for_getrecord() -> None:
    with pytest.raises(InvalidOaResponse, match="invalid_oai_envelope"):
        parse_oai_kci_get_record_xml(
            _broken_root_no_records_match(),
            expected_oai_identifier=OAI_IDENTIFIER,
            expected_source_id=SOURCE_ID,
        )


@pytest.mark.parametrize("code", ["badArgument", "badResumptionToken"])
def test_oai_protocol_errors_have_stable_codes_without_server_text(code: str) -> None:
    secret_server_text = "opaque token and internal server detail"

    with pytest.raises(FatalOaError, match=f"oai_{code}") as raised:
        parse_oai_dc_list_records_xml(
            _oai_error(code, secret_server_text),
            target_source_ids={SOURCE_ID},
        )

    assert secret_server_text not in str(raised.value)
    assert secret_server_text not in repr(raised.value)


class _MergeClient:
    def __init__(self, dc_rights: str, kci_value: str) -> None:
        self.dc_rights = dc_rights
        self.kci_value = kci_value
        self.request_count = 0

    def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
        self.request_count += 1
        assert kwargs["target_source_ids"] == {SOURCE_ID}
        assert kwargs["resumption_token"] is None
        return OaiListPage(
            (
                OaiDcRecord(
                    SOURCE_ID,
                    OAI_IDENTIFIER,
                    self.dc_rights,
                    True,
                ),
            ),
            None,
            1,
        )

    def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
        self.request_count += 1
        assert kwargs == {
            "oai_identifier": OAI_IDENTIFIER,
            "expected_source_id": SOURCE_ID,
        }
        return ParsedOaValue(
            "classified",
            SOURCE_ID,
            self.kci_value,
            True,
        )


@pytest.mark.parametrize(
    ("dc_rights", "kci_value", "status", "token", "error_code"),
    [
        ("Y", "Y", "classified", "Y", None),
        ("N", "N", "classified", "N", None),
        ("Y", "N", "failed", None, "oai_oa_value_mismatch"),
    ],
)
def test_dc_and_kci_evidence_merge_requires_exact_agreement(
    dc_rights: str,
    kci_value: str,
    status: str,
    token: str | None,
    error_code: str | None,
) -> None:
    run = collect_open_access_oai(
        [_paper()],
        _MergeClient(dc_rights, kci_value),
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )

    result = run.results[0]
    assert result.status == status
    assert result.dc_rights == dc_rights
    assert result.dc_rights_field_present is True
    assert result.kci_orte_open_yn == kci_value
    assert result.kci_orte_open_yn_field_present is True
    assert result.orte_open_yn == token
    assert result.error_code == error_code
    assert result.is_open_access is ({"Y": True, "N": False}.get(token))


class _EvidenceClient:
    def __init__(
        self,
        primary: OaiDcRecord,
        verification: ParsedOaValue,
    ) -> None:
        self.primary = primary
        self.verification = verification
        self.request_count = 0

    def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
        self.request_count += 1
        return OaiListPage((self.primary,), None, 1)

    def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
        self.request_count += 1
        return self.verification


@pytest.mark.parametrize(
    ("primary", "verification", "status", "error_code"),
    [
        (
            OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, None, False),
            ParsedOaValue("classified", SOURCE_ID, "Y", True),
            "field_missing",
            "dc_rights_missing",
        ),
        (
            OaiDcRecord(
                SOURCE_ID,
                OAI_IDENTIFIER,
                None,
                True,
                "UNKNOWN-DC",
            ),
            ParsedOaValue("classified", SOURCE_ID, "N", True),
            "invalid_value",
            "dc_rights_invalid",
        ),
        (
            OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, "Y", True),
            ParsedOaValue("field_missing", SOURCE_ID, None, False),
            "field_missing",
            "orte_open_yn_missing",
        ),
        (
            OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, "N", True),
            ParsedOaValue(
                "invalid_value",
                SOURCE_ID,
                None,
                True,
                "UNKNOWN-KCI",
            ),
            "invalid_value",
            "orte_open_yn_invalid",
        ),
    ],
)
def test_unresolved_oai_evidence_is_source_specific_and_cacheable(
    tmp_path: Path,
    primary: OaiDcRecord,
    verification: ParsedOaValue,
    status: str,
    error_code: str,
) -> None:
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )
    run = collect_open_access_oai(
        [_paper()],
        _EvidenceClient(primary, verification),
        cache=cache,
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )

    result = run.results[0]
    assert result.status == status
    assert result.error_code == error_code
    assert result.dc_rights == primary.rights
    assert result.dc_rights_field_present == primary.field_present
    assert result.observed_dc_rights == primary.observed_value
    assert result.kci_orte_open_yn == verification.orte_open_yn
    assert result.kci_orte_open_yn_field_present == verification.field_present
    assert result.observed_kci_orte_open_yn == verification.observed_value
    assert result.orte_open_yn is None

    loaded = cache.load(_paper())
    assert loaded is not None
    assert loaded.cache_hit is True
    assert loaded.error_code == error_code
    assert loaded.dc_rights_field_present == primary.field_present
    assert loaded.observed_dc_rights == primary.observed_value
    assert loaded.kci_orte_open_yn == verification.orte_open_yn
    assert loaded.kci_orte_open_yn_field_present == verification.field_present
    assert loaded.observed_kci_orte_open_yn == verification.observed_value
    assert (
        tmp_path / "cache" / "_evidence" / f"{SOURCE_ID}.json"
    ).is_file()


def test_oai_conflict_cache_preserves_both_tokens_and_verified_identity(
    tmp_path: Path,
) -> None:
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )
    run = collect_open_access_oai(
        [_paper()],
        _MergeClient("Y", "N"),
        cache=cache,
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )

    conflict = run.results[0]
    assert conflict.status == "failed"
    assert conflict.returned_source_id == SOURCE_ID
    assert conflict.dc_rights == "Y"
    assert conflict.kci_orte_open_yn == "N"
    assert conflict.orte_open_yn is None

    loaded = cache.load(_paper())
    assert loaded is not None
    assert loaded.cache_hit is True
    assert loaded.returned_source_id == SOURCE_ID
    assert loaded.dc_rights == "Y"
    assert loaded.kci_orte_open_yn == "N"
    assert loaded.orte_open_yn is None


class _XmlBackedOaiClient:
    def __init__(self, body: bytes, values: dict[str, str]) -> None:
        self.body = body
        self.values = values
        self.request_count = 0
        self.verified_source_ids: list[str] = []

    def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
        self.request_count += 1
        assert kwargs["resumption_token"] is None
        return parse_oai_dc_list_records_xml(
            self.body,
            target_source_ids=kwargs["target_source_ids"],
        )

    def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
        self.request_count += 1
        source_id = kwargs["expected_source_id"]
        self.verified_source_ids.append(source_id)
        return ParsedOaValue(
            "classified",
            source_id,
            self.values[source_id],
            True,
        )


def test_collection_ignores_non_targets_and_restores_database_order() -> None:
    papers = [_paper(1), _paper(2), _paper(3)]
    records = [
        _dc_record(
            source_id=papers[2].source_id,
            oai_identifier="oai:kci.go.kr:ARTI/303",
            rights="Y",
        ),
        _dc_record(
            source_id="ART999999999",
            oai_identifier="oai:kci.go.kr:ARTI/999",
            rights="N",
        ),
        _dc_record(
            source_id=papers[0].source_id,
            oai_identifier="oai:kci.go.kr:ARTI/101",
            rights="N",
        ),
        _dc_record(
            source_id=papers[1].source_id,
            oai_identifier="oai:kci.go.kr:ARTI/202",
            rights="Y",
        ),
    ]
    values = {
        papers[0].source_id: "N",
        papers[1].source_id: "Y",
        papers[2].source_id: "Y",
    }
    client = _XmlBackedOaiClient(_dc_page(records), values)

    run = collect_open_access_oai(
        papers,
        client,
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )

    assert [result.paper.source_id for result in run.results] == [
        paper.source_id for paper in papers
    ]
    assert [result.orte_open_yn for result in run.results] == ["N", "Y", "Y"]
    assert client.verified_source_ids == [paper.source_id for paper in papers]
    assert "ART999999999" not in {
        result.paper.source_id for result in run.results
    }
    assert run.network_requests == 4


def test_article_search_v1_cache_is_not_reused_for_oai_strategy(
    tmp_path: Path,
) -> None:
    paper = _paper()
    cache_directory = tmp_path / "cache"
    cache_directory.mkdir()
    old_payload = {
        "schema_version": "1.0",
        "api_code": "articleSearch",
        "source_id": paper.source_id,
        "paper_fingerprint": paper.fingerprint,
        "status": "classified",
        "returned_source_id": paper.source_id,
        "orte_open_yn": "Y",
        "field_present": True,
        "observed_value": None,
        "collected_at": NOW.isoformat(),
        "error_code": None,
    }
    (cache_directory / f"{paper.source_id}.json").write_text(
        json.dumps(old_payload),
        encoding="utf-8",
    )

    cache = OaCache(
        cache_directory,
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )

    assert cache.load(paper) is None


def _write_schema2_oai_base(
    directory: Path,
    paper: KciPaper,
    *,
    status: str,
    token: str | None,
    field_present: bool | None,
    error_code: str | None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "strategy": OA_COLLECTION_STRATEGY,
        "source_id": paper.source_id,
        "paper_fingerprint": paper.fingerprint,
        "status": status,
        "returned_source_id": paper.source_id,
        "oai_identifier": OAI_IDENTIFIER,
        "dc_rights": "Y",
        "orte_open_yn": token,
        "field_present": field_present,
        "observed_value": None,
        "collected_at": NOW.isoformat(),
        "error_code": error_code,
    }
    (directory / f"{paper.source_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_schema2_classified_cache_without_sidecar_derives_dual_evidence(
    tmp_path: Path,
) -> None:
    paper = _paper()
    directory = tmp_path / "cache"
    _write_schema2_oai_base(
        directory,
        paper,
        status="classified",
        token="Y",
        field_present=True,
        error_code=None,
    )
    cache = OaCache(
        directory,
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )

    loaded = cache.load(paper)

    assert loaded is not None
    assert loaded.cache_hit is True
    assert loaded.oai_identifier == OAI_IDENTIFIER
    assert loaded.dc_rights == "Y"
    assert loaded.dc_rights_field_present is True
    assert loaded.kci_orte_open_yn == "Y"
    assert loaded.kci_orte_open_yn_field_present is True
    assert not (directory / "_evidence" / f"{SOURCE_ID}.json").exists()


def test_ambiguous_schema2_unresolved_cache_without_sidecar_is_a_miss(
    tmp_path: Path,
) -> None:
    paper = _paper()
    directory = tmp_path / "cache"
    _write_schema2_oai_base(
        directory,
        paper,
        status="field_missing",
        token=None,
        field_present=False,
        error_code="orte_open_yn_missing",
    )
    cache = OaCache(
        directory,
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )

    assert cache.load(paper) is None


def test_oai_cache_and_export_reject_article_search_evidence(
    tmp_path: Path,
) -> None:
    paper = _paper()
    article_search_result = PaperOaResult(
        paper=paper,
        status="classified",
        returned_source_id=paper.source_id,
        orte_open_yn="Y",
        field_present=True,
        observed_value=None,
        collected_at=NOW.isoformat(),
        attempts=1,
    )
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )
    with pytest.raises(OaCollectionError, match="cache_strategy_mismatch"):
        cache.save(article_search_result)

    run = OaCollectionRun(
        (article_search_result,),
        network_requests=1,
        cache_hits=0,
        strategy=OA_COLLECTION_STRATEGY,
    )
    output = tmp_path / "should-not-exist.xlsx"
    with pytest.raises(OaCollectionError, match="run_evidence_strategy_mismatch"):
        export_oa_workbook(
            run,
            output,
            database=tmp_path / "source.db",
            generated_at=NOW,
        )
    assert not output.exists()


def _classified_result(paper: KciPaper, token: str) -> PaperOaResult:
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
        kci_orte_open_yn=token,
        kci_orte_open_yn_field_present=True,
    )


def test_harvest_log_separates_retained_total_from_pending_matches(
    tmp_path: Path,
) -> None:
    papers = [_paper(1), _paper(2), _paper(3)]
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )
    cache.save(_classified_result(papers[0], "Y"))
    cache.save(_classified_result(papers[1], "N"))

    class _MixedMappingClient:
        request_count = 0

        def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
            self.request_count += 1
            assert kwargs["target_source_ids"] == {
                paper.source_id for paper in papers
            }
            return OaiListPage(
                tuple(
                    OaiDcRecord(
                        paper.source_id,
                        f"oai:kci.go.kr:ARTI/{paper.database_id}",
                        token,
                        True,
                    )
                    for paper, token in zip(papers, ("Y", "N", "Y"))
                ),
                None,
                3,
            )

        def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
            self.request_count += 1
            assert kwargs["expected_source_id"] == papers[2].source_id
            return ParsedOaValue(
                "classified", papers[2].source_id, "Y", True
            )

    class _LogCapture:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def emit(self, _stage: str, message: str, **kwargs: Any) -> None:
            self.events.append((message, kwargs))

    logger = _LogCapture()
    run = collect_open_access_oai(
        papers,
        _MixedMappingClient(),
        cache=cache,
        logger=logger,  # type: ignore[arg-type]
        from_date="2026-08-22",
        until_date="2026-08-22",
        retries=0,
        now=lambda: NOW,
    )

    harvest_events = [
        event for event in logger.events if event[0].startswith("OAI harvest ")
    ]
    assert len(harvest_events) == 1
    message, progress = harvest_events[0]
    assert "matched_total=3" in message
    assert "matched_pending=1" in message
    assert "matched=" not in message
    assert progress["current"] == 1
    assert progress["total"] == 1
    assert run.cache_hits == 2
    assert run.results[2].orte_open_yn == "Y"


def test_complete_and_partial_workbooks_retain_oai_provenance(
    tmp_path: Path,
) -> None:
    complete_result = _classified_result(_paper(1), "Y")
    complete = export_oa_workbook(
        OaCollectionRun(
            (complete_result,),
            network_requests=2,
            cache_hits=0,
            strategy=OA_COLLECTION_STRATEGY,
        ),
        tmp_path / "oa_list.xlsx",
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert complete.path == tmp_path / "oa_list.xlsx"
    workbook = load_workbook(complete.path, read_only=True)
    try:
        headers = [cell.value for cell in workbook["OA 목록"][1]]
        row = {
            name: workbook["OA 목록"].cell(2, index + 1).value
            for index, name in enumerate(headers)
        }
        validation = {
            values[0].value: values[1].value
            for values in workbook["검증"].iter_rows(min_row=2)
        }
        assert row["oai_identifier"] == complete_result.oai_identifier
        assert row["dc_rights"] == "Y"
        assert row["dc_rights_field_present"] is True
        assert row["kci_orte_open_yn"] == "Y"
        assert row["kci_orte_open_yn_field_present"] is True
        assert row["orte_open_yn"] == "Y"
        assert validation["collection_strategy"] == OA_COLLECTION_STRATEGY
        assert validation["primary_metadata"] == KCI_OAI_PRIMARY_PREFIX
        assert validation["verification_metadata"] == KCI_OAI_VERIFY_PREFIX
        assert validation["report_kind"] == "complete"
        assert "OAI-PMH" in (workbook.properties.description or "")
    finally:
        workbook.close()

    conflict_paper = _paper(2)
    conflict = PaperOaResult(
        paper=conflict_paper,
        status="failed",
        returned_source_id=conflict_paper.source_id,
        orte_open_yn=None,
        field_present=None,
        observed_value=None,
        collected_at=NOW.isoformat(),
        attempts=1,
        error_code="oai_oa_value_mismatch",
        evidence_strategy=OA_COLLECTION_STRATEGY,
        oai_identifier="oai:kci.go.kr:ARTI/2",
        dc_rights="Y",
        dc_rights_field_present=True,
        kci_orte_open_yn="N",
        kci_orte_open_yn_field_present=True,
    )
    partial = export_oa_workbook(
        OaCollectionRun(
            (conflict,),
            network_requests=2,
            cache_hits=0,
            strategy=OA_COLLECTION_STRATEGY,
        ),
        tmp_path / "conflict.xlsx",
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert partial.path == tmp_path / "conflict.partial.xlsx"
    workbook = load_workbook(partial.path, read_only=True)
    try:
        headers = [cell.value for cell in workbook["OA 목록"][1]]
        row = {
            name: workbook["OA 목록"].cell(2, index + 1).value
            for index, name in enumerate(headers)
        }
        validation = {
            values[0].value: values[1].value
            for values in workbook["검증"].iter_rows(min_row=2)
        }
        assert row["oai_identifier"] == conflict.oai_identifier
        assert row["dc_rights"] == "Y"
        assert row["dc_rights_field_present"] is True
        assert row["returned_source_id"] == conflict_paper.source_id
        assert row["kci_orte_open_yn"] == "N"
        assert row["kci_orte_open_yn_field_present"] is True
        assert row["orte_open_yn"] is None
        assert row["error_code"] == "oai_oa_value_mismatch"
        assert validation["report_kind"] == "partial"
        assert validation["classification_complete"] is False
    finally:
        workbook.close()


def test_partial_workbook_identifies_dc_missing_but_kci_y(
    tmp_path: Path,
) -> None:
    run = collect_open_access_oai(
        [_paper()],
        _EvidenceClient(
            OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, None, False),
            ParsedOaValue("classified", SOURCE_ID, "Y", True),
        ),
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )
    exported = export_oa_workbook(
        run,
        tmp_path / "dc-missing.xlsx",
        database=tmp_path / "source.db",
        generated_at=NOW,
    )

    assert exported.path.name == "dc-missing.partial.xlsx"
    workbook = load_workbook(exported.path, read_only=True)
    try:
        headers = [cell.value for cell in workbook["OA 목록"][1]]
        row = {
            name: workbook["OA 목록"].cell(2, index + 1).value
            for index, name in enumerate(headers)
        }
        assert row["evidence_strategy"] == OA_COLLECTION_STRATEGY
        assert row["oai_identifier"] == OAI_IDENTIFIER
        assert row["dc_rights"] is None
        assert row["dc_rights_field_present"] is False
        assert row["observed_dc_rights"] is None
        assert row["kci_orte_open_yn"] == "Y"
        assert row["kci_orte_open_yn_field_present"] is True
        assert row["observed_kci_orte_open_yn"] is None
        assert row["orte_open_yn"] is None
        assert row["error_code"] == "dc_rights_missing"
    finally:
        workbook.close()


def test_wrong_namespace_elements_are_never_accepted_as_oa_evidence() -> None:
    dc_body = _dc_page([_dc_record(rights="Y")]).replace(
        b'<dc:rights>Y</dc:rights>',
        b'<evil:rights xmlns:evil="https://evil.invalid/">Y</evil:rights>',
    )
    dc_page = parse_oai_dc_list_records_xml(
        dc_body,
        target_source_ids={SOURCE_ID},
    )
    assert dc_page.matches[0].rights is None
    assert dc_page.matches[0].field_present is False

    kci_body = _kci_get_record(oa_value="Y").replace(
        b'<kci:orte-open-yn>Y</kci:orte-open-yn>',
        b'<evil:orte-open-yn xmlns:evil="https://evil.invalid/">Y</evil:orte-open-yn>',
    )
    parsed = parse_oai_kci_get_record_xml(
        kci_body,
        expected_oai_identifier=OAI_IDENTIFIER,
        expected_source_id=SOURCE_ID,
    )
    assert parsed == ParsedOaValue(
        "field_missing", SOURCE_ID, None, False
    )


class _TokenSequenceClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.request_count = 0
        self.page_params: list[dict[str, Any]] = []

    def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
        self.request_count += 1
        self.page_params.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, OaiListPage)
        return outcome

    def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
        raise AssertionError(f"unexpected verification: {kwargs}")


class _AlwaysEmptyOaiClient:
    def __init__(self) -> None:
        self.request_count = 0
        self.page_params: list[dict[str, Any]] = []

    def fetch_dc_page(self, **kwargs: Any) -> OaiListPage:
        self.request_count += 1
        self.page_params.append(dict(kwargs))
        return OaiListPage((), None, 0, no_records_match=True)

    def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
        raise AssertionError(f"unexpected verification: {kwargs}")


@pytest.mark.parametrize(
    "pages",
    [
        [OaiListPage((), "token-A", 100), OaiListPage((), "token-A", 100)],
        [
            OaiListPage((), "token-A", 100),
            OaiListPage((), "token-B", 100),
            OaiListPage((), "token-A", 100),
        ],
    ],
)
def test_resumption_token_repetition_or_cycle_aborts(pages: list[OaiListPage]) -> None:
    with pytest.raises(InvalidOaResponse, match="resumption_token_loop"):
        collect_open_access_oai(
            [_paper()],
            _TokenSequenceClient(list(pages)),
            from_date="2026-08-01",
            until_date="2026-08-01",
            retries=0,
            max_pages_per_window=10,
            now=lambda: NOW,
        )


def test_oai_page_limit_aborts_unbounded_token_chain() -> None:
    client = _TokenSequenceClient(
        [
            OaiListPage((), "token-A", 100),
            OaiListPage((), "token-B", 100),
        ]
    )
    with pytest.raises(InvalidOaResponse, match="oai_page_limit_exceeded"):
        collect_open_access_oai(
            [_paper()],
            client,
            from_date="2026-08-01",
            until_date="2026-08-01",
            retries=0,
            max_pages_per_window=2,
            now=lambda: NOW,
        )
    assert client.request_count == 2


def test_expired_resumption_token_restarts_one_window_once() -> None:
    match = OaiDcRecord(SOURCE_ID, OAI_IDENTIFIER, "Y", True)
    client = _TokenSequenceClient(
        [
            OaiListPage((), "expired-token", 100),
            FatalOaError("oai_badResumptionToken"),
            OaiListPage((match,), None, 1),
        ]
    )

    class _VerifyingTokenClient(_TokenSequenceClient):
        def fetch_kci_record(self, **kwargs: Any) -> ParsedOaValue:
            self.request_count += 1
            return ParsedOaValue("classified", SOURCE_ID, "Y", True)

    verifying = _VerifyingTokenClient(client.outcomes)
    run = collect_open_access_oai(
        [_paper()],
        verifying,
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )
    assert run.results[0].orte_open_yn == "Y"
    assert [params["resumption_token"] for params in verifying.page_params] == [
        None,
        "expired-token",
        None,
    ]


def test_limited_datestamp_scope_miss_is_not_global_no_record(tmp_path: Path) -> None:
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: NOW,
    )
    run = collect_open_access_oai(
        [_paper()],
        _TokenSequenceClient([OaiListPage((), None, 0, no_records_match=True)]),
        cache=cache,
        from_date="2026-08-01",
        until_date="2026-08-01",
        retries=0,
        now=lambda: NOW,
    )
    result = run.results[0]
    assert result.status == "failed"
    assert result.error_code == "oai_harvest_incomplete"
    assert result.is_open_access is None
    assert cache.load(_paper()) is None


@pytest.mark.parametrize(
    ("from_date", "until_date", "expected_status", "expected_error", "cached"),
    [
        (
            KCI_OAI_EARLIEST_DATE,
            "2026-08-22",
            "failed",
            "oai_harvest_incomplete",
            False,
        ),
        (
            KCI_OAI_EARLIEST_DATE,
            "2026-08-23",
            "no_record",
            "article_not_found",
            True,
        ),
        (
            KCI_OAI_EARLIEST_DATE,
            "2026-09-30",
            "no_record",
            "article_not_found",
            True,
        ),
        (
            "2013-03-22",
            "2026-08-23",
            "failed",
            "oai_harvest_incomplete",
            False,
        ),
    ],
)
def test_global_no_record_requires_complete_repository_as_of_both_bounds(
    tmp_path: Path,
    from_date: str,
    until_date: str,
    expected_status: str,
    expected_error: str,
    cached: bool,
) -> None:
    run_now = datetime(2026, 8, 23, 0, 0, 1, tzinfo=SEOUL)
    cache = OaCache(
        tmp_path / "cache",
        strategy=OA_COLLECTION_STRATEGY,
        now=lambda: run_now,
    )
    client = _AlwaysEmptyOaiClient()

    run = collect_open_access_oai(
        [_paper()],
        client,
        cache=cache,
        from_date=from_date,
        until_date=until_date,
        retries=0,
        now=lambda: run_now,
    )

    result = run.results[0]
    assert result.status == expected_status
    assert result.error_code == expected_error
    assert result.orte_open_yn is None
    assert (cache.load(_paper()) is not None) is cached
    assert client.page_params
    assert all(
        params["until_date"] <= "2026-08-23"
        for params in client.page_params
    )


def test_default_until_is_anchored_once_across_clock_rollover() -> None:
    values = iter(
        [
            datetime(2026, 8, 22, 23, 59, 59, tzinfo=SEOUL),
            datetime(2026, 8, 23, 0, 0, 1, tzinfo=SEOUL),
        ]
    )
    clock_calls = 0

    def rolling_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return next(values)

    client = _AlwaysEmptyOaiClient()
    run = collect_open_access_oai(
        [_paper()],
        client,
        retries=0,
        now=rolling_clock,
    )

    assert clock_calls == 1
    assert run.results[0].status == "no_record"
    assert run.results[0].error_code == "article_not_found"
    assert all(
        params["until_date"] <= "2026-08-22"
        for params in client.page_params
    )


def test_priority_day_precedes_complete_newest_first_monthly_windows() -> None:
    windows = _oai_date_windows(
        from_date="2026-06-15",
        until_date="2026-08-22",
        priority_dates=("2026-07-18", "2026-07-18"),
    )
    assert windows == (
        ("2026-07-18", "2026-07-18"),
        ("2026-08-01", "2026-08-22"),
        ("2026-07-01", "2026-07-31"),
        ("2026-06-15", "2026-06-30"),
    )
