"""Turn salary strings and structured amounts into annual USD (min, max)."""
from __future__ import annotations

import bisect
import math
import re

HOURS_PER_YEAR = 2080
MAX_DESCRIPTION_CHARS = 50_000  # descriptions are untrusted input; bound the scan
_PERIOD_MULT = {
    "hourly": 2080, "hour": 2080, "daily": 260, "day": 260, "weekly": 52, "week": 52,
    "monthly": 12, "month": 12, "yearly": 1, "year": 1, "annual": 1, "annually": 1,
}

# "$" must not directly follow a letter (C$, A$, AU$, NZ$, MX$ ...): those are not USD.
# "US$" is the one allowed letter prefix.
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_DOLLAR = r"(?:(?<![A-Za-z])|(?<=\b[Uu][Ss]))\$"
_PER_UNIT = r"(?:hr|hour|mo|month|wk|week|day|yr|year)"
_PERIOD_TOKEN = rf"(?:/\s?|per\s+|an?\s+){_PER_UNIT}\b"
_PERIOD_WORD = r"(?:hourly|monthly|weekly|daily|annually|yearly)\b"

# "and" is only a range separator after "between" (conditional on the btw group).
_RANGE = re.compile(
    rf"(?P<btw>\bbetween\s+)?{_DOLLAR}\s?(?P<lo>{_NUM})(?:\s?(?P<lok>[kK])(?![A-Za-z]))?"
    rf"(?P<mid>(?:\s*USD\b)?(?:\s*{_PERIOD_TOKEN})?)"
    rf"\s*(?P<sep>-|–|—|\bto\b|(?(btw)\band\b|(?!)))\s*"
    rf"(?P<hd>[Uu][Ss]\$|\$)?\s?(?P<hi>{_NUM})(?:\s?(?P<hik>[kK])(?![A-Za-z]))?",  # "$160-190K": 2nd "$" optional
    re.I,
)
_SINGLE = re.compile(rf"{_DOLLAR}\s?(?P<n>{_NUM})(?:\s?(?P<k>[kK])(?![A-Za-z]))?")
_PERIOD_AFTER = re.compile(rf"^\s*(?:USD\s*)?(?:(?:{_PERIOD_TOKEN})|(?:{_PERIOD_WORD}))", re.I)
_UPTO = re.compile(r"up to\s*$", re.I)
_KEYWORDS = re.compile(r"\b(salary|salaries|compensation|pay|base|range|ote|wage|wages)\b", re.I)
_NON_USD = re.compile(r"\b(?:CAD|AUD|NZD|MXN|SGD|HKD)\b")
_NOT_BASE = re.compile(r"equity|rsu|stock|bonus|sign-?on|signing|commission|401|relocation", re.I)
_BEFORE_CUT = re.compile(r"[;\n]|\.\s|,\s|\band\b|\bbut\b", re.I)
# After the amount, also cut at bullets/pipes, a spaced dash, ":" and "(" (the label is
# then read separately via _PAREN_AFTER). ":" is deliberately NOT a before-side cut.
_AFTER_CUT = re.compile(
    r"[;\n:(•·|]|\s[-–—]\s|\.\s|,\s|\band\b|\bbut\b|\bplus\b|\bwith\b|\+", re.I
)
_PAREN_AFTER = re.compile(r"\s*\(([^)]{0,30})\)")
MAX_SALARY_TEXT_CHARS = 1_000
_HOURLY_HINT_TEXT = re.compile(r"hourly|/\s?hr|per hour|an hour", re.I)
_HOURLY_WORD = re.compile(r"hourly", re.I)
_401K_TAIL = re.compile(r"\s*(?:\(k\)|match\b)", re.I)


def annualize(amount, interval: str | None) -> float | None:
    if amount is None or isinstance(amount, bool):
        return None
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(a) or a <= 0:
        return None
    mult = _PERIOD_MULT.get((interval or "yearly").strip().lower())
    if mult is None:
        return None
    return round(a * mult, 2)


