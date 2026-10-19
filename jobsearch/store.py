"""SQLite state: what was already emailed, per-day run status, 30-day company research cache."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Job, norm_company
from .watch import watch_key

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
  job_key TEXT PRIMARY KEY, first_sent TEXT NOT NULL, salary_max REAL, url TEXT, title TEXT, company TEXT);
CREATE TABLE IF NOT EXISTS runs (run_date TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_research (
  company_key TEXT PRIMARY KEY, researched TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watch_status (
  job_key TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, first_checked TEXT, last_seen_open TEXT,
  closed_on TEXT, last_status TEXT, last_updated_at TEXT);
"""


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def unsent(self, jobs: list[Job], today: date | None = None, resend_after_days: int = 90) -> list[Job]:
        """Jobs not emailed yet. A job emailed more than `resend_after_days` ago counts as new again
        (a re-posted role), and one re-posted with a higher salary resurfaces regardless of age."""
        today = today or date.today()
        cutoff = (today - timedelta(days=resend_after_days)).isoformat()
        out = []
        seen: set[str] = set()
        for j in jobs:
            if j.key in seen:  # same job twice in one batch: first wins
                continue
            seen.add(j.key)
            row = self.db.execute("SELECT salary_max, first_sent FROM sent WHERE job_key=?", (j.key,)).fetchone()
            if row is None:
                out.append(j)
            elif j.salary_max and (row[0] or 0) < j.salary_max:
                self._flag(j, "repost with higher salary")
                out.append(j)
            elif row[1] < cutoff:
                self._flag(j, f"reposted (last emailed {row[1]})")
                out.append(j)
        return out

    @staticmethod
    def _flag(job: Job, flag: str) -> None:
        if flag not in job.flags:
            job.flags.append(flag)

    def mark_sent(self, jobs: list[Job], run_date: str) -> None:
        # first_sent is refreshed on every re-send so it always means "last emailed" for the expiry window
        self.db.executemany(
            "INSERT INTO sent(job_key, first_sent, salary_max, url, title, company) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(job_key) DO UPDATE SET first_sent=excluded.first_sent, salary_max=CASE "
            "WHEN excluded.salary_max IS NULL THEN sent.salary_max "
            "WHEN sent.salary_max IS NULL THEN excluded.salary_max "
            "ELSE MAX(sent.salary_max, excluded.salary_max) END",
            [(j.key, run_date, j.salary_max, j.url, j.title, j.company) for j in jobs],
        )
        self.db.commit()

    def run_status(self, run_date: str) -> str | None:
        row = self.db.execute("SELECT status FROM runs WHERE run_date=?", (run_date,)).fetchone()
        return row[0] if row else None

    def set_run_status(self, run_date: str, status: str) -> None:
        self.db.execute(
            "INSERT INTO runs(run_date, status, updated) VALUES (?,?,?) "
            "ON CONFLICT(run_date) DO UPDATE SET status=excluded.status, updated=excluded.updated",
            (run_date, status, datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def get_research(self, companies: list[str], today: date, max_age_days: int = 30) -> dict[str, dict]:
        cutoff = (today - timedelta(days=max_age_days)).isoformat()
        out: dict[str, dict] = {}
        for name in companies:
            row = self.db.execute(
                "SELECT data FROM company_research WHERE company_key=? AND researched>=?",
                (norm_company(name), cutoff),
            ).fetchone()
            if row:
                try:
                    out[name] = json.loads(row[0])
                except ValueError:  # corrupt cache row: treat as a miss (it is overwritten when re-researched)
                    log.warning("undecodable research cache row for %r skipped", name)
        return out

    def delete_research(self, company: str) -> None:
        self.db.execute("DELETE FROM company_research WHERE company_key=?", (norm_company(company),))
        self.db.commit()

    def last_sent_date(self) -> date | None:
        row = self.db.execute("SELECT MAX(run_date) FROM runs WHERE status='sent'").fetchone()
        try:
            return date.fromisoformat(row[0]) if row and row[0] else None
        except ValueError:
            return None

    def put_research(self, company: str, data: dict, today: date) -> None:
        self.db.execute(
            "INSERT INTO company_research(company_key, researched, data) VALUES (?,?,?) "
            "ON CONFLICT(company_key) DO UPDATE SET researched=excluded.researched, data=excluded.data",
            (norm_company(company), today.isoformat(), json.dumps(data)),
        )
        self.db.commit()

    _WATCH_COLS = ("job_key", "company", "title", "url", "first_checked", "last_seen_open", "closed_on",
                   "last_status", "last_updated_at")

    def record_watch(self, entry: dict, result, today: date) -> dict:
        """Fold today's check of a watched posting into watch_status and return the updated row.
        first_checked is set once; last_seen_open moves on "open"; closed_on is the first day seen closed
        and is kept (cleared, with a "reopened" log, if the posting comes back); "unknown" changes neither."""
        key, day = watch_key(entry), today.isoformat()
        cols = ", ".join(self._WATCH_COLS)
        old = self.db.execute(f"SELECT {cols} FROM watch_status WHERE job_key=?", (key,)).fetchone()
        row = dict(zip(self._WATCH_COLS, old)) if old else dict.fromkeys(self._WATCH_COLS)
        row.update(job_key=key, company=entry.get("company"), title=entry.get("title"), url=entry.get("url"),
                   last_status=result.status)
        row["first_checked"] = row["first_checked"] or day
        if result.status == "open":
            if row["closed_on"]:
                log.info("watch: %s @ %s reopened (had been closed since %s)", row["title"], row["company"],
                         row["closed_on"])
            row["closed_on"] = None
            row["last_seen_open"] = day
            row["last_updated_at"] = getattr(result, "updated_at", "") or row["last_updated_at"]
        elif result.status == "closed" and not row["closed_on"]:
            row["closed_on"] = day
        self.db.execute(
            f"INSERT INTO watch_status({cols}) VALUES ({', '.join('?' * len(self._WATCH_COLS))}) "
            "ON CONFLICT(job_key) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in self._WATCH_COLS[1:]),
            [row[c] for c in self._WATCH_COLS])
        self.db.commit()
        return row
