"""Company exclusion list (real list lives in DATA_DIR/config/exclusions.yaml)."""
from __future__ import annotations

import re
from pathlib import Path

from .config import load_yaml
from .models import norm_company

# Staffing-agency boilerplate opens the ad. "our client success team" or "confidential client data"
# in the body must not match, so only the first few hundred characters are scanned.
_STAFFING_TEXT = re.compile(
    r"on behalf of (?:our|a) client"
    r"|\bour client,? (?:a|an|is)\b"
    r"|\bconfidential client\b(?=\s*(?:[,.:;)\-–—]|$))",
    re.I,
)
_STAFFING_SCAN_CHARS = 300

# Company-name heuristic for unknown staffing/recruiting firms. Matched case-insensitively against the
# normalized name (lowercase, punctuation -> spaces, ".com"/Inc/LLC suffixes dropped). Extend per user via
# the optional top-level `staffing_name_patterns` list in the private exclusions.yaml (merged with these).
STAFFING_NAME_PATTERNS: tuple[str, ...] = (
    r"\bstaffing\b",
    r"\brecruit(?:ing|ment|ers)?\b",
    r"\btalent (?:solutions|partners|group|acquisition)\b",
    r"\bworkforce (?:solutions|group)\b",
    r"\b(?:tech|it)?placements?\b",
    r"\b(?:it|tech|technical) resources\b",
    r"\bpersonnel\b",
    r"\bemployment (?:agency|services)\b",
    r"\bsearch (?:group|partners)\b",
)
# "Recruit Holdings" (Indeed's parent) is an operating company, not a staffing firm.
_NAME_PATTERN_EXEMPT = re.compile(r"\bholdings\b")
_NAME_PATTERN_KEY = "staffing_name_patterns"


class Exclusions:
    def __init__(self, data: dict):
        self.exact: dict[str, str] = {}
        self.prefix: dict[str, str] = {}
        self.name_patterns: list[re.Pattern] = []
        data = dict(data or {})
        custom = data.pop(_NAME_PATTERN_KEY, None) or []
        for pat in (*STAFFING_NAME_PATTERNS, *custom):
            try:
                self.name_patterns.append(re.compile(pat, re.I))
            except (re.error, TypeError) as e:
                raise ValueError(f"invalid {_NAME_PATTERN_KEY} pattern {pat!r}: {e}") from e
        for group, entries in data.items():
            target = self.prefix if group == "staffing_agencies" else self.exact
            for e in entries or []:
                if isinstance(e, str):
                    e = {"name": e}
                if not isinstance(e, dict) or not e.get("name"):
                    raise ValueError(f"exclusions entry missing 'name': {e!r}")
                for n in [e["name"], *(e.get("aliases") or [])]:
                    target[norm_company(n)] = e["name"]

    @classmethod
    def load(cls, path: Path) -> "Exclusions":
        return cls(load_yaml(path)) if path.exists() else cls({})

    def match(self, company: str, description: str = "") -> str | None:
        c = norm_company(company)
        if c == "confidential":
            return "confidential company"
        if c and c in self.exact:
            return self.exact[c]
        if c:
            for p, name in self.prefix.items():
                if c == p or c.startswith(p + " "):
                    return name
        if c and not _NAME_PATTERN_EXEMPT.search(c) and any(p.search(c) for p in self.name_patterns):
            return "staffing agency (name pattern)"
        if _STAFFING_TEXT.search((description or "")[:_STAFFING_SCAN_CHARS]):
            return "staffing agency (our client)"
        return None
