"""Daily entry point: collect → rank → render → send.
Exit codes: 0 ok/already sent, 2 send failed (digest saved; a rerun resends it), 3 crashed (owner notified),
4 skipped because another run holds the lock."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import collect as collect_mod
from . import rank as rank_mod
from . import send as send_mod
from . import watch as watch_mod
from .config import data_path, load_config, load_owner, load_watch, pay_rule, pay_target
from .exclusions import Exclusions
from .models import Job
from .notify import toast
from .rank import CompanyResearch
from .render import DigestContext, _day, all_sources_failed, render
from .store import Store

log = logging.getLogger("jobsearch")
LATE_START_AFTER = time(6, 30)  # the task is scheduled at 05:45; starting after 06:30 means the PC missed it
LOCK_MAX_AGE = timedelta(hours=2)
MAX_WINDOW_HOURS = 7 * 24
# Whole-run wall-clock budget (the Task Scheduler limit is 90 min). The ranker is squeezed to fit; below
# MIN_RANK_TIME left, ranking is skipped and the unranked list is sent.
RUN_BUDGET = timedelta(minutes=80)
SEND_RESERVE = timedelta(minutes=8)
MIN_RANK_TIME = timedelta(minutes=5)
COLLECT_BUDGET = timedelta(minutes=40)
_monotonic = _time.monotonic
_RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-preview)?$")
_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.log$")


def _housekeeping(real_today: date) -> None:
    """Best-effort retention: dated logs > 30 days, dated run folders > 60 days. Anchored on the real
    clock (not --date) and limited to date-named items; a locked file never aborts the run."""
    logs_cutoff = (real_today - timedelta(days=30)).isoformat()
    runs_cutoff = (real_today - timedelta(days=60)).isoformat()
    try:
        for old in data_path("logs").glob("*.log"):
            try:
                if old.is_file() and _LOG_RE.match(old.name) and old.stem < logs_cutoff:
                    old.unlink()
            except OSError as e:
                log.warning("could not delete old log %s: %s", old.name, e)
        runs = data_path("runs")
        for d in runs.iterdir() if runs.exists() else []:
            try:
                if d.is_dir() and _RUN_DIR_RE.match(d.name) and d.name[:10] < runs_cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError as e:
                log.warning("could not delete old run %s: %s", d.name, e)
    except OSError as e:
        log.warning("housekeeping skipped: %s", e)


def _setup_logging(today: date) -> None:
    logs = data_path("logs")
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(logs / f"{today.isoformat()}.log", encoding="utf-8"),
                                  logging.StreamHandler()], force=True)
    _housekeeping(date.today())


def _acquire_lock(path: Path) -> bool:
    """Create run.lock (PID inside). A lock younger than LOCK_MAX_AGE means another run is active."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = _time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < LOCK_MAX_AGE.total_seconds():
                return False
            log.warning("taking over stale lock %s (%.0f min old)", path, age / 60)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _one_line(s: str, limit: int = 200) -> str:
    return " ".join(str(s).split())[:limit]


def main(argv: list[str] | None = None) -> int:
    started = datetime.now()
    t0 = _monotonic()
    ap = argparse.ArgumentParser(description="Build and send the daily job digest.")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default today)")
    ap.add_argument("--no-send", action="store_true", help="render to runs/<date>/digest.preview.html only")
    ap.add_argument("--force", action="store_true", help="run even if today's digest was already sent")
    ap.add_argument("--skip-rank", action="store_true")
    ap.add_argument("--window-hours", type=int, help="override collection window (e.g. 720 to bootstrap)")
    ap.add_argument("--weekdays-only", action="store_true",
                    help="exit 0 on Saturday/Sunday unless --force (the logon trigger uses this)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else date.today()
    lock = data_path("run.lock")
    have_lock = False
    store: Store | None = None
    try:
        try:
            _setup_logging(today)
        except Exception:  # never lose the run over log files: fall back to stderr
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                                force=True)
            log.exception("logging setup failed; logging to stderr only")
        if args.weekdays_only and today.weekday() >= 5 and not args.force:
            log.info("%s is a weekend; nothing to do (--weekdays-only)", today)
            return 0
        if not _acquire_lock(lock):
            log.info("another run holds the lock; skipped (%s is younger than %s)", lock, LOCK_MAX_AGE)
            return 4
        have_lock = True
        store = Store(data_path("jobsearch.db"))
        return _run(args, today, store, started, t0)
    except Exception as e:  # unattended: never die silently
        return _crashed(args, today, store, e)
    finally:
        if store is not None:
            store.close()
        if have_lock:
            try:
                lock.unlink()
            except OSError:
                pass


