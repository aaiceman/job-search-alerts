import logging
import math

import pandas as pd

from jobsearch.sources import jobspy_src
from jobsearch.sources.base import SourceContext


def test_parse_dataframe_handles_nan_and_annualizes():
    df = pd.DataFrame([
        {"site": "linkedin", "job_url": "https://li/1", "job_url_direct": "https://acme/apply", "title": "Security Engineer",
         "company": "Acme", "location": "Orlando, FL", "date_posted": "2026-09-28", "job_type": "fulltime",
         "interval": "hourly", "min_amount": 90.0, "max_amount": 100.0, "currency": "USD", "is_remote": True,
         "description": "desc"},
        {"site": "indeed", "job_url": "https://in/2", "job_url_direct": math.nan, "title": "Security Architect",
         "company": "Beta", "location": "Remote", "date_posted": math.nan, "job_type": math.nan,
         "interval": math.nan, "min_amount": math.nan, "max_amount": math.nan, "currency": math.nan,
         "is_remote": math.nan, "description": math.nan},
    ])
    a, b = jobspy_src.parse(df)
    assert (a.source, a.salary_min, a.salary_max, a.remote, a.extra_urls) == ("linkedin", 187200, 208000, True, ["https://acme/apply"])
    assert (b.salary_min, b.remote, b.posted, b.description, b.extra_urls, b.employment_type) == (None, None, "", "", [], "")


def _one(**kw):
    row = {"site": "indeed", "job_url": "https://in/9", "title": "Senior Cybersecurity Infrastructure Engineer - Remote",
           "company": "", "location": "Remote"}
    row.update(kw)
    [j] = jobspy_src.parse(pd.DataFrame([row]))
    return j


def test_empty_company_falls_back_to_company_url_host():
    assert _one(company="", company_url="https://www.acme-corp.com/careers").company == "acme-corp.com"
    assert _one(company=math.nan, company_url="https://jobs.acme.io/x").company == "jobs.acme.io"
    assert _one(company="  ", company_url="acme.com/about").company == "acme.com"


def test_empty_company_without_usable_url_names_the_site():
    assert _one(company="").company == "Unknown company (Indeed)"
    assert _one(company=math.nan, company_url=math.nan).company == "Unknown company (Indeed)"
    assert _one(site="linkedin", company="").company == "Unknown company (LinkedIn)"
    # the job board's own host says nothing about the employer
    assert _one(company="", company_url="https://www.indeed.com/cmp/Acme").company == "Unknown company (Indeed)"
    assert _one(site="linkedin", company="", company_url="https://www.linkedin.com/company/acme"
                ).company == "Unknown company (LinkedIn)"


def test_present_company_untouched():
    assert _one(company="Acme", company_url="https://other.com").company == "Acme"


def test_fetch_counts_failures_as_warnings(monkeypatch):
    calls = []

    def fake_scrape(**kw):
        calls.append(kw)
        if kw["site_name"] == ["indeed"]:
            raise RuntimeError("429")
        return pd.DataFrame([{"site": "linkedin", "job_url": "u", "title": "T", "company": "C"}])

    monkeypatch.setattr(jobspy_src, "_scrape", fake_scrape)
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: None)
    ctx = SourceContext(cfg={"jobspy": {"sites": ["linkedin", "indeed"], "terms": ["a", "b"], "results_per_term": 5}}, hours_old=24)
    jobs = jobspy_src.fetch(ctx)
    assert len(jobs) == 2 and len(calls) == 4
    assert ctx.warnings == ["jobspy: 2 of 4 site searches failed"]
    assert calls[0]["hours_old"] == 24 and calls[0]["fetch_description"] is False


def _row(**kw):
    base = {"site": "linkedin", "job_url": "https://li/1", "title": "T", "company": "C",
            "interval": "yearly", "min_amount": 150000.0, "max_amount": 190000.0, "currency": "USD"}
    base.update(kw)
    return base


