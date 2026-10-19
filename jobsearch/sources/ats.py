"""Greenhouse / Lever / Ashby public job-board APIs for companies listed in config/portals.yml."""
from __future__ import annotations

import logging
import re
import time

from ..models import Job
from ..salary import annualize, parse_salary_text
from .base import (SourceContext, epoch_date, iso_date, note_skip, note_stopped_early, past_deadline, require,
                   text)
from .http import get_json, html_to_text

log = logging.getLogger(__name__)
NAME = "ats"
URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}
PARAMS = {"greenhouse": {"content": "true"}, "lever": {"mode": "json"}, "ashby": {"includeCompensation": "true"}}
_LEVER_INTERVAL = {"per-year-salary": "yearly", "per-hour-wage": "hourly", "per-month-salary": "monthly"}


_BOARD_SUFFIX = re.compile(r"(?:^|\s+)(?:job board|careers|jobs)\s*$", re.I)


def _greenhouse_company(name: str, fallback: str) -> str:
    """Greenhouse's company_name is often "Rubrik Job Board"; strip that suffix, else use the portal name."""
    return _BOARD_SUFFIX.sub("", name).strip() or fallback


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _greenhouse_job(d: dict, company: str) -> Job:
    loc = d.get("location")
    location = text(loc.get("name")) if isinstance(loc, dict) else (loc if isinstance(loc, str) else "")
    title, url = text(d.get("title")), text(d.get("absolute_url"))
    require(title, url)
    return Job(
        source="greenhouse", url=url, title=title, company=_greenhouse_company(text(d.get("company_name")), company),
        location=location, remote=None,
        posted=iso_date(d.get("first_published")) or iso_date(d.get("updated_at")),
        description=html_to_text(text(d.get("content"))),  # Greenhouse double-escapes its HTML
    )


def _lever_job(d: dict, company: str) -> Job:
    cats, sr = _dict(d.get("categories")), _dict(d.get("salaryRange"))
    interval = _LEVER_INTERVAL.get(text(sr.get("interval")), "yearly")
    usd = (text(sr.get("currency")) or "USD").upper() == "USD"
    lists = d.get("lists") if isinstance(d.get("lists"), list) else []
    parts = [text(d.get("descriptionPlain"))]
    parts += [f"{text(l.get('text'))}\n{html_to_text(l.get('content'))}" for l in lists if isinstance(l, dict)]
    parts.append(text(d.get("additionalPlain")))
    wt = text(d.get("workplaceType")).lower()
    title, url = text(d.get("text")), text(d.get("hostedUrl"))
    require(title, url)
    return Job(
        source="lever", url=url, title=title, company=company, location=text(cats.get("location")),
        remote=True if wt == "remote" else (False if wt in ("onsite", "on-site", "hybrid") else None),
        salary_min=annualize(sr.get("min"), interval) if usd else None,
        salary_max=annualize(sr.get("max"), interval) if usd else None,
        employment_type=text(cats.get("commitment")), posted=epoch_date(d.get("createdAt")),
        description="\n".join(p for p in parts if p),
    )


def _ashby_job(d: dict, company: str) -> Job:
    comp = text(_dict(d.get("compensation")).get("compensationTierSummary"))
    rng = parse_salary_text(comp) if comp else None
    secondary = d.get("secondaryLocations") if isinstance(d.get("secondaryLocations"), list) else []
    locs = [text(d.get("location"))] + [text(_dict(s).get("location")) for s in secondary]
    wt = text(d.get("workplaceType")).lower()
    if wt == "remote" or (d.get("isRemote") and wt not in ("hybrid", "onsite", "on-site")):
        remote = True
    else:
        remote = False if wt in ("hybrid", "onsite", "on-site") else None
    title, url = text(d.get("title")).strip(), text(d.get("jobUrl"))
    require(title, url)
    return Job(
        source="ashby", url=url, title=title, company=company,
        location="; ".join(l for l in locs if l), remote=remote,
        salary_min=rng[0] if rng else None, salary_max=rng[1] if rng else None, salary_text=comp,
        employment_type=text(d.get("employmentType")), posted=iso_date(d.get("publishedAt")),
        description=text(d.get("descriptionPlain")),
    )


def _parse(items, build, company: str, skipped: list | None, drop=lambda d: False) -> list[Job]:
    if not isinstance(items, list):
        return []
    jobs = []
    for d in items:
        try:
            if not isinstance(d, dict):
                raise TypeError("posting is not an object")
            if drop(d):
                continue
            jobs.append(build(d, company))
        except Exception as e:
            note_skip(skipped, d, e)
    return jobs


def parse_greenhouse(data, company: str, skipped: list | None = None) -> list[Job]:
    return _parse(data.get("jobs") if isinstance(data, dict) else None, _greenhouse_job, company, skipped)


def parse_lever(data, company: str, skipped: list | None = None) -> list[Job]:
    return _parse(data, _lever_job, company, skipped)


def parse_ashby(data, company: str, skipped: list | None = None) -> list[Job]:
    return _parse(data.get("jobs") if isinstance(data, dict) else None, _ashby_job, company, skipped,
                  drop=lambda d: d.get("isListed") is False)


PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}


def fetch(ctx: SourceContext) -> list[Job]:
    out: list[Job] = []
    failures = attempted = 0
    skipped: list = []
    for i, p in enumerate(ctx.portals):
        if past_deadline(ctx):
            note_stopped_early(ctx, NAME, len(ctx.portals) - i, len(ctx.portals), "company boards")
            break
        if i:
            time.sleep(0.5)
        attempted += 1
        try:
            data = get_json(URLS[p["ats"]].format(slug=p["slug"]), PARAMS[p["ats"]])
            out += PARSERS[p["ats"]](data, p["name"], skipped)
        except Exception as e:  # one dead board must not sink the source
            failures += 1
            log.warning("ats %s/%s failed: %s", p.get("ats"), p.get("slug"), e)
    if failures:
        ctx.warnings.append(f"ats: {failures} of {len(ctx.portals)} company boards failed")
    if skipped:
        ctx.warnings.append(f"ats: {len(skipped)} items skipped")
    if attempted and failures == attempted:
        raise RuntimeError("all ATS boards failed")
    return out
