"""LinkedIn + Indeed via python-jobspy (public, logged-out endpoints). ZipRecruiter (403) and
Google (0 rows) were dead on 2026-09-29, so config lists only linkedin + indeed.

JobSpy >= 1.2 gives each request a single 15 s timeout with no retries and logs board errors instead
of raising. fetch() therefore treats an ERROR record from a "JobSpy:<Site>" logger as a failed search,
opens a per-site circuit breaker after BREAKER_FAILURES consecutive failures, and stops issuing searches
once the collection deadline (ctx.deadline) has passed. When the breaker opens and the site still has
terms left, it first cools down (jobspy.breaker_cooldown_s, never past the deadline) and retries ONE term;
a success closes the breaker, a failure skips the rest of that site.

LinkedIn runs in two phases (2026-10-15, LinkedIn 429s): the searches go out with fetch_description=False
(one request per term), then _fetch_linkedin_details() fetches job pages only for rows whose title passes
filters.title_ok and that were not emailed before (ctx.skip_detail), at most jobspy.max_detail_fetches per
run. A failed page fetch only leaves that job without a description. Indeed already returns descriptions in
its search response and is unchanged."""
from __future__ import annotations

import logging
import math
import random
import re
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from ..filters import title_ok
from ..models import Job
from ..salary import annualize
from .base import SourceContext, iso_date, note_skip, note_stopped_early, past_deadline, require, text

log = logging.getLogger(__name__)
NAME = "jobspy"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return None if str(v) in ("nan", "NaT", "None", "<NA>", "") else v


_SITE_LABELS = {"indeed": "Indeed", "linkedin": "LinkedIn"}
_BOARD_HOSTS = ("indeed.com", "linkedin.com")