def test_salary_only_trusted_when_direct_data():
    df = pd.DataFrame([
        _row(salary_source="direct_data"),
        _row(job_url="https://li/2", salary_source="description"),
        _row(job_url="https://li/3", salary_source=math.nan),
    ])
    a, b, c = jobspy_src.parse(df)
    assert (a.salary_min, a.salary_max) == (150000, 190000)
    assert (b.salary_min, b.salary_max, b.salary_text) == (None, None, "")
    assert (c.salary_min, c.salary_max) == (150000, 190000)  # missing/NaN keeps old behaviour
    [d] = jobspy_src.parse(pd.DataFrame([_row()]))  # no salary_source column at all
    assert d.salary_max == 190000


def test_parse_skips_bad_rows_and_tolerates_odd_values():
    df = pd.DataFrame([_row(job_url="https://li/ok", interval=["weird"], date_posted="not-a-date"),
                       _row(title=None, job_url=None),
                       _row(job_url="https://li/ok2", title=None)])
    skipped: list = []
    jobs = jobspy_src.parse(df, skipped)
    assert [j.url for j in jobs] == ["https://li/ok"] and len(skipped) == 2
    assert jobs[0].posted == ""  # unparseable date


def test_parse_none_and_empty_frames():
    assert jobspy_src.parse(None) == [] and jobspy_src.parse(pd.DataFrame()) == []


def _ctx(sites, terms=("a",)):
    return SourceContext(cfg={"jobspy": {"sites": list(sites), "terms": list(terms), "results_per_term": 5}}, hours_old=24)


def test_indeed_search_term_gets_remote_suffix(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return pd.DataFrame([_row(site=kw["site_name"][0], job_url=f"https://x/{len(calls)}")])

    monkeypatch.setattr(jobspy_src, "_scrape", fake)
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: None)
    jobspy_src.fetch(_ctx(["linkedin", "indeed"], ["security engineer"]))
    li = next(c for c in calls if c["site_name"] == ["linkedin"])
    ind = next(c for c in calls if c["site_name"] == ["indeed"])
    assert li["search_term"] == "security engineer"
    assert ind["search_term"].endswith("remote") and ind["is_remote"] is True


def test_warns_when_a_site_returns_nothing_and_no_trailing_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(jobspy_src, "_scrape", lambda **kw: pd.DataFrame(
        [_row(site="linkedin")] if kw["site_name"] == ["linkedin"] else []))
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: sleeps.append(s))
    ctx = _ctx(["linkedin", "indeed"], ["a", "b"])
    jobspy_src.fetch(ctx)
    assert ctx.warnings == ["jobspy: indeed returned no results"]
    assert len(sleeps) == 3  # 4 calls -> 3 gaps


def test_fetch_reports_skipped_rows(monkeypatch):
    monkeypatch.setattr(jobspy_src, "_scrape", lambda **kw: pd.DataFrame([_row(), _row(title=None, job_url=None)]))
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: None)
    ctx = _ctx(["linkedin"])
    assert len(jobspy_src.fetch(ctx)) == 1
    assert ctx.warnings == ["jobspy: 1 items skipped"]


def _patched(monkeypatch, fake):
    monkeypatch.setattr(jobspy_src, "_scrape", fake)
    monkeypatch.setattr(jobspy_src, "_preload", lambda: None)
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: None)


