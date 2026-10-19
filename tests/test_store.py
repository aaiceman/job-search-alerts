from datetime import date

from jobsearch.models import Job
from jobsearch.store import Store


def J(title="Security Engineer", smax=200000):
    return Job(source="x", url="u", title=title, company="Acme", salary_max=smax)


def test_unsent_then_mark_sent(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert len(s.unsent([J()])) == 1
    s.mark_sent([J()], "2026-09-30")
    assert s.unsent([J()]) == []


def test_repost_with_higher_salary_resurfaces(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J(smax=180000)], "2026-09-30")
    out = s.unsent([J(smax=210000)])
    assert len(out) == 1 and "repost with higher salary" in out[0].flags


def test_sent_history_expires_after_resend_window(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J()], "2026-01-01")
    out = s.unsent([J()], today=date(2026, 4, 15))  # 104 days later
    assert len(out) == 1 and "reposted (last emailed 2026-01-01)" in out[0].flags


def test_sent_history_within_window_still_suppressed(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J()], "2026-01-01")
    assert s.unsent([J()], today=date(2026, 3, 31)) == []  # 89 days
    assert s.unsent([J()], today=date(2026, 4, 1)) == []  # exactly 90 days: not older
    assert len(s.unsent([J()], today=date(2026, 4, 2))) == 1  # 91 days


def test_resend_after_days_is_configurable(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J()], "2026-01-01")
    assert s.unsent([J()], today=date(2026, 2, 1), resend_after_days=90) == []
    assert len(s.unsent([J()], today=date(2026, 2, 1), resend_after_days=10)) == 1


def test_default_today_is_used_when_omitted(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J()], "2000-01-01")  # long ago relative to date.today()
    out = s.unsent([J()])
    assert len(out) == 1 and "reposted (last emailed 2000-01-01)" in out[0].flags


def test_run_status(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.run_status("2026-09-30") is None
    s.set_run_status("2026-09-30", "sent")
    assert s.run_status("2026-09-30") == "sent"


def test_research_cache_respects_age(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.put_research("Acme, Inc.", {"company": "Acme"}, date(2026, 9, 1))
    assert s.get_research(["ACME"], date(2026, 9, 30)) == {"ACME": {"company": "Acme"}}
    assert s.get_research(["ACME"], date(2026, 10, 2)) == {}


def test_resent_repost_is_suppressed_again_for_a_new_window(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J()], "2026-01-01")
    [j] = s.unsent([J()], today=date(2026, 5, 1))
    s.mark_sent([j], "2026-05-01")
    assert s.unsent([J()], today=date(2026, 5, 2)) == []


def test_mark_sent_keeps_known_salary_when_resent_without_one(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J(smax=150000)], "2026-09-30")
    s.mark_sent([J(smax=None)], "2026-10-01")
    row = s.db.execute("SELECT salary_max FROM sent").fetchone()
    assert row[0] == 150000
    s.mark_sent([J(smax=120000)], "2026-10-02")  # lower never lowers the record
    assert s.db.execute("SELECT salary_max FROM sent").fetchone()[0] == 150000
    s.mark_sent([J(smax=190000)], "2026-10-03")
    assert s.db.execute("SELECT salary_max FROM sent").fetchone()[0] == 190000


def test_store_accepts_str_path(tmp_path):
    s = Store(str(tmp_path / "sub" / "db.sqlite"))
    assert s.run_status("2026-09-30") is None


def test_unsent_does_not_duplicate_flag_and_dedupes_batch(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_sent([J(smax=180000)], "2026-09-30")
    j = J(smax=210000)
    [a] = s.unsent([j])
    [b] = s.unsent([a])
    assert b.flags.count("repost with higher salary") == 1
    out = s.unsent([J(title="Cloud Architect"), J(title="Cloud Architect", smax=999999)])
    assert len(out) == 1 and out[0].salary_max == 200000


def test_delete_research(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.put_research("Acme, Inc.", {"company": "Acme"}, date(2026, 9, 30))
    s.delete_research("ACME")
    assert s.get_research(["Acme"], date(2026, 9, 30)) == {}
    s.delete_research("Nobody")  # no-op


def test_get_research_skips_undecodable_rows(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.put_research("Good", {"company": "Good"}, date(2026, 9, 30))
    s.db.execute("INSERT INTO company_research VALUES ('bad', '2026-09-30', '{not json')")
    s.db.commit()
    assert s.get_research(["Good", "Bad"], date(2026, 9, 30)) == {"Good": {"company": "Good"}}


def test_last_sent_date(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.last_sent_date() is None
    s.set_run_status("2026-09-25", "sent")
    s.set_run_status("2026-09-29", "send_failed")
    s.set_run_status("2026-09-28", "sent")
    assert s.last_sent_date() == date(2026, 9, 28)


def test_last_sent_date_ignores_degraded_days(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.set_run_status("2026-09-28", "sent")
    s.set_run_status("2026-09-29", "sent_degraded")
    assert s.last_sent_date() == date(2026, 9, 28)
