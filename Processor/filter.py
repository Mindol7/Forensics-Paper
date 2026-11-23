# filter.py #

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from Core.normalizer import NormalizedPaper


# Maximum gap (in characters) between adjacent tokens for V3 proximity matching.
# Subsumed by V2 bag-of-words in the default OR composition; kept for tunability.
PROXIMITY_MAX_CHARS: int = 16

_WHITESPACE_RE = re.compile(r"\s+")

# Latin tokens up to this length must match whole words; longer ones need only
# start on a word boundary. See [[_token_pattern]].
SHORT_ASCII_MAX_CHARS: int = 4

_ASCII_TOKEN_RE = re.compile(r"^[0-9a-z]+$")
_TOKEN_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _strip_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def _is_ascii_token(token_cf: str) -> bool:
    """True for tokens written purely in Latin letters/digits.

    Korean keywords are excluded on purpose: Korean is written without spaces,
    so substring matching is exactly what makes '클라우드포렌식' match
    '클라우드 포렌식'. Only Latin tokens need word-boundary discipline.
    """
    return bool(_ASCII_TOKEN_RE.match(token_cf))


def _token_pattern(token_cf: str) -> "re.Pattern[str]":
    """Cached positional pattern for a Latin keyword token.

    Plain substring matching lets a token land anywhere inside a longer word,
    which is how 'ai' matched det(ai)l and 'logical' matched psycho(logical) —
    and once V3 proximity pairs two such hits ('ai artifact' across
    "Detail ... Metallic Artifact") the rule fires on unrelated fields. The
    boundary requirement is split by length:

      ≤ SHORT_ASCII_MAX_CHARS  both sides, plural 's' tolerated — short tokens
                               are too generic to survive any affix, so 'app'
                               matches 'app'/'apps' but never '(app)ear'
      longer                   left side only, so inflections still match
                               ('collect' → 'collected') while a token buried at
                               the end of another word does not (psycho|logical,
                               block|chain, multi|media)
    """
    pattern = _TOKEN_PATTERN_CACHE.get(token_cf)
    if pattern is None:
        pattern = re.compile(rf"(?<![0-9a-z]){re.escape(token_cf)}{_token_tail(token_cf)}")
        _TOKEN_PATTERN_CACHE[token_cf] = pattern
    return pattern


def _token_tail(token_cf: str) -> str:
    """Trailing constraint for a Latin token — see [[_token_pattern]]."""
    return r"s?(?![0-9a-z])" if len(token_cf) <= SHORT_ASCII_MAX_CHARS else ""


