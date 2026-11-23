"""Anonymous HTTP client with redirect, size, retry, and SSRF guardrails."""

from __future__ import annotations

import email.utils
import fcntl
import ipaddress
import math
import os
import random
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests


RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
SENSITIVE_QUERY_PARTS = (
    "token",
    "jwt",
    "key",
    "fileid",
    "signature",
    "credential",
    "auth",
    "cookie",
    "email",
    "mailto",
)


class HttpClientError(RuntimeError):
    code = "http_error"


class UnsafeUrlError(HttpClientError):
    code = "unsafe_url"


class RedirectLimitError(HttpClientError):
    code = "redirect_limit"


class ResponseTooLargeError(HttpClientError):
    code = "too_large"


class RetryableHttpError(HttpClientError):
    code = "retryable_error"


class PerHostRateLimiter:
    """Thread-safe minimum interval shared by all downloader workers."""

    def __init__(
        self,
        requests_per_second: float = 2.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._host_locks: dict[tuple[str, str, int], threading.Lock] = {}
        self._next_allowed: dict[tuple[str, str, int], float] = {}

    def wait(self, url: str) -> None:
        origin = _origin(url)
        with self._lock:
            host_lock = self._host_locks.setdefault(origin, threading.Lock())
        with host_lock:
            now = self._clock()
            allowed_at = self._next_allowed.get(origin, now)
            delay = max(0.0, allowed_at - now)
            if delay:
                self._sleep(delay)
                now = self._clock()
            self._next_allowed[origin] = max(now, allowed_at) + self._interval


class InterProcessRateLimiter:
    """Minimum interval shared by threads and independent CLI processes.

    The state file stores only the next permitted wall-clock timestamp.  The
    caller's URL and credentials are never persisted.  Waiters release the
    advisory lock while sleeping, then re-check the shared timestamp before
    reserving a slot so a concurrent ``Retry-After`` can extend the cooldown.
    """

    def __init__(
        self,
        requests_per_second: float,
        state_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._state_path = Path(state_path)
        self._clock = clock
        self._sleep = sleeper

    def wait(self, _url: str) -> None:
        """Reserve one request slot, blocking until it is globally available."""

        while True:
            with self._locked_state() as state:
                next_allowed = self._read_next_allowed(state)
                now = self._clock()
                delay = max(0.0, next_allowed - now)
                if not delay:
                    self._write_next_allowed(
                        state,
                        now + self._interval,
                    )
                    return
            # Never hold the interprocess lock while sleeping.  A concurrent
            # 429 handler must be able to push ``next_allowed`` farther out;
            # this loop re-reads that value before reserving a request slot.
            self._sleep(delay)

    def defer(self, seconds: float) -> None:
        """Push the next global request slot out by ``seconds``.

        This is used for a server-provided ``Retry-After`` cooldown.  It is
        safe for another process to call ``wait`` concurrently.
        """

        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("defer seconds must be non-negative")
        with self._locked_state() as state:
            next_allowed = self._read_next_allowed(state)
            self._write_next_allowed(
                state,
                max(next_allowed, self._clock() + seconds),
            )

    def _locked_state(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._state_path, flags, 0o600)
        state = os.fdopen(descriptor, "r+", encoding="ascii")

        class _LockedState:
            def __enter__(self_nonlocal):
                try:
                    fcntl.flock(state.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    state.close()
                    raise
                return state

            def __exit__(self_nonlocal, exc_type, exc, traceback):
                try:
                    fcntl.flock(state.fileno(), fcntl.LOCK_UN)
                finally:
                    state.close()
                return False

        return _LockedState()

    @staticmethod
    def _read_next_allowed(state) -> float:
        state.seek(0)
        try:
            value = float(state.read().strip())
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0 else 0.0

    @staticmethod
    def _write_next_allowed(state, value: float) -> None:
        state.seek(0)
        state.truncate()
        state.write(f"{value:.9f}\n")
        state.flush()
        os.fsync(state.fileno())


@dataclass(frozen=True, slots=True)
class HttpFetch:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    redirects: tuple[str, ...]

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()


class SafeHttpClient:
    """Fetch public HTTP resources without credentials or implicit redirects."""

    def __init__(
        self,
        *,
        user_agent: str = "Forensics-Paper/1.0 (public scholarly PDF resolver)",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_redirects: int = 5,
        retries: int = 2,
        allow_http: bool = False,
        session: requests.Session | None = None,
        dns_resolver: Callable[[str, int], list[str]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
        request_gate: Callable[[str], None] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = (connect_timeout, read_timeout)
        self.max_redirects = max_redirects
        self.retries = retries
        self.allow_http = allow_http
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._dns_resolver = dns_resolver or _resolve_addresses
        self._sleep = sleeper
        self._random = random_source or random.Random()
        self._request_gate = request_gate

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_html_bytes is not None and max_html_bytes < 1:
            raise ValueError("max_html_bytes must be positive")
        requested_url = url
        current_url = _prefer_https(url) if not self.allow_http else url
        redirects: list[str] = []
        caller_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.casefold() not in {"authorization", "cookie", "proxy-authorization"}
        }

        for redirect_index in range(self.max_redirects + 1):
            self.validate_public_url(current_url)
            response = self._request_with_retries(current_url, caller_headers)
            status = int(response.status_code)
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    return HttpFetch(
                        requested_url=requested_url,
                        final_url=current_url,
                        status_code=status,
                        headers=dict(response.headers),
                        body=b"",
                        redirects=tuple(redirects),
                    )
                if redirect_index >= self.max_redirects:
                    raise RedirectLimitError(f"more than {self.max_redirects} redirects")
                next_url = urljoin(current_url, location)
                if not self.allow_http:
                    next_url = _prefer_https(next_url)
                self.validate_public_url(next_url)
                redirects.append(next_url)
                if _origin(current_url) != _origin(next_url):
                    caller_headers = {
                        key: value
                        for key, value in caller_headers.items()
                        if key.casefold() not in {"referer", "origin"}
                    }
                current_url = next_url
                continue

            try:
                effective_limit = max_bytes
                response_content_type = response.headers.get("Content-Type", "").casefold()
                if max_html_bytes is not None and "html" in response_content_type:
                    effective_limit = min(effective_limit, max_html_bytes)
                content_length = response.headers.get("Content-Length")
                declared_length = _parse_content_length(content_length)
                if declared_length is not None and declared_length > effective_limit:
                    raise ResponseTooLargeError(
                        f"declared Content-Length {content_length} exceeds {effective_limit}"
                    )
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if (
                        max_html_bytes is not None
                        and effective_limit == max_bytes
                        and _body_looks_like_html(body)
                    ):
                        effective_limit = min(effective_limit, max_html_bytes)
                    if len(body) > effective_limit:
                        raise ResponseTooLargeError(
                            f"decoded response exceeds {effective_limit} bytes"
                        )
                return HttpFetch(
                    requested_url=requested_url,
                    final_url=current_url,
                    status_code=status,
                    headers=dict(response.headers),
                    body=bytes(body),
                    redirects=tuple(redirects),
                )
            finally:
                response.close()
        raise RedirectLimitError(f"more than {self.max_redirects} redirects")

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Send one non-retried anonymous form POST.

        This exists only for explicit public workflows that require a
        statistics/logging POST before a subsequent GET. Redirects are not
        followed and callers must not use it for state-changing APIs.
        """

        self.validate_public_url(url)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            **{
                key: value
                for key, value in (headers or {}).items()
                if key.casefold() not in {"authorization", "cookie", "proxy-authorization"}
            },
        }
        try:
            self.validate_public_url(url)
            if self._request_gate is not None:
                self._request_gate(url)
            response = self.session.post(
                url,
                data=dict(data),
                headers=request_headers,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
                verify=True,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RetryableHttpError(str(exc)) from exc
        try:
            _validate_response_peer(response, url)
        except UnsafeUrlError:
            response.close()
            raise
        try:
            content_length = response.headers.get("Content-Length")
            declared_length = _parse_content_length(content_length)
            if declared_length is not None and declared_length > max_bytes:
                raise ResponseTooLargeError(
                    f"declared Content-Length {content_length} exceeds {max_bytes}"
                )
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError(
                        f"decoded response exceeds {max_bytes} bytes"
                    )
            return HttpFetch(
                requested_url=url,
                final_url=url,
                status_code=int(response.status_code),
                headers=dict(response.headers),
                body=bytes(body),
                redirects=(),
            )
        finally:
            response.close()

    def validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        allowed_schemes = {"https", "http"} if self.allow_http else {"https"}
        if parsed.scheme.casefold() not in allowed_schemes:
            raise UnsafeUrlError(f"unsupported URL scheme: {parsed.scheme!r}")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeUrlError("URL userinfo is not allowed")
        if parsed.fragment:
            raise UnsafeUrlError("URL fragments are not allowed")
        hostname = parsed.hostname
        if not hostname:
            raise UnsafeUrlError("URL has no hostname")
        hostname = hostname.rstrip(".").casefold()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise UnsafeUrlError("localhost is not allowed")
        try:
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise UnsafeUrlError("invalid URL port") from exc
        expected_port = 443 if parsed.scheme.casefold() == "https" else 80
        if port != expected_port:
            raise UnsafeUrlError(f"non-standard port {port} is not allowed")

        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                addresses = self._dns_resolver(hostname, port)
            except OSError as exc:
                raise UnsafeUrlError(f"hostname resolution failed for {hostname}") from exc
            if not addresses:
                raise UnsafeUrlError(f"hostname did not resolve: {hostname}")
            literals = []
            for address in addresses:
                try:
                    literals.append(ipaddress.ip_address(address))
                except ValueError as exc:
                    raise UnsafeUrlError(f"resolver returned an invalid address: {address}") from exc
        else:
            literals = [literal]
        if any(not address.is_global for address in literals):
            raise UnsafeUrlError(f"hostname resolves to a non-public address: {hostname}")

    def _request_with_retries(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> requests.Response:
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            **headers,
        }
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                self.validate_public_url(url)
                if self._request_gate is not None:
                    self._request_gate(url)
                response = self.session.get(
                    url,
                    headers=request_headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                    verify=True,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                self._sleep(self._backoff(attempt, None))
                continue
            try:
                _validate_response_peer(response, url)
            except UnsafeUrlError:
                response.close()
                raise
            if response.status_code not in RETRYABLE_STATUSES or attempt >= self.retries:
                return response
            retry_after = response.headers.get("Retry-After")
            response.close()
            self._sleep(self._backoff(attempt, retry_after))
        raise RetryableHttpError(str(last_error or "retryable HTTP request failed"))

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        requested = parse_retry_after(retry_after)
        if requested is not None:
            return min(120.0, max(0.0, requested))
        ceiling = min(30.0, float(2 ** (attempt + 1)))
        return self._random.uniform(0.0, ceiling)


def redact_url(url: str) -> str:
    """Remove credentials and secret-looking query values before persistence."""

    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    redacted_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(part in key.casefold() for part in SENSITIVE_QUERY_PARTS):
            value = "[REDACTED]"
        redacted_query.append((key, value))
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            urlencode(redacted_query, doseq=True),
            "",
        )
    )


def _resolve_addresses(hostname: str, port: int) -> list[str]:
    return list(
        dict.fromkeys(
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
    )


def _prefer_https(url: str) -> str:
    """Upgrade a plain-HTTP candidate without ever sending a plain request."""

    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "http":
        return url
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.casefold()
    return scheme, (parsed.hostname or "").casefold(), parsed.port or (443 if scheme == "https" else 80)


def parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse either numeric seconds or an HTTP-date ``Retry-After`` value."""

    if not value:
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        pass
    else:
        return seconds if math.isfinite(seconds) else None
    try:
        when = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (when - reference).total_seconds()


def _parse_content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _body_looks_like_html(value: bytes | bytearray) -> bool:
    # ``bytes`` has ``lower()`` but no ``casefold()``. HTML signatures are
    # ASCII, so byte-wise lower-casing is sufficient and also handles servers
    # that omit or mislabel Content-Type.
    prefix = bytes(value[:512]).lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _validate_response_peer(response: requests.Response, url: str) -> None:
    """Reject a connected non-public peer when urllib3 exposes its socket.

    DNS is also revalidated immediately before every connection. A public-only
    egress firewall remains the strongest protection against hostile DNS
    rebinding because socket introspection is transport/version dependent.
    """

    raw = getattr(response, "raw", None)
    candidates = [
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(raw, "connection", None), "sock", None),
    ]
    nested = getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None)
    candidates.append(getattr(nested, "_sock", None))
    for sock in candidates:
        if sock is None:
            continue
        try:
            peer = sock.getpeername()[0]
            address = ipaddress.ip_address(peer)
        except (AttributeError, OSError, ValueError, TypeError):
            continue
        if not address.is_global:
            host = urlsplit(url).hostname or url
            raise UnsafeUrlError(f"connected peer for {host} is non-public")
        return
