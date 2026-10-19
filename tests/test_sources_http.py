import pytest
import requests

from jobsearch.sources import http


class Resp:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code, self.headers, self._p = status, headers or {}, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._p


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(http.time, "sleep", lambda s: calls.append(s))
    return calls


def script(monkeypatch, items):
    it = iter(items)

    def fake(*a, **kw):
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x

    monkeypatch.setattr(http.requests, "get", fake)


def test_retries_connection_errors_and_timeouts_then_succeeds(monkeypatch, sleeps):
    script(monkeypatch, [requests.ConnectionError("x"), requests.Timeout("y"), Resp(payload={"ok": 1})])
    assert http.get_json("https://x") == {"ok": 1}
    assert sleeps == [5, 10]


def test_connection_error_raised_after_retries_exhausted(monkeypatch, sleeps):
    script(monkeypatch, [requests.ConnectionError("x")] * 3)
    with pytest.raises(requests.ConnectionError):
        http.get("https://x")


def test_honors_numeric_retry_after_capped_at_60(monkeypatch, sleeps):
    script(monkeypatch, [Resp(429, {"Retry-After": "7"}), Resp(503, {"Retry-After": "600"}), Resp(payload=1)])
    assert http.get_json("https://x") == 1
    assert sleeps == [7, 60]


def test_non_numeric_retry_after_falls_back_to_backoff(monkeypatch, sleeps):
    script(monkeypatch, [Resp(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), Resp(payload=1)])
    http.get("https://x")
    assert sleeps == [5]


def test_client_errors_are_not_retried(monkeypatch, sleeps):
    script(monkeypatch, [Resp(404)])
    with pytest.raises(requests.HTTPError):
        http.get("https://x")
    assert sleeps == []


def test_final_retryable_status_raises(monkeypatch, sleeps):
    script(monkeypatch, [Resp(500)] * 3)
    with pytest.raises(requests.HTTPError):
        http.get("https://x")