def _crashed(args: argparse.Namespace, today: date, store: Store | None, exc: Exception) -> int:
    log.error("run crashed", exc_info=exc)
    msg = _one_line(f"{type(exc).__name__}: {exc}", 300)
    log_path = data_path("logs", f"{today.isoformat()}.log")
    if store is not None:
        try:
            store.set_run_status(today.isoformat(), "crashed")
        except Exception:
            log.exception("could not record crashed status")
    try:
        toast("Job digest run CRASHED", f"{_one_line(msg, 150)} — see {log_path}")
    except Exception:
        log.exception("crash toast failed")
    if not args.no_send:
        subject = f"Jobs {_day(today)} — run crashed: {msg}"  # deliberately no job data
        body = f"The job digest run crashed: {msg}\nLog: {log_path}"
        try:
            send_mod.send_email(subject, f"<p>{html.escape(body).replace(chr(10), '<br>')}</p>", body,
                                send_mod.load_env(data_path(".env")))
        except Exception:
            log.exception("crash email failed")
    return 3


def _window_override(args: argparse.Namespace, today: date, store: Store, searches: dict) -> int | None:
    """After missed days, look back far enough to cover them (capped at 7 days)."""
    if args.window_hours:
        return args.window_hours
    last = store.last_sent_date()
    if last is None:
        return None
    normal = collect_mod.window_hours(today, searches)
    hours = min(max(normal, (today - last).days * 24), MAX_WINDOW_HOURS)
    log.info("last digest sent %s; collection window %sh (normal %sh)", last, hours, normal)
    return hours


def _load_cache(store: Store, candidates: list[Job], today: date) -> dict[str, CompanyResearch]:
    """Validated research cache. Bad rows are skipped and deleted; this never raises."""
    cached: dict[str, CompanyResearch] = {}
    try:
        raw = store.get_research(sorted({j.company for j in candidates}), today)
    except Exception:
        log.exception("research cache unreadable; continuing without it")
        return cached
    for name, data in raw.items():
        try:
            cached[name] = CompanyResearch.model_validate(data)
        except Exception as e:
            log.warning("invalid cached research for %r (%s); deleting", name, type(e).__name__)
            try:
                store.delete_research(name)
            except Exception:
                log.exception("could not delete bad research row for %r", name)
    return cached


def _finish_send(store: Store, today: date, run_dir: Path, subject: str, html_body: str, text_body: str,
                 marks: list[Job], degraded: bool = False) -> int:
    try:
        send_mod.send_email(subject, html_body, text_body, send_mod.load_env(data_path(".env")))
    except Exception as e:
        log.exception("send failed")
        store.set_run_status(today.isoformat(), "send_failed")
        toast("Job digest NOT sent", f"{e} — saved to {run_dir / 'digest.html'}")
        return 2
    # "sent_degraded" (every source failed) is not a real digest: it must not block a same-day rerun.
    # The digest is already delivered, so a bookkeeping failure must never reach the crash-email path.
    try:
        store.set_run_status(today.isoformat(), "sent_degraded" if degraded else "sent")  # before mark_sent
    except Exception:
        log.exception("could not record run status after a delivered digest; a rerun may resend it")
    if not degraded:
        try:
            store.mark_sent(marks, today.isoformat())
        except Exception:
            log.exception("mark_sent failed; these jobs may be emailed again")
    log.info("sent: %s", subject)
    return 0


