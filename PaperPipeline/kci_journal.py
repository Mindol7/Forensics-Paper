"""Build a safe, cacheable KCI Journal (KJC) article-to-PDF index.

The public KJC sitemap lists article-view URLs containing stable KCI
``ART...`` identifiers.  This module converts those entries to the matching
public ``articlePdf`` URLs without crawling article pages.  It deliberately
does not support KCI/JAMS ``poDownload`` endpoints.

Network safety, response-size limits, retry policy, and request pacing remain
the responsibility of the injected :class:`~PaperPipeline.http_client.SafeHttpClient`.
This loader adds a strict host/path allowlist and checks the site's current
robots policy before using either a cached or freshly fetched index.
"""

from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.storage import load_json_object, write_json_atomic


KCI_JOURNAL_HOST = "journal.kci.go.kr"
ROBOTS_URL = f"https://{KCI_JOURNAL_HOST}/robots.txt"
SITEMAP_INDEX_URL = f"https://{KCI_JOURNAL_HOST}/sitemap"
CACHE_SCHEMA_VERSION = "1.1"

MAX_ROBOTS_BYTES = 64 * 1024
MAX_SITEMAP_INDEX_BYTES = 512 * 1024
MAX_JOURNAL_SITEMAP_BYTES = 16 * 1024 * 1024
MAX_ADVERTISED_SITEMAPS = 512

SOURCE_ID_RE = re.compile(r"ART[0-9]{9}\Z")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
JOURNAL_SITEMAP_PATH_RE = re.compile(
    r"/(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]{0,63})/sitemap\Z"
)


class KciJournalError(RuntimeError):
    """Base class for KJC index errors."""

    code = "kci_journal_error"


class KciJournalRobotsError(KciJournalError):
    """Raised when robots.txt is unavailable, invalid, or denies a path."""

    code = "robots_disallowed"


class KciJournalUrlError(KciJournalError):
    """Raised when an advertised URL leaves the KJC allowlist."""

    code = "invalid_kci_journal_url"


class KciJournalSitemapError(KciJournalError):
    """Raised for one malformed or unsuccessful journal sitemap."""

    code = "invalid_sitemap"


class InvalidKciSourceIdError(KciJournalError, ValueError):
    """Raised for a value that is not a canonical KCI ART identifier."""

    code = "invalid_source_id"


class KciJournalCollisionError(KciJournalError):
    """Raised when one ART identifier maps to different journal slugs."""

    code = "source_id_collision"


@dataclass(frozen=True, slots=True)
class KciJournalArticle:
    """One canonical article-view/PDF mapping advertised by KJC."""

    source_id: str
    slug: str
    article_url: str
    pdf_url: str


@dataclass(frozen=True, slots=True)
class KciJournalSitemapFailure:
    """A recoverable failure for one advertised journal sitemap."""

    url: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class KciJournalIndex:
    """Immutable KJC lookup index and its refresh diagnostics."""

    articles: Mapping[str, KciJournalArticle]
    advertised_sitemaps: int
    loaded_sitemaps: int
    failures: tuple[KciJournalSitemapFailure, ...]
    cached_at: float
    from_cache: bool = False

    def resolve(self, source_id: str) -> KciJournalArticle | None:
        """Return an article mapping, rejecting malformed source IDs."""

        _validate_source_id(source_id)
        return self.articles.get(source_id)

    def pdf_url_for(self, source_id: str) -> str | None:
        """Return the canonical PDF URL for ``source_id`` when indexed."""

        article = self.resolve(source_id)
        return article.pdf_url if article is not None else None


@dataclass(frozen=True, slots=True)
class _RobotsRule:
    allow: bool
    pattern: str


@dataclass(frozen=True, slots=True)
class _RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[_RobotsRule, ...]


@dataclass(frozen=True, slots=True)
class _RobotsPolicy:
    groups: tuple[_RobotsGroup, ...]
    sitemaps: tuple[str, ...]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        product = _robots_product_token(user_agent)
        matching: list[tuple[int, _RobotsGroup]] = []
        for group in self.groups:
            scores = [
                0 if agent == "*" else len(agent)
                for agent in group.agents
                if agent == "*" or agent in product
            ]
            if scores:
                matching.append((max(scores), group))
        if not matching:
            return True
        best_score = max(score for score, _group in matching)
        path = _robots_path(url)
        matched_rules: list[tuple[int, bool]] = []
        for score, group in matching:
            if score != best_score:
                continue
            for rule in group.rules:
                if _robots_rule_matches(rule.pattern, path):
                    specificity = len(rule.pattern.rstrip("$").replace("*", ""))
                    matched_rules.append((specificity, rule.allow))
        if not matched_rules:
            return True
        best_specificity = max(score for score, _allow in matched_rules)
        # RFC 9309 gives Allow precedence when equally specific rules conflict.
        return any(
            allow
            for specificity, allow in matched_rules
            if specificity == best_specificity
        )


