"""Shared context and small helpers used by every source."""
from __future__ import annotations

import html
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..models import Job

log = logging.getLogger(__name__)

QUERY_PAUSE_S = 1.0


@dataclass
class SourceContext:
    cfg: dict  # contents of config/searches.yaml
    hours_old: int
    portals: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # partial failures surfaced in the email footer
    deadline: float | None = None  # soft collection budget (time.monotonic value); None = unlimited
    filters_cfg: dict = field(default_factory=dict)  # config/filters.yaml (title include/exclude etc.)
    # True for a job not worth an extra detail request (already emailed); None = fetch for every job
    skip_detail: Callable[["Job"], bool] | None = None


def past_deadline(ctx: SourceContext) -> bool:
    """True once the collection budget is spent: sources stop issuing new requests (a request already in
    flight is never interrupted)."""
    return ctx.deadline is not None and time.monotonic() > ctx.deadline


def note_stopped_early(ctx: SourceContext, name: str, skipped: int, total: int, unit: str) -> None:
    log.warning("%s: time budget reached; %d of %d %s skipped", name, skipped, total, unit)
    ctx.warnings.append(f"{name}: stopped early (time budget); {skipped} of {total} {unit} skipped")


# C0 controls (keeping tab/newline) and Unicode bidi controls, which can spoof text such as URLs and titles.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
# Only semicolon-terminated entities are decoded, so a URL like "?a=1&copy=2" is left alone.
_ENTITY = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def text(v) -> str:
    """Coerce an API value to a string; None, NaN and dicts become ''; lists are comma-joined.

    HTML entities in strings are decoded ("hims &#038; hers" -> "hims & hers"), then control and
    bidi characters are stripped."""
    if v is None or isinstance(v, dict):
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(t for t in (text(x) for x in v) if t)
    if not isinstance(v, str):
        return str(v)
    return _CONTROLS.sub("", _ENTITY.sub(lambda m: html.unescape(m.group(0)), v))


def iso_date(v) -> str:
    """'YYYY-MM-DD' from an ISO-ish string or date/datetime, else ''."""
    try:
        if isinstance(v, (date, datetime)):
            return v.isoformat()[:10]
        if isinstance(v, str):
            return date.fromisoformat(v.strip()[:10]).isoformat()
    except ValueError:
        pass
    return ""


def epoch_date(v) -> str:
    """'YYYY-MM-DD' from an epoch in seconds or milliseconds (number or numeric string), else ''."""
    if v is None or isinstance(v, bool):
        return ""
    try:
        f = float(v)
        if not math.isfinite(f):
            return ""
        if abs(f) > 1e11:  # milliseconds
            f /= 1000
        return datetime.fromtimestamp(f, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def require(title: str, url: str) -> None:
    if not title or not url:
        raise ValueError("posting needs a title and a url")


def note_skip(skipped: list | None, item, err: Exception) -> None:
    log.debug("skipped malformed item %r: %s", item, err)
    if skipped is not None:
        skipped.append(f"{type(err).__name__}: {err}")


def run_queries(ctx: SourceContext, name: str, queries: list, fn: Callable[[object, list], list]) -> list:
    """Run fn(query, skipped) for each query with isolation, pacing and warnings.

    A failing query is counted, not fatal; raises only when every attempted query failed. Once the
    collection deadline has passed, the remaining queries are skipped (and reported)."""
    out: list = []
    failed = attempted = 0
    skipped: list = []
    for i, q in enumerate(queries):
        if past_deadline(ctx):
            note_stopped_early(ctx, name, len(queries) - i, len(queries), "queries")
            break
        if i:
            time.sleep(QUERY_PAUSE_S)
        attempted += 1
        try:
            out += fn(q, skipped)
        except Exception as e:
            failed += 1
            log.warning("%s query %r failed: %s", name, q, e)
    total = len(queries)
    if failed:
        ctx.warnings.append(f"{name}: {failed} of {total} queries failed")
    if skipped:
        ctx.warnings.append(f"{name}: {len(skipped)} items skipped")
    if attempted and failed == attempted:
        raise RuntimeError(f"all {name} queries failed")
    return out