def _resend_saved(store: Store, today: date, run_dir: Path) -> int | None:
    """After a failed send, resend the saved digest instead of recollecting. None if nothing usable saved."""
    try:
        html_body = (run_dir / "digest.html").read_text(encoding="utf-8")
        subject = (run_dir / "digest.subject.txt").read_text(encoding="utf-8")
        text_body = (run_dir / "digest.txt").read_text(encoding="utf-8")
        marks = [Job.from_dict(d) for d in json.loads((run_dir / "digest.marks.json").read_text(encoding="utf-8"))]
        stats = json.loads((run_dir / "stats.json").read_text(encoding="utf-8"))
        degraded = all_sources_failed(stats)
    except Exception as e:  # unreadable or malformed saved files: do a full run instead
        log.warning("saved digest unusable (%s: %s); doing a full run", type(e).__name__, e)
        return None
    log.info("resending saved digest from the failed attempt: %s", subject)
    return _finish_send(store, today, run_dir, subject, html_body, text_body, marks, degraded)


def _base_salary(owner: dict) -> float | None:
    """The owner's current base (annual USD) for the digest's "% vs current base"; None when absent/invalid.
    Never logged, never given to the ranker."""
    v = (owner or {}).get("current_base_salary")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _pay_gate(fcfg: dict) -> tuple[float | None, str, float | None]:
    """(pay target, rule label, current base). Never fails the run (no target -> no gate, no base -> no %)."""
    try:
        owner = load_owner()
        target = pay_target(fcfg, owner)
        rule = pay_rule(owner)
        base = _base_salary(owner)
    except Exception:
        log.exception("pay target unavailable; digest pay gate disabled")
        return None, "floor", None
    log.info("pay target %s (%s)", f"${target:,.0f}" if target is not None else "none", rule)
    return target, rule, base


def _profile_src() -> Path:
    """The ranker's profile: ranker-profile.md (no name, employers or salary) when present."""
    ranker = data_path("profile", "ranker-profile.md")
    if ranker.exists():
        return ranker
    log.warning("ranker-profile.md missing; the ranker sees the full personal profile")
    return data_path("profile", "personal-profile.md")


def _watch_rows(store: Store, today: date, stats: dict) -> list[dict]:
    """Check every watched application and record it. Best-effort: any failure only logs and adds a digest
    warning (one bad entry or a store error never costs the run or the other entries)."""
    rows: list[dict] = []
    problems: list[str] = []
    try:
        for entry, result in watch_mod.check_all(load_watch()):
            try:
                row = store.record_watch(entry, result, today)
            except Exception as e:
                log.exception("could not record watch status for %s", entry.get("job_id"))
                problems.append(f"{type(e).__name__}: {e}")
                continue
            rows.append({**row, "posted": entry.get("posted"), "applied": entry.get("applied")})
    except Exception as e:
        log.exception("application watch failed")
        problems.append(f"{type(e).__name__}: {e}")
    if problems:
        stats["warnings"] = [*(stats.get("warnings") or []),
                             _one_line(f"application watch problem ({len(problems)}): {problems[0]}", 200)]
    return rows


