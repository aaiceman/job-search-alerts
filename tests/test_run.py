import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jobsearch import config, run
from jobsearch.models import Job
from jobsearch.store import Store


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "personal-profile.md").write_text("P", encoding="utf-8")
    (tmp_path / ".env").write_text("GMAIL_ADDRESS=me@gmail.com\nGMAIL_APP_PASSWORD=x\n", encoding="utf-8")
    monkeypatch.setattr(run, "load_config", lambda name: {"ranker": {}} if name == "filters.yaml" else {})
    monkeypatch.setattr(run, "toast", lambda t, b: None)  # never pop a real toast
    monkeypatch.setattr(run.send_mod, "send_email",
                        lambda *a, **k: pytest.fail("test must patch send_email itself"))  # never send real mail
    monkeypatch.setattr(run.rank_mod, "run_ranker",
                        lambda *a, **k: pytest.fail("test must patch run_ranker itself"))  # never call real claude
    yield tmp_path
    for h in list(logging.getLogger().handlers):  # release the log file so tmp_path can be cleaned up
        logging.getLogger().removeHandler(h)
        h.close()


def J(t):
    return Job(source="s", url=f"https://x/{t}", title=t, company="Acme")


def fake_collect(jobs):
    return lambda *a, **k: (jobs, {"sources": [], "dropped": {}, "excluded": {}, "warnings": []})


def ranked_ok(jobs, scores=(90,)):
    def fake_rank(run_dir, cfg, today, timeout_s=None, pay_target=None):
        (run_dir / "ranked.json").write_text(json.dumps({"jobs": [
            {"id": j.id, "score": sc, "fit": "f", "pay": "p", "pay_estimated": False}
            for j, sc in zip(jobs, scores)]}), encoding="utf-8")
    return fake_rank


def raiser(exc):
    def f(*a, **k):
        raise exc
    return f


def test_ranking_failure_still_sends_and_marks_nothing(data, monkeypatch):
    jobs = [J("Security Engineer")]
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", raiser(RuntimeError("timeout")))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert sent == ["Jobs Wed Sep 30 — 1 unranked (ranking failed)"]
    store = Store(data / "jobsearch.db")
    assert store.run_status("2026-09-30") == "sent" and len(store.unsent(jobs)) == 1


def test_success_marks_ranked_jobs_sent(data, monkeypatch):
    jobs = [J("Security Engineer"), J("Security Architect")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs, (90, 30)))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert Store(data / "jobsearch.db").unsent(jobs) == []  # low scorers are marked seen too


def test_already_sent_short_circuits(data, monkeypatch):
    Store(data / "jobsearch.db").set_run_status("2026-09-30", "sent")
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("should not collect"))
    assert run.main(["--date", "2026-09-30"]) == 0


def test_weekdays_only_skips_weekends_unless_forced(data, monkeypatch):
    # 2026-10-17 is a Saturday: the logon trigger must not send a weekend digest
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("should not collect"))
    assert run.main(["--date", "2026-10-17", "--weekdays-only"]) == 0
    assert Store(data / "jobsearch.db").run_status("2026-10-17") is None
    calls = []

    def fake_collect(*a, **k):
        calls.append(1)
        return [], {"sources": [], "dropped": {}, "excluded": {}, "warnings": []}

    monkeypatch.setattr(run.collect_mod, "collect", fake_collect)
    assert run.main(["--date", "2026-10-17", "--weekdays-only", "--force", "--no-send"]) == 0
    assert calls == [1]
    # a weekday is unaffected by the flag
    assert run.main(["--date", "2026-10-16", "--weekdays-only", "--no-send"]) == 0
    assert calls == [1, 1]


def test_send_failure_returns_2_and_toasts(data, monkeypatch):
    toasts = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    monkeypatch.setattr(run, "toast", lambda t, b: toasts.append(t))
    assert run.main(["--date", "2026-09-30"]) == 2
    assert Store(data / "jobsearch.db").run_status("2026-09-30") == "send_failed" and toasts
    assert (data / "runs" / "2026-09-30" / "digest.html").exists()