def test_fetch_kwargs_for_jobspy_1_3(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return pd.DataFrame([_row(site=kw["site_name"][0], job_url=f"https://x/{len(calls)}")])

    _patched(monkeypatch, fake)
    jobspy_src.fetch(_ctx(["linkedin", "indeed"], ["security engineer"]))
    li = next(c for c in calls if c["site_name"] == ["linkedin"])
    ind = next(c for c in calls if c["site_name"] == ["indeed"])
    assert li["fetch_description"] is False and ind["fetch_description"] is False  # LinkedIn pages come later
    assert all(c["verbose"] == 0 for c in calls)
    assert not any("linkedin_fetch_description" in c for c in calls)
    # logged-out LinkedIn search ignores (and in 1.3 warns about) is_remote / job_type
    assert "is_remote" not in li and "job_type" not in li
    assert ind["is_remote"] is True and ind["search_term"] == "security engineer remote"


# ---- deadline ----

def test_fetch_stops_issuing_searches_after_the_deadline(monkeypatch):
    calls = []
    ctx = _ctx(["linkedin", "indeed"], ["a", "b"])
    ctx.deadline = 10 ** 12  # far in the future

    def fake(**kw):
        calls.append(kw)
        ctx.deadline = 0.0  # budget gone after the first search
        return pd.DataFrame([_row(site=kw["site_name"][0])])

    _patched(monkeypatch, fake)
    jobs = jobspy_src.fetch(ctx)
    assert len(calls) == 1 and len(jobs) == 1
    assert "jobspy: stopped early (time budget); 3 of 4 searches skipped" in ctx.warnings


def test_fetch_without_deadline_runs_everything(monkeypatch):
    calls = []
    _patched(monkeypatch, lambda **kw: calls.append(kw) or pd.DataFrame([_row(site=kw["site_name"][0])]))
    ctx = _ctx(["linkedin", "indeed"], ["a", "b"])
    jobspy_src.fetch(ctx)
    assert len(calls) == 4 and not any("time budget" in w for w in ctx.warnings)


# ---- circuit breaker ----

LI_LOG = "JobSpy:LinkedIn"


def test_breaker_skips_site_after_two_logged_errors(monkeypatch):
    calls = []

    def fake(**kw):
        site = kw["site_name"][0]
        calls.append((site, kw["search_term"]))
        if site == "linkedin":
            logging.getLogger(LI_LOG).error("LinkedIn response status code 429")
            return pd.DataFrame()
        return pd.DataFrame([_row(site="indeed", job_url=f"https://in/{len(calls)}")])

    _patched(monkeypatch, fake)
    ctx = _ctx(["linkedin", "indeed"], ["a", "b", "c", "d"])
    jobs = jobspy_src.fetch(ctx)
    # two failures open the breaker; after the cooldown one retry ("c") also fails, so "d" is skipped
    assert [c for c in calls if c[0] == "linkedin"] == [("linkedin", "a"), ("linkedin", "b"), ("linkedin", "c")]
    assert len([c for c in calls if c[0] == "indeed"]) == 4 and len(jobs) == 4
    assert "jobspy: linkedin skipped after repeated failures (LinkedIn response status code 429)" in ctx.warnings
    assert "jobspy: 3 of 7 site searches failed" in ctx.warnings


def test_breaker_counts_raised_exceptions_too(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw["site_name"][0])
        raise ConnectionError("x" * 300)

    _patched(monkeypatch, fake)
    ctx = _ctx(["linkedin"], ["a", "b", "c", "d"])
    try:
        jobspy_src.fetch(ctx)
    except RuntimeError:
        pass
    assert calls == ["linkedin", "linkedin", "linkedin"]  # two failures, cooldown, one failed retry
    [w] = [w for w in ctx.warnings if "repeated failures" in w]
    excerpt = w.split("(", 1)[1].rstrip(")")
    assert len(excerpt) <= 120 and excerpt.startswith("ConnectionError: xxx")


def test_breaker_resets_after_a_success(monkeypatch):
    outcomes = iter([False, True, False, True, False])
    calls = []

    def fake(**kw):
        calls.append(kw["search_term"])
        if not next(outcomes):
            logging.getLogger(LI_LOG).error("LinkedIn: boom")
        return pd.DataFrame([_row(job_url=f"https://li/{len(calls)}")])

    _patched(monkeypatch, fake)
    ctx = _ctx(["linkedin"], ["a", "b", "c", "d", "e"])
    jobspy_src.fetch(ctx)
    assert len(calls) == 5 and not any("repeated failures" in w for w in ctx.warnings)


def test_logged_error_with_rows_keeps_the_rows_and_does_not_raise(monkeypatch):
    def fake(**kw):
        logging.getLogger(LI_LOG).error("LinkedIn: page 2 failed")
        return pd.DataFrame([_row(job_url=f"https://li/{kw['search_term']}")])

    _patched(monkeypatch, fake)
    ctx = _ctx(["linkedin"], ["a", "b"])
    assert len(jobspy_src.fetch(ctx)) == 2  # partial results survive


def test_warnings_and_other_loggers_are_not_failures(monkeypatch):
    def fake(**kw):
        logging.getLogger(LI_LOG).warning("LinkedIn: empty page at start=10 (throttled)")
        logging.getLogger("somebody.else").error("unrelated")
        return pd.DataFrame([_row(job_url=f"https://li/{kw['search_term']}")])

    _patched(monkeypatch, fake)
    ctx = _ctx(["linkedin"], ["a", "b", "c"])
    jobspy_src.fetch(ctx)
    assert ctx.warnings == []


def test_capture_handler_is_detached_afterwards(monkeypatch):
    lg = logging.getLogger(LI_LOG)
    lg.propagate = False  # like JobSpy's own create_logger
    before = list(lg.handlers), list(logging.getLogger().handlers)
    try:
        _patched(monkeypatch, lambda **kw: lg.error("LinkedIn: boom") or pd.DataFrame())
        ctx = _ctx(["linkedin"], ["a", "b", "c", "d"])
        try:
            jobspy_src.fetch(ctx)
        except RuntimeError:
            pass
        assert any("repeated failures" in w for w in ctx.warnings)  # captured despite propagate=False
        assert (list(lg.handlers), list(logging.getLogger().handlers)) == before
    finally:
        lg.propagate = True


# ---- LinkedIn two-phase: cheap search, then descriptions only for jobs that can matter ----

FILTERS = {"title_include": [r"secur"], "title_exclude": [r"\bjunior\b"]}


def _li(job_id, title, company="C", **kw):
    return _row(job_url=f"https://www.linkedin.com/jobs/view/{job_id}", title=title, company=company,
                interval=math.nan, min_amount=math.nan, max_amount=math.nan, currency=math.nan, **kw)


def _two_phase(monkeypatch, rows_by_call, details=None, ctx=None, sleeps=None):
    """rows_by_call: one row list per _scrape call. details: job_id -> dict, or a callable.
    Returns (ctx, jobs, fetched job ids, scrape calls)."""
    calls, fetched = [], []
    it = iter(rows_by_call)

    def fake_scrape(**kw):
        calls.append(kw)
        return pd.DataFrame(next(it, []))

    def detail(job_id):
        fetched.append(job_id)
        if callable(details):
            return details(job_id)
        return (details or {}).get(job_id, {"description": f"full description {job_id}"})

    monkeypatch.setattr(jobspy_src, "_scrape", fake_scrape)
    monkeypatch.setattr(jobspy_src, "_preload", lambda: None)
    monkeypatch.setattr(jobspy_src.time, "sleep",
                        (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None))
    monkeypatch.setattr(jobspy_src, "_detail_fetcher", lambda: detail)
    ctx = ctx or _ctx(["linkedin"])
    if not ctx.filters_cfg:
        ctx.filters_cfg = FILTERS
    jobs = jobspy_src.fetch(ctx)
    return ctx, jobs, fetched, calls


def test_linkedin_search_no_longer_fetches_descriptions(monkeypatch):
    _, _, _, calls = _two_phase(monkeypatch, [[_li(1, "Security Engineer")]])
    assert calls[0]["fetch_description"] is False


def test_details_only_for_title_passing_unsent_rows(monkeypatch):
    from jobspy.model import Compensation, CompensationInterval, JobType
    ctx = _ctx(["linkedin"])
    ctx.skip_detail = lambda j: j.title == "Security Lead"  # already emailed
    rows = [_li(1, "Security Engineer"), _li(2, "Junior Security Analyst"), _li(3, "Accountant"),
            _li(4, "Security Lead")]
    details = {"1": {"description": "Full text", "job_type": [JobType.FULL_TIME],
                     "compensation": Compensation(interval=CompensationInterval.YEARLY, min_amount=180000.0,
                                                  max_amount=220000.0, currency="USD")}}
    ctx, jobs, fetched, _ = _two_phase(monkeypatch, [rows], details, ctx)
    assert fetched == ["1"]
    by = {j.title: j for j in jobs}
    assert len(jobs) == 4  # nothing dropped here; the filters do that later
    e = by["Security Engineer"]
    assert (e.description, e.salary_min, e.salary_max, e.employment_type) == ("Full text", 180000, 220000, "fulltime")
    assert e.salary_text == "yearly (structured)"
    assert all(by[t].description == "" for t in ("Junior Security Analyst", "Accountant", "Security Lead"))


def test_detail_compensation_is_annualized_and_non_usd_ignored(monkeypatch):
    from jobspy.model import Compensation, CompensationInterval
    details = {"1": {"description": "d", "compensation": Compensation(
                   interval=CompensationInterval.HOURLY, min_amount=90.0, max_amount=100.0, currency="USD")},
               "2": {"description": "d", "compensation": Compensation(
                   interval=CompensationInterval.YEARLY, min_amount=1.0, max_amount=2.0, currency="EUR")}}
    _, jobs, _, _ = _two_phase(monkeypatch, [[_li(1, "Security A"), _li(2, "Security B")]], details)
    a, b = jobs
    assert (a.salary_min, a.salary_max, a.salary_text) == (187200, 208000, "hourly (structured)")
    assert (b.salary_min, b.salary_max, b.salary_text) == (None, None, "")


def test_detail_fetches_capped_per_run(monkeypatch):
    ctx = _ctx(["linkedin"], ["a", "b"])
    ctx.cfg["jobspy"]["max_detail_fetches"] = 3
    rows = [[_li(i, f"Security {i}") for i in range(1, 4)], [_li(i, f"Security {i}") for i in range(4, 7)]]
    _, jobs, fetched, _ = _two_phase(monkeypatch, rows, None, ctx)
    assert fetched == ["1", "2", "3"] and len(jobs) == 6
    assert [j.description != "" for j in jobs] == [True] * 3 + [False] * 3


def test_detail_cap_defaults_to_40(monkeypatch):
    rows = [[_li(i, f"Security {i}") for i in range(1, 46)]]
    _, _, fetched, _ = _two_phase(monkeypatch, rows)
    assert len(fetched) == 40


def test_same_linkedin_job_from_two_terms_is_fetched_once(monkeypatch):
    ctx = _ctx(["linkedin"], ["a", "b"])
    _, jobs, fetched, _ = _two_phase(
        monkeypatch, [[_li(1, "Security Engineer")], [_li(1, "Security Engineer")]], None, ctx)
    assert fetched == ["1"] and [j.description for j in jobs] == ["full description 1"] * 2


def test_failed_detail_fetch_keeps_job_without_description(monkeypatch, caplog):
    def details(job_id):
        if job_id == "2":
            raise ConnectionError("reset")
        return {} if job_id == "1" else {"description": "ok"}

    rows = [[_li(1, "Security A"), _li(2, "Security B"), _li(3, "Security C")]]
    with caplog.at_level(logging.INFO, logger=jobspy_src.log.name):
        ctx, jobs, fetched, _ = _two_phase(monkeypatch, rows, details)
    assert fetched == ["1", "2", "3"]
    assert [j.description for j in jobs] == ["", "", "ok"]
    assert ctx.warnings == []  # a missing description is logged, not a failed search
    assert "description fetch" in caplog.text


def test_detail_phase_stops_after_consecutive_failures(monkeypatch):
    rows = [[_li(i, f"Security {i}") for i in range(1, 8)]]
    ctx, jobs, fetched, _ = _two_phase(monkeypatch, rows, lambda job_id: {})
    assert len(fetched) == jobspy_src.DETAIL_STOP_FAILURES and len(jobs) == 7
    assert any("linkedin descriptions stopped" in w for w in ctx.warnings)


def test_detail_phase_paced_like_searches(monkeypatch):
    sleeps: list = []
    _two_phase(monkeypatch, [[_li(1, "Security A"), _li(2, "Security B")]], sleeps=sleeps)
    assert len(sleeps) == 2 and all(3 <= s <= 7 for s in sleeps)  # one before each page fetch


def test_detail_phase_respects_deadline(monkeypatch):
    ctx = _ctx(["linkedin"])
    ctx.deadline = 10 ** 12

    def details(job_id):
        ctx.deadline = 0.0  # budget gone after the first page
        return {"description": "d"}

    rows = [[_li(i, f"Security {i}") for i in range(1, 5)]]
    _, jobs, fetched, _ = _two_phase(monkeypatch, rows, details, ctx)
    assert fetched == ["1"] and len(jobs) == 4
    assert "jobspy: stopped early (time budget); 3 of 4 LinkedIn description fetches skipped" in ctx.warnings


def test_indeed_rows_never_detail_fetched(monkeypatch):
    ctx = _ctx(["indeed"])
    rows = [[_row(site="indeed", job_url="https://in/1", title="Security Engineer", description="from search")]]
    _, jobs, fetched, calls = _two_phase(monkeypatch, rows, None, ctx)
    assert fetched == [] and jobs[0].description == "from search"
    assert calls[0]["fetch_description"] is False and calls[0]["is_remote"] is True


def test_linkedin_row_with_description_or_without_job_id_not_fetched(monkeypatch):
    rows = [[_li(1, "Security A", description="already here"),
             _row(job_url="https://www.linkedin.com/jobs/search?x=1", title="Security B")]]
    _, _, fetched, _ = _two_phase(monkeypatch, rows)
    assert fetched == []


def test_detail_fetcher_setup_failure_is_a_warning_not_a_crash(monkeypatch):
    monkeypatch.setattr(jobspy_src, "_scrape", lambda **kw: pd.DataFrame([_li(1, "Security A")]))
    monkeypatch.setattr(jobspy_src, "_preload", lambda: None)
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: None)

    def boom():
        raise ImportError("no jobspy")

    monkeypatch.setattr(jobspy_src, "_detail_fetcher", boom)
    ctx = _ctx(["linkedin"])
    ctx.filters_cfg = FILTERS
    jobs = jobspy_src.fetch(ctx)
    assert len(jobs) == 1 and jobs[0].description == ""
    assert any("linkedin descriptions unavailable" in w for w in ctx.warnings)


