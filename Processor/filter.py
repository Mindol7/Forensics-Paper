# filter.py #

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Iterable

from Core.normalizer import NormalizedPaper


# Maximum gap (in characters) between adjacent tokens for V3 proximity matching.
# Subsumed by V2 bag-of-words in the default OR composition; kept for tunability.
PROXIMITY_MAX_CHARS: int = 16

_WHITESPACE_RE = re.compile(r"\s+")


def _strip_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def keyword_matches_text(
    keyword_cf: str,
    text_cf: str,
    text_no_space: str | None = None,
    *,
    proximity_max_chars: int = PROXIMITY_MAX_CHARS,
) -> bool:
    """Return True if `keyword_cf` matches `text_cf` via V0 / V1 / V3 (OR).

    Inputs must be casefolded. Pass `text_no_space` if you've precomputed the
    whitespace-stripped text (avoids per-keyword recomputation in hot loops).

    Variants (OR semantics — checked in order; first hit returns True):
      V0 (exact substring):  keyword_cf appears verbatim in text_cf
      V1 (space-collapsed):  keyword with whitespace removed appears in text-no-space
                             (catches "클라우드 포렌식" vs "클라우드포렌식" both ways)
      V3 (proximity):        n tokens all present such that adjacent occurrences in
                             text-order have gap ≤ `proximity_max_chars` (N=16 default).
                             Order-independent: any permutation in text counts.

    V2 (bag-of-words anywhere) is intentionally excluded — see [[keyword-matching-policy]].
    """
    if not keyword_cf or not text_cf:
        return False

    # V0
    if keyword_cf in text_cf:
        return True

    tokens = keyword_cf.split()
    keyword_no_space = "".join(tokens)
    if not keyword_no_space:
        return False

    # V1
    if text_no_space is None:
        text_no_space = _strip_whitespace(text_cf)
    if keyword_no_space in text_no_space:
        return True

    # V3
    if len(tokens) >= 2 and _proximity_match(tokens, text_cf, max_chars=proximity_max_chars):
        return True

    return False


_EXCLUDE_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def exclude_matches_text(term_cf: str, text_cf: str) -> bool:
    """Whole-word match used for exclude terms only.

    Exclude terms must NOT use the fuzzy V0/V1/V3 logic: most are short
    bio/DNA-forensics words ('pcr', 'snp', 'acid', 'panel', 'glass', ...) that
    would otherwise false-match as substrings inside unrelated words — e.g.
    'pcr' inside 'app credential' → 'appcredential', which wrongly drops a
    legitimate digital-forensics paper. A false exclude silently removes a real
    paper, so excludes stay conservative: the term must appear on word
    boundaries (ASCII-alphanumeric on neither side). Korean text and hyphenated
    terms are handled naturally since Korean chars are not [0-9a-z].

    Inputs must be casefolded.
    """
    if not term_cf or not text_cf:
        return False
    pattern = _EXCLUDE_PATTERN_CACHE.get(term_cf)
    if pattern is None:
        pattern = re.compile(rf"(?<![0-9a-z]){re.escape(term_cf)}(?![0-9a-z])")
        _EXCLUDE_PATTERN_CACHE[term_cf] = pattern
    return pattern.search(text_cf) is not None


def _find_all_positions(text: str, token: str) -> list[tuple[int, int]]:
    """Return all (start, end) positions where `token` occurs in `text`,
    including overlapping occurrences. Empty list if not found."""
    if not token:
        return []
    positions: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text.find(token, start)
        if idx == -1:
            break
        positions.append((idx, idx + len(token)))
        start = idx + 1
    return positions


def _proximity_match(
    tokens: list[str],
    text_cf: str,
    *,
    max_chars: int = PROXIMITY_MAX_CHARS,
) -> bool:
    """V3: tokens all present in `text_cf` such that, when sorted by text position,
    every adjacent-pair gap (chars between end_i and start_{i+1}) is ≤ max_chars.

    Order-independent — permutations of token-to-occurrence assignments are considered.
    Examples (max_chars=16):
        tokens=["클라우드","포렌식"], text="클라우드 포렌식"        → True (gap=1)
        tokens=["클라우드","포렌식"], text="포렌식 환경 클라우드"     → True (reverse order)
        tokens=["클라우드","포렌식"], text="클라우드 <80자> 포렌식"  → False (gap too big)
        tokens=["aaa","bbb","ccc"],  text="ccc xxx bbb yyy aaa"   → True (permuted)
    """
    if not tokens or not text_cf:
        return False
    if len(tokens) == 1:
        return tokens[0] in text_cf

    positions_per_token: list[list[tuple[int, int]]] = []
    for token in tokens:
        spans = _find_all_positions(text_cf, token)
        if not spans:
            return False
        positions_per_token.append(spans)

    # Try every combination of one occurrence per token.
    for combo in product(*positions_per_token):
        # Reject combos that reuse the exact same span across two tokens.
        if len(set(combo)) != len(combo):
            continue
        ordered = sorted(combo, key=lambda span: span[0])
        valid = True
        for i in range(len(ordered) - 1):
            gap = ordered[i + 1][0] - ordered[i][1]
            # Overlap (gap < 0) is acceptable; only too-large positive gaps fail.
            if gap > max_chars:
                valid = False
                break
        if valid:
            return True

    return False