def test_no_send_and_skip_rank_writes_digest_only(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    assert run.main(["--date", "2026-09-30", "--no-send", "--skip-rank"]) == 0
    rd = data / "runs" / "2026-09-30-preview"
    assert "Security Engineer" in (rd / "digest.preview.html").read_text(encoding="utf-8")
    assert (rd / "digest.preview.txt").exists() and (rd / "digest.preview.subject.txt").exists()
    assert not (rd / "digest.html").exists() and not (rd / "digest.marks.json").exists()
    assert not (data / "runs" / "2026-09-30").exists()
    store = Store(data / "jobsearch.db")
    assert store.run_status("2026-09-30") is None and len(store.unsent(jobs)) == 1


# ---- crash safety ----------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["collect", "render"])
def test_crash_returns_3_notifies_and_emails_without_job_data(data, monkeypatch, where):
    jobs = [J("Secret Title")]
    sent, toasts, closed = [], [], []
    real_close = run.Store.close
    monkeypatch.setattr(run.Store, "close", lambda self: (closed.append(1), real_close(self)))
    if where == "collect":
        monkeypatch.setattr(run.collect_mod, "collect", raiser(RuntimeError("boom")))
    else:
        monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
        monkeypatch.setattr(run, "render", raiser(RuntimeError("boom")))
        monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append((s, h, t)))
    monkeypatch.setattr(run, "toast", lambda t, b: toasts.append((t, b)))
    assert run.main(["--date", "2026-09-30"]) == 3
    assert Store(data / "jobsearch.db").run_status("2026-09-30") == "crashed"
    assert toasts and toasts[0][0] == "Job digest run CRASHED" and "boom" in toasts[0][1]
    assert [x[0] for x in sent] == ["Jobs Wed Sep 30 — run crashed: RuntimeError: boom"]
    assert "Secret Title" not in sent[0][1] + sent[0][2]
    assert closed


def test_crash_notification_failures_are_swallowed(data, monkeypatch):
    monkeypatch.setattr(run.collect_mod, "collect", raiser(ValueError("x\ny")))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    monkeypatch.setattr(run, "toast", raiser(RuntimeError("no toast")))
    assert run.main(["--date", "2026-09-30"]) == 3


def test_crash_email_subject_is_single_line(data, monkeypatch):
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", raiser(ValueError("x\r\nBcc: e")))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30"]) == 3
    assert "\n" not in sent[0] and "\r" not in sent[0]


def test_no_send_crash_sends_no_email(data, monkeypatch):
    monkeypatch.setattr(run.collect_mod, "collect", raiser(RuntimeError("boom")))
    assert run.main(["--date", "2026-09-30", "--no-send"]) == 3  # a real send_email call would fail the test


# ---- research cache ----------------------------------------------------------------------------

def test_bad_cache_row_never_breaks_a_good_ranking(data, monkeypatch):
    jobs = [J("Security Engineer")]
    Store(data / "jobsearch.db").put_research("Acme", {"company": "Acme", "bogus": 1}, date(2026, 9, 30))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert sent == ["Jobs Wed Sep 30 — 1 new, top: Security Engineer @ Acme (90)"]
    store = Store(data / "jobsearch.db")
    assert store.unsent(jobs) == []  # ranked and marked sent
    assert store.get_research(["Acme"], date(2026, 9, 30)) == {}  # bad row deleted
    assert "bogus" not in (data / "runs" / "2026-09-30" / "company_cache.json").read_text(encoding="utf-8")


def test_valid_cache_row_is_used(data, monkeypatch):
    jobs = [J("Security Engineer")]
    good = {"company": "Acme", "researched_on": "2026-09-01", "benefits": [], "reviews": {"summary": "cached"}}
    Store(data / "jobsearch.db").put_research("Acme", good, date(2026, 9, 1))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert "cached" in (data / "runs" / "2026-09-30" / "digest.html").read_text(encoding="utf-8")