# ---- the wrapper around JobSpy's private LinkedIn page fetch ----

LI_PAGE = """<html><body>
<div class="show-more-less-html__markup"><p>Build <b>detections</b>.</p></div>
<div class="compensation__salary">$150,000.00/yr - $200,000.00/yr</div>
</body></html>"""


def test_jobspy_linkedin_still_has_the_private_detail_method():
    """Phase 2 calls jobspy.linkedin.LinkedIn._fetch_details(job_id). If a JobSpy upgrade renames or
    removes it, this fails loudly instead of silently losing every LinkedIn description."""
    import inspect

    from jobspy.linkedin import LinkedIn
    assert callable(getattr(LinkedIn, "_fetch_details", None)), "JobSpy LinkedIn._fetch_details is gone"
    assert list(inspect.signature(LinkedIn._fetch_details).parameters) == ["self", "job_id"]


def test_detail_fetcher_parses_a_job_page(monkeypatch):
    from types import SimpleNamespace
    fetch = jobspy_src._detail_fetcher()
    urls = []

    def fake_get(url, *a, **kw):
        urls.append(url)
        return SimpleNamespace(status_code=200, url=url, text=LI_PAGE)

    monkeypatch.setattr(fetch.scraper.session, "get", fake_get)
    d = fetch("4242")
    assert urls == ["https://www.linkedin.com/jobs/view/4242"]
    assert "detections" in d["description"] and "<div" not in d["description"]
    assert (d["compensation"].min_amount, d["compensation"].max_amount) == (150000.0, 200000.0)