class KciJournalIndexLoader:
    """Load the public KJC sitemap into a validated ART-to-PDF index.

    ``SafeHttpClient`` should normally be configured with a shared
    :class:`~PaperPipeline.http_client.PerHostRateLimiter` as its
    ``request_gate``.  Fetches are sequential; this class creates no additional
    concurrency.
    """

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        cache_path: Path | None = None,
        cache_ttl_seconds: float = 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if cache_ttl_seconds < 0 or not math.isfinite(cache_ttl_seconds):
            raise ValueError("cache_ttl_seconds must be finite and non-negative")
        self.http = http
        self.cache_path = cache_path
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._clock = clock

    def load(self, *, force_refresh: bool = False) -> KciJournalIndex:
        """Return a fresh or cached index after checking current robots.txt.

        Individual journal sitemap HTTP/XML failures are reported in
        ``index.failures`` and do not discard successfully loaded journals.
        An unsafe advertised URL, a robots denial, or a conflicting duplicate
        ART mapping is fatal and never silently downgraded.
        """

        policy = self._load_robots_policy()
        self._require_robots_allowed(policy, SITEMAP_INDEX_URL)

        if not force_refresh:
            cached = self._load_fresh_cache()
            if cached is not None:
                for article in cached.articles.values():
                    self._require_robots_allowed(policy, article.pdf_url)
                return replace(cached, from_cache=True)

        sitemap_urls = self._load_sitemap_index(policy)
        articles: dict[str, KciJournalArticle] = {}
        failures: list[KciJournalSitemapFailure] = []
        loaded_sitemaps = 0

        for sitemap_url in sitemap_urls:
            self._require_robots_allowed(policy, sitemap_url)
            try:
                journal_articles = self._load_journal_sitemap(
                    sitemap_url,
                    policy,
                )
            except (HttpClientError, KciJournalSitemapError) as exc:
                failures.append(
                    KciJournalSitemapFailure(
                        url=sitemap_url,
                        code=getattr(exc, "code", "sitemap_error"),
                        message=str(exc)[:500],
                    )
                )
                continue
            loaded_sitemaps += 1
            for source_id, article in journal_articles.items():
                _merge_article(articles, article)

        now = self._clock()
        index = KciJournalIndex(
            articles=MappingProxyType(dict(sorted(articles.items()))),
            advertised_sitemaps=len(sitemap_urls),
            loaded_sitemaps=loaded_sitemaps,
            failures=tuple(failures),
            cached_at=now,
            from_cache=False,
        )
        self._write_cache(index)
        return index

    def _load_robots_policy(self) -> _RobotsPolicy:
        try:
            fetch = self.http.get_bytes(
                ROBOTS_URL,
                max_bytes=MAX_ROBOTS_BYTES,
                headers={"Accept": "text/plain,*/*;q=0.1"},
            )
        except HttpClientError as exc:
            raise KciJournalRobotsError(
                f"unable to fetch KJC robots.txt: {exc}"
            ) from exc
        _require_exact_response_url(fetch, ROBOTS_URL, "robots.txt")
        if not 200 <= fetch.status_code < 300:
            raise KciJournalRobotsError(
                f"KJC robots.txt returned HTTP {fetch.status_code}"
            )
        try:
            text = fetch.body.decode("utf-8-sig")
        except UnicodeError as exc:
            raise KciJournalRobotsError("KJC robots.txt is not UTF-8") from exc
        policy = _parse_robots(text)
        advertised = set(policy.sitemaps)
        if advertised and SITEMAP_INDEX_URL not in advertised:
            raise KciJournalRobotsError(
                "KJC robots.txt no longer advertises the allowlisted sitemap index"
            )
        return policy

    def _require_robots_allowed(
        self,
        policy: _RobotsPolicy,
        url: str,
    ) -> None:
        user_agent = str(
            getattr(self.http, "user_agent", "Forensics-Paper/1.0")
        )
        if not policy.can_fetch(user_agent, url):
            raise KciJournalRobotsError(f"robots.txt disallows {url}")

    def _load_sitemap_index(self, policy: _RobotsPolicy) -> list[str]:
        try:
            fetch = self.http.get_bytes(
                SITEMAP_INDEX_URL,
                max_bytes=MAX_SITEMAP_INDEX_BYTES,
                headers={"Accept": "application/xml,text/xml;q=0.9"},
            )
        except HttpClientError as exc:
            raise KciJournalSitemapError(
                f"unable to fetch KJC sitemap index: {exc}"
            ) from exc
        _require_exact_response_url(fetch, SITEMAP_INDEX_URL, "sitemap index")
        if not 200 <= fetch.status_code < 300:
            raise KciJournalSitemapError(
                f"KJC sitemap index returned HTTP {fetch.status_code}"
            )
        root = _parse_xml(fetch.body, expected_root="sitemapindex")
        urls: list[str] = []
        seen: set[str] = set()
        for child in root:
            if _local_name(child.tag) != "sitemap":
                continue
            location = _single_location(child)
            _sitemap_slug(location)
            self._require_robots_allowed(policy, location)
            if location not in seen:
                seen.add(location)
                urls.append(location)
        if not urls:
            raise KciJournalSitemapError("KJC sitemap index contains no sitemaps")
        if len(urls) > MAX_ADVERTISED_SITEMAPS:
            raise KciJournalSitemapError(
                f"KJC sitemap index advertises more than {MAX_ADVERTISED_SITEMAPS} sitemaps"
            )
        return urls

    def _load_journal_sitemap(
        self,
        sitemap_url: str,
        policy: _RobotsPolicy,
    ) -> dict[str, KciJournalArticle]:
        expected_slug = _sitemap_slug(sitemap_url)
        fetch = self.http.get_bytes(
            sitemap_url,
            max_bytes=MAX_JOURNAL_SITEMAP_BYTES,
            headers={"Accept": "application/xml,text/xml;q=0.9"},
        )
        try:
            _require_exact_response_url(fetch, sitemap_url, "journal sitemap")
        except KciJournalUrlError as exc:
            raise KciJournalSitemapError(str(exc)) from exc
        if not 200 <= fetch.status_code < 300:
            raise KciJournalSitemapError(
                f"journal sitemap returned HTTP {fetch.status_code}"
            )
        root = _parse_xml(fetch.body, expected_root="urlset")
        articles: dict[str, KciJournalArticle] = {}
        for child in root:
            if _local_name(child.tag) != "url":
                continue
            location = _single_location(child)
            article = _article_from_location(location, expected_slug)
            if article is None:
                continue
            self._require_robots_allowed(policy, article.pdf_url)
            _merge_article(articles, article)
        return articles

    def _load_fresh_cache(self) -> KciJournalIndex | None:
        path = self.cache_path
        if path is None or self.cache_ttl_seconds == 0:
            return None
        try:
            if path.is_symlink():
                return None
            stat = path.stat()
        except OSError:
            return None
        now = self._clock()
        age = now - stat.st_mtime
        if age < 0 or age > self.cache_ttl_seconds:
            return None
        value = load_json_object(path)
        if value is None:
            return None
        try:
            return _index_from_cache(value)
        except (KeyError, TypeError, ValueError, KciJournalError):
            return None

    def _write_cache(self, index: KciJournalIndex) -> None:
        if self.cache_path is None:
            return
        write_json_atomic(
            self.cache_path,
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "cached_at": index.cached_at,
                "advertised_sitemaps": index.advertised_sitemaps,
                "loaded_sitemaps": index.loaded_sitemaps,
                "articles": [
                    {
                        "source_id": article.source_id,
                        "slug": article.slug,
                        "article_url": article.article_url,
                        "pdf_url": article.pdf_url,
                    }
                    for article in index.articles.values()
                ],
                "failures": [
                    {
                        "url": failure.url,
                        "code": failure.code,
                        "message": failure.message,
                    }
                    for failure in index.failures
                ],
            },
        )