def test_research_store_failure_does_not_fail_ranking(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))

    def rank(run_dir, cfg, today, timeout_s=None, pay_target=None):
        (run_dir / "ranked.json").write_text(json.dumps({"jobs": [
            {"id": jobs[0].id, "score": 90, "fit": "f", "pay": "p", "pay_estimated": False}],
            "companies": {"Acme": {"company": "Acme", "researched_on": "2026-09-30", "benefits": [],
                                   "reviews": {"summary": "s"}}}}), encoding="utf-8")

    monkeypatch.setattr(run.rank_mod, "run_ranker", rank)
    monkeypatch.setattr(run.Store, "put_research", raiser(RuntimeError("db locked")))
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert "top:" in sent[0]  # still a ranked digest


# ---- late logic ----------------------------------------------------------------------------

def _clock(monkeypatch, hh, mm):
    class Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, hh, mm)

    monkeypatch.setattr(run, "datetime", Fake)


@pytest.mark.parametrize("hh,mm,args,late", [(7, 0, [], True), (6, 29, [], False), (6, 31, [], True),
                                             (7, 0, ["--date", "2026-09-30"], False)])
def test_late_prefix_by_start_time(data, monkeypatch, hh, mm, args, late):
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    _clock(monkeypatch, hh, mm)
    assert run.main(args) == 0
    assert sent[0].startswith("[late] ") is late


def test_late_never_applies_to_no_send_preview(data, monkeypatch):
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    _clock(monkeypatch, 7, 0)
    assert run.main(["--no-send", "--force"]) == 0
    [subj] = list((data / "runs").glob("*preview*/digest.preview.subject.txt"))
    assert not subj.read_text(encoding="utf-8").startswith("[late]")


# ---- housekeeping ----------------------------------------------------------------------------

def _old_files(data):
    (data / "logs").mkdir(exist_ok=True)
    (data / "runs" / "2020-01-01").mkdir(parents=True)
    (data / "runs" / "1-keep-me").mkdir()
    (data / "logs" / "2020-01-01.log").write_text("x")
    (data / "logs" / "2020-01-02.log").write_text("x")
    (data / "logs" / "notes.log").write_text("x")
    (data / "logs" / "1-keep-me.log").write_text("x")


def test_housekeeping_removes_only_old_dated_items(data):
    _old_files(data)
    run._housekeeping(date.today())
    assert not (data / "runs" / "2020-01-01").exists() and not (data / "logs" / "2020-01-02.log").exists()
    assert (data / "runs" / "1-keep-me").exists() and (data / "logs" / "1-keep-me.log").exists()
    assert (data / "logs" / "notes.log").exists()


def test_housekeeping_locked_file_does_not_abort(data, monkeypatch):
    _old_files(data)
    real = Path.unlink

    def flaky(self, *a, **k):
        if self.name == "2020-01-01.log":
            raise PermissionError("locked")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky)
    run._housekeeping(date.today())
    assert (data / "logs" / "2020-01-01.log").exists() and not (data / "logs" / "2020-01-02.log").exists()


def test_backdated_run_does_not_delete_recent_runs(data, monkeypatch):
    recent = data / "runs" / (date.today() - timedelta(days=5)).isoformat()
    recent.mkdir(parents=True)
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2030-01-01"]) == 0
    assert recent.exists()


# ---- missed-day recovery ----------------------------------------------------------------------------

def _capture_window(monkeypatch):
    seen = []

    def col(today, searches, fcfg, excl, store, portals, window_override=None, only=None, deadline=None):
        seen.append(window_override)
        return [], {"sources": [], "dropped": {}, "excluded": {}, "warnings": []}

    monkeypatch.setattr(run.collect_mod, "collect", col)
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    return seen


def test_window_widens_after_missed_days(data, monkeypatch):
    seen = _capture_window(monkeypatch)
    Store(data / "jobsearch.db").set_run_status("2026-09-25", "sent")  # run on 09-30 -> 5 days later
    assert run.main(["--date", "2026-09-30"]) == 0
    assert seen == [5 * 24]


def test_window_capped_at_7_days(data, monkeypatch):
    seen = _capture_window(monkeypatch)
    Store(data / "jobsearch.db").set_run_status("2026-08-01", "sent")
    run.main(["--date", "2026-09-30"])
    assert seen == [168]