def test_detail_fetcher_returns_empty_on_429(monkeypatch):
    from types import SimpleNamespace
    fetch = jobspy_src._detail_fetcher()
    monkeypatch.setattr(fetch.scraper.session, "get",
                        lambda url, *a, **kw: SimpleNamespace(status_code=429, url=url, text=""))
    assert fetch("1") == {}


# ---- breaker cooldown: one retry after a pause before giving up on the site ----

def _flaky(monkeypatch, outcomes, cfg_extra=None, terms=("a", "b", "c", "d", "e")):
    """outcomes: one bool per LinkedIn search (False = logged 429); exhausted -> success."""
    calls, sleeps = [], []
    outcomes = iter(outcomes)

    def fake(**kw):
        calls.append(kw["search_term"])
        if not next(outcomes, True):
            logging.getLogger(LI_LOG).error("LinkedIn response status code 429")
            return pd.DataFrame()
        return pd.DataFrame([_row(job_url=f"https://li/{len(calls)}", description="d")])

    _patched(monkeypatch, fake)
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda s: sleeps.append(s))
    ctx = _ctx(["linkedin"], terms)
    ctx.cfg["jobspy"].update(cfg_extra or {})
    try:
        jobspy_src.fetch(ctx)
    except RuntimeError:  # every search failed
        pass
    return ctx, calls, sleeps