def _parse_robots(text: str) -> _RobotsPolicy:
    groups: list[_RobotsGroup] = []
    sitemaps: list[str] = []
    agents: list[str] = []
    rules: list[_RobotsRule] = []
    rules_started = False

    def finish_group() -> None:
        nonlocal agents, rules, rules_started
        if agents:
            groups.append(
                _RobotsGroup(
                    agents=tuple(dict.fromkeys(agents)),
                    rules=tuple(rules),
                )
            )
        agents = []
        rules = []
        rules_started = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            if rules_started:
                finish_group()
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        field = name.strip().casefold()
        content = value.strip()
        if field == "sitemap":
            if content:
                try:
                    _validate_exact_host_url(content)
                except KciJournalUrlError:
                    continue
                sitemaps.append(content)
            continue
        if field == "user-agent":
            if rules_started:
                finish_group()
            token = content.casefold()
            if token:
                agents.append(token)
            continue
        if field not in {"allow", "disallow"} or not agents:
            continue
        rules_started = True
        if field == "disallow" and not content:
            continue
        rules.append(_RobotsRule(allow=field == "allow", pattern=content))
    finish_group()
    return _RobotsPolicy(
        groups=tuple(groups),
        sitemaps=tuple(dict.fromkeys(sitemaps)),
    )


