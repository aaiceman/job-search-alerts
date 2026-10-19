import logging
from datetime import date

import pytest

from jobsearch import collect
from jobsearch.exclusions import Exclusions
from jobsearch.models import Job
from jobsearch.store import Store

FCFG = {"salary_floor": 175000, "home_state_abbr": "TX",
        "title_include": [r"secur"], "title_exclude": [], "max_candidates": 2}


def J(title, smax=200000, posted="2026-09-30"):
    return Job(source="s", url=f"https://x/{title}", title=title, company="Acme", remote=True,
               location="United States", salary_min=180000, salary_max=smax, posted=posted)


def test_window_hours():
    assert collect.window_hours(date(2026, 9, 28), {}) == 72  # Monday
    assert collect.window_hours(date(2026, 9, 29), {"window_hours": {"default": 24}}) == 24


def test_collect_isolates_source_failures_and_caps(monkeypatch, tmp_path):
    def good(ctx):
        return [J("Security Engineer"), J("Security Architect"), J("Security Analyst"), J("Accountant")]

    def bad(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(collect, "SOURCES", {"good": good, "bad": bad})
    store = Store(tmp_path / "db.sqlite")
    store.mark_sent([J("Security Analyst")], "2026-09-29")
    cands, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), store, portals=[])
    assert len(cands) == 2
    assert stats["already_sent"] == 1 and stats["candidates"] == 2 and stats["over_cap"] == 0
    assert stats["dropped"] == {"title": 1}
    by_name = {s["name"]: s for s in stats["sources"]}
    assert by_name["good"]["ok"] and by_name["good"]["count"] == 4
    assert not by_name["bad"]["ok"] and "boom" in by_name["bad"]["error"]


def test_cap_prefers_known_salary(monkeypatch, tmp_path):
    jobs = [J("Security A", smax=None), J("Security B"), J("Security C")]
    jobs[0].salary_min = None
    monkeypatch.setattr(collect, "SOURCES", {"only": lambda ctx: jobs})
    cands, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[])
    assert [c.title for c in cands] == ["Security B", "Security C"] and stats["over_cap"] == 1


def test_source_warnings_surface_in_stats(monkeypatch, tmp_path):
    def warns(ctx):
        ctx.warnings.append("warns: 1 of 2 queries failed")
        return [J("Security Engineer")]

    monkeypatch.setattr(collect, "SOURCES", {"warns": warns})
    _, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[])
    assert stats["warnings"] == ["warns: 1 of 2 queries failed"]


def test_collection_deadline_skips_remaining_sources(monkeypatch, tmp_path):
    ran = []
    clock = {"t": 1000.0}

    def slow(ctx):
        ran.append("slow")
        clock["t"] += 3000  # this source eats the whole budget
        return [J("Security Engineer")]

    def later(ctx):
        ran.append("later")
        return [J("Security Architect")]

    monkeypatch.setattr(collect, "SOURCES", {"slow": slow, "later": later, "last": later})
    monkeypatch.setattr(collect, "_monotonic", lambda: clock["t"])
    cands, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[],
                                   deadline=1000.0 + 2400)
    assert ran == ["slow"]
    by = {s["name"]: s for s in stats["sources"]}
    assert by["slow"]["ok"] and not by["later"]["ok"] and by["later"]["error"] == "skipped: time budget"
    assert not by["last"]["ok"] and by["last"]["count"] == 0
    assert "collection time budget reached; skipped: later, last" in stats["warnings"]
    assert [c.title for c in cands] == ["Security Engineer"]


def test_no_deadline_or_not_reached_runs_everything(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "SOURCES", {"a": lambda ctx: [], "b": lambda ctx: []})
    monkeypatch.setattr(collect, "_monotonic", lambda: 5.0)
    for dl in (None, 10.0):
        _, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[],
                                   deadline=dl)
        assert all(s["ok"] for s in stats["sources"]) and stats["warnings"] == []


def test_deadline_ignores_sources_excluded_by_only(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "SOURCES", {"a": lambda ctx: [], "b": lambda ctx: []})
    monkeypatch.setattr(collect, "_monotonic", lambda: 99.0)
    _, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[],
                               only=["b"], deadline=1.0)
    assert [s["name"] for s in stats["sources"]] == ["b"]
    assert stats["warnings"] == ["collection time budget reached; skipped: b"]


def test_per_source_seconds_and_log_lines(monkeypatch, tmp_path, caplog):
    clock = {"t": 100.0}

    def slow(ctx):
        clock["t"] += 1832.4
        return [J("Security Engineer")]

    def broken(ctx):
        clock["t"] += 12.2
        raise RuntimeError("all ATS boards failed")

    monkeypatch.setattr(collect, "SOURCES", {"jobspy": slow, "ats": broken})
    monkeypatch.setattr(collect, "_monotonic", lambda: clock["t"])
    with caplog.at_level(logging.INFO, logger="jobsearch.collect"):
        _, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[])
    by = {s["name"]: s for s in stats["sources"]}
    assert by["jobspy"]["seconds"] == pytest.approx(1832.4) and by["ats"]["seconds"] == pytest.approx(12.2)
    assert "source jobspy: 1 jobs in 1832 s" in caplog.text
    assert "source ats FAILED after 12 s: RuntimeError: all ATS boards failed" in caplog.text


def test_skipped_source_has_zero_seconds(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "SOURCES", {"a": lambda ctx: []})
    monkeypatch.setattr(collect, "_monotonic", lambda: 99.0)
    _, stats = collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[],
                               deadline=1.0)
    assert stats["sources"][0]["seconds"] == 0.0


def test_collect_passes_deadline_to_sources(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(collect, "SOURCES", {"a": lambda ctx: seen.append(ctx.deadline) or []})
    monkeypatch.setattr(collect, "_monotonic", lambda: 5.0)
    collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[], deadline=50.0)
    collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), Store(tmp_path / "d"), portals=[])
    assert seen == [50.0, None]


def test_collect_hands_filters_and_sent_keys_to_sources(monkeypatch, tmp_path):
    seen = {}

    def src(ctx):
        seen["filters"] = ctx.filters_cfg
        seen["sent"] = ctx.skip_detail(J("Security Analyst"))
        seen["new"] = ctx.skip_detail(J("Security Engineer"))
        return []

    monkeypatch.setattr(collect, "SOURCES", {"src": src})
    store = Store(tmp_path / "db.sqlite")
    store.mark_sent([J("Security Analyst")], "2026-09-29")
    collect.collect(date(2026, 9, 30), {}, FCFG, Exclusions({}), store, portals=[])
    assert seen == {"filters": FCFG, "sent": True, "new": False}
