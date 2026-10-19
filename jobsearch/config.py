"""Paths and config loading. DATA_DIR lives outside the repo and is never committed."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JOBSEARCH_DATA", r"C:\JobSearch\data"))
CONFIG_DIR = APP_DIR / "config"
PROMPTS_DIR = APP_DIR / "prompts"
SCHEDULER_DIR = APP_DIR / "scheduler"
DEFAULT_RAISE_PCT = 25

log = logging.getLogger(__name__)


def data_path(*parts: str) -> Path:
    """Resolve a path under DATA_DIR (read at call time so tests can monkeypatch it)."""
    return DATA_DIR.joinpath(*parts)


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_config(name: str) -> dict:
    return load_yaml(CONFIG_DIR / name)


def load_owner() -> dict:
    """Private owner settings from DATA_DIR/config/owner.yaml (current_base_salary, min_raise_pct).
    Missing or invalid -> {} with a warning; never raises. Values are never logged."""
    path = data_path("config", "owner.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        log.warning("owner settings missing (%s); pay target = salary floor only", path)
        return {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("owner settings unreadable (%s: %s); pay target = salary floor only", path, type(e).__name__)
        return {}
    if not isinstance(data, dict) or not data:
        log.warning("owner settings in %s are not a mapping; pay target = salary floor only", path)
        return {}
    return data


def _positive(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _raise_pct(owner: dict) -> float:
    v = owner.get("min_raise_pct")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else DEFAULT_RAISE_PCT


def pay_target(filters_cfg: dict, owner: dict) -> float | None:
    """Owner rule: max(salary_floor, current_base_salary * (1 + min_raise_pct/100)); the floor alone when no
    valid base. None only when neither a floor nor a base is known (then no pay gate applies)."""
    floor = _positive((filters_cfg or {}).get("salary_floor"))
    base = _positive((owner or {}).get("current_base_salary"))
    derived = base * (1 + _raise_pct(owner) / 100) if base is not None else None
    known = [v for v in (floor, derived) if v is not None]
    return max(known) if known else None


def pay_rule(owner: dict) -> str:
    """Human label for the pay target (never includes the base amount)."""
    if _positive((owner or {}).get("current_base_salary")) is None:
        return "floor"
    return f"max(floor, base+{_raise_pct(owner):g}%)"


WATCH_ATS = ("greenhouse", "lever", "ashby")
_WATCH_REQUIRED = ("company", "title", "ats", "board", "job_id")


def _as_date(v) -> date | None:
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


def parse_watch(data) -> list[dict]:
    """Validate/normalise the `applications:` list of a watch.yaml mapping. Bad entries are skipped with a
    warning; never raises. Normalised keys: company, title, url, ats (lowercase), board, job_id (str),
    posted/applied (date | None)."""
    apps = data.get("applications") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return []
    out = []
    for i, e in enumerate(apps):
        if not isinstance(e, dict):
            log.warning("watch entry #%d is not a mapping; skipped", i + 1)
            continue
        norm = {k: (str(e[k]).strip() if e.get(k) is not None else "") for k in (*_WATCH_REQUIRED, "url")}
        norm["ats"] = norm["ats"].lower()
        missing = [k for k in _WATCH_REQUIRED if not norm[k]]
        if missing or norm["ats"] not in WATCH_ATS:
            log.warning("watch entry #%d invalid (%s); skipped", i + 1,
                        f"missing {', '.join(missing)}" if missing else f"unsupported ats {norm['ats']!r}")
            continue
        for k in ("posted", "applied"):
            norm[k] = _as_date(e.get(k))
            if e.get(k) is not None and norm[k] is None:
                log.warning("watch entry #%d: %s date %r unreadable; ignored", i + 1, k, str(e.get(k))[:20])
        out.append(norm)
    return out


def load_watch() -> list[dict]:
    """Applications to watch, from DATA_DIR/config/watch.yaml (private). Missing/invalid -> [] + warning;
    an empty file or empty `applications:` list -> [] + info."""
    path = data_path("config", "watch.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        log.warning("application watch list missing (%s); watching nothing", path)
        return []
    except (OSError, yaml.YAMLError) as e:
        log.warning("application watch list unreadable (%s: %s); watching nothing", path, type(e).__name__)
        return []
    if data is None or (isinstance(data, dict) and "applications" in data and data["applications"] in (None, [])):
        # an intentionally empty list (`applications: []`) is a normal state, not a problem
        log.info("application watch list %s is empty; watching nothing", path)
        return []
    entries = parse_watch(data)
    if not entries:
        log.warning("application watch list %s has no usable entries", path)
    return entries