STRONG_TERMS = {
    "디지털 포렌식": 4.0,
    "디지털포렌식": 4.0,
    "digital forensic": 4.0,
    "digital forensics": 4.0,
    "mobile forensic": 3.5,
    "모바일 포렌식": 3.5,
    "memory forensic": 3.5,
    "메모리 포렌식": 3.5,
    "network forensic": 3.0,
    "네트워크 포렌식": 3.0,
    "disk forensic": 3.0,
    "dfir": 3.0,
    "incident response": 2.5,
    "artifact": 1.0,
    "forensic artifact": 2.0,
    "malware forensic": 2.0,
    "malware analysis": 1.5,
}

WEAK_TERMS = {
    "포렌식": 1.5,
    "forensic": 1.5,
    "사이버": 0.5,
    "cyber": 0.5,
    "로그": 0.5,
    "log": 0.5,
    "증거": 0.5,
    "evidence": 0.5,
}

EXCLUDE_TERMS = {
    "법의학",
    "forensic psychiatry",
    "forensic medicine",
    "forensic nursing",
    "legal medicine",
}

# 점수 계산
def _score_text(text: str | None) -> tuple[float, list[str]]:
    if not text:
        return 0.0, []
    lowered = text.casefold()
    score = 0.0
    reasons: list[str] = []

    for term in EXCLUDE_TERMS:
        if term.casefold() in lowered:
            score -= 3.0
            reasons.append(f"exclude:{term}")

    for term, weight in STRONG_TERMS.items():
        if term.casefold() in lowered:
            score += weight
            reasons.append(f"strong:{term}")

    for term, weight in WEAK_TERMS.items():
        if term.casefold() in lowered:
            score += weight
            reasons.append(f"weak:{term}")

    return score, reasons

# 최종 점수 판단
def apply_rule_based_filter(paper: NormalizedPaper, *, min_score: float = 3.0) -> NormalizedPaper:
    title_score, title_reasons = _score_text(paper.title)
    abstract_score, abstract_reasons = _score_text(paper.abstract)
    keywords_score, keywords_reasons = _score_text(" ".join(paper.keywords))

    score = title_score * 1.5 + abstract_score + keywords_score * 1.2
    reasons = title_reasons + abstract_reasons + keywords_reasons

    paper.relevance_score = round(score, 2)
    paper.relevance_reasons = reasons
    paper.is_relevant = score >= min_score
    return paper


def filter_digital_forensics_papers(
    papers: Iterable[NormalizedPaper],
    *, # *이후의 인자들은 반드시 키워드 인자로만 전달해야함
    min_score: float = 3.0,
    keep_irrelevant: bool = False,
) -> list[NormalizedPaper]:
    result: list[NormalizedPaper] = []
    for paper in papers:
        enriched = apply_rule_based_filter(paper, min_score=min_score)
        if keep_irrelevant or enriched.is_relevant:
            result.append(enriched)
    return result


# ---------- Keyword-based category filter (filters/keywords_*.json) ----------

KEYWORD_MATCH_MODES: tuple[str, ...] = (
    "direct",
    "anchored",
    "anchored_digital_forensic",
)

KEYWORD_CONFIG_META_KEYS: set[str] = {
    "search_config",
    "exclude",
    "exclude_any",
    "include_any",
}

DEFAULT_ANCHOR_MODES: dict[str, tuple[str, ...]] = {
    "anchored": ("포렌식", "forensic", "forensics"),
    "anchored_digital_forensic": (
        "디지털포렌식",
        "디지털 포렌식",
        "digital forensic",
        "digital forensics",
    ),
}