def _to_num(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _period(text: str) -> tuple[int, bool]:
    """(annual multiplier, is_hourly) for the period token at the START of `text`."""
    m = _PERIOD_AFTER.match(text)
    if not m:
        return 1, False
    s = m.group(0).lower()
    if "hr" in s or "hour" in s:
        return HOURS_PER_YEAR, True
    if "mo" in s:
        return 12, False
    if "wk" in s or "week" in s:
        return 52, False
    if "day" in s or "daily" in s:
        return 260, False
    return 1, False


def _finish(lo: float | None, hi: float | None, mult: int, hourly: bool):
    vals = [v for v in (lo, hi) if v is not None]
    if not vals:
        return None
    for v in vals:
        ok = 15 <= v <= 1000 if hourly else 30_000 <= v * mult <= 1_500_000
        if not ok:
            return None
    return (
        round(lo * mult, 2) if lo is not None else None,
        round(hi * mult, 2) if hi is not None else None,
    )


def _finish_hint(lo, hi, mult: int, hourly: bool, hint: bool):
    """_finish, retried as hourly when no period was given, the annual reading is
    implausible, and the caller saw an hourly marker elsewhere in the text."""
    r = _finish(lo, hi, mult, hourly)
    if r is None and hint and not hourly and mult == 1:
        r = _finish(lo, hi, HOURS_PER_YEAR, True)
        if r:
            return r, True
    return r, hourly


def _non_usd(text: str, start: int, end: int) -> bool:
    return bool(_NON_USD.search(text[max(0, start - 8): start]) or _NON_USD.search(text[end: end + 10]))


def _range_band(text: str, m: re.Match, hint: bool = False):
    """((lo, hi) annual, is_hourly) for a range match, or None if not a real/plausible range."""
    if not m["hd"] and (m["hi"] == "401" or _401K_TAIL.match(text, m.end())):
        return None  # "$150,000 - 401k match": not a range
    lo, hi = _to_num(m["lo"], m["lok"]), _to_num(m["hi"], m["hik"])
    if m["lok"] is None and m["hik"] and lo < 1000:  # "$160-190K"
        lo *= 1000
    if lo > hi:
        if m["sep"].lower() == "and":
            return None
        lo, hi = hi, lo
    mult, hourly = _period(m["mid"])
    if not hourly and mult == 1:
        mult, hourly = _period(text[m.end(): m.end() + 20])
    band, hourly = _finish_hint(lo, hi, mult, hourly, hint)
    return (band, hourly) if band else None


def _single_band(text: str, m: re.Match, hint: bool = False):
    v = _to_num(m["n"], m["k"])
    mult, hourly = _period(text[m.end(): m.end() + 20])
    if _UPTO.search(text[max(0, m.start() - 12): m.start()]):
        return _finish_hint(None, v, mult, hourly, hint)[0]
    if text[m.end(): m.end() + 1] == "+":
        return _finish_hint(v, None, mult, hourly, hint)[0]
    return _finish_hint(v, v, mult, hourly, hint)[0]


def parse_salary_text(text: str) -> tuple[float | None, float | None] | None:
    """Parse the first USD amount/range in a short salary string (first 1,000 chars only)."""
    if not text or "$" not in text:
        return None
    text = text[:MAX_SALARY_TEXT_CHARS]
    hint = bool(_HOURLY_HINT_TEXT.search(text))
    consumed: list[tuple[int, int]] = []
    for m in _RANGE.finditer(text):
        if _non_usd(text, m.start(), m.end()):
            consumed.append(m.span())
            continue
        r = _range_band(text, m, hint)
        if r:
            return r[0]  # an implausible "range" (e.g. "$150,000 to 10 years") falls through to single
    for m in _SINGLE.finditer(text):
        if any(s < m.end() and m.start() < e for s, e in consumed) or _non_usd(text, m.start(), m.end()):
            continue
        r = _single_band(text, m, hint)
        if r:
            return r
    return None


def _excluded(text: str, start: int, end: int) -> bool:
    """True when the amount sits in a clause about equity/bonus/etc rather than base pay."""
    before = _BEFORE_CUT.split(text[max(0, start - 40): start])[-1]
    window = text[end: end + 25]
    after = _AFTER_CUT.split(window)[0]
    paren = _PAREN_AFTER.match(window)  # "$60,000 (bonus)": the label right after the amount
    return bool(
        _NOT_BASE.search(before) or _NOT_BASE.search(after) or (paren and _NOT_BASE.search(paren.group(1)))
    )


def _overlaps(starts: list[int], ends: list[int], m: re.Match) -> bool:
    """Do the (sorted, non-overlapping) spans overlap match m?"""
    i = bisect.bisect_right(starts, m.start()) - 1
    if i >= 0 and ends[i] > m.start():
        return True
    return i + 1 < len(starts) and starts[i + 1] < m.end()


def find_salary_in_description(desc: str) -> tuple[float | None, float | None] | None:
    """Find a salary in free text, only when a pay keyword or hourly marker is adjacent.

    Collects every plausible keyword-adjacent band (skipping equity/bonus/etc clauses)
    and returns (lowest low, highest high) across them. Only the first
    MAX_DESCRIPTION_CHARS characters are scanned.
    """
    if not desc or "$" not in desc:
        return None
    desc = desc[:MAX_DESCRIPTION_CHARS]
    bands: list[tuple[float | None, float | None]] = []
    c_starts: list[int] = []  # spans of ranges consumed (not re-read as singles)
    c_ends: list[int] = []
    claims: list[int] = []  # end offsets of accepted base-pay bands (sorted)
    for m in _RANGE.finditer(desc):
        if _non_usd(desc, m.start(), m.end()):
            c_starts.append(m.start())
            c_ends.append(m.end())
            continue
        hint = bool(_HOURLY_WORD.search(desc[max(0, m.start() - 40): m.start()]))
        r = _range_band(desc, m, hint)
        if not r:
            continue
        c_starts.append(m.start())
        c_ends.append(m.end())
        band, hourly = r
        before = desc[max(0, m.start() - 100): m.start()]
        if (_KEYWORDS.search(before) or hourly) and not _excluded(desc, m.start(), m.end()):
            bands.append(band)
            bisect.insort(claims, m.end())
    for m in _SINGLE.finditer(desc):
        if _overlaps(c_starts, c_ends, m) or _non_usd(desc, m.start(), m.end()):
            continue
        # the pay keyword must come after the previous accepted amount, not before it
        i = bisect.bisect_right(claims, m.start())
        floor = claims[i - 1] if i else 0
        if not _KEYWORDS.search(desc[max(0, m.start() - 60, floor): m.start()]):
            continue
        hint = bool(_HOURLY_WORD.search(desc[max(0, m.start() - 40): m.start()]))
        band = _single_band(desc, m, hint)
        if band and not _excluded(desc, m.start(), m.end()):
            bands.append(band)
            bisect.insort(claims, m.end())
    if not bands:
        return None
    los = [b[0] for b in bands if b[0] is not None]
    his = [b[1] for b in bands if b[1] is not None]
    return (min(los) if los else None, max(his) if his else None)