def test_window_cli_override_wins(data, monkeypatch):
    seen = _capture_window(monkeypatch)
    Store(data / "jobsearch.db").set_run_status("2026-09-25", "sent")
    run.main(["--date", "2026-09-30", "--window-hours", "720"])
    assert seen == [720]


def test_first_run_uses_default_window(data, monkeypatch):
    seen = _capture_window(monkeypatch)
    run.main(["--date", "2026-10-02"])  # nothing sent yet -> normal window
    assert seen == [None]


@pytest.mark.parametrize("last_sent,today,expected", [
    ("2026-09-28", "2026-09-29", 24),  # Tue after a Mon send
    ("2026-09-25", "2026-09-28", 72),  # Mon after a Fri send
    ("2026-09-28", "2026-09-30", 48),  # Wed after a missed Tue (last sent Mon)
])
def test_window_is_days_since_last_sent_without_extra_slack(data, monkeypatch, last_sent, today, expected):
    seen = _capture_window(monkeypatch)
    Store(data / "jobsearch.db").set_run_status(last_sent, "sent")
    run.main(["--date", today])
    assert seen == [expected]


def test_consecutive_daily_runs_stay_at_24h(data, monkeypatch):
    seen = _capture_window(monkeypatch)
    run.main(["--date", "2026-10-02"])
    run.main(["--date", "2026-10-03"])
    assert seen == [None, 24]


# ---- skip-rank / ordering / resend / lock ---------------------------------------------------------------

def test_skip_rank_email_says_skipped(data, monkeypatch):
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([J("Security Engineer")]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30", "--skip-rank"]) == 0
    assert sent == ["Jobs Wed Sep 30 — 1 unranked (ranking skipped)"]


def test_run_marked_sent_even_if_mark_sent_fails(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    monkeypatch.setattr(run.Store, "mark_sent", raiser(RuntimeError("db locked")))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert Store(data / "jobsearch.db").run_status("2026-09-30") == "sent"


def test_rerun_after_send_failure_resends_saved_digest_without_recollecting(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("must not recollect"))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append((s, h, t)))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert sent[0][0] == "Jobs Wed Sep 30 — 1 new, top: Security Engineer @ Acme (90)"
    assert "Security Engineer" in sent[0][1] and "Security Engineer" in sent[0][2]
    store = Store(data / "jobsearch.db")
    assert store.run_status("2026-09-30") == "sent" and store.unsent(jobs) == []


def test_force_ignores_saved_digest(data, monkeypatch):
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    calls = []

    def col(*a, **k):
        calls.append(1)
        return fake_collect([])()

    monkeypatch.setattr(run.collect_mod, "collect", col)
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30", "--force"]) == 0 and calls


def test_fresh_lock_skips_run_and_is_kept(data, monkeypatch):
    lock = data / "run.lock"
    lock.write_text("1234")
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("locked: must not run"))
    assert run.main(["--date", "2026-09-30"]) == 4  # skipped, but not "ok": the caller can tell
    assert lock.read_text() == "1234"


def test_stale_lock_is_taken_over_and_lock_always_removed(data, monkeypatch):
    lock = data / "run.lock"
    lock.write_text("1234")
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert not lock.exists()
    monkeypatch.setattr(run.collect_mod, "collect", raiser(RuntimeError("boom")))
    assert run.main(["--date", "2026-10-01"]) == 3
    assert not lock.exists()


def test_no_send_never_touches_saved_digest_files(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    rd = data / "runs" / "2026-09-30"
    before = {n: (rd / n).read_bytes() for n in ("digest.html", "digest.subject.txt", "digest.txt", "digest.marks.json")}
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([J("Other Job")]))
    assert run.main(["--date", "2026-09-30", "--no-send", "--skip-rank"]) == 0
    assert {n: (rd / n).read_bytes() for n in before} == before
    assert "Other Job" in (data / "runs" / "2026-09-30-preview" / "digest.preview.html").read_text(encoding="utf-8")
    sent = []  # and the resend still works from the untouched originals
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("must not recollect"))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    assert run.main(["--date", "2026-09-30"]) == 0 and "Security Engineer" in sent[0]


