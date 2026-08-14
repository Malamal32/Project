import time

import httpx
import pytest

from pipeline.politeness import PolitenessError, PolitenessGate, RateLimiter


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_robots_disallow_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, json={"ok": True})

    gate = PolitenessGate(user_agent="test-agent/1.0", client=_client(handler))
    with pytest.raises(PolitenessError):
        gate.before_fetch("https://example.test/private/page", {"crawl_delay_seconds": 0})


def test_robots_allow_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, json={"ok": True})

    gate = PolitenessGate(user_agent="test-agent/1.0", client=_client(handler))
    gate.before_fetch("https://example.test/jobs", {"crawl_delay_seconds": 0})


def test_missing_robots_txt_is_treated_as_allow_all():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    gate = PolitenessGate(user_agent="test-agent/1.0", client=_client(handler))
    gate.before_fetch("https://example.test/jobs", {"crawl_delay_seconds": 0})


def test_rate_limiter_enforces_minimum_interval():
    limiter = RateLimiter()
    start = time.monotonic()
    limiter.wait("example.test", 0.3)
    limiter.wait("example.test", 0.3)
    assert time.monotonic() - start >= 0.3


def test_get_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    gate = PolitenessGate(user_agent="test-agent/1.0", client=_client(handler))
    resp = gate.get("https://example.test/jobs", {"crawl_delay_seconds": 0})

    assert resp.status_code == 200
    assert calls["n"] == 2


def test_get_does_not_retry_on_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(404)

    gate = PolitenessGate(user_agent="test-agent/1.0", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        gate.get("https://example.test/jobs", {"crawl_delay_seconds": 0})

    assert calls["n"] == 1