def _phrase_pattern(keyword_cf: str) -> "re.Pattern[str]":
    """Cached adjacency pattern for an all-Latin keyword, covering V0 and V1.

    Both variants are plain substring tests, which a boundary-checked token
    cannot rely on: 'logical data collection' is literally inside
    "psycho(logical data collection)", and collapsing spaces for V1 only widens
    that. Joining the tokens with `\\s*` accepts the verbatim phrase and the
    space-collapsed form at once, while the leading boundary keeps the phrase
    from starting mid-word.
    """
    pattern = _TOKEN_PATTERN_CACHE.get(keyword_cf)
    if pattern is None:
        tokens = keyword_cf.split()
        body = r"\s*".join(re.escape(token) for token in tokens)
        pattern = re.compile(rf"(?<![0-9a-z]){body}{_token_tail(tokens[-1])}")
        _TOKEN_PATTERN_CACHE[keyword_cf] = pattern
    return pattern


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

    tokens = keyword_cf.split()
    if not tokens:
        return False

    keyword_no_space = "".join(tokens)
    if text_no_space is None:
        text_no_space = _strip_whitespace(text_cf)

    # V0 + V1
    if all(_is_ascii_token(token) for token in tokens):
        # One boundary-checked pattern stands in for both: see [[_phrase_pattern]].
        # Collapsing the text removes exactly the whitespace that pattern spans,
        # so a hit implies `keyword_no_space in text_no_space` — screening on that
        # first keeps the leading lookbehind (which defeats the regex engine's
        # substring skipping) off the ~99% of pairs that cannot match at all.
        if keyword_no_space in text_no_space and _phrase_pattern(keyword_cf).search(text_cf):
            return True
    else:
        # V0
        if keyword_cf in text_cf:
            return True
        # V1
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
    including overlapping occurrences. Empty list if not found.

    Latin tokens are located by their boundary pattern — see [[_token_pattern]].
    Two such matches cannot start at the same offset, so `finditer` loses only
    overlaps that could never form a distinct proximity hit anyway.
    """
    if not token:
        return []
    if _is_ascii_token(token):
        # The pattern still requires the literal token, so this cheap scan
        # rejects non-candidates before the regex runs — see [[keyword_matches_text]].
        if token not in text:
            return []
        return [match.span() for match in _token_pattern(token).finditer(text)]
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
        return bool(_find_all_positions(text_cf, tokens[0]))

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


# ---------- KCI application-level selection policy ----------

KCI_FORENSIC_SOCIETY_NAMES = ("한국디지털포렌식학회",)
KCI_FORENSIC_SOCIETY_JOURNALS = ("디지털포렌식연구",)
KCI_SOCIETY_COLLECTION_REASON = (
    "제공된 키워드셋은 아니지만, 한국디지털포렌식 학회 논문이기에 수집해옴"
)
KCI_SOCIETY_COLLECTION_CATEGORY = "한국디지털포렌식학회"
KCI_SOCIETY_COLLECTION_KEYWORD = "학회 수집"
ORG_LEGAL_PREFIXES = (
    "사단법인",
    "재단법인",
    "공익사단법인",
    "공익재단법인",
)


def _is_kci_society_raw_paper(paper: Any) -> bool:
    publisher = (getattr(paper, "publisher_name", None) or "").casefold()
    journal = (getattr(paper, "journal_name", None) or "").casefold()
    return any(
        name.casefold() in publisher for name in KCI_FORENSIC_SOCIETY_NAMES
    ) or any(
        name.casefold() in journal for name in KCI_FORENSIC_SOCIETY_JOURNALS
    )


def _is_kci_society_paper(paper: NormalizedPaper) -> bool:
    publisher = (paper.institution_id or "").casefold()
    journal = (paper.journal_id or "").casefold()
    return any(
        name.casefold() in publisher for name in KCI_FORENSIC_SOCIETY_NAMES
    ) or any(
        name.casefold() in journal for name in KCI_FORENSIC_SOCIETY_JOURNALS
    )


def load_kci_blacklist(path: Path) -> tuple[str, ...]:
    """Load unique, non-comment organization names from the blacklist."""

    if not path.exists():
        return ()
    terms: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            cleaned = " ".join(line.strip().split())
            if not cleaned or cleaned.startswith("#"):
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(cleaned)
    return tuple(terms)


def _canonical_org_name(value: str) -> str:
    text = re.sub(r"\([^)]*\)|（[^）]*）", " ", value.casefold())
    for prefix in ORG_LEGAL_PREFIXES:
        text = text.replace(prefix.casefold(), "")
    return "".join(character for character in text if character.isalnum())


def matches_kci_blacklist(
    text_parts: Iterable[str | None],
    blacklist_terms: tuple[str, ...],
) -> bool:
    """Match complete canonical organization names, never fragments."""

    if not blacklist_terms:
        return False
    names = {_canonical_org_name(part) for part in text_parts if part}
    names.discard("")
    if not names:
        return False
    return any(
        _canonical_org_name(term) in names for term in blacklist_terms
    )


def _is_blacklisted_raw_paper(
    paper: Any,
    blacklist_terms: tuple[str, ...],
) -> bool:
    if _is_kci_society_raw_paper(paper):
        return False
    return matches_kci_blacklist(
        (
            getattr(paper, "publisher_name", None),
            getattr(paper, "journal_name", None),
        ),
        blacklist_terms,
    )


def _is_blacklisted_kci_paper(
    paper: NormalizedPaper,
    blacklist_terms: tuple[str, ...],
) -> bool:
    if _is_kci_society_paper(paper):
        return False
    return matches_kci_blacklist(
        (paper.institution_id, paper.journal_id),
        blacklist_terms,
    )


def filter_blacklisted_kci_papers(
    papers: Iterable[NormalizedPaper],
    blacklist_terms: tuple[str, ...],
) -> list[NormalizedPaper]:
    rows = list(papers)
    if not blacklist_terms:
        return rows
    return [
        paper
        for paper in rows
        if not _is_blacklisted_kci_paper(paper, blacklist_terms)
    ]


def _matches_terms(text_parts: Iterable[str | None], terms: list[str]) -> bool:
    haystack = " ".join(part or "" for part in text_parts).casefold()
    if not haystack.strip() or not terms:
        return False
    haystack_no_space = "".join(haystack.split())
    return any(
        keyword_matches_text(term, haystack, haystack_no_space)
        for term in terms
    )


def _matches_exclude_terms(
    text_parts: Iterable[str | None],
    terms: list[str],
) -> bool:
    haystack = " ".join(part or "" for part in text_parts).casefold()
    if not haystack.strip() or not terms:
        return False
    return any(exclude_matches_text(term, haystack) for term in terms)


def filter_kci_sweep_candidates(
    papers: Iterable[Any],
    config: KeywordFilterConfig,
    blacklist_terms: tuple[str, ...],
) -> list[Any]:
    """Apply society override, blacklist, then local include/exclude rules."""

    include_terms = [term.casefold() for term in config.include_any]
    exclude_terms = [term.casefold() for term in config.exclude_any]
    kept: list[Any] = []
    for paper in papers:
        if _is_kci_society_raw_paper(paper):
            paper.matched_query = KCI_FORENSIC_SOCIETY_NAMES[0]
            paper.matched_field = "journal"
            kept.append(paper)
            continue
        if _is_blacklisted_raw_paper(paper, blacklist_terms):
            continue
        searchable_parts = (
            getattr(paper, "title_original", None),
            getattr(paper, "title_english", None),
            getattr(paper, "title_foreign", None),
            " ".join(getattr(paper, "keywords", None) or ()),
            getattr(paper, "abstract_original", None),
            getattr(paper, "abstract_english", None),
        )
        if _matches_exclude_terms(searchable_parts, exclude_terms):
            continue
        if _matches_terms(searchable_parts, include_terms):
            kept.append(paper)
    return kept


def filter_kci_year_range(
    papers: Iterable[NormalizedPaper],
    *,
    start_year: int,
    end_year: int,
) -> list[NormalizedPaper]:
    return [
        paper
        for paper in papers
        if paper.publication_year is not None
        and start_year <= paper.publication_year <= end_year
    ]


def _set_kci_classification(
    paper: NormalizedPaper,
    *,
    categories: Iterable[str],
    matched_keywords: Iterable[str],
    reasons: Iterable[str] | None = None,
) -> None:
    paper.categories = [value for value in categories if value and value != "*"]
    paper.matched_keywords = [
        value for value in matched_keywords if value and value != "*"
    ]
    if reasons is not None:
        paper.relevance_reasons = [
            value for value in reasons if value and value != "*"
        ]


def classify_kci_relevance(
    papers: Iterable[NormalizedPaper],
    keyword_config: KeywordFilterConfig,
    breakdown: dict[str, int],
) -> list[NormalizedPaper]:
    """Classify locally by field priority plus the KDFS society override."""

    relevant: list[NormalizedPaper] = []
    for paper in papers:
        apply_keyword_filter(paper, keyword_config)
        if paper.is_relevant:
            if not paper.categories or not paper.matched_keywords:
                continue
            relevant.append(paper)
            for reason in paper.relevance_reasons:
                if not reason.startswith("matched_in:"):
                    continue
                field_name = reason.split(":", 1)[1]
                if field_name in breakdown:
                    breakdown[field_name] += 1
                break
            continue
        if not _is_kci_society_paper(paper):
            continue
        paper.is_relevant = True
        paper.relevance_score = 1.0
        _set_kci_classification(
            paper,
            categories=(KCI_SOCIETY_COLLECTION_CATEGORY,),
            matched_keywords=(KCI_SOCIETY_COLLECTION_KEYWORD,),
            reasons=(KCI_SOCIETY_COLLECTION_REASON,),
        )
        breakdown["journal"] = breakdown.get("journal", 0) + 1
        relevant.append(paper)
    return relevant


__all__ = [
    "DEFAULT_ANCHOR_MODES",
    "EXCLUDE_TERMS",
    "KCI_FORENSIC_SOCIETY_JOURNALS",
    "KCI_FORENSIC_SOCIETY_NAMES",
    "KCI_SOCIETY_COLLECTION_CATEGORY",
    "KCI_SOCIETY_COLLECTION_KEYWORD",
    "KCI_SOCIETY_COLLECTION_REASON",
    "KEYWORD_CONFIG_META_KEYS",
    "KEYWORD_MATCH_MODES",
    "KeywordFilterConfig",
    "KeywordRule",
    "MATCH_FIELD_ORDER",
    "PROXIMITY_MAX_CHARS",
    "SHORT_ASCII_MAX_CHARS",
    "STRONG_TERMS",
    "WEAK_TERMS",
    "apply_keyword_filter",
    "apply_rule_based_filter",
    "classify_kci_relevance",
    "exclude_matches_text",
    "filter_blacklisted_kci_papers",
    "filter_digital_forensics_papers",
    "filter_kci_sweep_candidates",
    "filter_kci_year_range",
    "filter_papers_by_keywords",
    "keyword_matches_text",
    "load_kci_blacklist",
    "load_keyword_filter_config",
    "load_keyword_filter_configs",
    "matches_kci_blacklist",
]