# ---- degraded day (every source failed) ------------------------------------------------------

ALL_FAILED = {"sources": [{"name": "a", "ok": False, "count": 0, "error": "x"},
                          {"name": "b", "ok": False, "count": 0, "error": "y"}],
              "dropped": {}, "excluded": {}, "warnings": []}


def test_all_sources_failed_is_recorded_as_degraded_and_rerun_recollects(data, monkeypatch):
    sent, calls = [], []

    def col(*a, **k):
        calls.append(1)
        return [], ALL_FAILED

    monkeypatch.setattr(run.collect_mod, "collect", col)
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    monkeypatch.setattr(run.Store, "mark_sent", lambda self, *a: pytest.fail("no jobs are marked on a degraded day"))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert sent == ["Jobs Wed Sep 30 — ALL SOURCES FAILED"]
    store = Store(data / "jobsearch.db")
    assert store.run_status("2026-09-30") == "sent_degraded" and store.last_sent_date() is None
    assert run.main(["--date", "2026-09-30"]) == 0  # not "already sent": collects again without --force
    assert len(calls) == 2 and len(sent) == 2


def test_partial_source_failure_is_a_normal_sent(data, monkeypatch):
    stats = {**ALL_FAILED, "sources": [ALL_FAILED["sources"][0], {"name": "b", "ok": True, "count": 3, "error": ""}]}
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: ([], stats))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert Store(data / "jobsearch.db").run_status("2026-09-30") == "sent"


def test_degraded_digest_resent_after_send_failure_stays_degraded(data, monkeypatch):
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: ([], ALL_FAILED))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    monkeypatch.setattr(run.collect_mod, "collect", lambda *a, **k: pytest.fail("must not recollect"))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert Store(data / "jobsearch.db").run_status("2026-09-30") == "sent_degraded"


# ---- setup failures are crashes too ------------------------------------------------------

def test_logging_setup_failure_still_runs_and_crash_paths_notify(data, monkeypatch):
    toasts = []
    monkeypatch.setattr(run, "toast", lambda t, b: toasts.append(t))
    monkeypatch.setattr(run, "_setup_logging", raiser(OSError("disk full")))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0  # fell back to stderr logging and carried on
    monkeypatch.setattr(run.collect_mod, "collect", raiser(RuntimeError("boom")))
    assert run.main(["--date", "2026-10-01"]) == 3 and toasts == ["Job digest run CRASHED"]


def test_lock_failure_is_a_crash_and_keeps_foreign_lock(data, monkeypatch):
    toasts = []
    monkeypatch.setattr(run, "toast", lambda t, b: toasts.append(t))
    monkeypatch.setattr(run, "_acquire_lock", raiser(PermissionError("denied")))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    (data / "run.lock").write_text("other")
    assert run.main(["--date", "2026-09-30"]) == 3 and toasts
    assert (data / "run.lock").read_text() == "other"  # we never owned it, so we never delete it