def _run(args: argparse.Namespace, today: date, store: Store, started: datetime, t0: float | None = None) -> int:
    if t0 is None:
        t0 = _monotonic()
    status = store.run_status(today.isoformat())
    if status == "sent" and not args.force:
        log.info("digest for %s already sent; nothing to do", today)
        return 0

    # previews get their own folder so they can never clobber the real run's artifacts (or its resend files)
    run_dir = data_path("runs", today.isoformat() + ("-preview" if args.no_send else ""))
    if status == "send_failed" and not (args.force or args.no_send):
        rc = _resend_saved(store, today, run_dir)
        if rc is not None:
            return rc
    run_dir.mkdir(parents=True, exist_ok=True)
    fcfg = load_config("filters.yaml")
    searches = load_config("searches.yaml")
    excl_path = data_path("config", "exclusions.yaml")
    candidates, stats = collect_mod.collect(
        today, searches, fcfg, Exclusions.load(excl_path),
        store, load_config("portals.yml").get("portals", []), _window_override(args, today, store, searches),
        deadline=t0 + COLLECT_BUDGET.total_seconds())
    if not excl_path.exists():
        msg = f"exclusions file missing: {excl_path} — employer/PE/staffing exclusions NOT applied"
        log.warning(msg)
        stats["warnings"] = [*(stats.get("warnings") or []), msg]
    watch_rows = _watch_rows(store, today, stats)  # also on degraded / zero-candidate days
    (run_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    log.info("collected: %s", {k: stats.get(k) for k in ("collected", "unique", "candidates")})

    target, rule, base = _pay_gate(fcfg)  # before ranking: the ranker scopes its research to the target
    ranked, ranking_error, research = None, None, {}
    remaining = (RUN_BUDGET - SEND_RESERVE).total_seconds() - (_monotonic() - t0)
    if candidates and args.skip_rank:
        ranking_error = "ranking skipped (--skip-rank)"
    elif candidates and remaining < MIN_RANK_TIME.total_seconds():
        ranking_error = "skipped: run time budget exhausted after collection"
        log.warning("%s (%.0f s left for ranking)", ranking_error, remaining)
    elif candidates:
        cached = _load_cache(store, candidates, today)  # outside the ranker try: can't fail the ranking
        try:
            rcfg = fcfg.get("ranker", {})
            rank_mod.prepare_run_dir(run_dir, candidates, _profile_src(),
                                     {k: v.model_dump() for k, v in cached.items()},
                                     rcfg.get("description_chars", 6000), rcfg.get("jobs_per_file", 20))
            rank_mod.run_ranker(run_dir, rcfg, today,
                                timeout_s=int(min(rank_mod.configured_timeout_s(rcfg), remaining)),
                                pay_target=target)
            ranked = rank_mod.load_ranked(run_dir, [j.id for j in candidates])
        except Exception as e:
            log.exception("ranking failed")
            ranked, ranking_error = None, f"{type(e).__name__}: {e}"[:300]
        if ranked is not None:
            for name, cr in ranked.companies.items():
                try:
                    store.put_research(name, cr.model_dump(), today)
                except Exception:
                    log.exception("could not cache research for %r", name)
            research = {**cached, **ranked.companies}

    ctx = DigestContext(run_date=today, candidates=candidates, stats=stats, ranked=ranked, research=research,
                        ranking_error=ranking_error, ranking_skipped=bool(args.skip_rank and candidates),
                        late=started.time() > LATE_START_AFTER and not args.date and not args.no_send,
                        pay_target=target, pay_rule=rule, watch=watch_rows, pay_base=base)
    subject, html_body, text_body = render(ctx)
    # only ranked jobs count as seen (including those below the pay target); unranked ones retry tomorrow
    by_id = {j.id: j for j in candidates}
    marks = [by_id[r.id] for r in ranked.jobs if r.id in by_id] if ranked is not None else []
    if args.no_send:  # preview only: never overwrite the files a resend after a failed send relies on
        (run_dir / "digest.preview.html").write_text(html_body, encoding="utf-8")
        (run_dir / "digest.preview.subject.txt").write_text(subject, encoding="utf-8")
        (run_dir / "digest.preview.txt").write_text(text_body, encoding="utf-8")
        print(run_dir / "digest.preview.html")
        return 0
    (run_dir / "digest.html").write_text(html_body, encoding="utf-8")
    (run_dir / "digest.subject.txt").write_text(subject, encoding="utf-8")
    (run_dir / "digest.txt").write_text(text_body, encoding="utf-8")
    (run_dir / "digest.marks.json").write_text(json.dumps([j.to_dict() for j in marks]), encoding="utf-8")
    return _finish_send(store, today, run_dir, subject, html_body, text_body, marks, all_sources_failed(stats))


if __name__ == "__main__":
    raise SystemExit(main())
