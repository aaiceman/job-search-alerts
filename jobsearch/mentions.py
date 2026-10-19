"""Free-text "mention" scanners: description wording that FLAGS a job for the ranker, never drops it.

Each scanner reads the posting description (travel, on-call, degree, clearance, contract wording, office
attendance). `clause_mentions` is the one shared split-into-clauses / skip / dedupe / snippet loop behind the
three snippet scanners. Pure functions, linear time (bounded regexes over clauses of the first 50k chars).
"""
from __future__ import annotations

import re
from typing import Callable

MAX_DESC = 50_000
CLAUSE_SPLIT = re.compile(r"[.\n;•]")


def _snippet(clause: str, start: int, limit: int, lead: int = 60) -> str:
    """Whitespace-collapsed clause text, windowed around `start` when longer than `limit`."""
    text = " ".join(clause.split())
    if len(text) > limit:
        pos = len(" ".join(clause[:start].split()))
        begin = max(0, pos - lead)
        text = text[begin:begin + limit].strip()
    return text


def clause_mentions(
    desc: str,
    term: "re.Pattern | Callable[[str], re.Match | None]",
    *,
    skip: Callable[[str, re.Match], bool] | None = None,
    limit: int = 140,
    max_snippets: int = 2,
    prepare: Callable[[str], str] | None = None,
) -> list[str]:
    """Up to `max_snippets` distinct whitespace-collapsed clause snippets of `desc` that match `term`.

    Only the first 50k chars are read (then `prepare`, if given, normalises them), split on `. ; newline •`.
    `term` is a compiled pattern or a callable `clause -> Match | None`; its match anchors the snippet window
    when a clause is longer than `limit`. `skip(clause, match)` returning True drops that clause.
    """
    search = term.search if hasattr(term, "search") else term
    text = (desc or "")[:MAX_DESC]
    if prepare is not None:
        text = prepare(text)
    out: list[str] = []
    for clause in CLAUSE_SPLIT.split(text):
        m = search(clause)
        if not m or (skip is not None and skip(clause, m)):
            continue
        snippet = _snippet(clause, m.start(), limit)
        if snippet and snippet not in out:
            out.append(snippet)
            if len(out) >= max_snippets:
                break
    return out


# ---- office attendance -----------------------------------------------------------------------
_OFFICE = re.compile(
    r"\b(\d|one|two|three|four)\s+days?\s+(?:a|per)\s+week\s+(?:in|at)\s+(?:the\s+|our\s+)?office"
    r"|must be (?:located|based) within (?:commuting|driving) distance"
    r"|relocation (?:is )?required",
    re.I,
)


def office_attendance_mentioned(desc: str) -> bool:
    return bool(_OFFICE.search((desc or "")[:MAX_DESC]))


# ---- contract wording ------------------------------------------------------------------------
_BAD_TEXT = re.compile(
    r"contract[-_ ]to[-_ ]hire|\bc2h\b|\bc2c\b|corp[- ]to[- ]corp|\bw-?2 contract\b"
    r"|\b1099\s+(?:contractor|only|basis|position|role|employee|consultant)\b"
    r"|\bcontract (?:role|position|assignment|engagement)\b|\b\d{1,2}[- ]month contract\b",
    re.I,
)
_TEXT_NEGATION = re.compile(r"\b(no|not|non|without|never)\b|n['’]t", re.I)


def _affirmed(pattern: re.Pattern, text: str, window: int = 30) -> bool:
    """True if `pattern` matches somewhere without a negation earlier in the same clause."""
    for m in pattern.finditer(text):
        before = text[max(0, m.start() - window):m.start()]
        before = re.split(r"[.\n;!?]", before)[-1]
        if not _TEXT_NEGATION.search(before):
            return True
    return False


def contract_terms_mentioned(desc: str) -> bool:
    """Description text that reads like a contract engagement (negated mentions are ignored)."""
    return _affirmed(_BAD_TEXT, desc or "")


