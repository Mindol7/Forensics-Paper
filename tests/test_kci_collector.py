from __future__ import annotations

from typing import Any

from Collector.kci import KciCollector


DETAIL_XML = """<?xml version='1.0' encoding='UTF-8'?>
<response>
  <result><total>1</total></result>
  <record>
    <journalInfo journal-id="JOURNAL-1">
      <journal-name>디지털포렌식연구</journal-name>
      <publisher-name>한국디지털포렌식학회</publisher-name>
      <pub-year>2026</pub-year><pub-mon>07</pub-mon>
    </journalInfo>
    <articleInfo article-id="ART000000001">
      <title-group><article-title lang="original">메타데이터 시험</article-title></title-group>
      <abstract-group><abstract lang="original">초록</abstract></abstract-group>
      <keyword-group><keyword>디지털 포렌식</keyword><keyword>무결성</keyword></keyword-group>
      <author-group><author>홍길동</author></author-group>
      <doi>10.1234/example</doi>
    </articleInfo>
  </record>
</response>
""".encode()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 16384
        yield DETAIL_XML

    def close(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response()


def test_article_detail_uses_official_api_code_and_parses_metadata() -> None:
    session = _Session()
    collector = KciCollector("secret-key", session=session, retry_count=0)

    paper = collector.fetch_article_detail("ART000000001")

    assert paper is not None
    assert paper.arti_id == "ART000000001"
    assert paper.journal_id == "JOURNAL-1"
    assert paper.keywords == ["디지털 포렌식", "무결성"]
    assert paper.authors == ["홍길동"]
    assert session.calls[0]["params"] == {
        "apiCode": "articleDetail",
        "key": "secret-key",
        "id": "ART000000001",
    }
    assert session.calls[0]["stream"] is True