def test_cooldown_then_successful_retry_closes_the_breaker(monkeypatch):
    ctx, calls, sleeps = _flaky(monkeypatch, [False, False, True, True, True], {"breaker_cooldown_s": 90})
    assert calls == ["a", "b", "c", "d", "e"]
    assert 90 in sleeps
    assert not any("repeated failures" in w for w in ctx.warnings)
    assert "jobspy: 2 of 5 site searches failed" in ctx.warnings


def test_cooldown_then_failed_retry_skips_the_site(monkeypatch):
    ctx, calls, sleeps = _flaky(monkeypatch, [False, False, False], {"breaker_cooldown_s": 90})
    assert calls == ["a", "b", "c"]
    assert sleeps.count(90) == 1
    assert "jobspy: linkedin skipped after repeated failures (LinkedIn response status code 429)" in ctx.warnings


def test_cooldown_happens_only_once_per_site(monkeypatch):
    # fail, fail -> cooldown -> ok -> fail, fail -> skipped (no second cooldown)
    ctx, calls, sleeps = _flaky(monkeypatch, [False, False, True, False, False, True],
                                {"breaker_cooldown_s": 90}, terms=("a", "b", "c", "d", "e", "f"))
    assert calls == ["a", "b", "c", "d", "e"] and sleeps.count(90) == 1
    assert any("repeated failures" in w for w in ctx.warnings)