# ---- education -------------------------------------------------------------------------------
_ADV = r"(master(?:'|’)?s\b|\bmsc\b|\bmba\b|\bphd\b|doctorate|doctoral)"
_REQ = re.compile(r"\b(required|require|requires|must have|must possess|minimum)\b", re.I)
_SOFT = re.compile(
    r"\b(preferred|a plus|nice[- ]to[- ]have|desired|desirable|beneficial|ideal|helpful|an asset|"
    r"advantageous|favou?red|or equivalent|equivalent experience|bonus)\b",
    re.I,
)
_BACH = re.compile(r"\bbachelor(?:'|’)?s?\b", re.I)
# "Master's degree or 8+ years of experience": an experience alternative means no degree is required.
# The alternative must follow "or" directly; "or related field ..., with 5+ years" is not an alternative.
_EXPERIENCE_ALT = re.compile(
    r"\bor\s+(?:an?\s+)?(?:(?:minimum of|at least|significant|substantial|extensive|proven|demonstrated|comparable"
    r"|commensurate|related|relevant|equivalent|industry)\s+)*"
    r"(?:equivalent\b|\d+\+?\s*(?:years?|yrs)|(?:work\s+|industry\s+|professional\s+|practical\s+)?experience)"
    r"|\bunless\b[^;.\n]{0,60}\b(?:\d+\+?\s*(?:years?|yrs)|experience)"
    r"|\bin lieu\b",
    re.I,
)
_EDU_NEG = re.compile(r"\b(no|not|non|without|neither|nor)\b|n['’]t", re.I)


def _normalize_degrees(text: str) -> str:
    text = re.sub(r"ph\.\s?d\.?", "PhD", text, flags=re.I)
    text = re.sub(r"\bm\.s\.", "masters", text, flags=re.I)
    return re.sub(r"\bb\.[sa]\.", "bachelors", text, flags=re.I)


def education_check(desc: str) -> str:
    """"ok" | "penalty" | "drop". Owner has no degree; Bachelor's-required is acceptable."""
    rank = {"ok": 0, "penalty": 1, "drop": 2}
    worst = "ok"
    text = _normalize_degrees(desc or "")  # "Ph.D." → "PhD" so the '.' split can't cut a clause
    for line in re.split(r"[.\n;]", text):
        if not re.search(_ADV, line, re.I):
            continue
        if _EDU_NEG.search(line) or _EXPERIENCE_ALT.search(line):
            continue  # "Master's degree is not required" / "Master's or 8+ years of experience"
        if _SOFT.search(line):
            verdict = "penalty"
        elif _BACH.search(line):
            verdict = "ok"
        elif _REQ.search(line):
            verdict = "drop"
        else:
            verdict = "penalty"
        if rank[verdict] > rank[worst]:
            worst = verdict
    return worst


# ---- clearance -------------------------------------------------------------------------------
_CLEARANCE_TERM = (
    r"(?:ts/sci|top secret|polygraph|(?:security|secret|dod|government|federal|active)\s+clearance"
    r"|clearance(?=\s*:))"
)
_CLEARANCE_AFFIRM = (
    r"(?:active|current(?:ly)?|must\s+(?:currently\s+)?(?:have|hold|possess|maintain)"
    r"|(?:currently\s+)?(?:hold|holds|possess|possesses)|requires?|required|requirement)"
)
_CLEARANCE = re.compile(
    rf"\b{_CLEARANCE_AFFIRM}\b.{{0,40}}?\b{_CLEARANCE_TERM}\b|\b{_CLEARANCE_TERM}\b.{{0,40}}?\b{_CLEARANCE_AFFIRM}\b",
    re.I,
)
# A negation exempts a clause only when it attaches to the clearance itself:
# "no security clearance required", "clearance is not required", "polygraph: none".
# "no sponsorship available" or "no exceptions" leave the requirement standing.
_CLEARANCE_WORD = r"(?:clearance|polygraph|ts/sci|top secret)"
_CLEARANCE_NEG = r"(?:\b(?:no|not|neither|without|never)\b|n['’]t)"
_CLEARANCE_EXEMPT = re.compile(
    rf"{_CLEARANCE_NEG}\W+(?:\w+\W+){{0,3}}(?:security\s+)?{_CLEARANCE_WORD}"
    # "do not need to hold a clearance", "no requirement to hold a clearance"
    rf"|{_CLEARANCE_NEG}\W+(?:\w+\W+){{0,6}}?\b(?:need|require\w*|have to|hold|possess|must)\b.{{0,50}}?{_CLEARANCE_WORD}"
    # "clearance is not required", "clearance requirement: none", "clearance required: no"
    rf"|{_CLEARANCE_WORD}\W+(?:(?:is|will|be)\W+)?(?:(?:not|none|n/a)\b"
    rf"|(?:requirements?|required?)\s*[:=]\s*(?:none|no|n/a|not)\b)"  # a colon, so "required, no exceptions" is not one
    r"|\b(?:obtain|obtaining|eligible|eligibility|a plus|preferred|nice to have)\b"
    r"|\b(?:will|can|able to)\s+(?:be\s+)?sponsor",  # only the affirmative form of sponsor exempts
    re.I,
)