def _company(r: dict, site: str) -> str:
    """The listed company, else the host of company_url, else "Unknown company (<site>)"."""
    name = text(_clean(r.get("company"))).strip()
    if name:
        return name
    url = text(_clean(r.get("company_url"))).strip()
    if url:
        try:
            host = (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
        except ValueError:
            host = ""
        host = host.removeprefix("www.")
        # the job board's own page says nothing about the employer
        if host and not any(host == b or host.endswith("." + b) for b in _BOARD_HOSTS):
            return host
    return f"Unknown company ({_SITE_LABELS.get(site.lower(), site.title() or NAME)})"


def _job(r: dict) -> Job:
    interval = text(_clean(r.get("interval"))) or "yearly"
    usd = (text(_clean(r.get("currency"))) or "USD").upper() == "USD"
    # JobSpy's amounts are reliable only when the site published them; salary_source "description"
    # means JobSpy guessed from prose, so leave it to the careful description parser instead.
    source = _clean(r.get("salary_source"))
    structured = usd and (source is None or str(source) == "direct_data")
    lo = annualize(_clean(r.get("min_amount")), interval) if structured else None
    hi = annualize(_clean(r.get("max_amount")), interval) if structured else None
    direct = text(_clean(r.get("job_url_direct")))
    remote = _clean(r.get("is_remote"))
    title, url = text(_clean(r.get("title"))), text(_clean(r.get("job_url")))
    require(title, url)
    site = text(_clean(r.get("site"))) or NAME
    return Job(
        source=site, url=url, title=title,
        company=_company(r, site), location=text(_clean(r.get("location"))),
        remote=bool(remote) if remote is not None else None,
        salary_min=lo, salary_max=hi, salary_text=f"{interval} (structured)" if (lo or hi) else "",
        employment_type=text(_clean(r.get("job_type"))), posted=iso_date(_clean(r.get("date_posted"))),
        description=text(_clean(r.get("description"))), extra_urls=[direct] if direct else [],
    )


def parse(df, skipped: list | None = None) -> list[Job]:
    if df is None or len(df) == 0:
        return []
    jobs = []
    for r in df.to_dict("records"):
        try:
            jobs.append(_job(r))
        except Exception as e:
            note_skip(skipped, r.get("job_url"), e)
    return jobs


def _preload() -> None:
    """Import JobSpy before the first search so its "JobSpy:<Site>" loggers exist when the error capture
    attaches (they are created at import time with propagate=False). A broken install is left for the
    search itself to report."""
    try:
        import jobspy  # noqa: F401
    except Exception as e:  # pragma: no cover - depends on the installed package
        log.warning("jobspy import failed: %s", e)


def _scrape(**kw):
    from jobspy import scrape_jobs  # lazy: heavy import, only needed at fetch time
    return scrape_jobs(**kw)


_LOGGER_PREFIX = "JobSpy"
BREAKER_FAILURES = 2  # consecutive failed searches on one site before its remaining terms are skipped
BREAKER_COOLDOWN_S = 90  # default pause before the one retry after the breaker opens
MAX_DETAIL_FETCHES = 40  # default cap on LinkedIn job-page fetches per run
DETAIL_STOP_FAILURES = 3  # consecutive failed job-page fetches before phase 2 gives up for the run
EXCERPT_CHARS = 120


class _ErrorCapture(logging.Handler):
    """Collects ERROR+ records from JobSpy's loggers. Since 1.2, JobSpy logs board failures (HTTP status,
    connection errors, block pages) instead of raising, so this is how a failed search is detected."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []
        self._seen: set[int] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(_LOGGER_PREFIX) or id(record) in self._seen:
            return
        self._seen.add(id(record))  # a propagating logger reaches both attachment points
        try:
            self.messages.append(record.getMessage())
        except Exception:  # a malformed record must not break the scrape
            self.messages.append(str(record.msg))


@contextmanager
def _capture_errors():
    """Attach an _ErrorCapture to every JobSpy logger (they don't propagate) and to the root logger (for
    any that do), and detach afterwards."""
    cap = _ErrorCapture()
    targets = [logging.getLogger()] + [
        logging.getLogger(n) for n, lg in list(logging.root.manager.loggerDict.items())
        if n.startswith(_LOGGER_PREFIX) and isinstance(lg, logging.Logger)]
    for lg in targets:
        lg.addHandler(cap)
    try:
        yield cap
    finally:
        for lg in targets:
            lg.removeHandler(cap)


def _kwargs(site: str, term: str, results: int, hours_old: int) -> dict:
    kw = dict(
        site_name=[site], search_term=term, location="United States", results_wanted=results,
        hours_old=hours_old, country_indeed="USA", fetch_description=False,  # LinkedIn pages: phase 2
        description_format="markdown", verbose=0,
    )
    if site == "indeed":
        # Logged-out LinkedIn search ignores is_remote/job_type (JobSpy 1.3 warns), so they go to Indeed
        # only; the " remote" suffix is kept as a belt-and-braces remote signal on Indeed.
        kw.update(search_term=f"{term} remote", is_remote=True)
    return kw


_LI_JOB_ID = re.compile(r"linkedin\.com/jobs/view/(\d+)")


def _detail_fetcher():
    """A callable job_id -> dict built on JobSpy's own LinkedIn scraper (one session, JobSpy's headers).

    It wraps jobspy.linkedin.LinkedIn._fetch_details(job_id): private, but in python-jobspy 1.3.0 it is the
    helper the search itself calls when fetch_description=True. It GETs /jobs/view/{job_id} and parses the
    description (markdown), compensation, job_type and more; it returns {} on any failure (non-200 such as
    429, a redirect to a login wall, a network error) and logs a WARNING on "JobSpy:LinkedIn".
    tests/test_sources_jobspy.py fails loudly if a JobSpy upgrade renames or removes it."""
    from jobspy.linkedin import LinkedIn
    from jobspy.model import DescriptionFormat, ScraperInput

    scraper = LinkedIn()
    # _fetch_details reads description_format from the scraper input a search would have set
    scraper.scraper_input = ScraperInput(fetch_description=True, description_format=DescriptionFormat.MARKDOWN)

    def fetch_details(job_id: str) -> dict:
        return scraper._fetch_details(job_id)

    fetch_details.scraper = scraper  # tests fake its session
    return fetch_details


def _apply_details(job: Job, d: dict) -> bool:
    """Copy a LinkedIn job page's description, pay and employment type onto job; True if it had a
    description. Pay from the page's compensation box is site data (like salary_source direct_data)."""
    desc = text(_clean(d.get("description"))).strip()
    if desc:
        job.description = desc
    comp = d.get("compensation")
    if comp is not None and job.salary_min is None and job.salary_max is None:
        interval = getattr(getattr(comp, "interval", None), "value", None) or "yearly"
        if (getattr(comp, "currency", None) or "USD").upper() == "USD":
            lo = annualize(getattr(comp, "min_amount", None), interval)
            hi = annualize(getattr(comp, "max_amount", None), interval)
            if lo or hi:
                job.salary_min, job.salary_max, job.salary_text = lo, hi, f"{interval} (structured)"
    types = d.get("job_type") or []
    if types and not job.employment_type:
        job.employment_type = ", ".join(t.value[0] for t in types if getattr(t, "value", None))
    return bool(desc)


