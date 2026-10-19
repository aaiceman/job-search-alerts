"""Remotive public API. Remote-only board; asks for low call volume (2 calls/day here)."""
from __future__ import annotations

from ..models import Job
from ..salary import parse_salary_text
from .base import SourceContext, iso_date, note_skip, require, run_queries, text
from .http import get_json, html_to_text

NAME = "remotive"
URL = "https://remotive.com/api/remote-jobs"


def _job(d: dict) -> Job:
    salary = text(d.get("salary"))
    rng = parse_salary_text(salary) if salary else None
    title, url = text(d.get("title")), text(d.get("url"))
    require(title, url)
    return Job(
        source=NAME, url=url, title=title, company=text(d.get("company_name")),
        location=text(d.get("candidate_required_location")), remote=True,
        salary_min=rng[0] if rng else None, salary_max=rng[1] if rng else None,
        salary_text=salary, employment_type=text(d.get("job_type")),
        posted=iso_date(d.get("publication_date")), description=html_to_text(d.get("description")),
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
    return run_queries(ctx, NAME, ctx.cfg.get("remotive_categories", []),
                       lambda cat, skipped: parse(get_json(URL, {"category": cat}), skipped))
