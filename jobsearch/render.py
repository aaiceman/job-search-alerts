"""Digest email: inline-CSS HTML (Gmail-safe) + plain-text alternative. All job text is escaped."""
from __future__ import annotations

import html as _html
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from .models import Job, norm_company
from .rank import CompanyResearch, RankedJob, RankedOutput

TOP, WORTH = 75, 60
MARKS = {"better": "✅", "same": "≈", "worse": "⚠️", "unknown": "?"}
CARD = "border:1px solid #e5e7eb;border-radius:8px;padding:14px 16px;margin:0 0 14px;"
MUTED = "color:#6b7280;font-size:13px;"


def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _safe_url(u) -> str:
    return u if isinstance(u, str) and u.lower().startswith(("https://", "http://")) else ""


def _link(u, text) -> str:
    u = _safe_url(u)
    return f'<a href="{_e(u)}" style="color:#1a56db">{_e(text)}</a>' if u else _e(text)


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


@dataclass
class DigestContext:
    run_date: date
    candidates: list[Job]
    stats: dict
    ranked: RankedOutput | None = None
    research: dict[str, CompanyResearch] = field(default_factory=dict)
    ranking_error: str | None = None
    ranking_skipped: bool = False
    late: bool = False
    pay_target: float | None = None          # annual USD; None = no pay gate
    pay_rule: str = "max(floor, base+25%)"   # label for the target; never contains the base amount
    watch: list[dict] = field(default_factory=list)  # application-watch rows (store row + posted/applied)
    pay_base: float | None = None            # owner's current base (annual USD) for "% vs current base"; never shown


# ---- "Your applications" watch block -------------------------------------------------------------------

