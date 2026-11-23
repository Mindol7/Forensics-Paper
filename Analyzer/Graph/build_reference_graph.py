#!/usr/bin/env python3
"""Build an explainable reference/topic graph over the bilingual Markdown corpus.

Directed citations are copied verbatim from the reviewed author-graph compiler.
Undirected similarity is an explicitly separate, local-corpus ranking signal:
weighted bilingual TF-IDF cosine and structured Markdown topic fields. Shared
references and internal citing papers are contextual evidence, not topic scores.
Only the Python standard library is required; no remote model or API is used.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import itertools
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Analyzer.Graph.build_author_graph import (  # noqa: E402
    DEFAULT_DATABASE,
    DEFAULT_SOURCE,
    DOI_RE,
    CorpusValidationError,
    ParsedPaper,
    _file_sha256,
    _write_json_atomic,
    compile_graph as compile_author_graph,
    normalize_doi,
    normalize_title,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "Analyzer" / "Graph" / "out" / "reference-graph.json"
DEFAULT_AUDIT = PROJECT_ROOT / "Analyzer/Graph/audit/reference_graph_audit.json"
ALGORITHM_VERSION = "md-structured-topic-similarity-v2"
TOKEN_RE = re.compile(r"[a-zà-öø-ÿ][a-z0-9à-öø-ÿ]*(?:[.+#-][a-z0-9à-öø-ÿ]+)*|[가-힣]{2,}")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")
STOPWORDS = frozenset("""
a about above across after again against all also am among an and any are as at
be because been before being below between both but by can cannot could did do
does doing done down during each either etc even few first for from further had
has have having hence here how however if in into is it its itself just many may
might more most much must my near need neither new no nor not now of off on once
one only or other our ours out over own per same second several shall should so
some such than that the their them themselves then there these they third this
those through thus to together too toward towards under until up upon us use
used using uses various very via was we well were what when where whether which
while who whom whose why will with within without would you your
paper papers study studies research result results method methods proposed
propose proposes provide provides provided present presents presented approach
approaches analysis analyze analyzed analysing analyzing investigate investigated
investigation investigations show shown shows based based-on including include
includes included consider considered regarding related respectively order able
possible particular therefore specifically important developed development
der die das des dem den ein eine einer eines einem einen und oder aber auch als
auf aus bei bis durch für im ist mit nach nicht sich sind über um vom von vor
wird werden zu zum zur dass sowie diese dieser dieses diesem diesen es sie er
habe haben hat hatte wenn doch muss müssen wurde wurden würde wäre sein seiner
seine seinem seinen eines einer noch nur schon soll sollen kann können mehr
digital forensic forensics evidence evidences information data example examples
abstract keywords keyword introduction conclusion conclusions na none null
본 논문 논문은 논문에서 연구 연구는 연구에서 연구를 연구의 결과 결과를 결과는
분석 분석을 분석한 분석하고 분석하여 분석하였다 제안 제안한 제안하는 제안하고
제시 제시한 제시하고 제시하였다 통해 통하여 위한 위해 대한 대해서 대해 따른
있다 있다면 있는 있으며 있었다 없다 없는 한다 하여 하였다 하는 하고 또한
그리고 그러나 하지만 따라서 이러한 이를 이는 이와 위해서 해당 관련 여러
경우 경우에는 경우에 것은 것을 것으로 것을 수가 그 그에 더 등 및 의 이 가
디지털 포렌식 포렌식을 포렌식의 디지털포렌식 디지털포렌식을 증거 증거를 정보
정보를 데이터 데이터를 사용 사용하는 사용하여 활용 활용한 활용하여 방법 방법을
기존 기존의 새로운 이용 이용한 이용하여 기반 기반으로 기반한 대상으로 각각
때문에 때문에만 필요 필요하다 목적 목적으로 확인 확인하였다 있도록 있도록한
가능 가능하다 다양한 많은 중요한 최근 최근에는 일반적으로 특히 그러므로
없음 미상 미확인 해당없음 초록 키워드
""".split())
# Only remove grammatical endings from long Korean tokens. This is a small,
# disclosed lexical normalizer, not a Korean morphological/semantic model.
KOREAN_SUFFIXES = (
    "으로부터", "에서는", "으로는", "에서도", "들에게", "으로써", "으로서",
    "에서의", "에대한", "으로", "에서", "에게", "에는", "까지", "부터",
    "처럼", "보다", "들의", "들을", "들이", "들은", "와의", "과의",
    "에는", "의", "을", "를", "은", "는", "이", "가", "에", "와", "과", "도",
)


@dataclass(frozen=True)
class SimilarityConfig:
    top_k: int = 8
    min_score: float = 0.20
    min_text_cosine: float = 0.14
    text_weight: float = 0.40
    tags_weight: float = 0.20
    applications_weight: float = 0.15
    techniques_weight: float = 0.25
    max_document_frequency: float = 0.60

    def validate(self) -> None:
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or not 1 <= self.top_k <= 100:
            raise CorpusValidationError("top_k must be an integer between 1 and 100")
        for name in ("min_score", "min_text_cosine", "max_document_frequency"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise CorpusValidationError(f"{name} must be finite and in (0, 1]")
        weights = (self.text_weight, self.tags_weight, self.applications_weight, self.techniques_weight)
        if any(not math.isfinite(value) or value < 0 for value in weights) or not math.isclose(sum(weights), 1):
            raise CorpusValidationError("similarity weights must be nonnegative and sum to one")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _inputs_digest(inputs: Mapping[str, str | None]) -> str:
    digest = hashlib.sha256()
    for key, value in sorted(inputs.items()):
        digest.update(key.encode("utf-8") + b"\0")
        digest.update((value if value is not None else "none").encode("ascii") + b"\0")
    return digest.hexdigest()


def clean_text(value: str) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", value))
    text = re.sub(r"<[^>]*>", " ", text)
    text = URL_RE.sub(" ", text)
    text = re.sub(r"(?i)\b(?:N\s*/\s*A|null|none|not available|not applicable)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: str) -> list[str]:
    tokens = []
    for token in TOKEN_RE.findall(clean_text(value).casefold()):
        if token in STOPWORDS:
            continue
        if re.fullmatch(r"[가-힣]+", token):
            for suffix in KOREAN_SUFFIXES:
                if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                    token = token[:-len(suffix)]
                    break
        elif token.endswith("s") and len(token) > 4 and not token.endswith(("ss", "is", "us", "os")):
            # Plural normalization only; avoid corrupting Windows/iOS/macOS.
            if token not in {"windows", "https", "ntfs", "apfs", "hdfs"}:
                token = token[:-1]
        if len(token) >= 2 and token not in STOPWORDS:
            tokens.append(token)
    return tokens


def _section(text: str, number: int) -> str:
    return clean_text(_raw_section(text, number))


def _raw_section(text: str, number: int) -> str:
    # Fail loudly: a renumbered template used to silently yield an empty
    # section here, which quietly emptied a whole similarity dimension.
    match = re.search(rf"(?m)^##\s+{number}\.\s+[^\n]*\n", text)
    if not match:
        raise CorpusValidationError(f"Markdown has no section {number}")
    following = text[match.end():]
    return re.split(r"(?m)^##\s+", following, maxsplit=1)[0]


TOPIC_COMPONENTS = ("text", "tags", "applications", "techniques")
TAG_DIMENSIONS = ("DEV", "FS", "OS", "APP", "CLOUD")
# Broad platform tags that almost every paper carries; on their own they cannot
# establish topic similarity. ``CLOUD-SaaS:*`` names a service flavour and is
# therefore specific, unlike the bare delivery models.
GENERIC_TAGS = frozenset({"DEV-PC", "DEV-MOB", "OS-WIN", "OS-LNX", "OS-MAC", "OS-AND", "OS-IOS", "OS-ETC", "CLOUD-DaaS", "CLOUD-IaaS"})
GENERIC_TOPIC_TERMS = frozenset("windows linux macos android ios pc mobile computer smartphone operating system application applications app apps software device devices platform cloud database internet network digital forensic forensics artifact artifacts crime criminal environment record user additionally recently 데이터베이스 시스템 플랫폼 운영체제 컴퓨터 스마트폰 모바일 애플리케이션 어플리케이션 프로그램 클라우드 인터넷 네트워크 사용자 사용자의 파일 아티팩트 흔적 범죄 범죄에 환경 환경에서 분석 방법 연구".split())

# Explicit identity aliases only. Different products (Telegram X vs Telegram,
# Wickr Pro vs Wickr Me, Office vs Word) are deliberately NOT collapsed.
APP_ALIAS_GROUPS = {
    "KakaoTalk": ("kakao talk", "카카오톡"),
    "WhatsApp": ("whats app", "왓츠앱"),
    "Google Chrome": ("chrome", "구글 크롬", "크롬"),
    "Microsoft Edge": ("edge", "마이크로소프트 엣지"),
    "Mozilla Firefox": ("firefox", "파이어폭스"),
    "NAVER Whale": ("whale", "네이버 웨일"),
    "Microsoft Teams": ("teams", "마이크로소프트 팀즈"),
    "Google Drive": ("구글 드라이브",),
    "OneDrive": ("microsoft onedrive", "원드라이브"),
    "Dropbox": ("드롭박스",),
    "Slack": ("슬랙",),
    "Telegram": ("텔레그램",),
    "WeChat": ("위챗",),
    "네이버 메일": ("naver mail",),
}


def _identity(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(unicodedata.normalize("NFKC", value))).strip().casefold()


APP_ALIASES = {_identity(alias): canonical for canonical, aliases in APP_ALIAS_GROUPS.items() for alias in (canonical, *aliases)}


def normalize_app(value: str) -> str:
    """Normalize Unicode/case and explicit aliases, never fuzzy substrings."""
    cleaned = clean_text(value).strip(" *`")
    return APP_ALIASES.get(_identity(cleaned), _identity(cleaned))


def parse_target_apps(content: str) -> list[str]:
    """Application names from the section-4 table, ignoring the registry id."""
    names = []
    for row in _table_rows(_raw_section(content, 4)):
        if len(row) != 2 or not row[1] or row[1].casefold() in {"n/a", "name", "이름"}:
            continue
        names.append(row[1])
    return sorted({normalized for name in names if (normalized := normalize_app(name))})


def parse_app_categories(tags: Sequence[str]) -> list[str]:
    """Application categories of a paper, taken from its own APP- tags.

    The per-application category taxonomy that used to back this dimension was
    at ``Analyzer/LV/Techniques/tech_taxonomy.json`` and no longer exists there.
    The classification workbook records each paper's application categories
    directly and the generator writes them as ``APP-`` tags, which is the same
    evidence without an app-name lookup.
    """
    return sorted({tag for tag in tags if tag.startswith("APP-")})


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", line):
            continue
        rows.append([clean_text(cell).strip(" `*") for cell in line.strip("|").split("|")])
    return rows


def parse_techniques(classification: Sequence[str]) -> list[dict[str, str]]:
    """Technique paths from the classification front matter.

    A Level-3 identifier already names its own parent, so the (level2, level3)
    pair the similarity model compares is derived from the identifier itself
    instead of a localized table that no longer exists.
    """
    paths = set()
    for identifier in classification:
        if not re.fullmatch(r"(?:PREP|IDEC|PRCP|COLL|PSCP|EXAM|ANYS|REPT|CMNF|CVGF)-\d{2}-\d{2}", identifier):
            raise CorpusValidationError(f"unexpected classification id: {identifier}")
        paths.add((identifier.rsplit("-", 1)[0], identifier))
    return [{"level2": parent, "level3": child} for parent, child in sorted(paths)]


def extract_topic_features(corpus: Mapping[str, Mapping[str, ParsedPaper]]) -> tuple[dict[str, Any], dict[str, Any]]:
    features = {}
    coverage: Counter[str] = Counter()
    for identifier, paper in sorted(corpus["ko"].items()):
        # Structured fields in the two MD translations describe the same paper.
        # Use the Korean source's canonical taxonomy paths ONCE. Translated rows
        # never become a second technique or infer a synonym from row position.
        content = paper.path.read_text(encoding="utf-8")
        tags = sorted({tag for tag in paper.tags if tag.partition("-")[0] in TAG_DIMENSIONS and clean_text(tag.partition("-")[2])})
        apps = parse_target_apps(content)
        paths = parse_techniques(paper.classification)
        app_categories = parse_app_categories(paper.tags)
        features[identifier] = {"tags": tags, "target_apps": apps, "app_categories": app_categories, "techniques": paths}
        coverage["tags_paper_count"] += bool(tags)
        coverage["target_apps_paper_count"] += bool(apps)
        coverage["techniques_paper_count"] += bool(paths)
        coverage["target_apps_without_app_tag_paper_count"] += bool(apps) and not any(tag.startswith("APP-") for tag in tags)
    return features, {**dict(sorted(coverage.items())), "structured_language": "ko; bilingual duplicates counted once"}


def weighted_jaccard(left: set[str], right: set[str], idf: Mapping[str, float]) -> float:
    union = left | right
    return sum(idf.get(value, 1.0) for value in sorted(left & right)) / sum(idf.get(value, 1.0) for value in sorted(union)) if union else 0.0


def _idf(sets: Mapping[str, set[str]]) -> dict[str, float]:
    frequencies = Counter(value for values in sets.values() for value in values)
    return {value: 1 + math.log((len(sets) + 1) / (count + 1)) for value, count in frequencies.items()}


def _path_key(path: Mapping[str, str]) -> str:
    return path["level2"] + "\x1f" + path["level3"]


def _structured_pair(left: Mapping[str, Any], right: Mapping[str, Any], idfs: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    left_apps, right_apps = set(left["target_apps"]), set(right["target_apps"])
    app_available = bool(left_apps and right_apps)
    left_categories, right_categories = set(left["app_categories"]), set(right["app_categories"])
    app_exact = weighted_jaccard(left_apps, right_apps, idfs["applications"])
    app_category = .20 * weighted_jaccard(left_categories, right_categories, idfs["categories"])
    # Category is only weak fallback credit; it cannot add a bonus on top of
    # exact app overlap or duplicate the APP dimension in tags.
    application_score = max(app_exact, app_category) if app_available else 0.0
    dimensions = {}
    shared_tags = []
    for dimension in TAG_DIMENSIONS:
        if dimension == "APP" and app_available:
            continue
        a = {tag for tag in left["tags"] if tag.startswith(dimension + "-")}
        b = {tag for tag in right["tags"] if tag.startswith(dimension + "-")}
        if a and b:
            dimensions[dimension] = weighted_jaccard(a, b, idfs["tags"])
            shared_tags.extend(a & b)
    tag_score = sum(dimensions.values()) / len(dimensions) if dimensions else 0.0
    a_paths = {_path_key(path) for path in left["techniques"]}
    b_paths = {_path_key(path) for path in right["techniques"]}
    a_parents = {path["level2"] for path in left["techniques"]}
    b_parents = {path["level2"] for path in right["techniques"]}
    technique_exact = weighted_jaccard(a_paths, b_paths, idfs["techniques"])
    technique_parent = .15 * weighted_jaccard(a_parents, b_parents, idfs["parents"])
    technique_score = max(technique_exact, technique_parent)
    return {
        "scores": {"tags": tag_score, "applications": application_score, "techniques": technique_score},
        "available": {"tags": bool(dimensions), "applications": app_available, "techniques": bool(a_paths and b_paths)},
        "shared_tags": sorted(shared_tags),
        "tag_evidence": {"dimensions": {key: round(value, 6) for key, value in dimensions.items()}, "app_dimension_excluded": app_available},
        "application_evidence": {"applicable": app_available, "source_apps": sorted(left_apps), "target_apps": sorted(right_apps), "shared_apps": sorted(left_apps & right_apps), "shared_categories": sorted(left_categories & right_categories), "exact_score": round(app_exact, 6), "category_score": round(app_category, 6)},
        "technique_evidence": {"source": left["techniques"], "target": right["techniques"], "shared_level3": [path for path in left["techniques"] if _path_key(path) in b_paths], "shared_level2": sorted(a_parents & b_parents), "exact_score": round(technique_exact, 6), "parent_score": round(technique_parent, 6)},
    }


def extract_text_features(corpus: Mapping[str, Mapping[str, ParsedPaper]]) -> tuple[dict[str, Counter[str]], dict[str, Any]]:
    documents = {}
    coverage = Counter()
    for identifier in sorted(corpus["ko"]):
        combined: Counter[str] = Counter()
        by_field: dict[str, list[Counter[str]]] = defaultdict(list)
        for language in ("ko", "en"):
            paper = corpus[language][identifier]
            content = paper.path.read_text(encoding="utf-8")
            fields = {"title": paper.subject, "abstract": _section(content, 2), "keywords": _section(content, 3)}
            for name, value in fields.items():
                counts = Counter(tokenize(value))
                by_field[name].append(counts)
                if counts:
                    coverage[f"{language}_{name}_paper_count"] += 1
        for name, weight in (("title", 3), ("keywords", 3), ("abstract", 1)):
            # A verbatim keyword list copied into both languages counts once.
            merged = by_field[name][0] | by_field[name][1]
            combined.update({term: count * weight for term, count in merged.items()})
        documents[identifier] = combined
        coverage["text_feature_paper_count"] += bool(combined)
    return documents, dict(sorted(coverage.items()))


def tfidf_vectors(documents: Mapping[str, Counter[str]], config: SimilarityConfig) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    frequencies = Counter(term for document in documents.values() for term in document)
    count = len(documents)
    # Tiny fixtures must still permit all-document topic terms. The production
    # corpus removes terms in >60% of papers in addition to explicit stopwords.
    kept = {term for term, frequency in frequencies.items() if count < 10 or frequency / count <= config.max_document_frequency}
    vectors = {}
    for identifier, document in sorted(documents.items()):
        vector = {
            term: (1 + math.log(frequency)) * (1 + math.log((count + 1) / (frequencies[term] + 1)))
            for term, frequency in sorted(document.items()) if term in kept
        }
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        vectors[identifier] = {term: weight / norm for term, weight in vector.items()} if norm else {}
    return vectors, {"vocabulary_size": len(kept), "high_frequency_terms_removed": sorted(set(frequencies) - kept)}


def canonical_reference(text: str, resolved_target: str | None = None) -> tuple[str | None, str]:
    """Return one defensible identity; URL/domain identity is never sufficient.

    Reviewed internal resolution wins. Ambiguous multi-DOI strings are omitted.
    For external DOI-less work the *whole* author/title/venue/year record must
    match, a deliberate lower-recall alternative to guessing field boundaries.
    """
    if resolved_target:
        return f"paper:{resolved_target}", "internal_paper"
    decoded = html.unescape(unicodedata.normalize("NFKC", text))
    dois = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(decoded)}
    dois.discard(None)
    if len(dois) == 1:
        return f"doi:{next(iter(dois))}", "doi"
    if len(dois) > 1:
        return None, "ambiguous_multiple_dois"
    # Internet references contain access/registration years, not necessarily a
    # publication year. Do not infer bibliographic identity from those fields.
    if decoded.lstrip().startswith("[인터넷자원]") or re.search(r"접속일\s*:|등록일\s*:", decoded):
        return None, "web_reference_without_doi"
    without_urls = URL_RE.sub("", decoded)
    body = re.sub(r"^\s*\[[^]]+]\s*\.?\s*", "", without_urls)
    if not YEAR_RE.search(body):
        return None, "no_publication_year"
    # Require substantial alphabetic title/author content, excluding year-only
    # placeholders and bare legal-case numbers, whose formatting is unstable.
    letters = "".join(character for character in body if character.isalpha())
    if len(letters) < 20 or len(re.findall(r"[가-힣]{2,}|[a-zA-Z]{2,}", body)) < 4:
        return None, "insufficient_bibliographic_text"
    if decoded.lstrip().startswith("[기타자료]"):
        return None, "unstructured_other_material"
    normalized = normalize_title(body)
    return f"record:{hashlib.sha256(normalized.encode()).hexdigest()}", "exact_bibliographic_record"


def collect_reference_sets(
    corpus: Mapping[str, Mapping[str, ParsedPaper]], citation_edges: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, Any]]:
    resolved = {
        (edge["source"], number): edge["target"]
        for edge in citation_edges for number in edge["reference_numbers"]
    }
    references: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    counts: Counter[str] = Counter()
    per_paper = {}
    for identifier in sorted(corpus["ko"]):
        keys: set[str] = set()
        for reference in corpus["ko"][identifier].references:
            key, method = canonical_reference(reference.text, resolved.get((identifier, reference.number)))
            counts[method] += 1
            if key:
                counts["duplicate_mentions_collapsed"] += key in keys
                keys.add(key)
                label = html.unescape(reference.text).strip()
                if key not in labels or (len(label), label) < (len(labels[key]), labels[key]):
                    labels[key] = label
        references[identifier] = keys
        per_paper[identifier] = {"raw_reference_count": len(corpus["ko"][identifier].references), "canonical_reference_count": len(keys)}
    return references, labels, {"identity_counts": dict(sorted(counts.items())), "per_paper": per_paper, "unique_canonical_reference_count": len(labels)}


def runtime_scoring_inputs(
    vectors: Mapping[str, Mapping[str, float]], references: Mapping[str, set[str]],
    reference_labels: Mapping[str, str],
) -> dict[str, Any]:
    """Export corpus-wide evidence before any threshold or top-k pruning.

    Preserve the exact Python float values used by the static scorer: the
    browser must not reconstruct cosine scores from rounded, retained edges.
    Empty vectors and reference sets remain explicit so missing evidence is
    distinguishable from a missing paper. All keys and arrays are canonical.
    """
    if set(vectors) != set(references):
        raise CorpusValidationError("runtime text/reference paper coverage differs")
    reference_keys = {identifier: sorted(references[identifier]) for identifier in sorted(vectors)}
    used_keys = {key for keys in reference_keys.values() for key in keys}
    if used_keys != set(reference_labels):
        raise CorpusValidationError("runtime references must have exactly one label for each canonical key")
    text_vectors = {
        identifier: dict(sorted(vectors[identifier].items())) for identifier in sorted(vectors)
    }
    for identifier, vector in text_vectors.items():
        if any(not math.isfinite(weight) or weight <= 0 for weight in vector.values()):
            raise CorpusValidationError(f"runtime text vector has invalid weights: {identifier}")
        if vector and not math.isclose(math.fsum(weight * weight for weight in vector.values()), 1.0, rel_tol=0, abs_tol=1e-12):
            raise CorpusValidationError(f"runtime text vector is not L2-normalized: {identifier}")
    return {
        "schema_version": 1,
        "text_vectors": text_vectors,
        "reference_keys": reference_keys,
        "reference_labels": dict(sorted(reference_labels.items())),
    }


def _overlap_pairs(sets: Mapping[str, set[str]]) -> dict[tuple[str, str], set[str]]:
    inverted: dict[str, list[str]] = defaultdict(list)
    for identifier, values in sorted(sets.items()):
        for value in sorted(values):
            inverted[value].append(identifier)
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for value, identifiers in sorted(inverted.items()):
        for pair in itertools.combinations(identifiers, 2):
            pairs[pair].add(value)
    return pairs


def build_similarity_edges(
    vectors: Mapping[str, Mapping[str, float]], references: Mapping[str, set[str]],
    reference_labels: Mapping[str, str], citation_edges: Sequence[Mapping[str, Any]],
    config: SimilarityConfig, topic_features: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config.validate()
    empty_features = {"tags": [], "target_apps": [], "app_categories": [], "techniques": []}
    features = {identifier: (topic_features or {}).get(identifier, empty_features) for identifier in vectors}
    structured_sets = {
        "tags": {identifier: set(feature["tags"]) for identifier, feature in features.items()},
        "applications": {identifier: set(feature["target_apps"]) for identifier, feature in features.items()},
        "categories": {identifier: set(feature["app_categories"]) for identifier, feature in features.items()},
        "techniques": {identifier: {_path_key(path) for path in feature["techniques"]} for identifier, feature in features.items()},
        "parents": {identifier: {path["level2"] for path in feature["techniques"]} for identifier, feature in features.items()},
    }
    idfs = {name: _idf(sets) for name, sets in structured_sets.items()}
    technique_frequency = Counter(value for values in structured_sets["techniques"].values() for value in values)
    common_techniques = {value for value, count in technique_frequency.items() if count >= max(5, math.ceil(len(features) * .08))}
    weights = {name: getattr(config, name + "_weight") for name in TOPIC_COMPONENTS}
    inverted: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for identifier, vector in sorted(vectors.items()):
        for term, weight in sorted(vector.items()):
            inverted[term].append((identifier, weight))
    dots: dict[tuple[str, str], float] = defaultdict(float)
    for term in sorted(inverted):
        for (left, a), (right, b) in itertools.combinations(inverted[term], 2):
            dots[(left, right)] += a * b
    shared_references = _overlap_pairs(references)
    citers: dict[str, set[str]] = {identifier: set() for identifier in vectors}
    for edge in citation_edges:
        citers[edge["target"]].add(edge["source"])
    shared_citers = _overlap_pairs(citers)
    candidates = []
    rejected = Counter()
    candidate_pairs = set(dots)
    for sets in structured_sets.values():
        candidate_pairs.update(_overlap_pairs(sets))
    for left, right in sorted(candidate_pairs):
        pair = (left, right)
        terms = set(vectors[left]) & set(vectors[right])
        ref_keys = shared_references.get(pair, set())
        citer_ids = shared_citers.get(pair, set())
        text_score = min(1.0, dots.get(pair, 0.0))
        coupling = len(ref_keys) / math.sqrt(len(references[left]) * len(references[right])) if ref_keys else 0.0
        co_citation = len(citer_ids) / math.sqrt(len(citers[left]) * len(citers[right])) if citer_ids else 0.0
        structure = _structured_pair(features[left], features[right], idfs)
        meaningful_terms = terms - GENERIC_TOPIC_TERMS
        app_match = bool(structure["application_evidence"]["shared_apps"])
        technique_match = bool(structure["technique_evidence"]["shared_level3"])
        distinctive_technique_match = any(_path_key(path) not in common_techniques for path in structure["technique_evidence"]["shared_level3"])
        specific_tags = [tag for tag in structure["shared_tags"] if tag not in GENERIC_TAGS and not tag.startswith("APP-")]
        # Broad OS/device/category tags or one general technique cannot create a
        # topic relation. Citations NEVER satisfy this gate. Structured matches
        # need a second independent signal or some meaningful text support.
        lexical_gate = text_score >= config.min_text_cosine and len(meaningful_terms) >= 2
        supporting_text = text_score >= .08 and len(meaningful_terms) >= 2
        independent_structure = app_match and (technique_match or bool(specific_tags))
        supported_structure = supporting_text and (app_match or distinctive_technique_match or bool(specific_tags))
        if not (lexical_gate or independent_structure or supported_structure):
            rejected["insufficient_evidence"] += 1
            continue
        available = {"text": bool(vectors[left] and vectors[right]), **structure["available"]}
        coverage = sum(weights[name] for name in TOPIC_COMPONENTS if available[name])
        if not coverage:
            rejected["no_jointly_available_component"] += 1
            continue
        effective_weights = {name: weights[name] / coverage if available[name] else 0.0 for name in TOPIC_COMPONENTS}
        components = {"text": text_score, **structure["scores"], "bibliographic_coupling": coupling, "co_citation": co_citation}
        score = sum(effective_weights[name] * components[name] for name in TOPIC_COMPONENTS)
        if score < config.min_score:
            rejected["below_threshold"] += 1
            continue
        ranked_terms = sorted(terms, key=lambda term: (-(vectors[left][term] * vectors[right][term]), term))
        candidates.append({
            "id": f"SIM-{left}-{right}", "source": left, "target": right,
            "score": round(score, 6),
            "components": {name: round(value, 6) for name, value in components.items()},
            "available_components": [name for name in TOPIC_COMPONENTS if available[name]],
            "effective_weights": {name: round(value, 9) for name, value in effective_weights.items()},
            "coverage": round(coverage, 6),
            "evidence_gate": {"meaningful_shared_terms": sorted(meaningful_terms), "lexical": lexical_gate, "independent_structure": bool(independent_structure), "text_supported_structure": bool(supported_structure), "specific_shared_tags": specific_tags, "distinctive_shared_technique": distinctive_technique_match},
            **{name: structure[name] for name in ("shared_tags", "tag_evidence", "application_evidence", "technique_evidence")},
            "shared_terms": ranked_terms[:12],
            "shared_references": [{"key": key, "text": reference_labels[key]} for key in sorted(ref_keys)],
            "shared_citers": sorted(citer_ids),
        })
    neighbors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in candidates:
        neighbors[edge["source"]].append(edge)
        neighbors[edge["target"]].append(edge)
    retained = {
        edge["id"]
        for entries in neighbors.values()
        for edge in sorted(entries, key=lambda item: (-item["score"], item["id"]))[:config.top_k]
    }
    edges = sorted((edge for edge in candidates if edge["id"] in retained), key=lambda edge: (edge["source"], edge["target"]))
    degree = Counter(endpoint for edge in edges for endpoint in (edge["source"], edge["target"]))
    return edges, {"candidate_pair_count": len(candidate_pairs), "eligible_pair_count": len(candidates), "sparsified_pair_count": len(candidates) - len(edges), "rejected_pair_counts": dict(rejected), "max_similarity_degree": max(degree.values(), default=0), "coverage_counts": dict(sorted(Counter(str(edge["coverage"]) for edge in edges).items())), "component_availability_counts": dict(sorted(Counter(name for edge in edges for name in edge["available_components"]).items())), "common_technique_paths": [{"level2": path.split("\x1f")[0], "level3": path.split("\x1f")[1], "paper_count": technique_frequency[path]} for path in sorted(common_techniques)]}


def method_description(config: SimilarityConfig) -> dict[str, Any]:
    return {
        "algorithm": ALGORITHM_VERSION,
        "score_interpretation": "Local-corpus similarity ranking in [0,1]; not a probability, citation, or proof of topic equivalence.",
        "text": {"source": "bilingual Markdown title, abstract (section 2), keywords (section 3); no authors, affiliations, references or template labels", "field_weights": {"title": 3, "abstract": 1, "keywords": 3}, "bilingual_duplicate_policy": "per-field maximum token count across languages", "formula": "L2-normalized (1 + ln(weighted term frequency)) * (1 + ln((N + 1) / (document frequency + 1))); cosine", "tokenization": "Unicode-normalized Korean/Latin tokens, Korean/English stopwords plus German function words found in source abstracts, conservative Korean particle and English plural normalization; no translation or embeddings", "max_document_frequency": config.max_document_frequency, "small_corpus_exception": "maximum document frequency is not applied when N < 10"},
        "structured_source": "canonical Korean Markdown tags, target applications (section 4), Level-3 classification front matter; translated duplicate counted once; N/A omitted",
        "structured_idf": "1 + ln((N + 1) / (papers containing value + 1)); weighted Jaccard = summed IDF(intersection) / summed IDF(union)",
        "tags": {"dimensions": list(TAG_DIMENSIONS), "formula": "arithmetic mean of IDF-weighted Jaccard for jointly present dimensions; unavailable dimensions omitted", "app_double_count_policy": "exclude APP dimension if both papers have actual target applications"},
        "applications": {"formula": "max(IDF-weighted exact normalized app Jaccard, 0.20 * IDF-weighted category Jaccard)", "availability": "both papers have actual section-4 applications; APP tag is optional because source tag vocabulary is incomplete", "aliases": APP_ALIAS_GROUPS, "identity": "Unicode NFKC, HTML decoding, whitespace/case normalization, explicit aliases only; no substring, brand-family, product-version, or fuzzy matching", "categories": "the paper own APP- tags, recorded by the classification workbook; no app-name lookup"},
        "techniques": {"formula": "max(IDF-weighted exact full (level2,level3) path Jaccard, 0.15 * IDF-weighted parent level2 Jaccard)", "identity": "full canonical Korean path, not a substring or level3 label alone"},
        "bibliographic_coupling": {"formula": "|canonical references(A) intersect canonical references(B)| / sqrt(|references(A)| * |references(B)|)", "identity_priority": ["reviewed internal paper ID", "single DOI", "exact normalized complete author/title/venue/year record"], "omissions": "URL/domain equality, ambiguous multiple DOIs, undated records, Internet access years and unstructured other material do not establish shared-reference identity; external variants without stable IDs can be missed"},
        "co_citation": {"formula": "|internal citers(A) intersect internal citers(B)| / sqrt(|internal citers(A)| * |internal citers(B)|)", "scope": "reviewed directed citations inside this Markdown corpus only"},
        "weights": {**{name: getattr(config, name + "_weight") for name in TOPIC_COMPONENTS}, "bibliographic_coupling": 0, "co_citation": 0},
        "score_formula": "sum(effective_weight * component); effective weights renormalize base weights across jointly available topic components; shared references and co-citation have zero weight",
        "coverage": "sum of base weights for jointly available components; coverage describes metadata availability, NOT confidence; equal scores with different coverage are not equally evidenced",
        "min_score": config.min_score,
        "evidence_gate": {"min_text_cosine": config.min_text_cosine, "min_meaningful_shared_terms": 2, "structure_support_text_cosine": .08, "common_technique_min_papers": 5, "common_technique_corpus_fraction": .08, "common_technique_policy": "paths occurring in at least max(5,ceil(N*.08)) papers cannot by themselves support the weaker .08 textual gate; threshold is disclosed heuristic, not learned", "rule": "meaningful lexical support OR exact app plus exact technique/specific tag OR exact app/distinctive technique/specific tag plus meaningful supporting text; generic platform tags, broad parent/category matches, common techniques with weak text, and citation overlap cannot alone establish topic similarity", "generic_tags": sorted(GENERIC_TAGS), "generic_topic_terms": sorted(GENERIC_TOPIC_TERMS)},
        "top_k": config.top_k,
        "sparsification": "union of each paper's top-k eligible neighbors; endpoint degree can exceed k; ties resolved by canonical edge ID",
        "similarity_direction": "undirected; canonical source ID < target ID",
        "citation_direction": "citing paper -> cited paper, unchanged reviewed reference evidence",
        "layout": "client-side force-directed; spatial proximity is a visual approximation, not another measured relation",
        "runtime": {"schema_version": 1, "scope": "all corpus papers, including empty text/reference evidence; no static similarity-edge threshold or top-k pruning", "text_vectors": "exact full-precision L2-normalized TF-IDF vectors used by the static scorer", "structured_features": "topic_features contains normalized tags, applications and complete Level-3 paths for every paper", "reference_evidence": "canonical reference_keys and reference_labels plus reviewed citation_edges allow shared-reference and co-citation explanations; these remain zero-weight context", "configuration": "browser selects one to four topic components and user-entered weights; static method weights and similarity_edges remain the reproducible default baseline", "integrity": "runtime_inputs_sha256 in build_inputs covers the complete runtime payload"},
    }


def compile_reference_graph(
    source_dir: Path = DEFAULT_SOURCE, *, database_path: Path = DEFAULT_DATABASE,
    expected_count: int | None = None, overrides_path: Path | None = None,
    author_overrides_path: Path | None = None, config: SimilarityConfig | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = config or SimilarityConfig()
    config.validate()
    author, author_audit, corpus = compile_author_graph(source_dir, database_path=database_path, expected_count=expected_count, overrides_path=overrides_path, author_overrides_path=author_overrides_path)
    documents, coverage = extract_text_features(corpus)
    vectors, vector_audit = tfidf_vectors(documents, config)
    topic_features, topic_audit = extract_topic_features(corpus)
    for identifier, feature in topic_features.items():
        feature["text_available"] = bool(vectors[identifier])
    references, labels, reference_audit = collect_reference_sets(corpus, author["citation_edges"])
    edges, similarity_audit = build_similarity_edges(vectors, references, labels, author["citation_edges"], config, topic_features)
    runtime_inputs = runtime_scoring_inputs(vectors, references, labels)
    method = method_description(config)
    inputs = {**author["build_inputs"], "similarity_method_sha256": _json_digest(method), "reference_builder_sha256": _file_sha256(Path(__file__)), "runtime_inputs_sha256": _json_digest(runtime_inputs)}
    digest = _inputs_digest(inputs)
    citation_nodes = {endpoint for edge in author["citation_edges"] for endpoint in (edge["source"], edge["target"])}
    similarity_nodes = {endpoint for edge in edges for endpoint in (edge["source"], edge["target"])}
    citation_pairs = {tuple(sorted((edge["source"], edge["target"]))) for edge in author["citation_edges"]}
    summary = {
        **author["corpus"], **coverage, **{key: value for key, value in topic_audit.items() if key.endswith("_count")}, "build_input_digest": digest,
        "similarity_edge_count": len(edges), "similarity_without_direct_citation_count": sum((edge["source"], edge["target"]) not in citation_pairs for edge in edges),
        "citation_connected_paper_count": len(citation_nodes), "similarity_connected_paper_count": len(similarity_nodes),
        "connected_paper_count": len(citation_nodes | similarity_nodes), "isolated_paper_count": len(author["papers"]) - len(citation_nodes | similarity_nodes),
        "canonical_reference_paper_count": sum(bool(value) for value in references.values()),
        "unique_canonical_reference_count": len(labels),
    }
    index = {"schema_version": 1, "graph_type": "reference", "source_digest": author["source_digest"], "build_inputs": inputs, "build_input_digest": digest, "corpus": summary, "papers": author["papers"], "citation_edges": author["citation_edges"], "similarity_edges": edges, "topic_features": topic_features, "runtime_inputs": runtime_inputs, "method": method}
    audit = {"schema_version": 1, "build_inputs": inputs, "build_input_digest": digest, "source_digest": author["source_digest"], "corpus": summary, "method": method, "text_features": vector_audit, "reference_identity": reference_audit, "similarity": similarity_audit, "citation_resolution_counts": author_audit["citation_resolution"]["counts"], "validation": {"paper_set_matches_author_graph": True, "reviewed_citations_unchanged": True, "similarity_endpoints_inside_corpus": all(edge["source"] in author["papers"] and edge["target"] in author["papers"] for edge in edges), "canonical_unique_undirected_pairs": len({(edge["source"], edge["target"]) for edge in edges}) == len(edges) and all(edge["source"] < edge["target"] for edge in edges)}, "isolated_paper_ids": sorted(set(author["papers"]) - citation_nodes - similarity_nodes)}
    audit["runtime_inputs"] = {
        "schema_version": 1,
        "sha256": inputs["runtime_inputs_sha256"],
        "paper_count": len(runtime_inputs["text_vectors"]),
        "text_term_weight_count": sum(len(vector) for vector in runtime_inputs["text_vectors"].values()),
        "empty_text_paper_count": sum(not vector for vector in runtime_inputs["text_vectors"].values()),
        "empty_reference_paper_count": sum(not keys for keys in runtime_inputs["reference_keys"].values()),
        "reference_label_count": len(runtime_inputs["reference_labels"]),
    }
    audit["validation"]["runtime_inputs_cover_every_paper"] = set(runtime_inputs["text_vectors"]) == set(runtime_inputs["reference_keys"]) == set(author["papers"])
    if not all(audit["validation"].values()):
        raise CorpusValidationError("reference graph invariant failed")
    audit["topic_features"] = topic_audit
    return index, audit


def build(
    source_dir: Path = DEFAULT_SOURCE, output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT, *, check_only: bool = False, **kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    index, audit = compile_reference_graph(source_dir, **kwargs)
    if not check_only:
        _write_json_atomic(output_path.resolve(), index)
        _write_json_atomic(audit_path.resolve(), audit)
    return index, audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="reference-graph.json path")
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--author-overrides", type=Path)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--min-score", type=float, default=0.20)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        index, _ = build(args.source, args.output, args.audit_output, database_path=args.database, expected_count=args.expected_count, overrides_path=args.overrides, author_overrides_path=args.author_overrides, config=SimilarityConfig(top_k=args.top_k, min_score=args.min_score), check_only=args.check_only)
    except (CorpusValidationError, OSError) as exc:
        print(f"Reference graph build failed: {exc}", file=sys.stderr)
        return 1
    corpus = index["corpus"]
    print(f"Reference graph {'validated' if args.check_only else 'built'}: {corpus['paper_count']} papers, {corpus['citation_edge_count']} reviewed directed citations, {corpus['similarity_edge_count']} undirected similarities ({corpus['similarity_without_direct_citation_count']} without direct citation), {corpus['isolated_paper_count']} isolated papers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
