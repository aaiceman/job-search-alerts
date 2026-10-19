"""Run every source → dedupe → hard filters → drop already-sent → cap. CLI: --dry-run for smoke tests."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from .config import data_path, load_config
from .dedupe import merge_duplicates
from .exclusions import Exclusions
from .filters import apply_filters
from .models import Job
from .sources import ats, himalayas, jobicy, jobspy_src, remoteok, remotive, wwr
from .sources.base import SourceContext
from .store import Store

log = logging.getLogger(__name__)
_monotonic = _time.monotonic

SOURCES = {
    "remotive": remotive.fetch, "himalayas": himalayas.fetch, "jobicy": jobicy.fetch,
    "remoteok": remoteok.fetch, "wwr": wwr.fetch, "ats": ats.fetch, "jobspy": jobspy_src.fetch,
}


@dataclass
class SourceStatus:
    name: str
    ok: bool
    count: int
    error: str = ""
    seconds: float = 0.0  # wall time spent in the source's fetch (0 when skipped)


def window_hours(today: date, searches: dict) -> int:
    w = searches.get("window_hours") or {}
    return w.get("monday", 72) if today.weekday() == 0 else w.get("default", 24)


def _has_salary(j: Job) -> bool:
    return j.salary_max is not None or j.salary_min is not None


def collect(today: date, searches: dict, filters_cfg: dict, exclusions: Exclusions, store: Store,
            portals: list[dict], window_override: int | None = None,
            only: list[str] | None = None, deadline: float | None = None) -> tuple[list[Job], dict]:
    """deadline: soft collection budget as a time.monotonic() value. Once it has passed, the remaining
    sources are skipped and reported; a running source sees it as ctx.deadline and stops issuing new
    requests (a request already in flight is never interrupted)."""
    hours = window_override or window_hours(today, searches)
    # Jobs already emailed: sources skip per-job detail requests for them (LinkedIn job pages).
    sent_keys = {row[0] for row in store.db.execute("SELECT job_key FROM sent")}
    ctx = SourceContext(cfg=searches, hours_old=hours, portals=portals, deadline=deadline,
                        filters_cfg=filters_cfg, skip_detail=lambda j: j.key in sent_keys)
    raw: list[Job] = []
    statuses: list[SourceStatus] = []
    skipped: list[str] = []
    for name, fetch in SOURCES.items():
        if only and name not in only:
            continue
        if deadline is not None and _monotonic() > deadline:
            skipped.append(name)
            statuses.append(SourceStatus(name, False, 0, "skipped: time budget"))
            continue
        start = _monotonic()
        try:
            jobs = fetch(ctx)
            secs = _monotonic() - start
            raw += jobs
            statuses.append(SourceStatus(name, True, len(jobs), seconds=round(secs, 1)))
            log.info("source %s: %d jobs in %.0f s", name, len(jobs), secs)
        except Exception as e:
            secs = _monotonic() - start
            err = f"{type(e).__name__}: {e}"[:200]
            log.exception("source %s FAILED after %.0f s: %s", name, secs, err)
            statuses.append(SourceStatus(name, False, 0, err, seconds=round(secs, 1)))
    if skipped:
        log.warning("collection time budget reached; skipped: %s", ", ".join(skipped))
        ctx.warnings.append(f"collection time budget reached; skipped: {', '.join(skipped)}")
    merged = merge_duplicates(raw)
    since = (today - timedelta(hours=hours) - timedelta(days=1)).isoformat()  # 1 day slack
    fr = apply_filters(merged, filters_cfg, exclusions, since)
    fresh = store.unsent(fr.kept, today)
    fresh.sort(key=lambda j: j.posted or "", reverse=True)  # newest first (stable)...
    fresh.sort(key=lambda j: 0 if _has_salary(j) else 1)    # ...then stated-salary jobs ahead of unlisted
    cap = filters_cfg.get("max_candidates", 100)
    capped, over = fresh[:cap], max(0, len(fresh) - cap)
    stats = {
        "window_hours": hours, "collected": len(raw), "unique": len(merged), "dropped": dict(fr.dropped),
        "excluded": dict(fr.excluded), "already_sent": len(fr.kept) - len(fresh), "over_cap": over,
        "candidates": len(capped), "sources": [asdict(s) for s in statuses], "warnings": ctx.warnings,
    }
    return capped, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect + filter jobs without ranking or sending.")
    ap.add_argument("--dry-run", action="store_true", help="collect + filter only (this CLI never marks jobs sent)")
    ap.add_argument("--window-hours", type=int)
    ap.add_argument("--source", action="append", help="limit to a source (repeatable)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for name in args.source or []:
        if name not in SOURCES:
            print(f"warning: unknown --source {name!r}; known: {', '.join(SOURCES)}", file=sys.stderr)
    store = Store(data_path("jobsearch.db"))
    try:
        cands, stats = collect(
            date.today(), load_config("searches.yaml"), load_config("filters.yaml"),
            Exclusions.load(data_path("config", "exclusions.yaml")), store,
            load_config("portals.yml").get("portals", []), args.window_hours, args.source,
        )
    finally:
        store.close()
    print(json.dumps(stats, indent=2))
    for j in cands:
        sal = f"${(j.salary_max or j.salary_min or 0) / 1000:.0f}k" if (j.salary_max or j.salary_min) else "—"
        print(f"{sal:>6}  {j.title}  @ {j.company}  [{j.source}]  {'; '.join(j.flags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
