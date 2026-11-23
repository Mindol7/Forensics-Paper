#!/usr/bin/env python3
"""LLM(Gemini) 기반 논문 → 기술분류(tech_taxonomy.json) 자동 매핑.

동작:
  Step 1) DB에 수집된 논문(제목/키워드/초록)을 입력으로 준다.
  Step 2) Gemini API로 tech_taxonomy.json의 기술 코드에 매핑한다.

특징:
  - 증분/재개: 이미 분류한 source_id는 건너뛴다 → 최초 1회 전체, 이후엔 새 논문만.
  - 무료 티어 대응: 배치 호출로 요청 수 절감 + RPM 스로틀 + 429(쿼터) 시 안전 종료.
  - 결과는 별도 테이블 `paper_tech_classification`에 저장(기존 테이블 무손상).

사용 예:
  export GEMINI_API_KEY=...            # Google AI Studio 키
  python3 scripts/classify_papers_llm.py --limit 800        # 하루 쿼터만큼
  python3 scripts/classify_papers_llm.py --limit 800        # 다음날 이어서(자동 재개)
  python3 scripts/classify_papers_llm.py --dry-run          # 프롬프트만 출력(호출 X)
  python3 scripts/classify_papers_llm.py --list-models      # 사용 가능한 모델 id 확인
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from sqlalchemy import text

from Storage.db import DatabaseManager

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
RESULTS_TABLE = "paper_tech_classification"


# ----------------------------- taxonomy -----------------------------

def load_taxonomy(path: Path, *, include_sublevels: bool) -> tuple[dict[str, str], str]:
    """Return ({code: name}, prompt_block). Level-3 codes only unless
    include_sublevels (then level-4/5 are added too)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    code_to_name: dict[str, str] = {}
    lines: list[str] = []
    for group_key, techs in data.items():
        if group_key.startswith("_"):
            continue
        lines.append(f"\n# {group_key}")
        for code, name in techs.items():
            depth = code.count("-")  # XXX-NN-NN => 2 ; deeper => 3,4
            if not include_sublevels and depth > 2:
                continue
            code_to_name[code] = name
            lines.append(f"{code}\t{name}")
    return code_to_name, "\n".join(lines)


SYSTEM_TEMPLATE = """당신은 디지털 포렌식 논문 분류 전문가입니다.
주어진 각 논문(제목/키워드/초록)을 아래 '기술 분류 목록'의 기술 코드에 매핑하세요.

규칙:
1) 반드시 목록에 있는 코드만 사용합니다. 목록에 없는 코드/이름은 절대 만들지 마세요.
2) 논문 내용과 직접 관련된 기술만, 관련도가 높은 순으로 최대 3개까지 고릅니다.
3) 어떤 기술과도 무관하거나 디지털 포렌식 논문이 아니면 codes=[] 이고 primary_code="NONE" 입니다.
4) rationale 은 한국어 한 문장(왜 그 기술로 판단했는지).
5) 출력은 지정된 JSON 배열만. 설명 문장이나 코드펜스(```) 없이 JSON 그 자체만 출력합니다.

[기술 분류 목록]
{taxonomy}
"""

USER_TEMPLATE = """다음 논문들을 분류하세요. 각 논문의 [n]은 index입니다.
반드시 아래 형식의 JSON 배열만 출력하세요(모든 index 포함):
[{{"index": 1, "codes": ["CODE", ...], "primary_code": "CODE 또는 NONE", "rationale": "..."}}, ...]

{papers}"""


def build_paper_block(idx: int, paper) -> str:
    title = paper.title or paper.title_kor or paper.title_eng or ""
    abstract = paper.abstract or paper.abstract_kor or paper.abstract_eng or ""
    keywords = ", ".join(paper.keywords) if paper.keywords else ""
    abstract = abstract[:3000]  # guard against very long abstracts
    parts = [f"[{idx}] 제목: {title}"]
    if keywords:
        parts.append(f"키워드: {keywords}")
    parts.append(f"초록: {abstract if abstract else '(초록 없음)'}")
    return "\n".join(parts)


# ----------------------------- results store -----------------------------

def ensure_results_table(db: DatabaseManager) -> None:
    with db.engine.begin() as conn:
        conn.execute(text(
            f"""CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                source_id     TEXT PRIMARY KEY,
                primary_code  TEXT,
                codes_json    TEXT NOT NULL DEFAULT '[]',
                names_json    TEXT NOT NULL DEFAULT '[]',
                rationale     TEXT,
                model         TEXT,
                classified_at TEXT
            )"""
        ))


