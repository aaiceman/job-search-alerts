"""Collapse the same posting seen on several sources into one Job with all links."""
from __future__ import annotations

from dataclasses import replace

from .models import Job

_ATS = {"greenhouse", "lever", "ashby"}
_AGGREGATORS = {"linkedin", "indeed"}


def _is_direct(j: Job) -> bool:
    """True for a posting hosted by the employer's own ATS (or any non-LinkedIn/Indeed link)."""
    sources = set(j.source.split("+"))
    if sources & _ATS:
        return True
    url = j.url.lower()
    return not (sources <= _AGGREGATORS or "linkedin." in url or "indeed." in url)


def _merge_group(group: list[Job]) -> Job:
    # direct/ATS first, then longest description; max() keeps the earliest member on ties
    primary = max(group, key=lambda j: (_is_direct(j), len(j.description)))
    members = [primary] + [j for j in group if j is not primary]

    extra_urls = list(primary.extra_urls)
    sources = primary.source.split("+")
    locations: list[str] = []
    flags = list(primary.flags)
    for j in members:
        for u in [j.url, *j.extra_urls]:
            if u and u != primary.url and u not in extra_urls:
                extra_urls.append(u)
        for src in j.source.split("+"):
            if src not in sources:
                sources.append(src)
        loc = j.location.strip()
        if loc and loc not in locations:
            locations.append(loc)
        for f in j.flags:
            if f not in flags:
                flags.append(f)

    mins = [j.salary_min for j in members if j.salary_min is not None]
    maxs = [j.salary_max for j in members if j.salary_max is not None]
    remotes = [j.remote for j in members]
    return replace(
        primary,
        source="+".join(sources),
        location="; ".join(locations),
        remote=True if True in remotes else (False if False in remotes else None),
        salary_min=min(mins) if mins else None,
        salary_max=max(maxs) if maxs else None,
        salary_text=next((j.salary_text for j in members if j.salary_text), ""),
        employment_type=next((j.employment_type for j in members if j.employment_type), ""),
        posted=max((j.posted for j in members if j.posted), default=""),
        extra_urls=extra_urls,
        flags=flags,
    )


def merge_duplicates(jobs: list[Job]) -> list[Job]:
    groups: dict[str, list[Job]] = {}
    for j in jobs:
        groups.setdefault(j.key, []).append(j)
    return [g[0] if len(g) == 1 else _merge_group(g) for g in groups.values()]
