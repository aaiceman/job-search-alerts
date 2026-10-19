"""Himalayas search API (remote-only)."""
from __future__ import annotations

import logging
import time

from ..models import Job
from ..salary import annualize
from .base import QUERY_PAUSE_S, SourceContext, epoch_date, note_skip, require, run_queries, text
from .http import get_json, html_to_text

log = logging.getLogger(__name__)
NAME = "himalayas"
URL = "https://himalayas.app/jobs/api/search"
MAX_PAGES = 3


def _job(d: dict) -> Job:
    period = text(d.get("salaryPeriod")) or "yearly"
    usd = (text(d.get("currency")) or "USD").upper() == "USD"
    lo = annualize(d.get("minSalary"), period) if usd else None
    hi = annualize(d.get("maxSalary"), period) if usd else None
    title, url = text(d.get("title")), text(d.get("applicationLink")) or text(d.get("guid"))
    require(title, url)
    return Job(
        source=NAME, url=url, title=title, company=text(d.get("companyName")),
        location=text(d.get("locationRestrictions")) or "Anywhere", remote=True,
        salary_min=lo, salary_max=hi,
        salary_text=f"{d.get('minSalary')}-{d.get('maxSalary')} {d.get('currency')}/{period}" if (lo or hi) else "",
        employment_type=text(d.get("employmentType")), posted=epoch_date(d.get("pubDate")),
        description=html_to_text(d.get("description")),
    )


def parse(data, skipped: list | None = None) -> list[Job]:
    items = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    jobs = []
    for d in items:
        try:
            jobs.append(_job(d))
        except Exception as e:
            note_skip(skipped, d, e)
    return jobs


def _query(ctx: SourceContext, q, skipped: list) -> list[Job]:
    out: list[Job] = []
    cursor = None
    for page in range(MAX_PAGES):
        params = {"q": q, "country": "US"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = get_json(URL, params)
        except Exception as e:
            if page == 0:
                raise
            log.warning("himalayas %r page %d failed: %s", q, page + 1, e)
            ctx.warnings.append(f"{NAME}: pagination stopped early for {q!r}")
            break
        out += parse(data, skipped)
        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not cursor or not data.get("jobs"):
            break
        if page < MAX_PAGES - 1:
            time.sleep(QUERY_PAUSE_S)
    return out


def fetch(ctx: SourceContext) -> list[Job]:
    return run_queries(ctx, NAME, ctx.cfg.get("himalayas_queries", []), lambda q, skipped: _query(ctx, q, skipped))