@dataclass(frozen=True)
class KeywordRule:
    category: str
    keyword: str
    mode: str
    taxonomy_path: tuple[str, ...]
    anchor_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeywordFilterConfig:
    rules: tuple[KeywordRule, ...]
    exclude_any: tuple[str, ...]

    @property
    def include_any(self) -> tuple[str, ...]:
        """Unique taxonomy keywords used as candidate search terms."""
        return _unique_normalized(rule.keyword for rule in self.rules)

    @property
    def direct_keywords(self) -> tuple[str, ...]:
        """Taxonomy keywords whose rule mode is `direct` (no anchor required).

        Used as the Path-1 server keyword-search set. Broad `anchored` terms
        ('ai', 'app', 'cloud', 'artificial intelligence', ...) match tens of
        thousands of non-forensic papers in KCI's keyword index, all later
        rejected for lacking the forensic anchor — searching them collects ~100k
        noise papers and stalls classification. Their genuine matches are still
        recovered via the sweep (title/abstract) plus post-enrichment
        re-classification of the keyword field.
        """
        return _unique_normalized(rule.keyword for rule in self.rules if rule.mode == "direct")

    @property
    def categories(self) -> tuple[str, ...]:
        return _unique_normalized(rule.category for rule in self.rules)


def _flatten_keyword_section(value: object) -> list[str]:
    if isinstance(value, dict):
        flat: list[str] = []
        for sub in value.values():
            flat.extend(_flatten_keyword_section(sub))
        return flat
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _normalize_keyword(value: str) -> str:
    return " ".join(value.strip().split())


