from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from PaperPipeline.http_client import (
    InterProcessRateLimiter,
    PerHostRateLimiter,
    ResponseTooLargeError,
    SafeHttpClient,
    UnsafeUrlError,
    parse_retry_after,
    redact_url,
)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from (self._chunks if self._chunks is not None else [self._body])

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict]] = []
        self.trust_env = True

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.popleft()


def public_dns(_hostname: str, _port: int) -> list[str]:
    return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


def test_rate_limiter_is_per_host() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = PerHostRateLimiter(
        2.0,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    limiter.wait("https://a.example/one")
    limiter.wait("https://b.example/one")
    limiter.wait("https://a.example/two")

    assert sleeps == [0.5]


def test_interprocess_rate_limiter_shares_slots_and_retry_cooldown(
    tmp_path: Path,
) -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    state_path = tmp_path / "semantic-scholar-rate-limit"
    first_process = InterProcessRateLimiter(
        0.5,
        state_path,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    second_process = InterProcessRateLimiter(
        0.5,
        state_path,
        clock=lambda: now[0],
        sleeper=sleep,
    )

    first_process.wait("https://api.semanticscholar.org/one")
    second_process.wait("https://api.semanticscholar.org/two")
    first_process.defer(7.0)
    second_process.wait("https://api.semanticscholar.org/three")

    assert sleeps == [2.0, 7.0]
    assert state_path.read_text(encoding="ascii") == "111.000000000\n"


def test_retry_cooldown_can_extend_a_slot_while_another_waiter_sleeps(
    tmp_path: Path,
) -> None:
    now = [100.0]
    sleeps: list[float] = []
    first_sleep_started = threading.Event()
    release_first_sleep = threading.Event()
    deferral_finished = threading.Event()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            first_sleep_started.set()
            assert release_first_sleep.wait(2)
        now[0] += seconds

    state_path = tmp_path / "semantic-scholar-rate-limit"
    limiter = InterProcessRateLimiter(
        0.5,
        state_path,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    limiter.wait("https://api.semanticscholar.org/first")
    waiter = threading.Thread(
        target=limiter.wait,
        args=("https://api.semanticscholar.org/second",),
    )
    waiter.start()
    assert first_sleep_started.wait(1)

    def apply_deferral() -> None:
        limiter.defer(7.0)
        deferral_finished.set()

    deferrer = threading.Thread(target=apply_deferral)
    deferrer.start()
    cooldown_was_not_blocked = deferral_finished.wait(1)
    release_first_sleep.set()
    waiter.join(2)
    deferrer.join(2)

    assert cooldown_was_not_blocked
    assert not waiter.is_alive()
    assert not deferrer.is_alive()
    assert sleeps == [2.0, 5.0]
    assert state_path.read_text(encoding="ascii") == "109.000000000\n"


def test_retry_after_accepts_seconds_and_http_date() -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

    assert parse_retry_after("3.5", now=now) == 3.5
    assert parse_retry_after(
        "Mon, 27 Jul 2026 12:00:09 GMT",
        now=now,
    ) == 9.0
    assert parse_retry_after("not-a-date", now=now) is None
    assert parse_retry_after("Infinity", now=now) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/file.pdf",
        "https://[::1]/file.pdf",
        "https://localhost/file.pdf",
        "https://user:secret@example.org/file.pdf",
        "https://example.org:8443/file.pdf",
        "https://example.org/file.pdf#fragment",
        "file:///tmp/file.pdf",
        "http://example.org/file.pdf",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    client = SafeHttpClient(dns_resolver=public_dns)
    with pytest.raises(UnsafeUrlError):
        client.validate_public_url(url)


def test_rejects_domain_when_any_address_is_private() -> None:
    client = SafeHttpClient(
        dns_resolver=lambda _host, _port: ["93.184.216.34", "10.0.0.7"]
    )
    with pytest.raises(UnsafeUrlError, match="non-public"):
        client.validate_public_url("https://example.org/paper.pdf")


def test_manual_redirect_revalidates_private_destination() -> None:
    response = FakeResponse(302, headers={"Location": "https://127.0.0.1/secret"})
    session = FakeSession([response])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    with pytest.raises(UnsafeUrlError):
        client.get_bytes("https://example.org/start", max_bytes=100)
    assert response.closed
    assert len(session.calls) == 1
    assert session.trust_env is False


def test_dns_is_revalidated_immediately_before_connection() -> None:
    answers = deque([["93.184.216.34"], ["10.0.0.8"]])
    session = FakeSession([FakeResponse(200, b"must not be reached")])
    client = SafeHttpClient(
        session=session,
        dns_resolver=lambda _host, _port: answers.popleft(),
    )

    with pytest.raises(UnsafeUrlError, match="non-public"):
        client.get_bytes("https://example.org/paper.pdf", max_bytes=100)
    assert not session.calls


def test_stream_cap_catches_lying_or_missing_content_length() -> None:
    response = FakeResponse(200, chunks=[b"1234", b"5678"])
    session = FakeSession([response])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    with pytest.raises(ResponseTooLargeError):
        client.get_bytes("https://example.org/paper.pdf", max_bytes=7)
    assert response.closed


def test_html_uses_smaller_cap_than_direct_pdf() -> None:
    response = FakeResponse(
        200,
        headers={"Content-Type": "text/html", "Content-Length": str(3 * 1024 * 1024)},
    )
    session = FakeSession([response])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    with pytest.raises(ResponseTooLargeError):
        client.get_bytes(
            "https://example.org/article",
            max_bytes=50 * 1024 * 1024,
            max_html_bytes=2 * 1024 * 1024,
        )


def test_html_signature_without_content_type_uses_smaller_cap() -> None:
    response = FakeResponse(
        200,
        chunks=[b"  <!DOCTYPE HTML><html>", b"x" * 100],
    )
    session = FakeSession([response])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    with pytest.raises(ResponseTooLargeError):
        client.get_bytes(
            "https://example.org/article",
            max_bytes=1024,
            max_html_bytes=32,
        )
    assert response.closed


def test_plain_http_candidate_is_upgraded_without_plain_request() -> None:
    response = FakeResponse(200, b"ok")
    session = FakeSession([response])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    fetched = client.get_bytes("http://example.org/paper", max_bytes=10)

    assert fetched.requested_url == "http://example.org/paper"
    assert fetched.final_url == "https://example.org/paper"
    assert session.calls[0][0] == "https://example.org/paper"


def test_cross_origin_redirect_drops_referer() -> None:
    redirect = FakeResponse(302, headers={"Location": "https://cdn.example.net/paper.pdf"})
    pdf = FakeResponse(200, b"%PDF-content", headers={"Content-Type": "application/pdf"})
    session = FakeSession([redirect, pdf])
    client = SafeHttpClient(session=session, dns_resolver=public_dns)

    fetched = client.get_bytes(
        "https://publisher.example.org/article",
        max_bytes=100,
        headers={"Referer": "https://publisher.example.org/article", "Cookie": "secret"},
    )

    assert fetched.final_url == "https://cdn.example.net/paper.pdf"
    assert len(fetched.redirects) == 1
    assert "Cookie" not in session.calls[0][1]["headers"]
    assert "Referer" not in session.calls[1][1]["headers"]


def test_redacts_secret_query_values_and_userinfo() -> None:
    value = redact_url(
        "https://user:pass@example.org/file.pdf?"
        "api_key=abc&email=a%40b.test&mailto=c%40d.test&x=1#frag"
    )
    assert value == (
        "https://example.org/file.pdf?"
        "api_key=%5BREDACTED%5D&email=%5BREDACTED%5D&"
        "mailto=%5BREDACTED%5D&x=1"
    )


def test_redacts_jwt_query_value() -> None:
    value = redact_url(
        "https://download.example.org/paper.pdf?"
        "jwt=header.payload.signature&filename=paper.pdf"
    )

    assert "header.payload.signature" not in value
    assert "jwt=%5BREDACTED%5D" in value
    assert "filename=paper.pdf" in value


def test_redacts_kci_file_id_query_value() -> None:
    value = redact_url(
        "https://www.kci.go.kr/download?"
        "sereArticleSearchBean.artiId=ART001234567&"
        "sereArticleSearchBean.orteFileId=KCI_FI001234567"
    )

    assert "KCI_FI001234567" not in value
    assert "sereArticleSearchBean.artiId=ART001234567" in value
    assert "orteFileId=%5BREDACTED%5D" in value
