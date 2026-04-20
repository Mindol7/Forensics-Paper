# kci.py #
"""
    - KCI API에서 논문 데이터 수집
    - KciQuery 받아 -> KCI API 호출 -> XML 응답 파싱 -> KciRawPaper 구조화된 논문 객체 리스트로 반환
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from html import unescape
from typing import Any
import time

import requests
from lxml import etree

from Core.query_builder import KciQuery


class KciApiError(RuntimeError):
    """Raised when KCI returns a non-success response."""

# KCI 데이터 구조 #
@dataclass
class KciRawPaper:
    """Raw paper data returned by openApiM310List."""

    matched_query: str
    insi_id: str | None
    sere_id: str | None
    vol_isse_id: str | None
    arti_id: str | None
    title_kor: str | None
    title_fola: str | None
    title_eng: str | None
    keyword_kor: str | None
    keyword_fola: str | None
    keyword_eng: str | None
    abstract_kor: str | None
    abstract_fola: str | None
    abstract_eng: str | None
    doi: str | None
    uci: str | None
    url: str | None
    first_page: str | None
    final_page: str | None
    total_page_count: str | None
    issn: str | None
    eissn: str | None
    subject_code: str | None
    is_fulltext: str | None
    registered_at_raw: str | None
    updated_at_raw: str | None
    raw_item: dict[str, Any]

# 실제 수집 #
class KciCollector:
    def __init__(
        self,
        service_key: str,
        *,
        base_url: str,
        timeout: int = 120,
        page_size: int = 10,
        session: requests.Session | None = None,
        retry_count: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.service_key = service_key
        self.base_url = base_url
        self.timeout = timeout
        self.page_size = page_size
        self.session = session or requests.Session()
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds

    # 하나의 Query로 페이지네이션을 통해 여러 페이지 데이터 반복 수집
    def collect(self, query: KciQuery, *, max_pages: int | None = None) -> list[KciRawPaper]:
        page_no = 1
        papers: list[KciRawPaper] = []
        while True:
            root = self._request_page(query=query, page_no=page_no) # 요청
            page_items, total_count = self._parse_items(root, matched_query=query.arti_nm) # 파싱

            if not page_items: # 종료 조건 체크 1
                break

            papers.extend(page_items) # 결과 추가

            if len(papers) >= total_count: # 종료 조건 체크 2
                break

            page_no += 1
            if max_pages is not None and page_no > max_pages: # 종료 조건 체크 2
                break

        return papers

    def collect_many(self, queries: list[KciQuery], *, max_pages: int | None = None) -> list[KciRawPaper]:
        aggregated: list[KciRawPaper] = []
        for query in queries:
            aggregated.extend(self.collect(query, max_pages=max_pages))
        return aggregated

    def _request_page(self, query: KciQuery, page_no: int) -> etree._Element:
        params: dict[str, Any] = {
            "serviceKey": self.service_key,
            "pageNo": page_no,
            "recordCnt": self.page_size,
            "artiNm": query.arti_nm,
        }
        if query.sere_id:
            params["sereId"] = query.sere_id
        if query.insi_id:
            params["insiId"] = query.insi_id

        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                response = self.session.get(self.base_url, params=params, timeout=self.timeout)
                response.raise_for_status()
                root = etree.fromstring(response.content)

                result_code = self._find_text(root, ".//header/resultCode")
                result_msg = self._find_text(root, ".//header/resultMsg")
                if result_code != "00":
                    raise KciApiError(f"KCI API error: code={result_code}, message={result_msg}")
                return root
            except (requests.RequestException, etree.XMLSyntaxError, KciApiError) as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                time.sleep(self.retry_backoff_seconds * (attempt + 1))

        raise KciApiError(f"Failed to fetch KCI data: {last_error}")

    def _parse_items(self, root: etree._Element, *, matched_query: str) -> tuple[list[KciRawPaper], int]:
        total_count = int(self._find_text(root, ".//body/totalCount") or "0")
        items = root.findall(".//body/items/item")
        parsed = [self._parse_item(item, matched_query=matched_query) for item in items]
        return parsed, total_count

    def _parse_item(self, item: etree._Element, *, matched_query: str) -> KciRawPaper:
        raw = {child.tag: self._clean_text(child.text) for child in item}
        return KciRawPaper(
            matched_query=matched_query,
            insi_id=raw.get("INSI_ID"),
            sere_id=raw.get("SERE_ID"),
            vol_isse_id=raw.get("VOL_ISSE_ID"),
            arti_id=raw.get("ARTI_ID"),
            title_kor=raw.get("ARTI_KOR_TITL"),
            title_fola=raw.get("ARTI_FOLA_TITL"),
            title_eng=raw.get("ARTI_ENG_TITL"),
            keyword_kor=raw.get("KOR_KEYW"),
            keyword_fola=raw.get("FOLA_KEYW"),
            keyword_eng=raw.get("ENG_KEYW"),
            abstract_kor=raw.get("KOR_ABST"),
            abstract_fola=raw.get("FOLA_ABST"),
            abstract_eng=raw.get("ENG_ABST"),
            doi=raw.get("DOI"),
            uci=raw.get("UCI"),
            url=raw.get("URL"),
            first_page=raw.get("FIRS_PG"),
            final_page=raw.get("FINI_PG"),
            total_page_count=raw.get("TOTAL_PG_CNT"),
            issn=raw.get("ISSN"),
            eissn=raw.get("EISSN"),
            subject_code=raw.get("STUD_FIEL_CD"),
            is_fulltext=raw.get("ORTE_YN"),
            registered_at_raw=raw.get("RESI_DT"),
            updated_at_raw=raw.get("UPDATE_DT"),
            raw_item=raw,
        )

    @staticmethod
    def _find_text(root: etree._Element, xpath: str) -> str | None:
        node = root.find(xpath)
        if node is None or node.text is None:
            return None
        return KciCollector._clean_text(node.text)

    @staticmethod
    def _clean_text(text: str | None) -> str | None:
        if text is None:
            return None
        cleaned = unescape(text).strip()
        return cleaned or None

    @staticmethod
    def to_dict(paper: KciRawPaper) -> dict[str, Any]:
        return asdict(paper)
