from datetime import date

import pytest

from jobsearch import config, run, watch
from jobsearch.store import Store

from tests.test_run import J, data, fake_collect, ranked_ok, raiser  # noqa: F401  (data is a fixture)

YAML = """applications:
  - company: Example Corp
    title: Principal Security Architect
    url: https://job-boards.greenhouse.io/examplecorp/jobs/1234567890
    ats: greenhouse
    board: examplecorp
    job_id: "1234567890"
    posted: 2026-09-22
    applied: 2026-09-30
"""


@pytest.fixture
def mails(data, monkeypatch):
    out = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: out.append((s, h, t)))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    return out


def write_watch(data, body=YAML):
    (data / "config").mkdir(exist_ok=True)
    (data / "config" / "watch.yaml").write_text(body, encoding="utf-8")


def set_status(monkeypatch, *statuses):
    it = iter(statuses)
    monkeypatch.setattr(run.watch_mod, "http_get", lambda *a, **k: pytest.fail("use check_posting patch"), raising=False)
    monkeypatch.setattr(watch, "check_posting", lambda e: watch.WatchResult(next(it), "", "2026-09-29T08:00:00Z"))


def test_watch_runs_with_zero_candidates_and_is_in_both_bodies(data, mails, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open")
    assert run.main(["--date", "2026-10-02"]) == 0
    [(subject, html, text)] = mails
    assert subject.endswith("no new matches")
    assert "Your applications" in html and "Your applications" in text
    assert ("Example Corp — Principal Security Architect: still open · posted Sep 22 (day 10) · "
            "applied Sep 30 (day 2) · listing last updated Sep 29") in text
    row = Store(data / "jobsearch.db").db.execute("SELECT last_status, closed_on FROM watch_status").fetchone()
    assert row == ("open", None)


def test_closed_detected_on_a_later_day_and_kept(data, mails, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open", "closed", "unknown")
    for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
        assert run.main(["--date", d]) == 0
    texts = [t for _, _, t in mails]
    assert "still open" in texts[0]
    assert "POSTING CLOSED on Oct 2 (posted Sep 22 → closed after 10 days; applied 2 days before close)" in texts[1]
    assert "POSTING CLOSED on Oct 2" in texts[2]  # an "unknown" check later does not reopen/forget it


def test_no_watch_file_means_no_block_and_no_warning(data, mails):
    assert run.main(["--date", "2026-10-02"]) == 0
    [(_, html, text)] = mails
    assert "Your applications" not in html and "application watch" not in text.lower()


def test_watch_runs_on_a_degraded_day(data, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open")
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append((s, t)))
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: (
        [], {"sources": [{"name": "a", "ok": False, "count": 0, "error": "x"}], "dropped": {}, "excluded": {},
             "warnings": []}))
    assert run.main(["--date", "2026-10-02"]) == 0
    assert "ALL SOURCES FAILED" in sent[0][0] and "Your applications" in sent[0][1]
    assert Store(data / "jobsearch.db").run_status("2026-10-02") == "sent_degraded"


def test_watch_runs_alongside_ranked_jobs(data, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open")
    jobs = [J("Security Architect")]
    bodies = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: bodies.append(h))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs, (90,)))
    assert run.main(["--date", "2026-10-02"]) == 0
    assert bodies[0].index("Your applications") < bodies[0].index("Top picks")


def test_preview_includes_block_without_sending(data, mails, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open")
    assert run.main(["--date", "2026-10-02", "--no-send"]) == 0
    assert mails == []
    assert "Your applications" in (data / "runs" / "2026-10-02-preview" / "digest.preview.txt").read_text(encoding="utf-8")


def test_watch_exception_does_not_crash_the_run_and_warns(data, mails, monkeypatch):
    monkeypatch.setattr(run, "load_watch", raiser(RuntimeError("yaml exploded")))
    assert run.main(["--date", "2026-10-02"]) == 0
    [(subject, html, text)] = mails
    assert "crashed" not in subject
    assert "application watch problem" in html and "yaml exploded" in html
    assert Store(data / "jobsearch.db").run_status("2026-10-02") == "sent"


def test_checker_exception_is_isolated_per_entry(data, mails, monkeypatch):
    write_watch(data, YAML + YAML.split("applications:\n")[1].replace("1234567890", "999").replace("Example Corp", "Other"))

    def cp(e):
        if e["job_id"] == "999":
            raise RuntimeError("boom")
        return watch.WatchResult("open")

    monkeypatch.setattr(watch, "check_posting", cp)
    assert run.main(["--date", "2026-10-02"]) == 0
    [(_, _, text)] = mails
    assert "Example Corp — Principal Security Architect: still open" in text
    assert "Other — Principal Security Architect: status unknown today" in text


def test_store_failure_on_record_is_a_warning_not_a_crash(data, mails, monkeypatch):
    write_watch(data)
    set_status(monkeypatch, "open")
    monkeypatch.setattr(Store, "record_watch", raiser(RuntimeError("db locked")))
    assert run.main(["--date", "2026-10-02"]) == 0
    [(subject, html, _)] = mails
    assert "crashed" not in subject and "db locked" in html and "Your applications" not in html