def test_corrupt_saved_marks_falls_back_to_full_run(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    (data / "runs" / "2026-09-30" / "digest.marks.json").write_text('[{"no": "fields"}]', encoding="utf-8")
    calls = []

    def col(*a, **k):
        calls.append(1)
        return jobs, {"sources": [], "dropped": {}, "excluded": {}, "warnings": []}

    monkeypatch.setattr(run.collect_mod, "collect", col)
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0 and calls


# ---- whole-run time budget ---------------------------------------------------------------------

MIN = 60


def _budget_setup(monkeypatch, data, elapsed_after_collect_min, jobs, timeout_minutes=45):
    """Fake monotonic clock: 0 at start, elapsed_after_collect_min once collection returns."""
    clock = {"t": 0.0}
    monkeypatch.setattr(run, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(run, "load_config",
                        lambda name: {"ranker": {"timeout_minutes": timeout_minutes}} if name == "filters.yaml" else {})
    seen = {}

    def col(*a, deadline=None, **k):
        seen["deadline"] = deadline
        clock["t"] = elapsed_after_collect_min * MIN
        return jobs, {"sources": [], "dropped": {}, "excluded": {}, "warnings": []}

    monkeypatch.setattr(run.collect_mod, "collect", col)
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    return seen


@pytest.mark.parametrize("elapsed_min,expected_s", [(10, 45 * MIN), (50, 22 * MIN), (66, 6 * MIN)])
def test_ranker_timeout_is_min_of_configured_and_remaining_budget(data, monkeypatch, elapsed_min, expected_s):
    jobs = [J("Security Engineer")]
    seen = _budget_setup(monkeypatch, data, elapsed_min, jobs)
    got = []

    def rank(run_dir, cfg, today, timeout_s=None, pay_target=None):
        got.append(timeout_s)
        ranked_ok(jobs)(run_dir, cfg, today)

    monkeypatch.setattr(run.rank_mod, "run_ranker", rank)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert got == [expected_s]  # remaining = 80 - elapsed - 8 min send reserve
    assert seen["deadline"] == 40 * MIN  # collection soft budget, monotonic seconds from run start


@pytest.mark.parametrize("elapsed_min", [68, 75, 100])
def test_ranking_skipped_when_budget_exhausted_sends_unranked_and_marks_nothing(data, monkeypatch, elapsed_min):
    jobs = [J("Security Engineer")]
    _budget_setup(monkeypatch, data, elapsed_min, jobs)
    monkeypatch.setattr(run.rank_mod, "run_ranker", lambda *a, **k: pytest.fail("no time left: must not rank"))
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append((s, h)))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert sent[0][0] == "Jobs Wed Sep 30 — 1 unranked (ranking failed)"
    assert "skipped: run time budget exhausted after collection" in sent[0][1]
    assert "Security Engineer" in sent[0][1]
    store = Store(data / "jobsearch.db")
    assert store.run_status("2026-09-30") == "sent" and len(store.unsent(jobs)) == 1


def test_exactly_five_minutes_remaining_still_ranks(data, monkeypatch):
    jobs = [J("Security Engineer")]
    _budget_setup(monkeypatch, data, 67, jobs)  # 80 - 67 - 8 = 5 min
    got = []
    monkeypatch.setattr(run.rank_mod, "run_ranker",
                        lambda rd, cfg, today, timeout_s=None, pay_target=None: (got.append(timeout_s), ranked_ok(jobs)(rd, cfg, today)))
    assert run.main(["--date", "2026-09-30"]) == 0 and got == [5 * MIN]


# ---- missing exclusions file -------------------------------------------------------------------

def test_missing_exclusions_file_warns_in_digest_and_stats(data, monkeypatch):
    sent = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(h))
    assert run.main(["--date", "2026-09-30"]) == 0
    path = data / "config" / "exclusions.yaml"
    msg = f"exclusions file missing: {path} — employer/PE/staffing exclusions NOT applied"
    assert msg in (data / "logs" / "2026-09-30.log").read_text(encoding="utf-8")
    assert json.loads((data / "runs" / "2026-09-30" / "stats.json").read_text(encoding="utf-8"))["warnings"] == [msg]
    assert "exclusions file missing" in sent[0]


def test_present_exclusions_file_adds_no_warning(data, monkeypatch):
    (data / "config").mkdir()
    (data / "config" / "exclusions.yaml").write_text("employers: []\n", encoding="utf-8")
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([]))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert json.loads((data / "runs" / "2026-09-30" / "stats.json").read_text(encoding="utf-8"))["warnings"] == []


# ---- bookkeeping after a delivered digest never crashes the run -------------------------------------

def test_set_run_status_failure_after_send_is_not_a_crash(data, monkeypatch):
    jobs = [J("Security Engineer")]
    sent, toasts = [], []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(s))
    monkeypatch.setattr(run, "toast", lambda t, b: toasts.append(t))
    monkeypatch.setattr(run.Store, "set_run_status", raiser(RuntimeError("db locked")))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert len(sent) == 1 and toasts == []  # no crash email, no crash toast after a delivered digest
    assert Store(data / "jobsearch.db").unsent(jobs) == []  # mark_sent still attempted


# ---- previews never clobber the real run ---------------------------------------------------------