def _unique_normalized(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _normalize_keyword(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return tuple(out)


def _load_anchor_modes(payload: dict[str, object]) -> dict[str, tuple[str, ...]]:
    search_config = payload.get("search_config")
    anchor_modes: dict[str, tuple[str, ...]] = dict(DEFAULT_ANCHOR_MODES)
    if not isinstance(search_config, dict):
        return anchor_modes

    raw_anchor_modes = search_config.get("anchor_modes")
    if not isinstance(raw_anchor_modes, dict):
        return anchor_modes

    for mode, values in raw_anchor_modes.items():
        if not isinstance(mode, str):
            continue
        terms = _unique_normalized(_flatten_keyword_section(values))
        if terms:
            anchor_modes[mode] = terms
    return anchor_modes


def _iter_keyword_rules(
    node: object,
    *,
    path: tuple[str, ...],
    anchor_modes: dict[str, tuple[str, ...]],
) -> Iterable[KeywordRule]:
    if not isinstance(node, dict):
        return

    present_modes = [mode for mode in KEYWORD_MATCH_MODES if mode in node]
    if present_modes:
        category = path[-1] if path else "미분류"
        for mode in present_modes:
            keywords = _unique_normalized(_flatten_keyword_section(node.get(mode)))
            anchor_terms = () if mode == "direct" else anchor_modes.get(mode, ())
            for keyword in keywords:
                yield KeywordRule(
                    category=category,
                    keyword=keyword,
                    mode=mode,
                    taxonomy_path=path,
                    anchor_terms=anchor_terms,
                )
        return

    for key, child in node.items():
        if not isinstance(key, str) or key in KEYWORD_CONFIG_META_KEYS:
            continue
        yield from _iter_keyword_rules(
            child,
            path=path + (key,),
            anchor_modes=anchor_modes,
        )


def _legacy_include_rules(payload: dict[str, object]) -> tuple[KeywordRule, ...]:
    keywords = _unique_normalized(_flatten_keyword_section(payload.get("include_any")))
    return tuple(
        KeywordRule(
            category="키워드 매칭",
            keyword=keyword,
            mode="direct",
            taxonomy_path=("키워드 매칭",),
        )
        for keyword in keywords
    )


def load_keyword_filter_configs(paths: list[Path]) -> KeywordFilterConfig:
    """Load and merge multiple keyword taxonomy JSON files."""
    rules: list[KeywordRule] = []
    exclude: list[str] = []
    seen_rules: set[tuple[str, str, str, tuple[str, ...]]] = set()
    seen_exc: set[str] = set()
    for path in paths:
        cfg = load_keyword_filter_config(path)
        for rule in cfg.rules:
            key = (
                rule.category.casefold(),
                rule.keyword.casefold(),
                rule.mode.casefold(),
                tuple(anchor.casefold() for anchor in rule.anchor_terms),
            )
            if key not in seen_rules:
                seen_rules.add(key)
                rules.append(rule)
        for term in cfg.exclude_any:
            key = term.casefold()
            if key not in seen_exc:
                seen_exc.add(key)
                exclude.append(term)
    return KeywordFilterConfig(rules=tuple(rules), exclude_any=tuple(exclude))


def load_keyword_filter_config(path: Path) -> KeywordFilterConfig:
    """Load category-aware keyword rules from a JSON file."""
    if not path.exists():
        return KeywordFilterConfig(rules=(), exclude_any=())
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Keyword filter file must be a JSON object: {path}")

    anchor_modes = _load_anchor_modes(payload)
    rules = list(_iter_keyword_rules(payload, path=(), anchor_modes=anchor_modes))
    if not rules:
        rules.extend(_legacy_include_rules(payload))

    return KeywordFilterConfig(
        rules=tuple(rules),
        exclude_any=_unique_normalized(
            _flatten_keyword_section(payload.get("exclude"))
            + _flatten_keyword_section(payload.get("exclude_any"))
        ),
    )


MATCH_FIELD_ORDER: tuple[str, ...] = ("title", "keyword", "abstract")


def _field_texts(paper: NormalizedPaper) -> dict[str, str]:
    """Group searchable text by field for priority matching."""
    title_parts = [
        v for v in (paper.title, paper.title_kor, paper.title_eng, paper.title_other) if v
    ]
    abstract_parts = [
        v for v in (paper.abstract, paper.abstract_kor, paper.abstract_eng, paper.abstract_other) if v
    ]
    keyword_parts = [
        v for v in (paper.keyword_text_kor, paper.keyword_text_eng, paper.keyword_text_other) if v
    ]
    if paper.keywords:
        keyword_parts.append(" ".join(paper.keywords))
    return {
        "title": " ".join(title_parts).casefold(),
        "keyword": " ".join(keyword_parts).casefold(),
        "abstract": " ".join(abstract_parts).casefold(),
    }


def apply_keyword_filter(
    paper: NormalizedPaper,
    config: KeywordFilterConfig,
) -> NormalizedPaper:
    """Match taxonomy rules against title → keyword → abstract in priority order.

    `direct` rules match on their own. `anchored` rules also require a configured
    forensic/forensics anchor somewhere in the paper text, and
    `anchored_digital_forensic` rules require the stricter digital forensic(s)
    anchor. Excludes are evaluated against the combined text.
    """
    fields = _field_texts(paper)
    fields_no_space = {name: _strip_whitespace(text) for name, text in fields.items()}

    combined = " ".join(fields.values())
    combined_no_space = _strip_whitespace(combined)
    matched_exclude = [
        t
        for t in config.exclude_any
        if exclude_matches_text(t.casefold(), combined)
    ]

    match_field: str | None = None
    matched_rules: list[KeywordRule] = []
    for field_name in MATCH_FIELD_ORDER:
        text = fields[field_name]
        if not text:
            continue
        text_ns = fields_no_space[field_name]
        hits: list[KeywordRule] = []
        for rule in config.rules:
            if rule.anchor_terms and not any(
                keyword_matches_text(anchor.casefold(), combined, combined_no_space)
                for anchor in rule.anchor_terms
            ):
                continue
            if keyword_matches_text(rule.keyword.casefold(), text, text_ns):
                hits.append(rule)
        if hits:
            match_field = field_name
            matched_rules = hits
            break

    matched_categories = _unique_normalized(rule.category for rule in matched_rules)
    matched_keywords = _unique_normalized(rule.keyword for rule in matched_rules)

    reasons: list[str] = []
    if match_field:
        reasons.append(f"matched_in:{match_field}")
        for rule in matched_rules:
            reasons.append(f"{match_field}:{rule.keyword}")
            reasons.append(f"category:{rule.category}")
            reasons.append(f"mode:{rule.mode}")
    reasons.extend(f"exclude:{t}" for t in matched_exclude)

    paper.categories = list(matched_categories)
    paper.matched_keywords = list(matched_keywords)
    paper.relevance_reasons = reasons
    paper.relevance_score = float(len(matched_keywords) - len(matched_exclude))
    paper.is_relevant = bool(match_field) and not matched_exclude
    return paper


def filter_papers_by_keywords(
    papers: Iterable[NormalizedPaper],
    config: KeywordFilterConfig,
    *,
    keep_irrelevant: bool = False,
) -> list[NormalizedPaper]:
    result: list[NormalizedPaper] = []
    for paper in papers:
        enriched = apply_keyword_filter(paper, config)
        if keep_irrelevant or enriched.is_relevant:
            result.append(enriched)
    return result