def _robots_product_token(user_agent: str) -> str:
    first = user_agent.strip().split(maxsplit=1)[0] if user_agent.strip() else ""
    return first.split("/", 1)[0].casefold()


def _robots_path(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _robots_rule_matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    raw_pattern = pattern[:-1] if anchored else pattern
    expression = re.escape(raw_pattern).replace(r"\*", ".*")
    expression = "^" + expression + ("$" if anchored else "")
    return re.search(expression, path) is not None


def _parse_xml(body: bytes, *, expected_root: str) -> ElementTree.Element:
    lowered = body[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise KciJournalSitemapError("DTD/entity declarations are not allowed")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise KciJournalSitemapError(f"malformed sitemap XML: {exc}") from exc
    if _local_name(root.tag) != expected_root:
        raise KciJournalSitemapError(
            f"expected {expected_root!r} XML root, got {_local_name(root.tag)!r}"
        )
    return root


def _single_location(element: ElementTree.Element) -> str:
    values = [
        (child.text or "").strip()
        for child in element
        if _local_name(child.tag) == "loc"
    ]
    if len(values) != 1 or not values[0]:
        raise KciJournalSitemapError(
            "each sitemap entry must contain exactly one non-empty loc"
        )
    return values[0]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _validate_exact_host_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.rstrip(".").casefold() != KCI_JOURNAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise KciJournalUrlError(f"URL leaves the KJC HTTPS allowlist: {url}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise KciJournalUrlError(f"URL has an invalid port: {url}") from exc
    if port not in {None, 443}:
        raise KciJournalUrlError(f"URL uses a non-standard port: {url}")


def _require_exact_response_url(
    fetch: HttpFetch,
    expected_url: str,
    label: str,
) -> None:
    _validate_exact_host_url(fetch.final_url)
    if fetch.final_url != expected_url:
        raise KciJournalUrlError(
            f"{label} redirected outside its exact allowlisted URL"
        )


def _sitemap_slug(url: str) -> str | None:
    _validate_exact_host_url(url)
    parsed = urlsplit(url)
    if parsed.query:
        raise KciJournalUrlError(f"sitemap URL must not contain a query: {url}")
    if parsed.path == "/kjcSitemap":
        return None
    match = JOURNAL_SITEMAP_PATH_RE.fullmatch(parsed.path)
    if not match:
        raise KciJournalUrlError(f"unapproved journal sitemap path: {url}")
    slug = match.group("slug")
    _validate_slug(slug)
    return slug


def _article_from_location(
    url: str,
    expected_slug: str | None,
) -> KciJournalArticle | None:
    _validate_exact_host_url(url)
    parsed = urlsplit(url)
    if "%" in parsed.path or ".." in parsed.path.split("/"):
        raise KciJournalSitemapError(f"unsafe path in journal sitemap: {url}")
    path_segments = parsed.path.split("/")
    slug = path_segments[1] if len(path_segments) > 1 else ""
    if slug:
        _validate_slug(slug)
    if expected_slug is not None:
        expected_prefix = f"/{expected_slug}"
        if parsed.path != expected_prefix and not parsed.path.startswith(
            expected_prefix + "/"
        ):
            raise KciJournalSitemapError(
                f"journal sitemap contains a URL for another slug: {url}"
            )
        slug = expected_slug

    # The special /kjcSitemap advertises site-level pages at the bare host.
    # They are valid sitemap entries but cannot represent journal articles.
    if not slug:
        return None
    expected_article_path = f"/{slug}/archive/articleView" if slug else ""
    if parsed.path != expected_article_path:
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != 1 or query[0][0] != "artiId":
        raise KciJournalSitemapError(
            f"article URL has an unexpected query shape: {url}"
        )
    source_id = query[0][1]
    try:
        _validate_source_id(source_id)
    except InvalidKciSourceIdError as exc:
        raise KciJournalSitemapError(str(exc)) from exc
    article_url = _canonical_article_url(slug, source_id)
    pdf_url = _canonical_pdf_url(slug, source_id)
    return KciJournalArticle(
        source_id=source_id,
        slug=slug,
        article_url=article_url,
        pdf_url=pdf_url,
    )


def _canonical_article_url(slug: str, source_id: str) -> str:
    _validate_slug(slug)
    _validate_source_id(source_id)
    return (
        f"https://{KCI_JOURNAL_HOST}/{slug}/archive/articleView?"
        f"{urlencode({'artiId': source_id})}"
    )


def _canonical_pdf_url(slug: str, source_id: str) -> str:
    _validate_slug(slug)
    _validate_source_id(source_id)
    return (
        f"https://{KCI_JOURNAL_HOST}/{slug}/archive/articlePdf?"
        f"{urlencode({'artiId': source_id})}"
    )


def _validate_source_id(source_id: str) -> None:
    if not isinstance(source_id, str) or SOURCE_ID_RE.fullmatch(source_id) is None:
        raise InvalidKciSourceIdError(
            f"invalid KCI source_id (expected ART plus nine digits): {source_id!r}"
        )


def _validate_slug(slug: str) -> None:
    if SLUG_RE.fullmatch(slug) is None:
        raise KciJournalUrlError(f"invalid KJC journal slug: {slug!r}")


def _merge_article(
    destination: dict[str, KciJournalArticle],
    article: KciJournalArticle,
) -> None:
    existing = destination.get(article.source_id)
    if existing is None:
        destination[article.source_id] = article
        return
    if existing != article:
        raise KciJournalCollisionError(
            f"{article.source_id} maps to both {existing.pdf_url} and {article.pdf_url}"
        )


def _index_from_cache(value: Mapping[str, object]) -> KciJournalIndex:
    if value.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported KJC cache schema")
    cached_at = value["cached_at"]
    advertised = value["advertised_sitemaps"]
    loaded = value["loaded_sitemaps"]
    raw_articles = value["articles"]
    raw_failures = value["failures"]
    if (
        not isinstance(cached_at, (int, float))
        or not math.isfinite(float(cached_at))
        or not isinstance(advertised, int)
        or isinstance(advertised, bool)
        or not isinstance(loaded, int)
        or isinstance(loaded, bool)
        or advertised < 0
        or loaded < 0
        or loaded > advertised
        or not isinstance(raw_articles, list)
        or not isinstance(raw_failures, list)
    ):
        raise ValueError("invalid KJC cache metadata")

    articles: dict[str, KciJournalArticle] = {}
    for raw in raw_articles:
        if not isinstance(raw, dict):
            raise ValueError("invalid KJC cached article")
        source_id = raw.get("source_id")
        slug = raw.get("slug")
        article_url = raw.get("article_url")
        pdf_url = raw.get("pdf_url")
        if not all(
            isinstance(item, str)
            for item in (source_id, slug, article_url, pdf_url)
        ):
            raise ValueError("invalid KJC cached article fields")
        _validate_source_id(source_id)
        _validate_slug(slug)
        canonical_article = _canonical_article_url(slug, source_id)
        canonical_pdf = _canonical_pdf_url(slug, source_id)
        if article_url != canonical_article or pdf_url != canonical_pdf:
            raise KciJournalUrlError("cached KJC article URL is not canonical")
        _merge_article(
            articles,
            KciJournalArticle(
                source_id=source_id,
                slug=slug,
                article_url=article_url,
                pdf_url=pdf_url,
            ),
        )

    failures: list[KciJournalSitemapFailure] = []
    for raw in raw_failures:
        if not isinstance(raw, dict):
            raise ValueError("invalid KJC cached failure")
        url = raw.get("url")
        code = raw.get("code")
        message = raw.get("message")
        if not all(isinstance(item, str) for item in (url, code, message)):
            raise ValueError("invalid KJC cached failure fields")
        _sitemap_slug(url)
        failures.append(
            KciJournalSitemapFailure(url=url, code=code, message=message)
        )
    if loaded + len(failures) != advertised:
        raise ValueError("cached KJC sitemap counts are inconsistent")
    return KciJournalIndex(
        articles=MappingProxyType(dict(sorted(articles.items()))),
        advertised_sitemaps=advertised,
        loaded_sitemaps=loaded,
        failures=tuple(failures),
        cached_at=float(cached_at),
        from_cache=False,
    )