def _wdate(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _md(d: date) -> str:
    return f"{d:%b} {d.day}"


def _days(n: int) -> str:
    return f"{n} day" + ("" if n == 1 else "s")


def watch_line(row: dict, today: date) -> tuple[str, str]:
    """(label, status text) for one watched application. Day counts are calendar days relative to the run
    date. A recorded closed_on wins over today's "unknown" check: a closed posting stays closed."""
    label = f"{row.get('company') or '?'} — {row.get('title') or '?'}"
    posted, applied = _wdate(row.get("posted")), _wdate(row.get("applied"))
    closed = _wdate(row.get("closed_on"))
    if row.get("closed_on") and closed is not None:
        bits = []
        if posted is not None:
            bits.append(f"posted {_md(posted)} → closed after {_days(max((closed - posted).days, 0))}")
        if applied is not None:
            gap = (closed - applied).days
            bits.append(f"applied {_days(gap)} before close" if gap > 0 else
                        "applied the day it closed" if gap == 0 else "applied after it closed")
        return label, f"POSTING CLOSED on {_md(closed)}" + (f" ({'; '.join(bits)})" if bits else "")
    if row.get("last_status") == "open":
        parts = ["still open"]
        if posted is not None:
            parts.append(f"posted {_md(posted)}" + (f" (day {(today - posted).days})" if posted <= today else ""))
        if applied is not None:
            parts.append(f"applied {_md(applied)}" + (f" (day {(today - applied).days})" if applied <= today else ""))
        upd = _wdate(row.get("last_updated_at"))
        if upd is not None:
            parts.append(f"listing last updated {_md(upd)}")
        return label, " · ".join(parts)
    return label, "status unknown today (check failed)"


def _watch_block(ctx: DigestContext) -> tuple[str, list[str]]:
    """(html, plain-text lines) for the applications block; empty when nothing is watched."""
    rows = [r for r in ctx.watch if isinstance(r, dict)]
    if not rows:
        return "", []
    lines = [watch_line(r, ctx.run_date) for r in rows]
    items = "".join(f'<div style="font-size:14px;margin:2px 0">{_link(r.get("url"), label)}: {_e(rest)}</div>'
                    for r, (label, rest) in zip(rows, lines))
    html = (f'<div style="{CARD}background:#f0f9ff"><div style="font-size:15px;margin:0 0 6px">'
            f'<b>Your applications</b></div>{items}</div>')
    return html, ["Your applications", *(f"- {label}: {rest}" for label, rest in lines), ""]


def is_dealbreaker(r: RankedJob) -> bool:
    return bool(r.gaps) and r.gaps[0].lstrip().upper().startswith("DEALBREAKER")


def ranked_rows(ctx: DigestContext) -> list[tuple[RankedJob, Job]]:
    """Ranked jobs that match a candidate, one per id (first wins)."""
    if ctx.ranked is None:
        return []
    by_id = {j.id: j for j in ctx.candidates}
    seen: set[str] = set()
    rows = []
    for r in ctx.ranked.jobs:
        if r.id in by_id and r.id not in seen:
            seen.add(r.id)
            rows.append((r, by_id[r.id]))
    return rows


# ---- pay-target gate ----------------------------------------------------------------------------------

def _amount(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _midpoint(lo, hi) -> float | None:
    """filters.salary_check rule: a range is judged by its midpoint; a lone value is its own midpoint."""
    lo, hi = _amount(lo), _amount(hi)
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo if lo is not None else hi


def pay_midpoint(r: RankedJob | None, j: Job) -> float | None:
    """The posting's STATED salary first, else the ranker's pay_min/pay_max (stated or estimated)."""
    m = _midpoint(j.salary_min, j.salary_max)
    if m is None and r is not None:
        m = _midpoint(r.pay_min, r.pay_max)
    return m


def below_target(ctx: DigestContext, r: RankedJob | None, j: Job) -> bool:
    """Known pay midpoint under the pay target. Unknown pay (or no target) is never gated."""
    if ctx.pay_target is None:
        return False
    m = pay_midpoint(r, j)
    return m is not None and m < ctx.pay_target


def _k(v: float) -> str:
    return f"${v / 1000:.0f}k"


VS_BASE = "vs current base"


def _pct(v: float, base: float) -> str:
    return f"{round((v / base - 1) * 100):+d}%"


def pay_text(r: RankedJob, base: float | None) -> str:
    """The ranker's pay text plus "+X% to +Y% vs current base", computed here so the ranker never sees the
    base. The base amount itself is never rendered."""
    base = _amount(base)
    lo, hi = _amount(r.pay_min), _amount(r.pay_max)
    if base is None or (lo is None and hi is None) or VS_BASE in r.pay:
        return r.pay
    if lo is not None and hi is not None and round(lo) != round(hi):
        rel = f"{_pct(lo, base)} to {_pct(hi, base)}"
    else:
        rel = _pct(lo if lo is not None else hi, base)
    return f"{r.pay} · {rel} {VS_BASE}"


def _stated_pay(j: Job) -> str:
    lo, hi = _amount(j.salary_min), _amount(j.salary_max)
    if lo is not None and hi is not None:
        return f"{_k(lo)}–{_k(hi)} stated"
    if lo is not None:
        return f"{_k(lo)}+ stated"
    return f"up to {_k(hi)} stated" if hi is not None else "not stated"


def _unranked_all(ctx: DigestContext) -> list[Job]:
    if ctx.ranked is None:
        return []
    ranked_ids = {j.id for _, j in ranked_rows(ctx)}
    return [j for j in ctx.candidates if j.id not in ranked_ids]


def unranked_candidates(ctx: DigestContext) -> list[Job]:
    """Candidates the ranker returned no usable row for (only meaningful once ranking produced output),
    minus those whose STATED salary is below the pay target."""
    return [j for j in _unranked_all(ctx) if not below_target(ctx, None, j)]


def failed_rows(ctx: DigestContext) -> list[Job]:
    """Candidates listed when ranking failed or was skipped: gated on stated salary only."""
    if ctx.ranked is not None:
        return []
    return [j for j in ctx.candidates if not below_target(ctx, None, j)]


def below_pay_target(ctx: DigestContext) -> list[tuple[Job, str]]:
    """(job, pay text) for every job the pay gate removed from what the digest would otherwise show."""
    if ctx.pay_target is None:
        return []
    if ctx.ranked is None:
        return [(j, _stated_pay(j)) for j in ctx.candidates if below_target(ctx, None, j)]
    ranked = sorted(((r, j) for r, j in ranked_rows(ctx)
                     if not is_dealbreaker(r) and r.score >= WORTH and below_target(ctx, r, j)),
                    key=lambda t: -t[0].score)
    return ([(j, pay_text(r, ctx.pay_base)) for r, j in ranked]
            + [(j, _stated_pay(j)) for j in _unranked_all(ctx) if below_target(ctx, None, j)])


def count_dealbreakers(ctx: DigestContext) -> int:
    return sum(1 for r, _ in ranked_rows(ctx) if is_dealbreaker(r))


def split_ranked(ctx: DigestContext) -> tuple[list[tuple[RankedJob, Job]], list[tuple[RankedJob, Job]]]:
    """Top / worth-a-look lists. Jobs whose first gap is a DEALBREAKER, or whose pay midpoint is below the
    pay target, never appear, whatever their score."""
    rows = sorted((t for t in ranked_rows(ctx) if not is_dealbreaker(t[0]) and not below_target(ctx, *t)),
                  key=lambda t: -t[0].score)
    return [t for t in rows if t[0].score >= TOP], [t for t in rows if WORTH <= t[0].score < TOP]


def _sources(stats: dict) -> list[dict]:
    return [s for s in stats.get("sources") or [] if isinstance(s, dict)]


def _failed_sources(stats: dict) -> tuple[list[str], int]:
    srcs = _sources(stats)
    return [str(s.get("name", "?")) for s in srcs if not s.get("ok")], len(srcs)


def all_sources_failed(stats: dict) -> bool:
    failed, total = _failed_sources(stats)
    return bool(total) and len(failed) == total


def subject(ctx: DigestContext) -> str:
    d = _day(ctx.run_date)
    prefix = "[late] " if ctx.late else ""
    failed, total = _failed_sources(ctx.stats)
    if total and len(failed) == total:
        return f"{prefix}Jobs {d} — ALL SOURCES FAILED"
    if ctx.ranked is None:
        if ctx.candidates:
            why = "ranking skipped" if ctx.ranking_skipped else "ranking failed"
            n = len(failed_rows(ctx))
            return f"{prefix}Jobs {d} — {n} unranked ({why})" if n else f"{prefix}Jobs {d} — no new matches ({why})"
        return f"{prefix}Jobs {d} — no new matches"
    top, worth = split_ranked(ctx)
    shown = top + worth
    if not shown:
        n = len(unranked_candidates(ctx))
        if n:
            return f"{prefix}Jobs {d} — {n} unranked (ranker omitted)"
        return f"{prefix}Jobs {d} — no new matches"
    r, j = shown[0]
    title = " ".join(f"{j.title} @ {j.company}".split())  # header must stay single-line
    return f"{prefix}Jobs {d} — {len(shown)} new, top: {title} ({r.score})"


def _links(j: Job) -> str:
    urls = [u for u in [j.url, *j.extra_urls] if _safe_url(u)]
    return " · ".join(_link(u, "apply" if i == 0 else f"alt {i}") for i, u in enumerate(urls)) or "no link"


def _research_index(research: dict[str, CompanyResearch]) -> dict[str, CompanyResearch]:
    return {k: v for name, v in research.items() if (k := norm_company(name))}


def _benefits(cr: CompanyResearch | None) -> str:
    """Known rows as a table; rows with no data (mark or label unknown) collapse into one muted line."""
    if cr is None:
        return f'<div style="{MUTED}">Benefits: not researched</div>'
    known = [b for b in cr.benefits if b.mark != "unknown" and b.label != "Unknown"]
    unknown = [b.item for b in cr.benefits if b.mark == "unknown" or b.label == "Unknown"]
    rows = "".join(
        f'<tr><td style="padding:2px 8px 2px 0">{MARKS[b.mark]}</td><td style="padding:2px 8px 2px 0">{_e(b.item)}</td>'
        f'<td style="padding:2px 8px 2px 0">{_e(b.detail)}</td>'
        f'<td style="{MUTED}">{_link(b.source, b.label) if b.source else _e(b.label)}</td></tr>'
        for b in known)
    table = f'<table style="font-size:14px;border-collapse:collapse;margin:6px 0">{rows}</table>' if known else ""
    none = f'<div style="{MUTED}">No public data: {_e(", ".join(unknown))}</div>' if unknown else ""
    return table + none


def _reviews(cr: CompanyResearch | None) -> str:
    if cr is None:
        return ""
    rv = cr.reviews
    bits = [f"Glassdoor {rv.glassdoor_rating}" if rv.glassdoor_rating is not None else "",
            _e(rv.glassdoor_trend) if rv.glassdoor_trend else "",
            f"{rv.recommend_pct}% recommend" if rv.recommend_pct is not None else ""]
    head = " · ".join(b for b in bits if b)
    out = f'<div style="font-size:14px"><b>Reviews:</b> {head + " — " if head else ""}{_e(rv.summary)}</div>'
    links = " · ".join(_link(u, f"source {i}") for i, u in enumerate(rv.sources, 1) if _safe_url(u))
    if links:
        out += f'<div style="{MUTED}">{links}</div>'
    if rv.pros or rv.cons:
        out += f'<div style="{MUTED}">+ {_e("; ".join(rv.pros))} &nbsp; − {_e("; ".join(rv.cons))}</div>'
    if rv.red_flags:
        out += f'<div style="font-size:14px;color:#b91c1c">🚩 {_e("; ".join(rv.red_flags))}</div>'
    return out


DEGREE_LEVEL_LABEL = {"associate": "Associate's", "bachelor": "Bachelor's", "master": "Master's", "phd": "PhD",
                      "unspecified": "Unspecified degree", "none": "No degree"}
DEGREE_QUOTE_CHARS = 120
_DEGREE_FLAG = 'degree: "'


def degree_line(r: RankedJob | None, j: Job) -> str:
    """One plain-text line on the job's degree requirement, or "" when nothing is known."""
    d = r.degree if r is not None else None
    if d is None:
        snippet = next((f[len("degree: "):] for f in j.flags if f.startswith(_DEGREE_FLAG)), None)
        return f"Degree: see posting — {snippet}" if snippet else ""
    if d.level == "none" or d.requirement == "not_mentioned":
        return "Degree: not mentioned"
    level = DEGREE_LEVEL_LABEL[d.level]
    if d.requirement == "preferred":
        return f"Degree: {level} preferred"
    tail = {"yes": "equivalent experience accepted", "no": "no equivalency stated ⚠",
            "unknown": "equivalency unclear"}[d.equivalent_experience]
    return f"Degree: {level} required — {tail}"


def degree_quote(r: RankedJob | None) -> str:
    q = " ".join((r.degree.quote if r is not None and r.degree is not None else "").split())
    return q if len(q) <= DEGREE_QUOTE_CHARS else q[:DEGREE_QUOTE_CHARS - 1].rstrip() + "…"


def _degree_html(r: RankedJob | None, j: Job, with_quote: bool) -> str:
    line = degree_line(r, j)
    if not line:
        return ""
    quote = degree_quote(r) if with_quote else ""
    q = f'<div style="{MUTED}">“{_e(quote)}”</div>' if quote else ""
    return f'<div style="font-size:14px">{_e(line)}</div>{q}'


def _degree_text(r: RankedJob | None, j: Job, with_quote: bool) -> list[str]:
    line = degree_line(r, j)
    if not line:
        return []
    quote = degree_quote(r) if with_quote else ""
    return [f"    {line}"] + ([f"      “{quote}”"] if quote else [])


# Quoted-snippet flags are ranker inputs (the ranker turns them into gaps / the degree line): not shown on cards.
_SNIPPET_FLAGS = ('degree: "', 'travel: "', 'on-call: "')


def _card(r: RankedJob, j: Job, cr: CompanyResearch | None, pay: str | None = None) -> str:
    gaps = f'<div style="{MUTED}">Gaps: {_e("; ".join(r.gaps))}</div>' if r.gaps else ""
    shown = [f for f in j.flags if not f.startswith(_SNIPPET_FLAGS)]
    flags = f'<div style="{MUTED}">Flags: {_e("; ".join(shown))}</div>' if shown else ""
    return (f'<div style="{CARD}"><div style="font-size:16px"><b>{r.score}</b> · <b>{_e(j.title)}</b> @ {_e(j.company)}</div>'
            f'<div style="{MUTED}">{_e(j.location)} · {_links(j)}</div>'
            f'<div style="font-size:14px;margin-top:6px">{_e(r.fit)}</div>{gaps}{flags}'
            f'<div style="font-size:14px;margin-top:6px"><b>Pay:</b> {_e(r.pay if pay is None else pay)}</div>'
            f'{_degree_html(r, j, with_quote=True)}'
            f'{_benefits(cr)}{_reviews(cr)}</div>')


def _row(r: RankedJob | None, j: Job, pay: str | None = None) -> str:
    score = f"<b>{r.score}</b> · " if r else ""
    extra = f" — {_e(r.pay if pay is None else pay)} — {_e(r.fit)}" if r else (f" — {_e(j.salary_text)}" if j.salary_text else "")
    return (f'<li style="margin:0 0 8px">{score}{_e(j.title)} @ {_e(j.company)}{extra} · {_links(j)}'
            f'{_degree_html(r, j, with_quote=False)}</li>')


def _num(v) -> float:
    return v if isinstance(v, (int, float)) else 0


DEALBREAKER_GAP_CHARS = 160
RESEARCH_NOTE = "Company research incomplete today (ranker stopped early)"


DEALBREAKER_KEYS = ("travel", "on-call", "onsite", "clearance", "degree", "contract", "staffing", "residency")
_DEALBREAKER_SYN = {"on-site": "onsite", "hybrid": "onsite", "oncall": "on-call"}
_DEALBREAKER_KEY_RE = re.compile(r"^\s*dealbreaker\s*[:\-–—]?\s*([a-z]+(?:-[a-z]+)?)", re.I)


def dealbreaker_key(gap: str) -> str:
    """'DEALBREAKER: travel — 25% travel' -> 'travel'; anything off the canonical list -> 'other'."""
    m = _DEALBREAKER_KEY_RE.match(gap or "")
    key = m.group(1).lower() if m else ""
    key = _DEALBREAKER_SYN.get(key, key)
    return key if key in DEALBREAKER_KEYS else "other"


def _dealbreaker_block(ctx: DigestContext) -> str:
    rows = [(dealbreaker_key(r.gaps[0]), r, j) for r, j in ranked_rows(ctx) if is_dealbreaker(r)]
    if not rows:
        return ""
    order = [*DEALBREAKER_KEYS, "other"]
    counts = Counter(k for k, _, _ in rows)
    groups = sorted(counts, key=lambda k: (-counts[k], order.index(k)))
    rows.sort(key=lambda t: groups.index(t[0]))
    head = " · ".join(f"{k} {counts[k]}" for k in groups)
    items = "".join(f'<div style="{MUTED}">{_e(" ".join(f"{j.title} @ {j.company}".split()))} — '
                    f'{_e(" ".join(r.gaps[0].split())[:DEALBREAKER_GAP_CHARS])}</div>' for _, r, j in rows)
    return f'<div style="{MUTED}margin-top:8px"><b>Dealbreakers (auto-excluded):</b> {_e(head)}</div>{items}'


BELOW_TARGET_CHARS = 160


def _below_head(ctx: DigestContext) -> str:
    return f"Below pay target ({_k(ctx.pay_target or 0)} = {ctx.pay_rule}):"


def _below_lines(ctx: DigestContext) -> list[str]:
    return [" ".join(f"{j.title} @ {j.company} — {pay}".split())[:BELOW_TARGET_CHARS]
            for j, pay in below_pay_target(ctx)]


def _below_block(ctx: DigestContext) -> str:
    lines = _below_lines(ctx)
    if not lines:
        return ""
    items = "".join(f'<div style="{MUTED}">{_e(line)}</div>' for line in lines)
    return f'<div style="{MUTED}margin-top:8px"><b>{_e(_below_head(ctx))}</b></div>{items}'


def _research_incomplete(ctx: DigestContext, top: list[tuple[RankedJob, Job]]) -> bool:
    """The ranker returned jobs but no company research, and a Top-pick card has no cached research either.
    Only Top picks count: Pass B researches ≥ 75 jobs only, and only cards show research."""
    if ctx.ranked is None or ctx.ranked.companies or not top:
        return False
    research = _research_index(ctx.research)
    return any(norm_company(j.company) not in research for _, j in top)


def _minutes(s: dict) -> str:
    """' (31m)' when the source entry has a numeric `seconds` worth at least a minute (rounded), else ''."""
    sec = s.get("seconds")
    if not isinstance(sec, (int, float)) or isinstance(sec, bool) or round(sec / 60) < 1:
        return ""
    return f" ({round(sec / 60)}m)"


def _footer(stats: dict, ranked: int = 0, dealbreakers: int = 0, unranked: int = 0, extra: str = "",
            below: int = 0) -> str:
    src = ", ".join(f"{_e(s.get('name', '?'))} "
                    f"{'✓' + _e(s.get('count', 0)) if s.get('ok') else '✗ ' + _e(s.get('error') or 'failed')}"
                    f"{_minutes(s)}"
                    for s in _sources(stats))
    dropped = ", ".join(f"{_e(k)} {_e(v)}" for k, v in sorted((stats.get("dropped") or {}).items(), key=lambda kv: -_num(kv[1])))
    excluded = ", ".join(f"{_e(k)} {_e(v)}" for k, v in (stats.get("excluded") or {}).items()) or "none"
    warnings = "; ".join(_e(w) for w in stats.get("warnings") or [])
    return (f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">'
            f'<div style="{MUTED}">Window {_e(stats.get("window_hours"))}h · collected {_e(stats.get("collected"))} · '
            f'unique {_e(stats.get("unique"))} · already sent {_e(stats.get("already_sent"))} · '
            f'over cap {_e(stats.get("over_cap"))} · candidates {_e(stats.get("candidates"))} · '
            f'ranked {ranked} · '
            f'{f"unranked {unranked} · " if unranked else ""}'
            f'{f"below pay target {below} · " if below else ""}'
            f'dealbreakers {dealbreakers}<br>'
            f'Sources: {src}<br>Dropped: {dropped or "none"}<br>Excluded: {excluded}'
            f'{"<br>Warnings: " + warnings if warnings else ""}</div>{extra}')


def render(ctx: DigestContext) -> tuple[str, str, str]:
    subj = subject(ctx)
    parts: list[str] = []
    text: list[str] = [subj, ""]
    watch_html, watch_text = _watch_block(ctx)
    if watch_html:
        parts.append(watch_html)
        text += watch_text
    failed, total = _failed_sources(ctx.stats)
    if failed:
        line = f"⚠ {len(failed)} of {total} sources failed today: {', '.join(failed)}"
        parts.append(f'<div style="{CARD}background:#fffbeb;color:#92400e">{_e(line)}</div>')
        text += [line, ""]
    if ctx.late:
        parts.append(f'<div style="{MUTED}">This digest started late (the PC was off or asleep at the '
                     f'scheduled 05:45 run).</div>')
    research = _research_index(ctx.research)
    top, worth = split_ranked(ctx) if ctx.ranked is not None else ([], [])
    if ctx.ranked is None and ctx.candidates:
        if ctx.ranking_skipped:
            head = "<b>Ranking skipped (--skip-rank)</b>"
        else:
            head = f"<b>Ranking failed</b> — {_e(ctx.ranking_error)}"
        parts.append(f'<div style="{CARD}background:#fef2f2">{head}. '
                     f'Unranked matches below.</div><ul style="padding-left:18px">'
                     + "".join(_row(None, j) for j in failed_rows(ctx)) + "</ul>")
        for j in failed_rows(ctx):
            text += [f"- {j.title} @ {j.company} {_safe_url(j.url)}", *_degree_text(None, j, False)]
    elif top or worth:
        note = f'<div style="{MUTED}">{_e(RESEARCH_NOTE)}</div>' if _research_incomplete(ctx, top) else ""
        if top:
            parts.append('<h2 style="font-size:18px;margin:8px 0 12px">Top picks</h2>' + note)
            parts += [_card(r, j, research.get(norm_company(j.company)), pay_text(r, ctx.pay_base))
                      for r, j in top]
        if worth:
            parts.append('<h2 style="font-size:18px;margin:16px 0 8px">Worth a look</h2>'
                         + ("" if top else note) + '<ul style="padding-left:18px">'
                         + "".join(_row(r, j, pay_text(r, ctx.pay_base)) for r, j in worth) + "</ul>")
        if note:
            text.append(RESEARCH_NOTE)
        top_ids = {j.id for _, j in top}
        for r, j in top + worth:
            text += [f"{r.score}  {j.title} @ {j.company}  {pay_text(r, ctx.pay_base)}  {_safe_url(j.url)}",
                     *_degree_text(r, j, j.id in top_ids)]
    elif not unranked_candidates(ctx):
        parts.append('<p style="font-size:15px">No new matches today.</p>')
        text.append("No new matches today.")
    unranked = unranked_candidates(ctx)
    if unranked:
        parts.append('<h2 style="font-size:18px;margin:16px 0 8px">Unranked (ranker omitted or invalid)</h2>'
                     '<ul style="padding-left:18px">' + "".join(_row(None, j) for j in unranked) + "</ul>")
        text.append("Unranked (ranker omitted or invalid):")
        for j in unranked:
            text += [f"- {j.title} @ {j.company} {_safe_url(j.url)}", *_degree_text(None, j, False)]
    below = _below_lines(ctx)
    if below:
        text += ["", _below_head(ctx), *(f"- {line}" for line in below)]
    footer = _footer(ctx.stats, len(ranked_rows(ctx)), count_dealbreakers(ctx), len(unranked),
                     _dealbreaker_block(ctx) + _below_block(ctx), len(below))
    body = (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px;color:#111827">'
            f'<h1 style="font-size:20px;margin:0 0 12px">{_e(subj)}</h1>{"".join(parts)}{footer}</div>')
    return subj, body, "\n".join(text)