def clearance_required(desc: str) -> bool:
    """True only on an affirmative requirement (active/must hold/required) not negated or waived."""
    for clause in re.split(r"[.\n;]", desc or ""):
        if _CLEARANCE.search(clause) and not _CLEARANCE_EXEMPT.search(clause):
            return True
    return False


_CLEARANCE_MENTION = re.compile(r"\bclearance\b|ts/sci|top secret|polygraph", re.I)


def clearance_mentioned(desc: str) -> bool:
    """True when the description mentions clearance wording at all (requirement or not)."""
    return bool(_CLEARANCE_MENTION.search(desc or ""))


# --- Work travel -------------------------------------------------------------------------------------
_TRAVEL_TERM = re.compile(
    r"\btravel(?:l?ing|s|ed)?\b|\bon-?site visits?\b|\b(?:customer|client) sites?\b"
    r"|\bsite visits?\b|\bdata cent(?:er|re) visits?\b",
    re.I,
)
_TRAVEL_NEGATED = re.compile(
    r"\b(?:no|zero|without|never|non)\W+(?:\w+\W+){0,2}travel"
    r"|(?<![\d.])0\s*%\s*travel"
    r"|\btravel\W+(?:(?:is|are)\s+)?(?:not|never)\s+(?:required|needed|expected|necessary)"
    r"|\btravel\s+isn['’]t\s+(?:required|needed|expected)"
    r"|\bnot\s+(?:require|need|expect)\w*\s+(?:\w+\s+){0,2}travel"
    r"|\btravel[- ]free\b",
    re.I,
)
_TRAVEL_LABEL_NONE = re.compile(
    r"\btravel\s*[:\-–—]\s*(?:0\s*%|none\b|n/?a\b|no\b|not\s+required\b)", re.I)
# Onsite presence as a job duty (incidents, installs, customer sites ...) counts even without "travel".
_ONSITE = re.compile(r"\b(?:on-?site|in[- ]person)\b", re.I)
_ONSITE_DUTY = re.compile(
    r"\b(?:incidents?|outage|installs?|installation|deploy(?:ment)?|hardware|data cent(?:er|re)|customer|client)\b",
    re.I,
)
_ONSITE_NEGATED = re.compile(r"\b(?:no|not|without|never)\W+(?:\w+\W+){0,3}(?:on-?site|in[- ]person)", re.I)
_TRAVEL_BENEFIT = re.compile(
    r"reimburs|stipend|per diem|allowance|expenses?\b|covered|paid for|perks?\b|benefits?\b|lodging",
    re.I,
)
_TRAVEL_GATHERING = re.compile(
    r"off-?site|retreat|summit|all-hands|team gathering|company gathering|on-?site week|meet-?ups?",
    re.I,
)
_TRAVEL_STRONG = re.compile(
    r"\d\s*%|\b(?:customers?|clients?|partners?|field|data\s*cent(?:er|re))\b|(?<![-\w])sites?\b",
    re.I,
)


def _travel_match(clause: str) -> re.Match | None:
    """Travel wording, or onsite presence as a duty; None when the clause is negated or not about either."""
    m = _TRAVEL_TERM.search(clause)
    if m:
        return None if (_TRAVEL_NEGATED.search(clause) or _TRAVEL_LABEL_NONE.search(clause)) else m
    m = _ONSITE.search(clause)
    if not m or not _ONSITE_DUTY.search(clause) or _ONSITE_NEGATED.search(clause):
        return None
    return m


def _travel_benefit_or_gathering(clause: str, _m: re.Match) -> bool:
    return bool(
        (_TRAVEL_BENEFIT.search(clause) or _TRAVEL_GATHERING.search(clause)) and not _TRAVEL_STRONG.search(clause)
    )


def travel_mentions(desc: str) -> list[str]:
    """Up to two short snippets of description clauses that mention work travel.

    Skips clauses that are negated ("no travel", "0% travel"), benefit wording (reimbursement, stipend,
    per diem) or company-gathering wording (offsite, summit, retreat), unless the clause also has a
    percentage or customer/client/partner/site wording. Linear time: split into clauses, bounded regexes.
    """
    return clause_mentions(desc, _travel_match, skip=_travel_benefit_or_gathering)