def test_preview_uses_own_run_dir_and_leaves_real_artifacts_alone(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", raiser(RuntimeError("smtp down")))
    assert run.main(["--date", "2026-09-30"]) == 2
    real = data / "runs" / "2026-09-30"
    before = {p.name: p.read_bytes() for p in real.iterdir()}
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([J("Other Job")]))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok([J("Other Job")]))
    assert run.main(["--date", "2026-09-30", "--no-send", "--force"]) == 0
    assert {p.name: p.read_bytes() for p in real.iterdir()} == before  # stats.json, ranked.json, candidates...
    prev = data / "runs" / "2026-09-30-preview"
    assert (prev / "stats.json").exists() and (prev / "ranked.json").exists()
    assert (prev / "digest.preview.html").exists()


def test_preview_dir_is_not_used_for_degraded_resend(data, monkeypatch):
    prev = data / "runs" / "2026-09-30-preview"
    prev.mkdir(parents=True)
    for n, body in (("digest.html", "<p>PREVIEW</p>"), ("digest.subject.txt", "s"), ("digest.txt", "t"),
                    ("digest.marks.json", "[]"), ("stats.json", "{}")):
        (prev / n).write_text(body, encoding="utf-8")
    Store(data / "jobsearch.db").set_run_status("2026-09-30", "send_failed")
    calls = []

    def col(*a, **k):
        calls.append(1)
        return [], {"sources": [], "dropped": {}, "excluded": {}, "warnings": []}

    monkeypatch.setattr(run.collect_mod, "collect", col)
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append(h))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert calls and "PREVIEW" not in sent[0]  # real run recollected; the preview folder was ignored


def test_housekeeping_prunes_old_preview_dirs_only(data):
    (data / "runs" / "2020-01-01-preview").mkdir(parents=True)
    (data / "runs" / "2020-01-01").mkdir()
    (data / "runs" / "9999-01-01-preview").mkdir()
    (data / "runs" / "2020-01-01-keep").mkdir()
    (data / "logs").mkdir(exist_ok=True)
    run._housekeeping(date.today())
    assert not (data / "runs" / "2020-01-01-preview").exists() and not (data / "runs" / "2020-01-01").exists()
    assert (data / "runs" / "9999-01-01-preview").exists() and (data / "runs" / "2020-01-01-keep").exists()


# ---- pay-target gate wiring (made-up owner numbers; the real owner.yaml is never read by tests) ----

def _ranked_pay(jobs_pay, seen=None):
    def fake_rank(run_dir, cfg, today, timeout_s=None, pay_target=None):
        if seen is not None:
            seen.append(pay_target)
        (run_dir / "ranked.json").write_text(json.dumps({"jobs": [
            {"id": j.id, "score": 90, "fit": "f", "pay": pay, "pay_estimated": True, "pay_min": lo, "pay_max": hi}
            for j, pay, lo, hi in jobs_pay]}), encoding="utf-8")
    return fake_rank


def _gate_setup(data, monkeypatch, owner_yaml, floor=175000):
    if owner_yaml is not None:
        (data / "config").mkdir(exist_ok=True)
        (data / "config" / "owner.yaml").write_text(owner_yaml, encoding="utf-8")
    monkeypatch.setattr(run, "load_config",
                        lambda name: {"ranker": {}, "salary_floor": floor} if name == "filters.yaml" else {})
    sent = []
    monkeypatch.setattr(run.send_mod, "send_email", lambda s, h, t, env: sent.append((s, h, t)))
    return sent


def test_pay_target_from_owner_yaml_gates_digest_and_still_marks_sent(data, monkeypatch):
    sent = _gate_setup(data, monkeypatch, "current_base_salary: 160000\nmin_raise_pct: 25\n")  # target 200k
    low, high = J("Cloud Architect"), J("Security Architect")
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([low, high]))
    monkeypatch.setattr(run.rank_mod, "run_ranker", _ranked_pay([(low, "~$180k–$200k est.", 180000, 200000),
                                                                (high, "~$200k–$230k est.", 200000, 230000)]))
    assert run.main(["--date", "2026-09-30"]) == 0
    (subject, html, text), = sent
    assert subject == "Jobs Wed Sep 30 — 1 new, top: Security Architect @ Acme (90)"
    assert "Below pay target ($200k = max(floor, base+25%)):" in html
    assert "160000" not in html and "$160k" not in html and "160000" not in text
    assert Store(data / "jobsearch.db").unsent([low, high]) == []  # the gated job was ranked: marked sent


