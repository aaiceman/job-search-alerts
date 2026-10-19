"""The one normalized job record every source produces."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields

_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh", "lp", "llp"}

_STATE_NAMES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii "
    "idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan "
    "minnesota mississippi missouri montana nebraska nevada new hampshire jersey mexico york north "
    "carolina dakota ohio oklahoma oregon pennsylvania rhode island south tennessee texas utah "
    "vermont virginia washington west wisconsin wyoming district of columbia"
).split()
_STATE_CODES = (
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny "
    "nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc"
).split()
_LOCATION_WORDS = (
    {"remote", "us", "usa", "united", "states", "hybrid", "anywhere", "americas", "america", "and", "or"}
    | set(_STATE_NAMES)
    | set(_STATE_CODES)
)
_TRAILING_REGIONS = ("north america", "americas")


def _fold(text: str) -> str:
    """Lowercase and strip accents (NFKD, drop combining marks); keeps non-Latin letters."""
    s = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def norm_company(name: str) -> str:
    s = _fold(name)
    s = re.sub(r"\.com\b", "", s.replace("&", " and "))
    words = re.sub(r"[\W_]+", " ", s).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def _is_location_paren(content: str) -> bool:
    content = re.sub(r"remote[- ]first", "remote", content)
    return all(w in _LOCATION_WORDS for w in re.findall(r"[a-z0-9]+", content))  # empty -> True


def norm_title(title: str) -> str:
    t = _fold(title)
    t = re.sub(r"\bu\.s\.(?:a\.?)?", "us", t)
    # drop parentheticals that only say where/how the job is worked; keep others (e.g. "(Python)")
    t = re.sub(r"\(([^)]*)\)", lambda m: " " if _is_location_paren(m.group(1)) else f" {m.group(1)} ", t)
    t = re.sub(r"remote[- ]first", " ", t)
    t = re.sub(r"\b(remote|us|usa|united states)\b", " ", t)
    t = re.sub(r"\bsr\b\.?", "senior", t)
    t = " ".join(re.sub(r"[^a-z0-9 ]+", " ", t).split())
    stripped = True
    while stripped:
        stripped = False
        for region in _TRAILING_REGIONS:
            if t.endswith(" " + region):
                t = t[: -len(region)].rstrip()
                stripped = True
    return t


@dataclass
class Job:
    source: str
    url: str
    title: str
    company: str
    location: str = ""
    remote: bool | None = None
    salary_min: float | None = None  # annual USD
    salary_max: float | None = None  # annual USD
    salary_text: str = ""
    employment_type: str = ""
    posted: str = ""  # ISO date YYYY-MM-DD, "" if unknown
    description: str = ""
    extra_urls: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        company, title = norm_company(self.company), norm_title(self.title)
        if not company or not title:  # never merge unrelated jobs on an empty part
            return f"url|{self.url}"
        return f"{company}|{title}"

    @property
    def id(self) -> str:
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()[:10]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
