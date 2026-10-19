"""Hard filters. Pure functions: a Job in, a verdict out. No I/O.

Governing principle: a hard DROP needs an explicit, affirmative signal. Anything ambiguous becomes a
FLAG (appended to job.flags, which the ranker sees). A false drop costs more than a false keep.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

from .mentions import (  # noqa: F401  (re-exported: callers and tests import these names from filters)
    MAX_DESC,
    ONCALL,
    clause_mentions,
    clearance_mentioned,
    clearance_required,
    contract_terms_mentioned,
    degree_mentions,
    education_check,
    office_attendance_mentioned,
    oncall_mentions,
    travel_mentions,
)
from .models import Job
from .salary import find_salary_in_description

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_ABBRS = set(US_STATES.values())


def _state_alt(name: str) -> str:
    # "Kansas City, Missouri" is one state, not two; "Washington, DC" is the district, not WA.
    # "New York City" really is New York, so it gets no lookahead.
    if name == "new york":
        return re.escape(name)
    extra = r"(?!,?\s+d\.?c\b)" if name == "washington" else ""
    return re.escape(name) + r"(?!\s+city\b)" + extra


_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(_state_alt(n) for n in sorted(US_STATES, key=len, reverse=True)) + r")\b", re.I
)
_NON_REMOTE = re.compile(r"\b(hybrid|on-?site|in[- ]office)\b", re.I)
_US_OK = re.compile(
    r"\b(united states|usa|u\.s\.|us|anywhere|worldwide|global|americas|north america)\b", re.I
)
_NATIONWIDE = re.compile(r"\bnation-?wide\b", re.I)
_NON_US = re.compile(
    r"\b(canada|united kingdom|uk|england|ireland|germany|france|spain|portugal|netherlands|poland|"
    r"india|philippines|brazil|(?<!new )mexico|argentina|colombia|australia|new zealand|singapore|japan|"
    r"europe|emea|apac|latam|israel|romania|ukraine)\b",
    re.I,
)
_RESIDENCY = re.compile(
    r"\b(reside|residing|residents? of|located in|based in|eligible to work in|hire in|"
    r"hiring in|live in|living in)\b",
    re.I,
)
# Words that exclude the listed states ("all states except CA") vs. words that prohibit ("unable to hire in TX").
_EXCLUSION_WORDS = re.compile(r"\b(except|excluding|excluded|other than|outside of)\b", re.I)
_PROHIBITION_WORDS = re.compile(r"\b(not (?:able|eligible)|unable|cannot)\b|can['’]t", re.I)
# A state token: a state name, or an UPPERCASE two-letter code (validated against _ABBRS afterwards).
_STATE_TOKEN = (
    r"(?:" + "|".join(_state_alt(n) for n in sorted(US_STATES, key=len, reverse=True)) + r"|(?-i:[A-Z]{2})\b)"
)
_STATE_LIST = rf"\b{_STATE_TOKEN}(?:\s*(?:,|/|&|\band\b|\bor\b)\s*{_STATE_TOKEN})*"
# A negation only counts when it attaches directly to a state list: "excluding TX, CA",
# "not available in NY". "Remote (not hybrid) - Texas" or "No Travel, Texas" do not qualify.
_NEG_THEN_STATES = re.compile(
    rf"\b(?:not|no|excluding|except|excluded|other than|without)\b\s+"
    rf"(?:(?:in|for|open to|available (?:in|to))\s+)?({_STATE_LIST})",
    re.I,
)
_STATES_THEN_NEG = re.compile(rf"({_STATE_LIST})\s*\)?\s*(?:excluded|not eligible|ineligible)\b", re.I)
# Only these phrasings make a state list a hard restriction; plain "based in ..." is a flag at most.
_EXPLICIT_RESTRICTION = re.compile(
    r"must (?:reside|live|be located) in"
    r"|open (?:only )?to (?:candidates|residents) (?:in|of)"
    r"|(?:currently )?(?:hire|hiring) (?:only )?in (?:the following|these)"
    r"|only (?:considering|accepting) candidates (?:in|from)"
    r"|eligible (?:states|locations)"
    r"|we (?:can|are able to) (?:only )?hire in",
    re.I,
)
_MUST_RESIDE = re.compile(r"must (?:reside|live|be located) in", re.I)
# Restrictions that also name a single state ("Must reside in NJ.").
_EXPLICIT_SINGLE = re.compile(
    r"must (?:reside|live|be located) in"
    r"|open only to (?:candidates|residents) (?:in|of)"
    r"|only (?:considering|accepting) candidates (?:in|from)",
    re.I,
)
# A US-wide scope or an illustrative list means the states named are examples, not the whole allowed set.
_ILLUSTRATIVE = re.compile(
    r"\b(?:including|such as|for (?:example|instance)|hubs?|offices?|50 states|contiguous)\b|\beg\b", re.I
)
# Clauses about pay ranges / privacy notices list states for reasons unrelated to where you may live.
_NOT_RESIDENCY_CONTEXT = re.compile(
    r"\b(pay|salary|salaries|compensation|ranges?|privacy|notice|ccpa)\b|\$", re.I
)
_CLAUSE_SPLIT = re.compile(r"[.\n;]")
_MAX_DESC = MAX_DESC
_MAX_LOCATION = 2_000
_MAX_SEGMENTS = 20


def _find_states(s: str, min_abbrs: int = 2) -> set[str]:
    found = {US_STATES[m.group(1).lower()] for m in _STATE_NAME_RE.finditer(s)}
    abbrs = [t for t in re.findall(r"\b([A-Z]{2})\b", s) if t in _ABBRS]
    if len(abbrs) >= min_abbrs:  # lone "IN"/"OR"/"ME" tokens are too ambiguous
        found |= set(abbrs)
    return found


def _remote_then_only_states(loc: str) -> bool:
    """True for "Remote - CA, NY, WA": after the word remote, nothing but states and separators."""
    m = re.search(r"\bremote\b", loc, re.I)
    if not m:
        return False
    rest = _STATE_NAME_RE.sub(" ", loc[m.end():])
    rest = re.sub(r"\b[A-Z]{2}\b", lambda t: " " if t.group(0) in _ABBRS else t.group(0), rest)
    return not re.sub(r"\b(?:and|or|city)\b|[\s,/&|;()\-–—:]", "", rest, flags=re.I)


def residency_check(desc: str, home_abbr: str = "TX") -> tuple[str, str]:
    """Description-only residency verdict: ("ok"|"flag"|"drop", reason).

    Free text is noisy, so apply_filters turns even a "drop" here into a flag; the ranker reads the ad.
    """
    # "e.g." / "i.e." must not split a clause
    desc = re.sub(r"\bi\.e\.?", "ie", re.sub(r"\be\.g\.?", "eg", (desc or "")[:_MAX_DESC]), flags=re.I)
    flags: list[str] = []
    for clause in _CLAUSE_SPLIT.split(desc):
        explicit = bool(_EXPLICIT_RESTRICTION.search(clause))
        if not (explicit or _RESIDENCY.search(clause)):
            continue
        if _NOT_RESIDENCY_CONTEXT.search(clause) and not _MUST_RESIDE.search(clause):
            continue
        single_ok = bool(_EXPLICIT_SINGLE.search(clause))
        shouting = len(re.findall(r"\b[A-Z]{3,}\b", clause)) >= 2  # "IN"/"OR" are words in all-caps text
        found = _find_states(clause, min_abbrs=10**9 if shouting else 1 if single_ok else 2)
        if not found:
            continue
        excl = bool(_EXCLUSION_WORDS.search(clause))
        skip = excl or (bool(_PROHIBITION_WORDS.search(clause)) and not explicit)
        if home_abbr in found:
            if skip:
                return "drop", f"residency excludes {home_abbr}"
            continue
        if skip:
            continue
        scoped = bool(_US_OK.search(clause) or _NATIONWIDE.search(clause) or _ILLUSTRATIVE.search(clause))
        if not scoped and (single_ok or (explicit and len(found) >= 2)):
            return "drop", f"state-restricted (no {home_abbr})"
        flags.append(f"may require residence in {', '.join(sorted(found))}")
    return ("flag", "; ".join(dict.fromkeys(flags))) if flags else ("ok", "")


_MULTI_REGION_FLAG = "multi-region posting; kept US-eligible location"


def classify_location(job: Job, home_abbr: str = "TX", *, include_description: bool = True) -> tuple[str, str]:
    """Return ("ok"|"flag"|"drop", reason).

    include_description=False judges the title and location field only (the structured signals).
    When the whole location would be dropped but contains ";" (several postings merged into one), each
    segment is judged on its own and the best outcome wins (ok > flag > drop), so a US posting is not
    lost to a sibling posting abroad.
    """
    location = (job.location or "")[:_MAX_LOCATION]  # bound regex work on absurd location strings
    status, flags = _location_flags(job, home_abbr)
    if status == "drop" and ";" in location:
        segments = [seg.strip() for seg in location.split(";") if seg.strip()][:_MAX_SEGMENTS]
        if len(segments) > 1:
            verdicts = [_location_flags(replace(job, location=seg), home_abbr) for seg in segments]
            best = next((v for v in verdicts if v[0] == "ok"), None) or next(
                (v for v in verdicts if v[0] == "flag"), None)
            if best is not None:  # some segment is US-eligible: keep the job, note the others were not
                flags = [*best[1], _MULTI_REGION_FLAG]
                status = "flag"
    if status == "drop":
        return "drop", flags[0]
    return _with_description(job, home_abbr, flags, include_description)


def _with_description(job: Job, home_abbr: str, flags: list[str], include_description: bool) -> tuple[str, str]:
    if include_description:
        if office_attendance_mentioned(job.description):
            flags.append("office attendance mentioned")
        status, reason = residency_check(job.description, home_abbr)
        if status == "drop":
            return "drop", reason
        if status == "flag":
            flags.append(reason)
    return ("flag", "; ".join(dict.fromkeys(flags))) if flags else ("ok", "")


def _location_flags(job: Job, home_abbr: str) -> tuple[str, list[str]]:
    """Structured (title + location field) verdict: ("drop", [reason]) or ("ok"|"flag", flags)."""
    location = (job.location or "")[:_MAX_LOCATION]
    tl = f"{job.title} {location}"
    tl_low = tl.lower()
    has_remote = "remote" in tl_low or "anywhere" in tl_low
    flags: list[str] = []

    if _NON_REMOTE.search(tl):
        if not has_remote:
            return "drop", ["hybrid/on-site"]
        flags.append("remote/hybrid ambiguity")
    if job.remote is not True and not has_remote:
        return "drop", ["not remote"]

    loc = location
    if _NON_US.search(loc) and not _US_OK.search(loc):
        return "drop", ["outside US"]
    # Negation counts only when attached to a state list: "Remote - US (excluding TX, CA)" drops,
    # "Remote (not hybrid) - Texas" and "No Travel, Texas" do not.
    excluded: set[str] = set()
    for m in _NEG_THEN_STATES.finditer(loc):
        excluded |= _find_states(m.group(1), min_abbrs=1)
    for m in _STATES_THEN_NEG.finditer(loc):
        excluded |= _find_states(m.group(1), min_abbrs=1)
    if home_abbr in excluded:
        return "drop", [f"location excludes {home_abbr}"]
    if not excluded and not (_US_OK.search(loc) or _NATIONWIDE.search(loc)):
        loc_states = _find_states(loc)
        if len(loc_states) >= 2 and home_abbr not in loc_states:
            if _remote_then_only_states(loc):
                return "drop", [f"state-restricted (no {home_abbr})"]  # "Remote - CA, NY, WA"
            flags.append(f"multi-office location, {home_abbr} not listed")  # ambiguous, not a restriction

    return ("flag" if flags else "ok"), flags


# ---- employment type -------------------------------------------------------------------------
_BAD_TYPES = re.compile(
    r"(?<![A-Za-z])(?:contract(?:or)?|temporary|temp|part[-_ ]?time|intern(?:ship)?|freelance|seasonal|per[- ]diem)"
    r"(?![A-Za-z])",
    re.I,
)
_FULL_TYPE = re.compile(r"(?<![A-Za-z])(?:full[-_ ]?time|permanent)(?![A-Za-z])", re.I)
# "Contract Software Engineer" / "Temp Software Engineer": probably a contract role, but could be a subject.
_TITLE_LEADS_CONTRACT = re.compile(r"^\s*(?:contract|temp)\s", re.I)
# "contract"/"temp" in a title only counts when it reads like an engagement, not a subject
# ("Contract Management Systems Engineer", "Temp Sensor Engineer").
_BAD_TITLE = re.compile(
    r"\bcontract(?:or)?\b\s*[-–—(/)]"
    r"|\bcontract(?:or)?\s*$"
    r"|\((?:contract|temp|temporary)\b"
    r"|\bcontract\s+(?:role|position|to hire)\b"
    r"|\bcontract-to-hire\b"
    r"|\btemp(?:orary)?\b\s*[-–—(/)]"
    r"|\btemp(?:orary)?\s*$"
    r"|\bpart[- ]time\b"
    r"|\bintern(?:ship)?\b",
    re.I,
)


def employment_ok(job: Job, *, include_description: bool = True) -> tuple[bool, str]:
    """(keep?, reason). A kept job may carry a non-empty reason, which apply_filters records as a flag.

    include_description=False judges only the structured type field and the title.
    """
    reasons: list[str] = []
    et = job.employment_type or ""
    if _BAD_TYPES.search(et):
        if not _FULL_TYPE.search(et):
            return False, "not full-time"
        reasons.append("employment type ambiguous")  # e.g. "Full-time / Contract"
    if _BAD_TITLE.search(job.title or ""):
        return False, "not full-time"
    if include_description and contract_terms_mentioned(job.description):
        return False, "contract terms"
    if _TITLE_LEADS_CONTRACT.match(job.title or ""):
        reasons.append("title suggests contract/temp")
    return True, "; ".join(reasons)


# ---- title -----------------------------------------------------------------------------------
def title_ok(title: str, include: list[str], exclude: list[str]) -> bool:
    t = title or ""
    if any(re.search(p, t, re.I) for p in exclude):
        return False
    return not include or any(re.search(p, t, re.I) for p in include)


# ---- salary ----------------------------------------------------------------------------------
def salary_check(job: Job, floor: float) -> tuple[str, str]:
    """Owner rule: a stated range is judged by its MIDPOINT (a single stated value is its own midpoint).

    Only a max is known ("up to X"): drop if max < floor, else flag (midpoint unknown).
    Only a min is known ("$X+"): ok if min >= floor, else flag.
    """
    lo, hi = job.salary_min, job.salary_max
    if lo is None and hi is None:
        return "flag", "salary not listed"
    if lo is not None and hi is not None:
        if (lo + hi) / 2 < floor:
            return "drop", "salary midpoint below floor"
        if lo < floor:
            return "flag", "salary range straddles floor (midpoint ok)"
        return "ok", ""
    if hi is not None:
        if hi < floor:
            return "drop", "salary below floor"
        return "flag", "salary max only (midpoint unknown)"
    if lo < floor:
        return "flag", f"salary from ${lo / 1000:.0f}k (no max listed)"
    return "ok", ""


# ---- combined --------------------------------------------------------------------------------
# --- Work travel -------------------------------------------------------------------------------------
# Hard drop only when the (structured) title/location field states travel in explicit phrasing.
# Description text only FLAGS: the ranker adjudicates, since company offsites 1-2x/year are fine.
_TRAVEL_FIELD = re.compile(
    r"(?<![\d.])[1-9]\d{0,2}\s*%\s*travel\b"
    r"|(?<!\bno\s)(?<!\bwithout\s)\btravel\s*(?:required|up to|[1-9]\d{0,2}\s*%)",
    re.I,
)


def travel_in_title_or_location(job: Job) -> bool:
    """True when the title or location field states work travel ("25% travel", "travel required")."""
    text = f"{(job.title or '')[:_MAX_LOCATION]} | {(job.location or '')[:_MAX_LOCATION]}"
    return bool(_TRAVEL_FIELD.search(text))


# --- On-call ------------------------------------------------------------------------------------
# Owner rule: a required on-call rotation is a dealbreaker. Title/location stating it drops; description
# wording only FLAGS (the ranker decides: occasional after-hours help without a rotation is fine).
_ONCALL_FIELD = re.compile(
    rf"\(\s*{ONCALL}\s*\)"
    rf"|^\s*{ONCALL}\b"
    rf"|\b{ONCALL}\s+(?:rotations?|required|schedule|shifts?)\b"
    rf"|\b24\s*[/x]\s*7\s+{ONCALL}\b"
    r"|\bpager\s*duty\s+rotations?\b",
    re.I,
)
_FIELD_NEG = re.compile(r"\b(?:no|not|non|without|never)\b|n['’]t", re.I)


def oncall_in_title_or_location(job: Job) -> bool:
    """True when the title or location field states on-call ("(On-Call)", "on-call rotation", "24/7 on-call")."""
    for text in ((job.title or "")[:_MAX_LOCATION], (job.location or "")[:_MAX_LOCATION]):
        for m in _ONCALL_FIELD.finditer(text):
            before = re.split(r"[-–—|,(/;:]", text[max(0, m.start() - 25):m.start()])[-1]
            if not _FIELD_NEG.search(before):
                return True
    return False


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class FilterResult:
    kept: list[Job] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)
    excluded: Counter = field(default_factory=Counter)


def apply_filters(jobs: list[Job], cfg: dict, exclusions, since: str | None) -> FilterResult:
    """Run the hard filters in a fixed order; first failing rule decides the drop reason.

    Only structured signals drop a job. Description-derived findings (degree, clearance, residency,
    contract wording, office attendance) are appended to job.flags for the ranker.
    """
    res = FilterResult()
    floor = cfg["salary_floor"]
    home = cfg.get("home_state_abbr", "TX")
    include = cfg.get("title_include") or []
    exclude = cfg.get("title_exclude") or []
    for job in jobs:
        if not title_ok(job.title, include, exclude):
            res.dropped["title"] += 1
            continue
        if since and _ISO_DATE.match(job.posted or "") and job.posted[:10] < since:
            res.dropped["too old"] += 1
            continue
        hit = exclusions.match(job.company, job.description)
        if hit:
            res.dropped["excluded company"] += 1
            res.excluded[hit] += 1
            continue
        # Hard drops use structured signals only (type field, title, location field). Anything read out of
        # the free-text description becomes a flag: the ranker reads the whole ad and handles negation.
        ok, why = employment_ok(job, include_description=False)
        if not ok:
            res.dropped[why] += 1
            continue
        if why:
            job.flags.extend(why.split("; "))
        if contract_terms_mentioned(job.description):
            job.flags.append("contract terms mentioned")
        status, why = classify_location(job, home, include_description=False)
        if status == "drop":
            res.dropped[why] += 1
            continue
        if status == "flag":
            job.flags.append(why)
        if travel_in_title_or_location(job):
            res.dropped["work travel (title/location)"] += 1
            continue
        if oncall_in_title_or_location(job):
            res.dropped["on-call (title/location)"] += 1
            continue
        if office_attendance_mentioned(job.description):
            job.flags.append("office attendance mentioned")
        status, why = residency_check(job.description, home)
        if status != "ok":
            job.flags.append(f"residency: {why}")
        if job.salary_min is None and job.salary_max is None:
            found = find_salary_in_description(job.description)
            if found:
                job.salary_min, job.salary_max = found
                job.salary_text = job.salary_text or "parsed from description"
        status, why = salary_check(job, floor)
        if status == "drop":
            res.dropped[why] += 1
            continue
        if status == "flag":
            job.flags.append(why)
        for snippet in travel_mentions(job.description):
            job.flags.append(f'travel: "{snippet}"')
        for snippet in oncall_mentions(job.description):
            job.flags.append(f'on-call: "{snippet}"')
        edu = education_check(job.description)
        if edu == "drop":
            job.flags.append("degree: Master's/PhD may be required")
        elif edu == "penalty":
            job.flags.append("degree: advanced degree mentioned")
        for snippet in degree_mentions(job.description):
            job.flags.append(f'degree: "{snippet}"')
        # cfg["drop_clearance_required"] is tolerated but ignored: clearance is always a flag now.
        if clearance_required(job.description):
            job.flags.append("clearance may be required")
        elif clearance_mentioned(job.description):
            job.flags.append("clearance mentioned")
        res.kept.append(job)
    return res
