"""Application watch: is the posting I applied to still up? Public ATS endpoints only, read-only GETs.
Every failure degrades to status "unknown"; one bad entry never breaks the run."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .sources.http import get as http_get

log = logging.getLogger(__name__)
TIMEOUT_S = 15
RETRIES = 1


@dataclass
class WatchResult:
    status: str            # "open" | "closed" | "unknown"
    detail: str = ""
    updated_at: str = ""   # the listing's own last-updated stamp, when the ATS gives one (Greenhouse)


def watch_key(entry: dict) -> str:
    return f"{entry.get('ats')}:{entry.get('board')}:{entry.get('job_id')}"


def _q(v) -> str:
    return quote(str(v), safe="")


def _fetch(url: str):
    """(response, False) on 2xx; (None, True) on 404; raises on anything else."""
    try:
        return http_get(url, timeout=TIMEOUT_S, retries=RETRIES), False
    except requests.HTTPError as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return None, True
        raise


def _single(url: str) -> WatchResult:
    """Endpoints addressing exactly one posting: 200 = open, 404 = closed."""
    resp, gone = _fetch(url)
    if gone:
        return WatchResult("closed", "HTTP 404")
    payload = resp.json()
    updated = payload.get("updated_at") if isinstance(payload, dict) else None
    return WatchResult("open", "HTTP 200", updated if isinstance(updated, str) else "")


def _greenhouse(e: dict) -> WatchResult:
    return _single(f"https://boards-api.greenhouse.io/v1/boards/{_q(e['board'])}/jobs/{_q(e['job_id'])}")


def _lever(e: dict) -> WatchResult:
    return _single(f"https://api.lever.co/v0/postings/{_q(e['board'])}/{_q(e['job_id'])}")


def _ashby(e: dict) -> WatchResult:
    resp, gone = _fetch(f"https://api.ashbyhq.com/posting-api/job-board/{_q(e['board'])}")
    if gone:  # the whole board is gone: say nothing about this posting
        return WatchResult("unknown", "board not found (HTTP 404)")
    data = resp.json()
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return WatchResult("unknown", "unexpected board response")
    want = str(e["job_id"])
    for j in jobs:
        if isinstance(j, dict) and str(j.get("id")) == want and j.get("isListed") is not False:
            return WatchResult("open", "listed on board")
    return WatchResult("closed", "not on board")


CHECKERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def check_posting(entry) -> WatchResult:
    """Status of one watched posting. Never raises."""
    try:
        if not isinstance(entry, dict):
            return WatchResult("unknown", "entry is not a mapping")
        ats = str(entry.get("ats") or "").lower()
        if ats not in CHECKERS:
            return WatchResult("unknown", f"unsupported ats {ats!r}")
        if not entry.get("board") or not entry.get("job_id"):
            return WatchResult("unknown", "entry missing board/job_id")
        return CHECKERS[ats](entry)
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        return WatchResult("unknown", f"HTTP {code}")
    except Exception as e:
        return WatchResult("unknown", f"{type(e).__name__}: {str(e)[:120]}")


def check_all(entries: list[dict]) -> list[tuple[dict, WatchResult]]:
    out = []
    for e in entries:
        key = watch_key(e) if isinstance(e, dict) else "?"
        try:
            res = check_posting(e)
        except Exception as exc:  # check_posting never raises, but a patched/buggy one must not stop the rest
            log.exception("watch check crashed for %s", key)
            res = WatchResult("unknown", f"{type(exc).__name__}: {str(exc)[:120]}")
        log.info("watch %s: %s%s", key, res.status, f" ({res.detail})" if res.detail else "")
        out.append((e, res))
    return out