def load_done_ids(db: DatabaseManager) -> set[str]:
    with db.engine.begin() as conn:
        return {row[0] for row in conn.execute(text(f"SELECT source_id FROM {RESULTS_TABLE}"))}


def save_results(db: DatabaseManager, rows: list[dict]) -> None:
    if not rows:
        return
    with db.engine.begin() as conn:
        conn.execute(
            text(
                f"""INSERT INTO {RESULTS_TABLE}
                    (source_id, primary_code, codes_json, names_json, rationale, model, classified_at)
                    VALUES (:source_id, :primary_code, :codes_json, :names_json, :rationale, :model, :classified_at)
                    ON CONFLICT(source_id) DO UPDATE SET
                        primary_code=excluded.primary_code,
                        codes_json=excluded.codes_json,
                        names_json=excluded.names_json,
                        rationale=excluded.rationale,
                        model=excluded.model,
                        classified_at=excluded.classified_at"""
            ),
            rows,
        )


# ----------------------------- LLM call -----------------------------

def parse_json_array(raw: str) -> list[dict]:
    """Defensive parse: strip code fences, isolate the JSON array."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        s = s.lstrip("json").lstrip("JSON").strip()
    start, end = s.find("["), s.rfind("]")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    data = json.loads(s)
    return data if isinstance(data, list) else []


def is_quota_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 429 or "RESOURCE_EXHAUSTED" in msg or "429" in msg or "QUOTA" in msg


def is_retryable(exc: Exception) -> bool:
    msg = str(exc).upper()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code in (500, 502, 503, 504) or "UNAVAILABLE" in msg or "INTERNAL" in msg or "DEADLINE" in msg


class GeminiClassifier:
    def __init__(self, api_key: str, model: str, system_instruction: str):
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=4096,
        )

    def classify_batch(self, user_prompt: str, *, max_retries: int) -> list[dict]:
        """One API call for a batch. Raises on quota so caller can stop cleanly."""
        attempt = 0
        while True:
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=user_prompt, config=self.config
                )
                return parse_json_array(resp.text or "")
            except Exception as exc:  # noqa: BLE001
                if is_quota_error(exc):
                    raise
                attempt += 1
                if attempt > max_retries or not is_retryable(exc):
                    raise
                backoff = min(60, 2 ** attempt)
                print(f"    retry {attempt}/{max_retries} after {backoff}s ({exc})", flush=True)
                time.sleep(backoff)


class QuotaExhausted(Exception):
    pass


# ----------------------------- main flow -----------------------------

def iter_source_papers(db: DatabaseManager, source_table: str):
    if source_table == "kci_corpus":
        return db.iter_corpus_papers()
    return db.iter_kci_papers()


def source_total(db: DatabaseManager, source_table: str) -> int:
    return db.count_corpus() if source_table == "kci_corpus" else db.count_papers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini 기반 논문→기술분류 자동 매핑")
    parser.add_argument("--source-table", choices=["kci_papers", "kci_corpus"], default="kci_papers",
                        help="분류 대상 테이블 (기본: kci_papers = 수집된 관련 논문)")
    parser.add_argument("--taxonomy", default="filters/tech_taxonomy.json")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=10, help="한 번의 API 호출에 넣을 논문 수")
    parser.add_argument("--rpm", type=float, default=15.0, help="분당 최대 요청 수(무료 티어 스로틀)")
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 분류할 최대 논문 수(일일 쿼터용)")
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--include-sublevels", action="store_true", help="level-4/5 세부기술까지 분류 대상에 포함")
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 첫 배치 프롬프트만 출력")
    parser.add_argument("--list-models", action="store_true", help="사용 가능한 Gemini 모델 id 나열 후 종료")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", "sqlite:///kci_pipeline.db")
    db = DatabaseManager(database_url)

    code_to_name, taxonomy_block = load_taxonomy(Path(args.taxonomy), include_sublevels=args.include_sublevels)
    system_instruction = SYSTEM_TEMPLATE.format(taxonomy=taxonomy_block)
    print(f"[cfg] db={database_url} source={args.source_table} model={args.model} "
          f"taxonomy_codes={len(code_to_name)} batch={args.batch_size} rpm={args.rpm}", flush=True)

    if args.list_models:
        from google import genai
        client = genai.Client(api_key=_require_key())
        for m in client.models.list():
            print(" ", getattr(m, "name", m))
        return 0

    ensure_results_table(db)
    done_ids = load_done_ids(db)
    total = source_total(db, args.source_table)
    remaining_est = max(0, total - len(done_ids))
    if args.limit is not None:
        remaining_est = min(remaining_est, args.limit)
    print(f"[cfg] source total={total} already_classified={len(done_ids)} "
          f"to_classify_this_run≈{remaining_est} "
          f"(≈{-(-remaining_est // args.batch_size)} API calls at {args.rpm} rpm)", flush=True)
    if remaining_est == 0:
        print("Nothing to classify. Done.")
        return 0

    def stream_batches():
        """Yield lists of up to batch_size pending papers, streamed from the DB
        (never materializes the whole table — safe for the 508k corpus)."""
        batch, taken = [], 0
        for paper in iter_source_papers(db, args.source_table):
            if paper.source_id in done_ids:
                continue
            batch.append(paper)
            taken += 1
            if len(batch) >= args.batch_size:
                yield batch
                batch = []
            if args.limit is not None and taken >= args.limit:
                break
        if batch:
            yield batch

    # ---- dry run: show one prompt and exit ----
    if args.dry_run:
        batch = next(iter(stream_batches()), [])
        user_prompt = USER_TEMPLATE.format(
            papers="\n\n".join(build_paper_block(i, p) for i, p in enumerate(batch, 1))
        )
        print("\n===== SYSTEM (truncated) =====")
        print(system_instruction[:800] + "\n...(생략)...")
        print("\n===== USER (first batch) =====")
        print(user_prompt[:3000])
        return 0

    classifier = GeminiClassifier(_require_key(), args.model, system_instruction)
    min_interval = 60.0 / args.rpm if args.rpm > 0 else 0.0

    processed = classified = none_count = 0
    last_call = 0.0
    valid_codes = set(code_to_name)
    try:
        for batch in stream_batches():
            # rate limit
            wait = min_interval - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
            user_prompt = USER_TEMPLATE.format(
                papers="\n\n".join(build_paper_block(i, p) for i, p in enumerate(batch, 1))
            )
            last_call = time.monotonic()
            try:
                results = classifier.classify_batch(user_prompt, max_retries=args.max_retries)
            except Exception as exc:  # noqa: BLE001
                if is_quota_error(exc):
                    raise QuotaExhausted(str(exc)) from exc
                print(f"  batch @ {start} failed permanently ({exc}); skipping (retry next run)", flush=True)
                continue

            by_index = {int(r.get("index", -1)): r for r in results if isinstance(r, dict)}
            rows = []
            now = datetime.now(timezone.utc).isoformat()
            for i, paper in enumerate(batch, 1):
                r = by_index.get(i)
                if r is None:
                    continue  # model omitted this one -> leave unclassified for next run
                codes = [c for c in (r.get("codes") or []) if c in valid_codes]
                primary = r.get("primary_code") or ("NONE" if not codes else codes[0])
                if primary != "NONE" and primary not in valid_codes:
                    primary = codes[0] if codes else "NONE"
                names = [code_to_name[c] for c in codes]
                rows.append({
                    "source_id": paper.source_id,
                    "primary_code": primary,
                    "codes_json": json.dumps(codes, ensure_ascii=False),
                    "names_json": json.dumps(names, ensure_ascii=False),
                    "rationale": (r.get("rationale") or "")[:500],
                    "model": args.model,
                    "classified_at": now,
                })
                classified += 1
                if primary == "NONE":
                    none_count += 1
            save_results(db, rows)
            processed += len(batch)
            print(f"  [{processed}/≈{remaining_est}] stored={len(rows)} "
                  f"(classified={classified} none={none_count})", flush=True)
    except QuotaExhausted as exc:
        print(f"\n[QUOTA] 무료 티어 일일 쿼터 소진으로 중단합니다. 지금까지 저장됨={classified}. "
              f"내일 같은 명령으로 재실행하면 이어서 진행됩니다.\n  detail: {exc}", flush=True)
        return 0
    except KeyboardInterrupt:
        print(f"\n[STOP] 사용자 중단. 저장됨={classified} (재실행 시 이어서 진행).", flush=True)
        return 0

    print(f"\n[DONE] this run classified={classified} none/무관={none_count} "
          f"processed={processed}. 남은 논문은 다음 실행에서 이어집니다.", flush=True)
    return 0


def _require_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        print("ERROR: GEMINI_API_KEY(또는 GOOGLE_API_KEY) 환경변수를 설정하세요.", file=sys.stderr)
        raise SystemExit(2)
    return key


if __name__ == "__main__":
    raise SystemExit(main())