# --- On-call ------------------------------------------------------------------------------------
ONCALL = r"on[- ]?call(?!\s*cent(?:er|re)s?\b)"
_ONCALL_TERM = re.compile(
    rf"\b{ONCALL}\b"
    r"|\bpager\s+duty\b"
    r"|\bcarry(?:ing)?\s+(?:the\s+|a\s+)?pager\b"
    r"|\bpagerduty\b(?=.{0,80}?\b(?:rotations?|schedules?|shifts?|after[- ]hours|24\s*/\s*7|weekends?)\b)"
    r"|\b(?:after[- ]hours|outside\s+(?:of\s+)?(?:normal\s+|regular\s+|standard\s+)?business\s+hours)\b"
    r"(?=.{0,80}?\b(?:support|incidents?|escalations?|assist\w*|availab\w*|coverage|respond\w*|work)\b)",
    re.I,
)
_ONCALL_NEGATED = re.compile(
    r"\b(?:no|not|non|without|never|zero)\W+(?:\w+\W+){0,2}?(?:on[- ]?call|pager|after[- ]hours)"
    r"|\bon[- ]?call\W+(?:(?:is|are)\s+)?(?:not|never|none\b|n/a\b)"
    r"|\bon[- ]?call\s+isn['’]t\b",
    re.I,
)


def oncall_mentions(desc: str) -> list[str]:
    """Up to two short snippets of description clauses that mention on-call / pager / after-hours duty.

    Negated clauses ("no on-call", "on-call not required", "without on-call") are skipped. Linear time:
    split into clauses, bounded regexes.
    """
    return clause_mentions(desc, _ONCALL_TERM, skip=lambda clause, _m: bool(_ONCALL_NEGATED.search(clause)))



# --- Degree mentions ----------------------------------------------------------------------------------
# Quote the posting's degree / "or equivalent experience" wording so the ranker (and the digest, when the
# ranker gives no degree object) can see it. Flags only; education_check still decides the advanced-degree flag.
_DEG_ABBR = r"(?-i:\b(?:BS|BA|MS|BSc|MSc)\b)"  # "MS" alone is usually Microsoft: only counted next to degree words
_DEGREE_TERM = re.compile(
    r"(?<![\w-])degrees?\b(?!\s+of\b)"  # not "a high degree of", not "360-degree"
    r"|\bbachelor(?:['’]?s)?\b"
    r"|\bmaster(?:['’]s|s)\b|\bmaster\s+of\s+(?:science|arts|business|engineering)\b"
    r"|\bphd\b|\bdoctora(?:te|l)\b|\bmba\b"
    rf"|{_DEG_ABBR}\s*(?:/|,|&|\bor\b|\band\b)\s*(?-i:(?:BS|BA|MS|BSc|MSc|PhD|MBA)\b)"
    rf"|{_DEG_ABBR}\s+(?:in|degree)\b"
    r"|\bequivalent\s+(?:\w+\s+){0,4}?experience\b",
    re.I,
)
_DEGREE_DOTS = (
    (re.compile(r"\bph\.\s?d\b\.?", re.I), "PhD"),
    (re.compile(r"\bm\.b\.a\.", re.I), "MBA"),
    (re.compile(r"\bb\.s\.", re.I), "BS"),
    (re.compile(r"\bb\.a\.", re.I), "BA"),
    (re.compile(r"\bm\.s\.", re.I), "MS"),
    (re.compile(r"\be\.g\.", re.I), "eg"),
    (re.compile(r"\bi\.e\.", re.I), "ie"),
)


def _normalize_dots(text: str) -> str:
    for pat, repl in _DEGREE_DOTS:
        text = pat.sub(repl, text)
    return text


def degree_mentions(desc: str) -> list[str]:
    """Up to two short snippets of description clauses about degrees or "or equivalent experience".

    Dotted abbreviations ("B.S.", "Ph.D.") are collapsed first so they do not split a clause. "MS"/"BS"/"BA"
    only count next to degree words ("BS/MS", "MS in", "BS degree"): "MS Intune" is Microsoft.
    Linear time: split into clauses, bounded regexes.
    """
    return clause_mentions(desc, _DEGREE_TERM, limit=160, prepare=_normalize_dots)
