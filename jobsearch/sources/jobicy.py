"""Jobicy v2 API (remote-only). Requires credit + original apply links, which the digest keeps."""
from __future__ import annotations

from ..models import Job
from ..salary import annualize
from .base import SourceContext, iso_date, note_skip, require, run_queries, text
from .http import get_json, html_to_text

NAME = "jobicy"
URL = "https://jobicy.com/api/v2/remote-jobs"


def _job(d: dict) -> Job:
    usd = (text(d.get("salaryCurrency")) or "USD").upper() == "USD"
    period = text(d.get("salaryPeriod")) or "yearly"
    lo = annualize(d.get("salaryMin") or d.get("annualSalaryMin"), period) if usd else None
    hi = annualize(d.get("salaryMax") or d.get("annualSalaryMax"), period) if usd else None
    title, url = text(d.get("jobTitle")), text(d.get("url"))
    require(title, url)
    return Job(
        source=NAME, url=url, title=title, company=text(d.get("companyName")),
        location=text(d.get("jobGeo")), remote=True, salary_min=lo, salary_max=hi,
        employment_type=text(d.get("jobType")), posted=iso_date(d.get("pubDate")),
        description=html_to_text(d.get("jobDescription")),
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


def fetch(ctx: SourceContext) -> list[Job]:
    return run_queries(ctx, NAME, ctx.cfg.get("jobicy_tags", []),
                       lambda tag, skipped: parse(get_json(URL, {"count": 50, "geo": "usa", "tag": tag}), skipped))