def test_missing_owner_yaml_gates_at_floor(data, monkeypatch):
    sent = _gate_setup(data, monkeypatch, None)
    low, high = J("Cloud Architect"), J("Security Architect")
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([low, high]))
    monkeypatch.setattr(run.rank_mod, "run_ranker", _ranked_pay([(low, "~$110k–$140k est.", 110000, 140000),
                                                                (high, "~$180k–$220k est.", 180000, 220000)]))
    assert run.main(["--date", "2026-09-30"]) == 0
    (subject, html, _), = sent
    assert "1 new" in subject and "Below pay target ($175k = floor):" in html


def test_pay_target_failure_never_breaks_the_run(data, monkeypatch):
    sent = _gate_setup(data, monkeypatch, None)
    low = J("Cloud Architect")
    monkeypatch.setattr(run, "pay_target", raiser(RuntimeError("bad owner file")))
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([low]))
    monkeypatch.setattr(run.rank_mod, "run_ranker", _ranked_pay([(low, "~$110k est.", 100000, 120000)]))
    assert run.main(["--date", "2026-09-30"]) == 0
    (subject, html, _), = sent
    assert "1 new" in subject and "Below pay target" not in html  # no target -> no gate


# ---- ranker profile and pay target (made-up owner numbers) ------------------------------------------------

def test_ranker_gets_ranker_profile_when_present(data, monkeypatch, caplog):
    (data / "profile" / "ranker-profile.md").write_text("RANKER", encoding="utf-8")
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert (data / "runs" / "2026-09-30" / "profile.md").read_text(encoding="utf-8") == "RANKER"
    assert "ranker-profile.md missing" not in (data / "logs" / "2026-09-30.log").read_text(encoding="utf-8")


def test_missing_ranker_profile_falls_back_with_warning(data, monkeypatch):
    jobs = [J("Security Engineer")]
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect(jobs))
    monkeypatch.setattr(run.rank_mod, "run_ranker", ranked_ok(jobs))
    monkeypatch.setattr(run.send_mod, "send_email", lambda *a: None)
    assert run.main(["--date", "2026-09-30"]) == 0
    assert (data / "runs" / "2026-09-30" / "profile.md").read_text(encoding="utf-8") == "P"
    log_text = (data / "logs" / "2026-09-30.log").read_text(encoding="utf-8")
    assert "ranker-profile.md missing; the ranker sees the full personal profile" in log_text


def test_ranker_gets_pay_target_and_digest_gets_pay_vs_base(data, monkeypatch):
    sent = _gate_setup(data, monkeypatch, "current_base_salary: 160000\nmin_raise_pct: 25\n")  # target 200k
    job = J("Security Architect")
    seen = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([job]))
    monkeypatch.setattr(run.rank_mod, "run_ranker",
                        _ranked_pay([(job, "~$200k–$232k est.", 200000, 232000)], seen))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert seen == [200000.0]  # the target, never the base
    (_, html, text), = sent
    assert "~$200k–$232k est. · +25% to +45% vs current base" in html
    assert "~$200k–$232k est. · +25% to +45% vs current base" in text
    assert "160000" not in html and "$160k" not in html and "160,000" not in html and "160000" not in text


def test_no_owner_file_means_no_pay_vs_base(data, monkeypatch):
    sent = _gate_setup(data, monkeypatch, None)
    job = J("Security Architect")
    seen = []
    monkeypatch.setattr(run.collect_mod, "collect", fake_collect([job]))
    monkeypatch.setattr(run.rank_mod, "run_ranker",
                        _ranked_pay([(job, "~$200k–$232k est.", 200000, 232000)], seen))
    assert run.main(["--date", "2026-09-30"]) == 0
    assert seen == [175000.0]
    (_, html, _), = sent
    assert "vs current base" not in html
