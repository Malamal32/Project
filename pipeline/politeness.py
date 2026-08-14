"""Shared politeness layer every collector must go through before hitting a source:
robots.txt enforcement (hard fail if disallowed), per-domain rate limiting, an
identifying User-Agent, and retry-with-backoff for transient failures.
"""

from __future__ import annotations

import os
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = structlog.get_logger()

DEFAULT_CRAWL_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 15.0


class PolitenessError(RuntimeError):
    """Raised when a fetch is not permitted (robots.txt disallow)."""


def get_user_agent() -> str:
    contact = os.environ.get("PIPELINE_CONTACT_EMAIL", "not-configured@example.invalid")
    return f"hiring-db-pipeline/0.1 (+contact: {contact})"


class RateLimiter:
    """Enforces a minimum interval between requests to the same domain."""

    def __init__(self) -> None:
        self._last_request_at: dict[str, float] = {}

    def wait(self, domain: str, min_interval_seconds: float) -> None:
        now = time.monotonic()
        last = self._last_request_at.get(domain)
        if last is not None:
            remaining = min_interval_seconds - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at[domain] = time.monotonic()


class RobotsCache:
    """Fetches and caches robots.txt per origin; missing/unreachable robots.txt is
    treated as allow-all, per convention."""

    def __init__(self, user_agent: str, client: httpx.Client) -> None:
        self._user_agent = user_agent
        self._client = client
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _origin(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_parser(self, origin: str) -> urllib.robotparser.RobotFileParser:
        if origin not in self._parsers:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                resp = self._client.get(robots_url, timeout=10)
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                log.warning("politeness.robots_fetch_failed", url=robots_url)
                parser.parse([])
            self._parsers[origin] = parser
        return self._parsers[origin]

    def check(self, url: str) -> None:
        parser = self._get_parser(self._origin(url))
        if not parser.can_fetch(self._user_agent, url):
            raise PolitenessError(f"robots.txt disallows fetching {url!r} for UA {self._user_agent!r}")

    def crawl_delay(self, url: str) -> Optional[float]:
        parser = self._get_parser(self._origin(url))
        delay = parser.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None


class PolitenessGate:
    """One instance per pipeline run. Wraps an httpx.Client and enforces robots.txt +
    rate limiting before every fetch."""

    def __init__(self, user_agent: Optional[str] = None, client: Optional[httpx.Client] = None) -> None:
        self.user_agent = user_agent or get_user_agent()
        self._owns_client = client is None
        self.client = client or httpx.Client(
            headers={"User-Agent": self.user_agent}, timeout=DEFAULT_TIMEOUT_SECONDS
        )
        self._robots = RobotsCache(self.user_agent, self.client)
        self._rate_limiter = RateLimiter()

    def before_fetch(self, url: str, politeness_config: Optional[dict] = None) -> None:
        politeness_config = politeness_config or {}
        self._robots.check(url)  # raises PolitenessError -> caller must hard-fail
        configured_delay = float(politeness_config.get("crawl_delay_seconds", DEFAULT_CRAWL_DELAY_SECONDS))
        robots_delay = self._robots.crawl_delay(url)
        delay = max(configured_delay, robots_delay or 0.0)
        self._rate_limiter.wait(urlparse(url).netloc, delay)

    def get(self, url: str, politeness_config: Optional[dict] = None) -> httpx.Response:
        self.before_fetch(url, politeness_config)
        return _get_with_retry(self.client, url)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "PolitenessGate":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)
def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    resp = client.get(url)
    resp.raise_for_status()
    return resp