def _fetch_linkedin_details(ctx: SourceContext, js: dict, jobs: list[Job]) -> None:
    """Phase 2: job-page fetches for LinkedIn rows that can still reach the digest (title passes, not
    emailed before, no description yet), deduped by job id, capped and paced like the searches."""
    fcfg = ctx.filters_cfg or {}
    include, exclude = fcfg.get("title_include") or [], fcfg.get("title_exclude") or []
    by_id: dict[str, list[Job]] = {}
    for j in jobs:
        if j.source != "linkedin" or j.description:
            continue
        m = _LI_JOB_ID.search(j.url)
        if not m or not title_ok(j.title, include, exclude):
            continue
        if ctx.skip_detail is not None and ctx.skip_detail(j):
            continue
        by_id.setdefault(m.group(1), []).append(j)
    if not by_id:
        return
    cap = int(js.get("max_detail_fetches", MAX_DETAIL_FETCHES))
    todo = list(by_id)[:max(cap, 0)]
    over = len(by_id) - len(todo)
    if over:
        log.info("jobspy: linkedin description cap %d reached; %d jobs kept without a description", cap, over)
    if not todo:
        return
    try:
        fetch_details = _detail_fetcher()
    except Exception as e:
        log.warning("jobspy: linkedin description fetcher unavailable: %s", e)
        ctx.warnings.append(f"jobspy: linkedin descriptions unavailable ({type(e).__name__}: {e})"[:200])
        return
    got = failed = streak = 0
    for n, job_id in enumerate(todo):
        if past_deadline(ctx):
            note_stopped_early(ctx, NAME, len(todo) - n, len(todo), "LinkedIn description fetches")
            break
        time.sleep(random.uniform(3, 7))
        try:
            d = fetch_details(job_id) or {}
        except Exception as e:
            log.info("jobspy: linkedin description fetch for job %s raised %s: %s", job_id, type(e).__name__, e)
            d = {}
        ok = False
        for j in by_id[job_id]:
            ok = _apply_details(j, d) or ok
        if ok:
            got += 1
            streak = 0
            continue
        failed += 1
        streak += 1
        log.info("jobspy: linkedin description fetch for job %s returned no description", job_id)
        if streak >= DETAIL_STOP_FAILURES:
            left = len(todo) - n - 1
            if left:
                log.warning("jobspy: linkedin descriptions stopped after %d consecutive failures", streak)
                ctx.warnings.append(f"jobspy: linkedin descriptions stopped after {streak} consecutive failed "
                                    f"page fetches; {left} jobs kept without a description")
            break
    log.info("jobspy: linkedin descriptions: %d fetched, %d failed, %d over the cap (%d eligible)",
             got, failed, over, len(by_id))


def _cooldown(ctx: SourceContext, site: str, seconds: float) -> bool:
    """Pause before the one retry after the breaker opens. False (no pause, no retry) when the pause
    would run into the collection deadline."""
    if ctx.deadline is not None and time.monotonic() + seconds >= ctx.deadline:
        log.warning("jobspy: %s circuit breaker open; no time for a %.0f s cooldown before the deadline",
                    site, seconds)
        return False
    log.warning("jobspy: %s circuit breaker open; cooling down %.0f s before one retry", site, seconds)
    if seconds > 0:
        time.sleep(seconds)
    return True


def fetch(ctx: SourceContext) -> list[Job]:
    js = ctx.cfg["jobspy"]
    out: list[Job] = []
    failures = total = 0
    skipped: list = []
    ok_calls: dict[str, int] = {}
    rows: dict[str, int] = {}
    streak: dict[str, int] = {}       # consecutive failed searches per site
    tripped: set[str] = set()
    cooled: set[str] = set()          # sites that already had their one cooldown retry this run
    cooldown_s = float(js.get("breaker_cooldown_s", BREAKER_COOLDOWN_S))
    plan = [(term, site) for term in js["terms"] for site in js["sites"]]
    _preload()
    for i, (term, site) in enumerate(plan):
        if site in tripped:
            continue
        if past_deadline(ctx):
            left = sum(1 for _, s in plan[i:] if s not in tripped)
            note_stopped_early(ctx, NAME, left, len(plan), "searches")
            break
        if total:
            time.sleep(random.uniform(3, 7))
        total += 1
        df, error = None, ""
        with _capture_errors() as cap:
            try:
                df = _scrape(**_kwargs(site, term, js.get("results_per_term", 15), ctx.hours_old))
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("jobspy %s %r failed: %s", site, term, e)
        if df is not None:
            out += parse(df, skipped)
            rows[site] = rows.get(site, 0) + len(df)
        if not error and cap.messages:
            error = cap.messages[-1]
            log.warning("jobspy %s %r logged %d error(s); last: %s", site, term, len(cap.messages), error[:200])
        if error:
            failures += 1
            streak[site] = streak.get(site, 0) + 1
            more = any(s == site for _, s in plan[i + 1:])
            if streak[site] >= BREAKER_FAILURES and more and site not in cooled and _cooldown(ctx, site, cooldown_s):
                cooled.add(site)  # the next search on this site is the retry; failing it trips the breaker
            elif streak[site] >= BREAKER_FAILURES:
                tripped.add(site)
                if more:
                    excerpt = " ".join(error.split())[:EXCERPT_CHARS]
                    log.warning("jobspy: %s circuit breaker open after %d consecutive failures", site, streak[site])
                    ctx.warnings.append(f"jobspy: {site} skipped after repeated failures ({excerpt})")
        else:
            streak[site] = 0
            ok_calls[site] = ok_calls.get(site, 0) + 1
    if "linkedin" in js["sites"]:
        _fetch_linkedin_details(ctx, js, out)
    if failures:
        ctx.warnings.append(f"jobspy: {failures} of {total} site searches failed")
    for site in js["sites"]:
        if ok_calls.get(site) and not rows.get(site):
            ctx.warnings.append(f"jobspy: {site} returned no results")
    if skipped:
        ctx.warnings.append(f"jobspy: {len(skipped)} items skipped")
    if total and failures == total and not out:
        raise RuntimeError("all jobspy searches failed")
    return out
