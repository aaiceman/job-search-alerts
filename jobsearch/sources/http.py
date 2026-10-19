"""Polite HTTP GET with retry, plus HTML-to-text."""
from __future__ import annotations

import time

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JobSearchAlerts/0.1 (personal use)"
_RETRY = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60
MAX_HTML_CHARS = 200_000


def _delay(r: requests.Response, attempt: int) -> float:
    """Seconds to wait before retrying: a numeric Retry-After (capped), else linear backoff."""
    try:
        ra = float(r.headers.get("Retry-After", ""))
        if ra >= 0:
            return min(ra, MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        pass
    return 5 * (attempt + 1)


def get(url: str, params: dict | None = None, timeout: int = 30, retries: int = 2) -> requests.Response:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        if r.status_code in _RETRY and attempt < retries:
            time.sleep(_delay(r, attempt))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def get_json(url: str, params: dict | None = None):
    return get(url, params).json()


def html_to_text(s: str | None) -> str:
    """Strip tags to plain lines. Callers with double-escaped HTML must unescape first."""
    if not s or not isinstance(s, str):
        return ""
    text = BeautifulSoup(s[:MAX_HTML_CHARS], "html.parser").get_text("\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())
