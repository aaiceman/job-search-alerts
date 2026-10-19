"""Headless Claude ranking. Claude sees only its run directory and can write only ranked.json."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import PROMPTS_DIR, SCHEDULER_DIR
from .models import Job

log = logging.getLogger(__name__)


class BenefitRow(BaseModel):
    item: str
    mark: Literal["better", "same", "worse", "unknown"]
    detail: str
    label: Literal["Confirmed", "Likely", "Unknown"]
    source: str | None = None


class Reviews(BaseModel):
    summary: str
    glassdoor_rating: float | None = None
    glassdoor_trend: str | None = None
    recommend_pct: int | None = None
    pros: list[str] = []
    cons: list[str] = []
    red_flags: list[str] = []
    sources: list[str] = []


class CompanyResearch(BaseModel):
    company: str
    researched_on: str
    benefits: list[BenefitRow]
    reviews: Reviews


ROLE_TYPES = ("security_ops", "security_architecture", "it_infrastructure", "identity_m365", "integration",
              "ai_enablement", "it_leadership", "app_dev", "other")
RoleType = Literal["security_ops", "security_architecture", "it_infrastructure", "identity_m365", "integration",
                   "ai_enablement", "it_leadership", "app_dev", "other"]
PART_MAX = {"skills": 40, "seniority": 15, "compensation": 15, "pto_flex": 15, "remote": 10, "education": 5}
CAPPED_ROLES = ("app_dev", "other")  # the candidate is an architect/IT leader, not an application developer
CAPPED_SKILLS = 15                   # skills cap for CAPPED_ROLES
CAPPED_FALLBACK_TOTAL = 75           # total cap for CAPPED_ROLES when score_parts is unusable


class ScoreParts(BaseModel):
    skills: int = Field(ge=0, le=40)
    seniority: int = Field(ge=0, le=15)
    compensation: int = Field(ge=0, le=15)
    pto_flex: int = Field(ge=0, le=15)
    remote: int = Field(ge=0, le=10)
    education: int = Field(ge=0, le=5)


DEGREE_LEVELS = ("none", "associate", "bachelor", "master", "phd", "unspecified")
DEGREE_REQUIREMENTS = ("required", "preferred", "not_mentioned")
DEGREE_EQUIVALENT = ("yes", "no", "unknown")
DEGREE_QUOTE_MAX = 200


class DegreeInfo(BaseModel):
    level: Literal["none", "associate", "bachelor", "master", "phd", "unspecified"]
    requirement: Literal["required", "preferred", "not_mentioned"]
    equivalent_experience: Literal["yes", "no", "unknown"] = "unknown"
    quote: str = Field(default="", max_length=DEGREE_QUOTE_MAX)


class RankedJob(BaseModel):
    id: str
    score: int = Field(ge=0, le=100)  # final score, computed in Python by load_ranked
    role_type: RoleType | None = None  # None only for legacy output without role_type (no cap)
    score_parts: ScoreParts | None = None
    model_score: int | None = None    # Claude's own total, kept for debugging only
    fit: str
    gaps: list[str] = []
    pay: str
    pay_estimated: bool
    pay_min: int | None = None  # annual USD: stated range if the posting has one, else the model's estimate
    pay_max: int | None = None
    degree: DegreeInfo | None = None  # None: model omitted it or it was invalid (the digest falls back to flags)


class RankedOutput(BaseModel):
    jobs: list[RankedJob]
    companies: dict[str, CompanyResearch] = {}


_CANDIDATE_FIELDS = ("title", "company", "location", "salary_min", "salary_max", "salary_text",
                     "employment_type", "posted", "flags", "url", "extra_urls")
TRUNCATION_MARK = "…[truncated]"


# Paragraphs that never help the ranker. A paragraph with "$" or a pay word is always kept (pay transparency).
_BOILERPLATE = re.compile(
    r"equal opportunity employer|equal employment opportunity|e-verify|reasonable accommodation"
    r"|without regard to (?:race|sex)|protected veteran|affirmative action|privacy (?:notice|policy)|\bccpa\b"
    r"|we celebrate diversity|is committed to (?:diversity|creating a diverse)", re.I)
_PAY_WORDS = re.compile(r"\$|\b(?:salary|salaries|pay|wages?)\b|compensation (?:range|package|for this)"
                        r"|(?:base|annual|total) compensation", re.I)
_SECTION_HEADING = re.compile(r"responsibilit|qualification|requirement|what you['’]ll (?:do|bring)|who you are"
                              r"|about the role|skills", re.I)
TRIM_HEAD_CHARS = 1500     # the opening of a posting (title context, summary) is always kept
_HEADING_MAX_CHARS = 100   # a "heading" is a short line; long prose lines mentioning "skills" are not


def _drop_boilerplate(desc: str) -> str:
    parts = re.split(r"(\n\s*\n|\n)", desc)  # text, separator, text, separator, ...
    kept: list[str] = []
    for i in range(0, len(parts), 2):
        para, sep = parts[i], parts[i + 1] if i + 1 < len(parts) else ""
        if _BOILERPLATE.search(para) and not _PAY_WORDS.search(para):
            continue
        kept += [para, sep]
    return "".join(kept).strip()


def _first_heading(text: str, start: int) -> int | None:
    """Offset of the first short line at or after `start` that looks like a requirements-style heading."""
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.strip()
        if pos >= start and body and len(body) <= _HEADING_MAX_CHARS and _SECTION_HEADING.search(body):
            return pos
        pos += len(line)
    return None


def trim_description(desc: str, chars: int) -> tuple[str, bool]:
    """Fit a posting into `chars`: drop boilerplate paragraphs (EEO, E-Verify, privacy...) first; if still too
    long keep the first TRIM_HEAD_CHARS, then the text from the first responsibilities/qualifications-style
    heading, then fill from the head. truncated is True only when non-boilerplate text was cut."""
    if len(desc) <= chars:
        return desc, False
    text = _drop_boilerplate(desc)
    if len(text) <= chars:
        return text, False
    head_n = min(TRIM_HEAD_CHARS, chars)
    at = _first_heading(text, head_n)
    if at is None:
        return text[:chars] + TRUNCATION_MARK, True
    window = text[at:at + chars - head_n]
    head = text[:min(chars - len(window), at)]
    tail = TRUNCATION_MARK if at + len(window) < len(text) else ""
    gap = f"\n{TRUNCATION_MARK}\n\n" if len(head) < at else ""
    return head + gap + window + tail, True


def _candidate_row(j: Job, chars: int) -> dict:
    desc, truncated = trim_description(j.description or "", chars)
    return {"id": j.id, **{k: getattr(j, k) for k in _CANDIDATE_FIELDS},
            "description_truncated": truncated, "description": desc}


def prepare_run_dir(run_dir: Path, candidates: list[Job], profile_src: Path, cache: dict,
                    chars: int = 4000, per_file: int = 10) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for old in run_dir.glob("candidates-*.json"):
        old.unlink()
    (run_dir / "ranked.json").unlink(missing_ok=True)
    rows = [_candidate_row(j, chars) for j in candidates]
    for n, start in enumerate(range(0, len(rows), per_file), 1):
        (run_dir / f"candidates-{n:02d}.json").write_text(
            json.dumps(rows[start:start + per_file], indent=1), encoding="utf-8")
    shutil.copyfile(profile_src, run_dir / "profile.md")
    (run_dir / "company_cache.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")
    shutil.copyfile(PROMPTS_DIR / "rank.md", run_dir / "rank-instructions.md")
    (run_dir / "ranked.schema.json").write_text(json.dumps(claude_schema(), indent=1), encoding="utf-8")


def claude_schema() -> dict:
    """The schema Claude writes against: role_type and score_parts required, model_score hidden."""
    schema = RankedOutput.model_json_schema()
    job = schema["$defs"]["RankedJob"]
    job["properties"].pop("model_score", None)
    job["properties"]["score_parts"] = {"$ref": "#/$defs/ScoreParts"}
    job["properties"]["role_type"] = {"type": "string", "enum": list(ROLE_TYPES)}
    job["properties"]["score"]["description"] = ("Your own total for reference; Python computes the final "
                                                 "score from score_parts.")
    job["properties"]["degree"] = {"$ref": "#/$defs/DegreeInfo"}
    for k in ("pay_min", "pay_max"):
        job["properties"][k] = {"anyOf": [{"type": "integer"}, {"type": "null"}],
                                "description": "Annual USD (annualize hourly pay); null only if truly unknown."}
    job["required"] = sorted(set(job.get("required", [])) | {"score", "role_type", "score_parts", "degree",
                                                             "pay_min", "pay_max"})
    deg = schema["$defs"]["DegreeInfo"]
    deg["required"] = ["level", "requirement", "equivalent_experience", "quote"]
    return schema


# Built-in tools the ranker may even see. No shell, no Edit/NotebookEdit, no subagents; MCP is off via
# --strict-mcp-config. Write is further confined to ./ranked.json by the Edit(...) rule in ranker-settings.json.
RANKER_TOOLS = "Read,Glob,Write,WebSearch,WebFetch"
# Belt and braces. Never list "Edit" here: Edit permission rules cover every file-writing tool incl. Write.
RANKER_DISALLOWED = "Bash,PowerShell,NotebookEdit,Task,Agent"


# Hard cost cap per ranker run (probe-verified 2026-10-15 on 2.1.241 under subscription auth: the result is
# subtype "error_max_budget_usd", an early stop that keeps a ranked.json already written). A normal day uses
# roughly $2.6-5.2 of API-equivalent usage.
RANKER_MAX_BUDGET_USD = "8"


def build_command(claude_exe: str, model: str, settings: Path, today: date,
                  pay_target: float | None = None) -> list[str]:
    """pay_target is the owner's annual pay target (never the current base salary)."""
    target = f"Pay target: ${pay_target:,.0f} (annual USD). " if pay_target else ""
    task = (f"Today is {today.isoformat()}. {target}Read rank-instructions.md in the current directory and "
            f"follow it exactly. Write ranked.json, then stop.")
    return [
        claude_exe, "-p", task,
        "--model", model,
        "--permission-mode", "dontAsk",
        "--settings", str(settings),
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--safe-mode",  # probe-verified: drops CLAUDE.md/auto-memory/hooks/plugins, keeps --settings rules
        "--tools", RANKER_TOOLS,
        "--disallowedTools", RANKER_DISALLOWED,
        "--no-session-persistence",
        "--max-budget-usd", RANKER_MAX_BUDGET_USD,
        "--output-format", "json",
    ]


