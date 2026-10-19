import logging
from datetime import date

import pytest
import requests

from jobsearch import config, watch
from jobsearch.store import Store

GH = {"company": "Acme", "title": "Staff Architect", "url": "https://job-boards.greenhouse.io/acme/jobs/42",
      "ats": "greenhouse", "board": "acme", "job_id": "42", "posted": date(2026, 9, 22),
      "applied": date(2026, 9, 30)}


class Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._p, self._bad = status, payload, bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._p


def fake_get(monkeypatch, resp):
    calls = []

    def get(url, *a, **k):
        calls.append(url)
        if isinstance(resp, Exception):
            raise resp
        resp.raise_for_status()  # like the real http.get
        return resp

    monkeypatch.setattr(watch, "http_get", get)
    return calls


# ---- config.load_watch ----

@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "config").mkdir()
    return tmp_path


def test_load_watch_missing_is_empty_with_warning(data, caplog):
    with caplog.at_level(logging.WARNING):
        assert config.load_watch() == []
    assert "watch" in caplog.text.lower()


def test_load_watch_invalid_yaml_is_empty(data, caplog):
    (data / "config" / "watch.yaml").write_text("applications: [unclosed", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert config.load_watch() == []
    assert caplog.records


@pytest.mark.parametrize("body", ["- a\n- b\n", "applications: nope\n", "other: 1\n", ""])
def test_load_watch_wrong_shape_is_empty(data, body):
    (data / "config" / "watch.yaml").write_text(body, encoding="utf-8")
    assert config.load_watch() == []


def test_load_watch_parses_and_normalises(data):
    (data / "config" / "watch.yaml").write_text(
        "applications:\n  - {company: Example Corp, title: PSA, url: 'https://x/1', ats: Greenhouse, board: examplecorp,"
        " job_id: 123, posted: 2026-09-22, applied: '2026-09-30'}\n", encoding="utf-8")
    [e] = config.load_watch()
    assert e["job_id"] == "123" and e["ats"] == "greenhouse"
    assert e["posted"] == date(2026, 9, 22) and e["applied"] == date(2026, 9, 30)


def test_load_watch_skips_bad_entries_keeps_good(data, caplog):
    (data / "config" / "watch.yaml").write_text(
        "applications:\n  - not a dict\n  - {company: A, title: T, ats: greenhouse, board: b}\n"
        "  - {company: A, title: T, ats: workday, board: b, job_id: 1}\n"
        "  - {company: A, title: T, ats: lever, board: b, job_id: 9, posted: garbage}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        out = config.load_watch()
    assert len(out) == 1 and out[0]["ats"] == "lever" and out[0]["posted"] is None


def test_example_file_is_valid_and_generic():
    entries = config.parse_watch(config.load_yaml(config.CONFIG_DIR / "watch.example.yaml"))
    assert entries and all(e["ats"] in watch.CHECKERS for e in entries)


# ---- check_posting ----

def test_greenhouse_open_captures_updated_at(monkeypatch):
    calls = fake_get(monkeypatch, Resp(200, {"id": 42, "updated_at": "2026-09-29T10:00:00-04:00"}))
    r = watch.check_posting(GH)
    assert r.status == "open" and r.updated_at == "2026-09-29T10:00:00-04:00"
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/42"]


def test_greenhouse_closed_on_404(monkeypatch):
    fake_get(monkeypatch, Resp(404))
    assert watch.check_posting(GH).status == "closed"


@pytest.mark.parametrize("resp", [Resp(500), Resp(403), Resp(429), requests.ConnectionError("down"),
                                  requests.Timeout("slow"), RuntimeError("boom"), Resp(200, bad_json=True)])
def test_greenhouse_errors_are_unknown(monkeypatch, resp):
    fake_get(monkeypatch, resp)
    r = watch.check_posting(GH)
    assert r.status == "unknown" and r.detail


def test_lever_open_and_closed(monkeypatch):
    e = {**GH, "ats": "lever", "board": "acme", "job_id": "abc-123"}
    calls = fake_get(monkeypatch, Resp(200, {"id": "abc-123", "createdAt": 1}))
    assert watch.check_posting(e).status == "open"
    assert calls == ["https://api.lever.co/v0/postings/acme/abc-123"]
    fake_get(monkeypatch, Resp(404))
    assert watch.check_posting(e).status == "closed"
    fake_get(monkeypatch, Resp(502))
    assert watch.check_posting(e).status == "unknown"


ASHBY = {**GH, "ats": "ashby", "board": "acme", "job_id": "uuid-1"}


def test_ashby_present_is_open(monkeypatch):
    calls = fake_get(monkeypatch, Resp(200, {"jobs": [{"id": "other"}, {"id": "uuid-1", "title": "T"}]}))
    assert watch.check_posting(ASHBY).status == "open"
    assert calls == ["https://api.ashbyhq.com/posting-api/job-board/acme"]


def test_ashby_absent_is_closed(monkeypatch):
    fake_get(monkeypatch, Resp(200, {"jobs": [{"id": "other"}]}))
    assert watch.check_posting(ASHBY).status == "closed"


def test_ashby_unlisted_counts_as_closed(monkeypatch):
    fake_get(monkeypatch, Resp(200, {"jobs": [{"id": "uuid-1", "isListed": False}]}))
    assert watch.check_posting(ASHBY).status == "closed"


@pytest.mark.parametrize("resp", [Resp(404), Resp(500), Resp(200, {"nope": 1}), Resp(200, ["x"]),
                                  Resp(200, bad_json=True), requests.ConnectionError("x")])
def test_ashby_bad_board_is_unknown_never_closed(monkeypatch, resp):
    fake_get(monkeypatch, resp)
    assert watch.check_posting(ASHBY).status == "unknown"


@pytest.mark.parametrize("entry", [None, "x", {}, {**GH, "ats": "workday"}, {**GH, "board": ""},
                                   {**GH, "job_id": None}])
def test_bad_entry_is_unknown_without_network(monkeypatch, entry):
    monkeypatch.setattr(watch, "http_get", lambda *a, **k: pytest.fail("no network for a bad entry"))
    assert watch.check_posting(entry).status == "unknown"


def test_ids_are_url_quoted(monkeypatch):
    calls = fake_get(monkeypatch, Resp(200, {}))
    watch.check_posting({**GH, "job_id": "../x?y"})
    assert "/../" not in calls[0] and "?" not in calls[0]


# ---- store transitions ----

def R(status, updated=""):
    return watch.WatchResult(status, "", updated)


def test_store_open_closed_reopened(tmp_path, caplog):
    s = Store(tmp_path / "db.sqlite")
    row = s.record_watch(GH, R("open", "2026-09-29T00:00:00Z"), date(2026, 10, 1))
    assert (row["first_checked"], row["last_seen_open"], row["closed_on"], row["last_status"]) == \
        ("2026-10-01", "2026-10-01", None, "open")
    assert row["last_updated_at"] == "2026-09-29T00:00:00Z" and row["company"] == "Acme"
    row = s.record_watch(GH, R("closed"), date(2026, 10, 3))
    assert (row["first_checked"], row["last_seen_open"], row["closed_on"]) == ("2026-10-01", "2026-10-01", "2026-10-03")
    row = s.record_watch(GH, R("closed"), date(2026, 10, 4))
    assert row["closed_on"] == "2026-10-03"  # first closed day is kept
    with caplog.at_level(logging.INFO):
        row = s.record_watch(GH, R("open"), date(2026, 10, 5))
    assert row["closed_on"] is None and row["last_seen_open"] == "2026-10-05" and "reopened" in caplog.text
    assert row["last_updated_at"] == "2026-09-29T00:00:00Z"  # kept when the new result has none


def test_store_unknown_is_a_noop_for_dates(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.record_watch(GH, R("open"), date(2026, 10, 1))
    row = s.record_watch(GH, R("unknown"), date(2026, 10, 2))
    assert (row["last_seen_open"], row["closed_on"], row["last_status"]) == ("2026-10-01", None, "unknown")
    s.record_watch(GH, R("closed"), date(2026, 10, 3))
    row = s.record_watch(GH, R("unknown"), date(2026, 10, 4))
    assert row["closed_on"] == "2026-10-03"


def test_store_first_unknown_then_rows_independent(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    row = s.record_watch(GH, R("unknown"), date(2026, 10, 1))
    assert row["first_checked"] == "2026-10-01" and row["last_seen_open"] is None and row["closed_on"] is None
    other = {**GH, "job_id": "43", "title": "Other"}
    row2 = s.record_watch(other, R("closed"), date(2026, 10, 2))
    assert row2["closed_on"] == "2026-10-02" and row["job_key"] != row2["job_key"]
    assert s.db.execute("SELECT COUNT(*) FROM watch_status").fetchone()[0] == 2


def test_store_adds_table_to_an_existing_db(tmp_path):
    import sqlite3
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE sent (job_key TEXT PRIMARY KEY, first_sent TEXT NOT NULL, salary_max REAL, "
              "url TEXT, title TEXT, company TEXT)")
    c.commit()
    c.close()
    assert Store(p).record_watch(GH, R("open"), date(2026, 10, 1))["last_status"] == "open"


# ---- check_all ----

def test_check_all_isolates_failures(monkeypatch):
    def cp(e):
        if e["job_id"] == "boom":
            raise RuntimeError("bug")
        return R("open")

    monkeypatch.setattr(watch, "check_posting", cp)
    out = watch.check_all([GH, {**GH, "job_id": "boom"}, {**GH, "job_id": "3"}])
    assert [r.status for _, r in out] == ["open", "unknown", "open"]
    assert "bug" in out[1][1].detail


@pytest.mark.parametrize("body", ["applications: []\n", "applications:\n", "", "# nothing yet\n"])
def test_load_watch_empty_list_logs_info_not_warning(data, caplog, body):
    (data / "config" / "watch.yaml").write_text(body, encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="jobsearch.config"):
        assert config.load_watch() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.levelno == logging.INFO and "empty" in r.getMessage() for r in caplog.records)


def test_load_watch_entries_all_unusable_still_warns(data, caplog):
    (data / "config" / "watch.yaml").write_text("applications:\n  - company: X\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jobsearch.config"):
        assert config.load_watch() == []
    assert "no usable entries" in caplog.text


def test_load_watch_wrong_key_still_warns(data, caplog):
    (data / "config" / "watch.yaml").write_text("other: 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jobsearch.config"):
        assert config.load_watch() == []
    assert "no usable entries" in caplog.text
