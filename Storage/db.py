# db.py #
"""
    - 논문을 DB 테이블에 저장 -> 기존 데이터와 비교해 update, insert + 마지막 실행 시간 등도 관리.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator

from sqlalchemy import Boolean, Float, Integer, String, Text, create_engine, delete, func, inspect, or_, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from Core.normalizer import NormalizedPaper


class Base(DeclarativeBase):
    pass


class PaperRecord(Base):
    __tablename__ = "kci_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    uci: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    journal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    institution_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eissn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_fulltext: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    registered_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    matched_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    relevance_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    is_relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class CorpusRecord(Base):
    """Full KCI corpus — every fetched paper (with author keywords), unmatched.

    Same column layout as ``kci_papers`` so ``_apply_paper`` can write either.
    Phase 1 (``--build-corpus``) populates this; Phase 2 (``--match-corpus``)
    reads it offline to classify into ``kci_papers``. The category / relevance /
    matched columns stay at their defaults here (set only during matching).
    """

    __tablename__ = "kci_corpus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    uci: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    journal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    institution_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eissn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_fulltext: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    registered_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    matched_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    relevance_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    is_relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class PipelineState(Base):
    __tablename__ = "pipeline_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class ScopusPaperRecord(Base):
    __tablename__ = "scopus_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="scopus")
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    uci: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_kor: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_eng: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    journal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    institution_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eissn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_fulltext: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    registered_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    matched_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class DatabaseManager:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, future=True)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_tables(self) -> None:
        Base.metadata.create_all(self.engine)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        inspector = inspect(self.engine)
        table_names = set(inspector.get_table_names())
        with self.engine.begin() as connection:
            if "kci_papers" in table_names:
                columns = {column["name"] for column in inspector.get_columns("kci_papers")}
                if "category" not in columns and "matched_queries_json" in columns:
                    connection.execute(text("ALTER TABLE kci_papers RENAME COLUMN matched_queries_json TO category"))
                    columns.remove("matched_queries_json")
                    columns.add("category")
                if "category" not in columns:
                    connection.execute(text("ALTER TABLE kci_papers ADD COLUMN category TEXT NOT NULL DEFAULT '[]'"))
                if "matched_keywords_json" not in columns:
                    connection.execute(text("ALTER TABLE kci_papers ADD COLUMN matched_keywords_json TEXT NOT NULL DEFAULT '[]'"))

            if "scopus_papers" in table_names:
                columns = {column["name"] for column in inspector.get_columns("scopus_papers")}
                if "category" not in columns:
                    connection.execute(text("ALTER TABLE scopus_papers ADD COLUMN category TEXT NOT NULL DEFAULT '[]'"))
                if "matched_keywords_json" not in columns:
                    connection.execute(text("ALTER TABLE scopus_papers ADD COLUMN matched_keywords_json TEXT NOT NULL DEFAULT '[]'"))

    def get_state(self, key: str) -> str | None:
        with self.session_factory() as session:
            record = session.get(PipelineState, key)
            return record.value if record else None

    def set_state(self, key: str, value: str) -> None:
        with self.session_factory() as session:
            record = session.get(PipelineState, key)
            if record is None:
                record = PipelineState(key=key, value=value)
                session.add(record)
            else:
                record.value = value
            session.commit()

    def existing_source_ids(self, source_ids: Iterable[str]) -> set[str]:
        normalized_ids = [source_id for source_id in source_ids if source_id]
        if not normalized_ids:
            return set()
        with self.session_factory() as session:
            stmt = select(PaperRecord.source_id).where(PaperRecord.source_id.in_(normalized_ids))
            return set(session.execute(stmt).scalars().all())

    def upsert_papers(self, papers: Iterable[NormalizedPaper]) -> int:
        count = 0
        with self.session_factory() as session:
            for paper in papers:
                count += 1
                record = session.execute(
                    select(PaperRecord).where(PaperRecord.source_id == paper.source_id)
                ).scalar_one_or_none()
                if record is None:
                    record = PaperRecord(source=paper.source, source_id=paper.source_id)
                    session.add(record)
                self._apply_paper(record, paper)
            session.commit()
        return count

    def count_papers(self) -> int:
        with self.session_factory() as session:
            return int(session.query(PaperRecord).count())

    def iter_kci_papers(self, *, chunk_size: int = 2000) -> Iterator[NormalizedPaper]:
        """Stream all matched KCI rows as NormalizedPaper (for the accumulated
        Excel export). PaperRecord shares columns with CorpusRecord, so
        `_record_to_normalized` reconstructs it too. Keyset-paginated; each
        chunk's session closes before yielding."""
        last_id = 0
        while True:
            with self.session_factory() as session:
                records = list(
                    session.scalars(
                        select(PaperRecord)
                        .where(PaperRecord.source == "kci")
                        .where(PaperRecord.id > last_id)
                        .order_by(PaperRecord.id)
                        .limit(chunk_size)
                    )
                )
                normalized = [DatabaseManager._record_to_normalized(record) for record in records]
                if records:
                    last_id = records[-1].id
            if not normalized:
                break
            yield from normalized

    # ---------- Full corpus (kci_corpus) ----------

    def existing_corpus_ids(self, source_ids: Iterable[str]) -> set[str]:
        normalized_ids = [source_id for source_id in source_ids if source_id]
        if not normalized_ids:
            return set()
        found: set[str] = set()
        with self.session_factory() as session:
            for start in range(0, len(normalized_ids), 500):
                chunk = normalized_ids[start:start + 500]
                stmt = select(CorpusRecord.source_id).where(CorpusRecord.source_id.in_(chunk))
                found.update(session.execute(stmt).scalars().all())
        return found

    def upsert_corpus_papers(self, papers: Iterable[NormalizedPaper]) -> int:
        count = 0
        with self.session_factory() as session:
            for paper in papers:
                count += 1
                record = session.execute(
                    select(CorpusRecord).where(CorpusRecord.source_id == paper.source_id)
                ).scalar_one_or_none()
                if record is None:
                    record = CorpusRecord(source=paper.source, source_id=paper.source_id)
                    session.add(record)
                self._apply_corpus_paper(record, paper)
            session.commit()
        return count

    def count_corpus(self) -> int:
        with self.session_factory() as session:
            return int(session.query(CorpusRecord).count())

    def keywordless_corpus_ids(self) -> list[str]:
        """source_ids of corpus rows with no keywords — for keyword backfill.

        Mix of genuinely keyword-less articles and ones whose articleDetail
        enrichment failed (e.g. a network outage mid-build). `--reenrich-corpus`
        re-fetches these and fills the recoverable ones.
        """
        with self.session_factory() as session:
            stmt = select(CorpusRecord.source_id).where(CorpusRecord.keywords_json == "[]")
            return list(session.execute(stmt).scalars().all())

    def set_corpus_keywords(self, updates: dict[str, list[str]]) -> int:
        if not updates:
            return 0
        count = 0
        with self.session_factory() as session:
            for source_id, keywords in updates.items():
                record = session.execute(
                    select(CorpusRecord).where(CorpusRecord.source_id == source_id)
                ).scalar_one_or_none()
                if record is not None:
                    record.keywords_json = json.dumps(keywords, ensure_ascii=False)
                    count += 1
            session.commit()
        return count

    def corpus_id_bounds(self) -> tuple[int, int]:
        """(min_id, max_id) of the corpus — used to split it into parallel
        segments for the multi-process match. (0, 0) when empty."""
        with self.session_factory() as session:
            low, high = session.execute(
                select(func.min(CorpusRecord.id), func.max(CorpusRecord.id))
            ).one()
        return (int(low) if low is not None else 0, int(high) if high is not None else 0)

    def iter_corpus_papers(
        self,
        *,
        chunk_size: int = 1000,
        id_min: int | None = None,
        id_max: int | None = None,
    ) -> Iterator[NormalizedPaper]:
        """Stream corpus rows (optionally an id range) as detached NormalizedPaper.

        Keyset pagination on the PK: each chunk's read session is opened, fully
        converted to plain dataclasses, then CLOSED before the rows are yielded —
        so writers don't hit a SQLite read/write lock and memory stays bounded.
        `id_min`/`id_max` restrict to one segment for parallel matching.
        """
        last_id = (id_min - 1) if id_min is not None else 0
        while True:
            with self.session_factory() as session:
                stmt = select(CorpusRecord).where(CorpusRecord.id > last_id)
                if id_max is not None:
                    stmt = stmt.where(CorpusRecord.id <= id_max)
                stmt = stmt.order_by(CorpusRecord.id).limit(chunk_size)
                records = list(session.scalars(stmt))
                normalized = [DatabaseManager._record_to_normalized(record) for record in records]
                if records:
                    last_id = records[-1].id
            if not normalized:
                break
            yield from normalized

    @staticmethod
    def _record_to_normalized(record: CorpusRecord) -> NormalizedPaper:
        def _loads(value: str | None, default):
            if not value:
                return default
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return default

        return NormalizedPaper(
            source=record.source,
            source_id=record.source_id,
            title=record.title,
            title_kor=record.title_kor,
            title_eng=record.title_eng,
            title_other=record.title_other,
            doi=record.doi,
            uci=record.uci,
            url=record.url,
            abstract=record.abstract,
            abstract_kor=record.abstract_kor,
            abstract_eng=record.abstract_eng,
            abstract_other=record.abstract_other,
            keywords=_loads(record.keywords_json, []),
            authors=_loads(record.authors_json, []),
            journal_id=record.journal_id,
            institution_id=record.institution_id,
            issue_id=record.issue_id,
            first_page=record.first_page,
            final_page=record.final_page,
            page_count=record.page_count,
            issn=record.issn,
            eissn=record.eissn,
            subject_code=record.subject_code,
            is_fulltext=record.is_fulltext,
            registered_at=record.registered_at,
            updated_at=record.updated_at,
            publication_year=record.publication_year,
            categories=_loads(record.category, []),
            matched_keywords=_loads(record.matched_keywords_json, []),
            relevance_reasons=_loads(record.relevance_reasons_json, []),
            relevance_score=record.relevance_score,
            is_relevant=record.is_relevant,
            summary=record.summary,
            raw_payload=_loads(record.raw_payload_json, {}),
        )

    def clear_kci_papers(self) -> int:
        """Delete all KCI rows from kci_papers so --match-corpus rebuilds the
        matched set fresh from the corpus (kci_papers is a derived table; without
        this, tightening the keyword set leaves old false positives behind)."""
        with self.session_factory() as session:
            result = session.execute(delete(PaperRecord).where(PaperRecord.source == "kci"))
            session.commit()
            return int(result.rowcount or 0)

    def delete_kci_papers_outside_year_range(self, *, start_year: int, end_year: int) -> int:
        with self.session_factory() as session:
            result = session.execute(
                delete(PaperRecord)
                .where(PaperRecord.source == "kci")
                .where(
                    or_(
                        PaperRecord.publication_year.is_(None),
                        PaperRecord.publication_year < start_year,
                        PaperRecord.publication_year > end_year,
                    )
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def delete_kci_papers_with_invalid_match_evidence(self) -> int:
        with self.session_factory() as session:
            result = session.execute(
                delete(PaperRecord)
                .where(PaperRecord.source == "kci")
                .where(
                    or_(
                        PaperRecord.category.in_(("", "[]")),
                        PaperRecord.matched_keywords_json.in_(("", "[]")),
                        PaperRecord.relevance_reasons_json.in_(("", "[]")),
                        PaperRecord.category.like('%"*"%'),
                        PaperRecord.matched_keywords_json.like('%"*"%'),
                        PaperRecord.relevance_reasons_json.like('%"*"%'),
                    )
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def existing_scopus_source_ids(self, source_ids: Iterable[str]) -> set[str]:
        normalized_ids = [source_id for source_id in source_ids if source_id]
        if not normalized_ids:
            return set()
        with self.session_factory() as session:
            stmt = select(ScopusPaperRecord.source_id).where(ScopusPaperRecord.source_id.in_(normalized_ids))
            return set(session.execute(stmt).scalars().all())

    def all_scopus_source_ids(self) -> set[str]:
        with self.session_factory() as session:
            stmt = select(ScopusPaperRecord.source_id)
            return set(session.execute(stmt).scalars().all())

    def upsert_scopus_papers(self, papers: Iterable[NormalizedPaper]) -> int:
        count = 0
        with self.session_factory() as session:
            for paper in papers:
                count += 1
                record = session.execute(
                    select(ScopusPaperRecord).where(ScopusPaperRecord.source_id == paper.source_id)
                ).scalar_one_or_none()
                if record is None:
                    record = ScopusPaperRecord(source="scopus", source_id=paper.source_id)
                    session.add(record)
                self._apply_scopus_paper(record, paper)
            session.commit()
        return count

    def count_scopus_papers(self) -> int:
        with self.session_factory() as session:
            return int(session.query(ScopusPaperRecord).count())

    @staticmethod
    def _clean_list(values: Iterable[str]) -> list[str]:
        cleaned_values: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip() if isinstance(value, str) else ""
            if not cleaned or cleaned == "*":
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned_values.append(cleaned)
        return cleaned_values

    @staticmethod
    def _clean_kci_classification(paper: NormalizedPaper) -> tuple[list[str], list[str], list[str]]:
        categories = DatabaseManager._clean_list(paper.categories)
        matched_keywords = DatabaseManager._clean_list(paper.matched_keywords)
        reasons = DatabaseManager._clean_list(paper.relevance_reasons) or matched_keywords
        if paper.source == "kci" and (not categories or not matched_keywords):
            raise ValueError(f"KCI paper has no category or matched keyword evidence: {paper.source_id}")
        return categories, matched_keywords, reasons

    @staticmethod
    def _apply_paper(record: PaperRecord, paper: NormalizedPaper) -> None:
        categories, matched_keywords, reasons = DatabaseManager._clean_kci_classification(paper)

        record.source = paper.source
        record.title = paper.title
        record.title_kor = paper.title_kor
        record.title_eng = paper.title_eng
        record.title_other = paper.title_other
        record.doi = paper.doi
        record.uci = paper.uci
        record.url = paper.url
        record.abstract = paper.abstract
        record.abstract_kor = paper.abstract_kor
        record.abstract_eng = paper.abstract_eng
        record.abstract_other = paper.abstract_other
        record.keywords_json = json.dumps(paper.keywords, ensure_ascii=False)
        record.authors_json = json.dumps(paper.authors, ensure_ascii=False)
        record.journal_id = paper.journal_id
        record.institution_id = paper.institution_id
        record.issue_id = paper.issue_id
        record.first_page = paper.first_page
        record.final_page = paper.final_page
        record.page_count = paper.page_count
        record.issn = paper.issn
        record.eissn = paper.eissn
        record.subject_code = paper.subject_code
        record.is_fulltext = paper.is_fulltext
        record.registered_at = paper.registered_at
        record.updated_at = paper.updated_at
        record.publication_year = paper.publication_year
        record.category = json.dumps(categories, ensure_ascii=False)
        record.matched_keywords_json = json.dumps(matched_keywords, ensure_ascii=False)
        record.relevance_score = paper.relevance_score
        record.relevance_reasons_json = json.dumps(reasons, ensure_ascii=False)
        record.is_relevant = paper.is_relevant
        record.summary = paper.summary
        record.raw_payload_json = json.dumps(paper.raw_payload, ensure_ascii=False)

    @staticmethod
    def _apply_corpus_paper(record: CorpusRecord, paper: NormalizedPaper) -> None:
        # Corpus stores every fetched paper, matched or not — no match-evidence
        # guard (that guard belongs to kci_papers, the matched-results table).
        record.source = paper.source
        record.title = paper.title
        record.title_kor = paper.title_kor
        record.title_eng = paper.title_eng
        record.title_other = paper.title_other
        record.doi = paper.doi
        record.uci = paper.uci
        record.url = paper.url
        record.abstract = paper.abstract
        record.abstract_kor = paper.abstract_kor
        record.abstract_eng = paper.abstract_eng
        record.abstract_other = paper.abstract_other
        record.keywords_json = json.dumps(paper.keywords, ensure_ascii=False)
        record.authors_json = json.dumps(paper.authors, ensure_ascii=False)
        record.journal_id = paper.journal_id
        record.institution_id = paper.institution_id
        record.issue_id = paper.issue_id
        record.first_page = paper.first_page
        record.final_page = paper.final_page
        record.page_count = paper.page_count
        record.issn = paper.issn
        record.eissn = paper.eissn
        record.subject_code = paper.subject_code
        record.is_fulltext = paper.is_fulltext
        record.registered_at = paper.registered_at
        record.updated_at = paper.updated_at
        record.publication_year = paper.publication_year
        record.category = json.dumps(DatabaseManager._clean_list(paper.categories), ensure_ascii=False)
        record.matched_keywords_json = json.dumps(
            DatabaseManager._clean_list(paper.matched_keywords), ensure_ascii=False
        )
        record.relevance_score = paper.relevance_score
        record.relevance_reasons_json = json.dumps(
            DatabaseManager._clean_list(paper.relevance_reasons), ensure_ascii=False
        )
        record.is_relevant = paper.is_relevant
        record.summary = paper.summary
        record.raw_payload_json = json.dumps(paper.raw_payload, ensure_ascii=False)

    @staticmethod
    def _apply_scopus_paper(record: ScopusPaperRecord, paper: NormalizedPaper) -> None:
        record.source = "scopus"
        record.title = paper.title
        record.title_kor = paper.title_kor
        record.title_eng = paper.title_eng
        record.title_other = paper.title_other
        record.doi = paper.doi
        record.uci = paper.uci
        record.url = paper.url
        record.abstract = paper.abstract
        record.abstract_kor = paper.abstract_kor
        record.abstract_eng = paper.abstract_eng
        record.abstract_other = paper.abstract_other
        record.keywords_json = json.dumps(paper.keywords, ensure_ascii=False)
        record.authors_json = json.dumps(paper.authors, ensure_ascii=False)
        record.journal_id = paper.journal_id
        record.institution_id = paper.institution_id
        record.issue_id = paper.issue_id
        record.first_page = paper.first_page
        record.final_page = paper.final_page
        record.page_count = paper.page_count
        record.issn = paper.issn
        record.eissn = paper.eissn
        record.subject_code = paper.subject_code
        record.is_fulltext = paper.is_fulltext
        record.registered_at = paper.registered_at
        record.updated_at = paper.updated_at
        record.publication_year = paper.publication_year
        record.category = json.dumps(DatabaseManager._clean_list(paper.categories), ensure_ascii=False)
        record.matched_keywords_json = json.dumps(DatabaseManager._clean_list(paper.matched_keywords), ensure_ascii=False)
        record.summary = paper.summary
        record.raw_payload_json = json.dumps(paper.raw_payload, ensure_ascii=False)