def resolve_claude_exe(configured: str | None) -> str:
    """Return a real claude.exe path. Never a .cmd shim: cmd.exe would re-parse the task argument."""
    candidates = [
        os.path.expandvars(configured or ""),
        shutil.which("claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "@anthropic-ai", "claude-code",
                     "bin", "claude.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"),
    ]
    for c in candidates:
        if c and "%" not in c and Path(c).is_file():
            return str(Path(c))
    tried = "; ".join(c for c in candidates if c) or "(none)"
    raise FileNotFoundError(f"claude.exe not found (configured: {configured!r}); tried: {tried}")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill claude and any children (taskkill /T), falling back to proc.kill()."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
    except Exception:
        log.exception("taskkill failed for pid %s", proc.pid)
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        log.exception("kill failed for pid %s", proc.pid)
    try:
        proc.wait(timeout=10)
    except Exception:
        log.warning("ranker pid %s did not exit after kill", proc.pid)


def _read_result(run_dir: Path) -> dict | None:
    """Best-effort parse of `claude -p --output-format json` stdout."""
    try:
        data = json.loads((run_dir / "ranker-output.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# Result subtypes meaning "stopped early" rather than "broke": a ranked.json written before that is usable.
_EARLY_STOP_MARKERS = ("max_turns", "timeout", "time_limit", "max_budget")


def _early_stop(subtype: str) -> bool:
    return any(m in subtype.lower() for m in _EARLY_STOP_MARKERS)


def configured_timeout_s(cfg: dict) -> int:
    return int(cfg.get("timeout_minutes", 45)) * 60


def run_ranker(run_dir: Path, cfg: dict, today: date, timeout_s: int | None = None,
               pay_target: float | None = None) -> None:
    """Run headless claude in run_dir. Returns normally whenever a ranked.json is worth validating.
    timeout_s overrides the configured timeout (the caller shrinks it to fit the whole-run budget).
    pay_target (annual USD, never the base salary) scopes the company research to jobs the digest will show."""
    exe = resolve_claude_exe(cfg.get("claude_exe"))
    cmd = build_command(exe, cfg.get("model", "sonnet"), SCHEDULER_DIR / "ranker-settings.json", today,
                        pay_target)
    if timeout_s is None:
        timeout_s = configured_timeout_s(cfg)
    ranked = run_dir / "ranked.json"
    log.info("ranker: %s (cwd=%s)", " ".join(cmd[:2] + ["<task>"] + cmd[3:]), run_dir)
    # Output goes to files (always recreated) so a hung child can't block us on a pipe. stdin=DEVNULL:
    # `claude -p` reads piped stdin, so an inherited open handle could stall the run.
    with open(run_dir / "ranker-output.json", "wb") as out, open(run_dir / "ranker-stderr.txt", "wb") as err:
        proc = subprocess.Popen(cmd, cwd=run_dir, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            if ranked.exists():
                log.warning("ranker timed out; using partial ranked.json")
                return
            raise TimeoutError(f"ranker exceeded {timeout_s}s and wrote no ranked.json")

    result = _read_result(run_dir)
    result_text = str((result or {}).get("result") or "")
    if result and result.get("is_error"):
        subtype = str(result.get("subtype") or "error")
        if ranked.exists() and _early_stop(subtype):
            log.warning("ranker stopped early (%s); using partial ranked.json", subtype)
            return
        raise RuntimeError(f"claude error ({subtype}): {result_text[:300]}")
    if rc != 0:
        if ranked.exists():
            log.warning("claude exited %s; using the ranked.json it wrote", rc)
            return
        stderr = (run_dir / "ranker-stderr.txt").read_text(encoding="utf-8", errors="replace").strip()
        stdout = (run_dir / "ranker-output.json").read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"claude exited {rc}: {(stderr or result_text or stdout)[:300]}")


# ---- output validation: per entry, with cheap coercion of near-misses ----

_MARKS = {m.lower(): m for m in ("better", "same", "worse", "unknown")}
_LABELS = {m.lower(): m for m in ("Confirmed", "Likely", "Unknown")}
_NUM = re.compile(r"\s*(-?\d+(?:\.\d+)?)")


def _num(v: Any) -> Any:
    """'4.1/5' -> 4.1, '85%' -> 85.0, 'N/A' -> None; numbers pass through; anything else unchanged."""
    if isinstance(v, str):
        m = _NUM.match(v)
        return float(m.group(1)) if m else (None if not v.strip() or not any(c.isdigit() for c in v) else v)
    return v


def _int(v: Any) -> Any:
    v = _num(v)
    return round(v) if isinstance(v, float) else v


def _lists(d: dict, *keys: str) -> None:
    for k in keys:
        if k in d and d[k] is None:
            d[k] = []


def _as_int(v: Any) -> int | None:
    v = _int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _role(v: Any, job_id: Any) -> str | None:
    if v is None:
        log.warning("job %s: no role_type; skills cap not applied", job_id)
        return None
    key = re.sub(r"[\s\-/]+", "_", v.strip().lower()) if isinstance(v, str) else ""
    if key in ROLE_TYPES:
        return key
    log.warning("job %s: unknown role_type %r; treating as 'other'", job_id, v)
    return "other"


def _parts(v: Any) -> dict | None:
    """Coerce and clamp score_parts; None when missing or any part is not a number."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k, mx in PART_MAX.items():
        x = _as_int(v.get(k))
        if x is None:
            return None
        out[k] = max(0, min(mx, x))
    return out


_DEGREE_LEVEL_SYN = {
    "none": "none", "no": "none", "no degree": "none", "not required": "none",
    "associate": "associate", "associates": "associate",
    "bachelor": "bachelor", "bachelors": "bachelor", "bs": "bachelor", "ba": "bachelor", "bsc": "bachelor",
    "undergraduate": "bachelor",
    "master": "master", "masters": "master", "ms": "master", "msc": "master", "ma": "master", "mba": "master",
    "phd": "phd", "doctorate": "phd", "doctoral": "phd", "doctor": "phd",
    "unspecified": "unspecified", "unknown": "unspecified", "any": "unspecified", "": "unspecified",
}
_DEGREE_REQ_SYN = {
    "required": "required", "require": "required", "requires": "required", "must": "required",
    "must have": "required", "mandatory": "required", "minimum": "required",
    "preferred": "preferred", "prefer": "preferred", "desired": "preferred", "nice to have": "preferred",
    "plus": "preferred", "a plus": "preferred", "optional": "preferred", "bonus": "preferred",
    "not mentioned": "not_mentioned", "none": "not_mentioned", "not stated": "not_mentioned",
    "unmentioned": "not_mentioned", "not required": "not_mentioned", "no": "not_mentioned",
}
_DEGREE_EQ_SYN = {
    "yes": "yes", "y": "yes", "true": "yes", "accepted": "yes", "allowed": "yes",
    "no": "no", "n": "no", "false": "no", "not accepted": "no",
    "unknown": "unknown", "unclear": "unknown", "not stated": "unknown", "n/a": "unknown", "": "unknown",
}


def _degree_key(v: str) -> str:
    """'Bachelor's degree' -> 'bachelors', 'Ph.D.' -> 'phd', 'Not_Mentioned' -> 'not mentioned'."""
    v = re.sub(r"['’.]", "", v.strip().lower())
    v = re.sub(r"\bdegree\b", "", v)
    return " ".join(re.sub(r"[_\-]+", " ", v).split())


def _syn(v: Any, table: dict[str, str]) -> Any:
    return table.get(_degree_key(v), v) if isinstance(v, str) else v


def _degree(v: Any, job_id: Any) -> dict | None:
    """Coerce the degree object; None (logged) when invalid. An invalid degree never invalidates the job."""
    if v is None:
        return None
    if not isinstance(v, dict):
        log.warning("job %s: degree is not an object (%r); ignoring it", job_id, v)
        return None
    d = dict(v)
    d["level"] = _syn(d.get("level"), _DEGREE_LEVEL_SYN)
    d["requirement"] = _syn(d.get("requirement"), _DEGREE_REQ_SYN)
    eq = d.get("equivalent_experience")
    if isinstance(eq, bool):
        d["equivalent_experience"] = "yes" if eq else "no"
    elif eq is None:
        d["equivalent_experience"] = "unknown"
    else:
        d["equivalent_experience"] = _syn(eq, _DEGREE_EQ_SYN)
    if d.get("quote") is None:
        d["quote"] = ""
    elif isinstance(d["quote"], str):
        d["quote"] = " ".join(d["quote"].split())[:DEGREE_QUOTE_MAX]
    try:
        return DegreeInfo.model_validate(d).model_dump()
    except ValidationError as e:
        log.warning("job %s: invalid degree object dropped: %s", job_id, _why(e))
        return None


PAY_MIN_SANE, PAY_MAX_SANE = 10_000, 2_000_000  # annual USD; outside this is junk (or an unannualized rate)
_PAY_RE = re.compile(r"(-?\d+(?:\.\d+)?)([km])?", re.I)


def _pay_amount(v: Any, job_id: Any, name: str) -> int | None:
    """'$110k' -> 110000, '110,000' -> 110000, 1.2M -> 1200000; junk, negative or absurd -> None (logged)."""
    if v is None:
        return None
    amount = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        amount = float(v)
    elif isinstance(v, str):
        cleaned = re.sub(r"[\s$,]|usd", "", v.strip(), flags=re.I)
        m = _PAY_RE.fullmatch(cleaned)
        if m:
            amount = float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
        elif not cleaned or not any(c.isdigit() for c in cleaned):
            if cleaned:
                log.warning("job %s: %s %r is not an amount; ignoring it", job_id, name, v)
            return None
    if amount is None or not PAY_MIN_SANE <= amount <= PAY_MAX_SANE:
        log.warning("job %s: %s %r is not a plausible annual USD amount; ignoring it", job_id, name, v)
        return None
    return round(amount)


def _is_dealbreaker(gaps: Any) -> bool:
    return (isinstance(gaps, list) and bool(gaps) and isinstance(gaps[0], str)
            and gaps[0].strip().lower().startswith("dealbreaker"))


def _coerce_job(e: Any) -> Any:
    """Normalise one job entry and compute the final score from score_parts (Python owns the score)."""
    if not isinstance(e, dict):
        return e
    e = dict(e)
    _lists(e, "gaps")
    if isinstance(e.get("gaps"), str):  # a bare string is one gap, not a list
        e["gaps"] = [e["gaps"]] if e["gaps"].strip() else []
    if "pay_estimated" not in e and isinstance(e.get("pay"), str):
        e["pay_estimated"] = bool(re.search(r"\best", e["pay"], re.I))
    jid = e.get("id")
    e["degree"] = _degree(e.get("degree"), jid)
    lo, hi = _pay_amount(e.get("pay_min"), jid, "pay_min"), _pay_amount(e.get("pay_max"), jid, "pay_max")
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    e["pay_min"], e["pay_max"] = lo, hi
    e["role_type"] = _role(e.get("role_type"), jid)
    e["model_score"] = _as_int(e.pop("score", None))
    capped = e["role_type"] in CAPPED_ROLES
    parts = _parts(e.get("score_parts"))
    if parts is not None:
        if capped:
            parts["skills"] = min(parts["skills"], CAPPED_SKILLS)
        e["score_parts"] = parts
        score = sum(parts.values())
    else:
        e["score_parts"] = None
        score = e["model_score"] if e["model_score"] is not None and 0 <= e["model_score"] <= 100 else None
        if capped and score is not None:
            score = min(score, CAPPED_FALLBACK_TOTAL)
        log.warning("job %s: score_parts missing or invalid; using model score %r (final %r)",
                    jid, e["model_score"], score)
    if score is not None and _is_dealbreaker(e.get("gaps")):
        score = 0
    if score is not None:
        e["score"] = score
    return e


def _coerce_company(e: Any) -> Any:
    if not isinstance(e, dict):
        return e
    e = dict(e)
    _lists(e, "benefits")
    if isinstance(e.get("benefits"), list):
        rows = []
        for b in e["benefits"]:
            if isinstance(b, dict):
                b = dict(b)
                if isinstance(b.get("mark"), str):
                    b["mark"] = _MARKS.get(b["mark"].strip().lower(), b["mark"])
                if isinstance(b.get("label"), str):
                    b["label"] = _LABELS.get(b["label"].strip().lower(), b["label"])
            rows.append(b)
        e["benefits"] = rows
    r = e.get("reviews")
    if isinstance(r, dict):
        r = dict(r)
        if "glassdoor_rating" in r:
            r["glassdoor_rating"] = _num(r["glassdoor_rating"])
        if "recommend_pct" in r:
            r["recommend_pct"] = _int(r["recommend_pct"])
        _lists(r, "pros", "cons", "red_flags", "sources")
        e["reviews"] = r
    return e


def _why(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc'])) or '<entry>'}: {x['msg']}" for x in e.errors()[:3])


def load_ranked(run_dir: Path, candidate_ids: list[str]) -> RankedOutput:
    """Validate ranked.json entry by entry: bad or unknown entries are dropped and logged, not fatal."""
    path = run_dir / "ranked.json"
    if not path.exists():
        said = str((_read_result(run_dir) or {}).get("result") or "").strip()
        raise FileNotFoundError(f"ranker did not write {path}" + (f"; claude said: {said[:300]}" if said else ""))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("ranked.json is not a JSON object")
    known = set(candidate_ids)
    jobs: list[RankedJob] = []
    seen: set[str] = set()
    raw_jobs = raw.get("jobs") or []
    if not isinstance(raw_jobs, list):
        log.warning("ranked.json 'jobs' is not a list; ignoring it")
        raw_jobs = []
    for i, entry in enumerate(raw_jobs):
        try:
            job = RankedJob.model_validate(_coerce_job(entry))
        except ValidationError as e:
            log.warning("dropping invalid job entry #%d: %s", i, _why(e))
            continue
        if job.id not in known:
            log.warning("dropping job entry #%d with unknown id %r", i, job.id)
        elif job.id in seen:
            log.warning("dropping duplicate job entry #%d for id %s (first wins)", i, job.id)
        else:
            seen.add(job.id)
            jobs.append(job)
    missing = [i for i in candidate_ids if i not in seen]
    if missing:
        log.warning("ranked.json is missing %d of %d candidates: %s", len(missing), len(candidate_ids),
                    ", ".join(missing))
    if candidate_ids and not jobs:
        raise ValueError("ranked.json contains no valid entries for the candidate ids")

    companies: dict[str, CompanyResearch] = {}
    raw_companies = raw.get("companies") or {}
    if not isinstance(raw_companies, dict):
        log.warning("ranked.json 'companies' is not an object; ignoring it")
        raw_companies = {}
    for name, entry in raw_companies.items():
        try:
            companies[name] = CompanyResearch.model_validate(_coerce_company(entry))
        except ValidationError as e:
            log.warning("dropping invalid company research for %r: %s", name, _why(e))
    return RankedOutput(jobs=jobs, companies=companies)
