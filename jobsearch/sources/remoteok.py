"""RemoteOK public API (remote-only). Element 0 of the response is a legal notice."""
from __future__ import annotations

from ..models import Job
from ..salary import annualize
from .base import SourceContext, iso_date, note_skip, require, run_queries, text
from .http import get_json, html_to_text

NAME = "remoteok"
URL = "https://remoteok.com/api"


def _job(d: dict) -> Job:
    title, url = text(d.get("position")), text(d.get("url")) or text(d.get("apply_url"))
    require(title, url)
    return Job(
        source=NAME, url=url, title=title, company=text(d.get("company")),
        location=text(d.get("location")).strip() or "Anywhere",
        remote=True, salary_min=annualize(d.get("salary_min"), "yearly"),
        salary_max=annualize(d.get("salary_max"), "yearly"),
        posted=iso_date(d.get("date")), description=html_to_text(d.get("description")),
    )


def parse(data, skipped: list | None = None) -> list[Job]:
    if not isinstance(data, list):
        return []
    jobs = []
    for d in data:
        try:
            if isinstance(d, dict) and "id" not in d:
                continue  # the legal-notice element
            if not isinstance(d, dict):
                raise TypeError("job entry is not an object")
            jobs.append(_job(d))
        except Exception as e:
            note_skip(skipped, d, e)
    return jobs


def fetch(ctx: SourceContext) -> list[Job]:
    return run_queries(ctx, NAME, ctx.cfg.get("remoteok_tags", []),
                       lambda tag, skipped: parse(get_json(URL, {"tag": tag}), skipped))