def test_cooldown_defaults_to_90s(monkeypatch):
    _, _, sleeps = _flaky(monkeypatch, [False, False, True])
    assert 90 in sleeps


def test_no_cooldown_when_no_terms_left_for_the_site(monkeypatch):
    _, calls, sleeps = _flaky(monkeypatch, [False, False], {"breaker_cooldown_s": 90}, terms=("a", "b"))
    assert calls == ["a", "b"] and 90 not in sleeps


def test_cooldown_never_sleeps_past_the_deadline(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(jobspy_src.time, "monotonic", lambda: now[0])
    ctx = _ctx(["linkedin"], ("a", "b", "c", "d"))
    ctx.deadline = 1030.0  # 30 s left when the breaker opens
    ctx.cfg["jobspy"]["breaker_cooldown_s"] = 90
    calls, sleeps = [], []

    def fake(**kw):
        calls.append(kw["search_term"])
        logging.getLogger(LI_LOG).error("LinkedIn response status code 429")
        return pd.DataFrame()

    _patched(monkeypatch, fake)

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    monkeypatch.setattr(jobspy_src.time, "sleep", sleep)
    try:
        jobspy_src.fetch(ctx)
    except RuntimeError:
        pass
    # the cooldown would end past the deadline: no pointless wait, the site is skipped as before
    assert 90 not in sleeps and max(sleeps) <= 7
    assert calls == ["a", "b"]
    assert any("repeated failures" in w for w in ctx.warnings)
