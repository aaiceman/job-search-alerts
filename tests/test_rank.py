import json
from datetime import date
from pathlib import Path

import pytest

from jobsearch import rank
from jobsearch.models import Job


def J(i):
    return Job(source="s", url=f"https://x/{i}", title=f"Security Engineer {i}", company=f"Co{i}",
               description="d" * 5000)


def test_prepare_run_dir_chunks_and_truncates(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("PROFILE", encoding="utf-8")
    run = tmp_path / "run"
    cands = [J(i) for i in range(25)]
    rank.prepare_run_dir(run, cands, profile, {"Co1": {"company": "Co1"}}, chars=4000, per_file=20)
    files = sorted(p.name for p in run.glob("candidates-*.json"))
    assert files == ["candidates-01.json", "candidates-02.json"]
    first = json.loads((run / "candidates-01.json").read_text(encoding="utf-8"))
    assert len(first) == 20 and first[0]["id"] == cands[0].id
    assert first[0]["description"] == "d" * 4000 + "…[truncated]" and first[0]["description_truncated"] is True
    assert (run / "profile.md").read_text(encoding="utf-8") == "PROFILE"
    assert json.loads((run / "company_cache.json").read_text(encoding="utf-8")) == {"Co1": {"company": "Co1"}}
    assert (run / "rank-instructions.md").exists() and (run / "ranked.schema.json").exists()


def test_build_command_is_locked_down(tmp_path):
    cmd = rank.build_command("claude.exe", "sonnet", tmp_path / "s.json", date(2026, 9, 30))
    joined = " ".join(cmd)
    assert cmd[0] == "claude.exe" and cmd[1] == "-p" and "2026-09-30" in cmd[2]
    assert "--permission-mode dontAsk" in joined and "--strict-mcp-config" in joined
    assert "--setting-sources project" in joined and "--no-session-persistence" in joined
    assert "--safe-mode" in cmd  # no CLAUDE.md / auto-memory / hooks / plugins in the ranker context
    assert "Bash" in cmd[cmd.index("--disallowedTools") + 1]
    assert "dangerously" not in joined and "--bare" not in joined


def test_build_command_limits_builtin_tools(tmp_path):
    cmd = rank.build_command("claude.exe", "sonnet", tmp_path / "s.json", date(2026, 9, 30))
    tools = set(cmd[cmd.index("--tools") + 1].split(","))
    assert tools == {"Read", "Glob", "Write", "WebSearch", "WebFetch"}
    # "Edit" permission rules cover every file-writing tool, so never deny it wholesale (it would block Write)
    assert "Edit" not in cmd[cmd.index("--disallowedTools") + 1].split(",")


def test_ranker_settings_file_is_locked_down():
    from jobsearch.config import SCHEDULER_DIR
    perms = json.loads((SCHEDULER_DIR / "ranker-settings.json").read_text(encoding="utf-8"))["permissions"]
    assert perms["defaultMode"] == "dontAsk"
    assert "Edit(./ranked.json)" in perms["allow"] and "Read(./**)" in perms["allow"]
    assert not any(r.startswith(("Write", "Bash", "PowerShell")) or r in ("Edit", "Read") for r in perms["allow"])
    assert {"Bash", "PowerShell"} <= set(perms["deny"])
    assert "Read(//c/JobSearch/data/profile/**)" in perms["deny"] and "Read(~/**)" in perms["deny"]
    # Rules use gitignore semantics: "data/*" matches the runs/ DIRECTORY and so everything under it,
    # which (probe-verified) blocked the run dir itself, including writing ranked.json.
    for rule in perms["deny"]:
        assert rule not in ("Read(//c/JobSearch/data/*)", "Read(//c/JobSearch/data/**)", "Read(//c/**)")
        assert not rule.startswith("Read(//c/JobSearch/data/runs")


def test_resolve_claude_exe_prefers_existing_configured_path(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    assert rank.resolve_claude_exe(str(exe)) == str(exe)


def test_resolve_claude_exe_falls_back_to_npm_install(tmp_path, monkeypatch):
    npm_exe = tmp_path / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    npm_exe.parent.mkdir(parents=True)
    npm_exe.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(rank.shutil, "which", lambda name: None)
    assert rank.resolve_claude_exe(str(tmp_path / "missing" / "claude.exe")) == str(npm_exe)


def test_resolve_claude_exe_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(rank.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError) as ei:
        rank.resolve_claude_exe(str(tmp_path / "missing.exe"))
    msg = str(ei.value)
    assert "missing.exe" in msg and "claude-code" in msg and ".local" in msg


class FakePopen:
    """Stands in for subprocess.Popen: writes canned stdout/stderr, optionally 'hangs' past the timeout."""
    instances = []

    def __init__(self, cmd, *, stdout_bytes=b"", stderr_bytes=b"", rc=0, hang=False, writes_ranked=None,
                 **kw):
        self.cmd, self.kw, self.pid = cmd, kw, 4242
        self.rc, self.hang, self.killed, self.waits = rc, hang, False, []
        kw["stdout"].write(stdout_bytes)
        kw["stderr"].write(stderr_bytes)
        if writes_ranked is not None:
            (Path(kw["cwd"]) / "ranked.json").write_text(json.dumps(writes_ranked), encoding="utf-8")
        FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.hang and not self.killed:
            raise rank.subprocess.TimeoutExpired(self.cmd, timeout)
        return self.rc

    def poll(self):
        return None if (self.hang and not self.killed) else self.rc

    def kill(self):
        self.killed = True


def _patch_popen(monkeypatch, **behaviour):
    FakePopen.instances = []
    killed = []
    monkeypatch.setattr(rank, "resolve_claude_exe", lambda configured: "claude.exe")
    monkeypatch.setattr(rank.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd, **behaviour, **kw))

    def fake_kill_tree(proc):
        killed.append(proc.pid)
        proc.killed = True

    monkeypatch.setattr(rank, "_kill_tree", fake_kill_tree)
    return killed


CFG = {"model": "sonnet", "timeout_minutes": 1}
OK_RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}).encode()


def test_run_ranker_uses_popen_with_files_and_closed_stdin(tmp_path, monkeypatch):
    (tmp_path / "ranker-stderr.txt").write_text("STALE", encoding="utf-8")
    _patch_popen(monkeypatch, stdout_bytes=OK_RESULT)
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    p = FakePopen.instances[0]
    assert p.kw["stdin"] is rank.subprocess.DEVNULL and p.kw["cwd"] == tmp_path and p.waits[0] == 60
    assert p.cmd[0] == "claude.exe"
    assert json.loads((tmp_path / "ranker-output.json").read_text(encoding="utf-8"))["result"] == "done"
    assert (tmp_path / "ranker-stderr.txt").read_text(encoding="utf-8") == ""  # recreated, no stale text


def test_run_ranker_timeout_with_partial_output_returns(tmp_path, monkeypatch, caplog):
    killed = _patch_popen(monkeypatch, hang=True, writes_ranked={"jobs": [], "companies": {}})
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    assert killed == [4242]
    assert "ranker timed out; using partial ranked.json" in caplog.text


def test_run_ranker_timeout_without_output_raises(tmp_path, monkeypatch):
    killed = _patch_popen(monkeypatch, hang=True)
    with pytest.raises(TimeoutError):
        rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    assert killed == [4242]


def test_kill_tree_uses_taskkill_then_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(rank.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    class P:
        pid, killed = 7, False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 1

    p = P()
    rank._kill_tree(p)
    assert calls == [["taskkill", "/F", "/T", "/PID", "7"]] and p.killed


def test_run_ranker_is_error_result_raises(tmp_path, monkeypatch):
    res = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "API Error: overloaded"}).encode()
    _patch_popen(monkeypatch, stdout_bytes=res, writes_ranked={"jobs": []})
    with pytest.raises(RuntimeError, match=r"claude error \(error_during_execution\): API Error: overloaded"):
        rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))


def test_run_ranker_max_turns_with_ranked_proceeds(tmp_path, monkeypatch, caplog):
    res = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}).encode()
    _patch_popen(monkeypatch, stdout_bytes=res, rc=1, writes_ranked={"jobs": []})
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    assert "error_max_turns" in caplog.text


def test_run_ranker_max_turns_without_ranked_raises(tmp_path, monkeypatch):
    res = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "x"}).encode()
    _patch_popen(monkeypatch, stdout_bytes=res, rc=1)
    with pytest.raises(RuntimeError, match="error_max_turns"):
        rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))


def test_run_ranker_nonzero_exit_with_ranked_warns(tmp_path, monkeypatch, caplog):
    _patch_popen(monkeypatch, stdout_bytes=b"garbage", stderr_bytes=b"boom", rc=3, writes_ranked={"jobs": []})
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    assert "exited 3" in caplog.text


def test_run_ranker_nonzero_exit_without_ranked_raises_with_stderr(tmp_path, monkeypatch):
    _patch_popen(monkeypatch, stdout_bytes=b"", stderr_bytes=b"Failed to authenticate", rc=1)
    with pytest.raises(RuntimeError, match="claude exited 1: Failed to authenticate"):
        rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))


def _valid(ids):
    return {"jobs": [{"id": i, "score": 80, "fit": "f", "gaps": [], "pay": "p", "pay_estimated": False} for i in ids],
            "companies": {"Co1": {"company": "Co1", "researched_on": "2026-09-30",
                                  "benefits": [{"item": "PTO", "mark": "better", "detail": "unlimited",
                                                "label": "Confirmed", "source": "https://co1/benefits"}],
                                  "reviews": {"summary": "ok"}}}}


def test_load_ranked_filters_unknown_ids(tmp_path):
    (tmp_path / "ranked.json").write_text(json.dumps(_valid(["a", "zzz"])), encoding="utf-8")
    out = rank.load_ranked(tmp_path, ["a", "b"])
    assert [j.id for j in out.jobs] == ["a"] and "Co1" in out.companies


def test_load_ranked_rejects_bad_score(tmp_path):
    bad = _valid(["a"])
    bad["jobs"][0]["score"] = 150
    (tmp_path / "ranked.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(Exception):
        rank.load_ranked(tmp_path, ["a"])


def test_load_ranked_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        rank.load_ranked(tmp_path, ["a"])


def _write(tmp_path, data):
    (tmp_path / "ranked.json").write_text(json.dumps(data), encoding="utf-8")


def _job(i, **kw):
    return {"id": i, "score": 70, "fit": "f", "gaps": [], "pay": "p", "pay_estimated": False, **kw}


def test_load_ranked_drops_bad_job_entries_keeps_good(tmp_path, caplog):
    _write(tmp_path, {"jobs": [_job("a", score=150), _job("b"), {"id": "c"}, "junk"], "companies": {}})
    out = rank.load_ranked(tmp_path, ["a", "b", "c"])
    assert [j.id for j in out.jobs] == ["b"]
    assert "dropping invalid job" in caplog.text and "missing" in caplog.text


def test_load_ranked_dedupes_first_wins(tmp_path, caplog):
    _write(tmp_path, {"jobs": [_job("a", score=10), _job("a", score=90)], "companies": {}})
    out = rank.load_ranked(tmp_path, ["a"])
    assert [(j.id, j.score) for j in out.jobs] == [("a", 10)] and "duplicate" in caplog.text


def test_load_ranked_raises_when_no_valid_known_job(tmp_path):
    _write(tmp_path, {"jobs": [_job("zzz"), _job("a", score="high")], "companies": {}})
    with pytest.raises(ValueError):
        rank.load_ranked(tmp_path, ["a"])


def test_load_ranked_logs_missing_candidates(tmp_path, caplog):
    _write(tmp_path, {"jobs": [_job("a")], "companies": {}})
    rank.load_ranked(tmp_path, ["a", "bbb", "ccc"])
    assert "bbb" in caplog.text and "ccc" in caplog.text and "missing" in caplog.text


def test_load_ranked_drops_bad_company_keeps_good(tmp_path, caplog):
    data = _valid(["a"])
    data["companies"]["BadCo"] = {"company": "BadCo", "benefits": "nope"}
    _write(tmp_path, data)
    out = rank.load_ranked(tmp_path, ["a"])
    assert list(out.companies) == ["Co1"] and "BadCo" in caplog.text


def test_load_ranked_coerces_near_misses(tmp_path):
    data = {"jobs": [_job("a", score=72.6, gaps=None)],
            "companies": {"Co1": {"company": "Co1", "researched_on": "2026-09-30",
                                  "benefits": [{"item": "PTO", "mark": "Better", "detail": "d",
                                                "label": "confirmed", "source": None},
                                               {"item": "401(k) match", "mark": "UNKNOWN", "detail": "d",
                                                "label": "LIKELY"}],
                                  "reviews": {"summary": "s", "glassdoor_rating": "4.1/5",
                                              "recommend_pct": "85%", "pros": None, "cons": None,
                                              "red_flags": None, "sources": None}}}}
    _write(tmp_path, data)
    out = rank.load_ranked(tmp_path, ["a"])
    assert out.jobs[0].score == 73 and out.jobs[0].gaps == []
    c = out.companies["Co1"]
    assert [(b.mark, b.label) for b in c.benefits] == [("better", "Confirmed"), ("unknown", "Likely")]
    r = c.reviews
    assert r.glassdoor_rating == 4.1 and r.recommend_pct == 85
    assert r.pros == [] and r.cons == [] and r.red_flags == [] and r.sources == []


def test_load_ranked_coerces_numeric_strings(tmp_path):
    data = _valid(["a"])
    data["jobs"][0]["score"] = "64"
    data["companies"]["Co1"]["reviews"].update(glassdoor_rating=3.9, recommend_pct=85.0)
    _write(tmp_path, data)
    out = rank.load_ranked(tmp_path, ["a"])
    assert out.jobs[0].score == 64 and out.companies["Co1"].reviews.recommend_pct == 85


def test_load_ranked_null_benefits_become_empty(tmp_path):
    data = _valid(["a"])
    data["companies"]["Co1"]["benefits"] = None
    _write(tmp_path, data)
    assert rank.load_ranked(tmp_path, ["a"]).companies["Co1"].benefits == []


def test_load_ranked_missing_file_includes_claude_result(tmp_path):
    (tmp_path / "ranker-output.json").write_text(
        json.dumps({"type": "result", "is_error": False, "result": "I could not read the candidates"}),
        encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="I could not read the candidates"):
        rank.load_ranked(tmp_path, ["a"])


# ---- score_parts: Python computes the final score ----

PARTS = {"skills": 30, "seniority": 15, "compensation": 10, "pto_flex": 7, "remote": 10, "education": 5}  # = 77


def _pjob(i="a", **kw):
    return _job(i, **{"score": 90, "role_type": "security_ops", "score_parts": dict(PARTS), **kw})


def _one(tmp_path, entry):
    _write(tmp_path, {"jobs": [entry], "companies": {}})
    return rank.load_ranked(tmp_path, [entry["id"]]).jobs[0]


def test_score_is_sum_of_parts_and_model_score_kept(tmp_path):
    j = _one(tmp_path, _pjob())
    assert j.score == 77 and j.model_score == 90 and j.role_type == "security_ops"
    assert j.score_parts.skills == 30 and j.score_parts.education == 5


def test_parts_are_clamped_to_their_max(tmp_path):
    parts = {"skills": 55, "seniority": 20, "compensation": -3, "pto_flex": 15, "remote": 12, "education": 9}
    j = _one(tmp_path, _pjob(score_parts=parts))
    assert (j.score_parts.skills, j.score_parts.seniority, j.score_parts.compensation) == (40, 15, 0)
    assert (j.score_parts.remote, j.score_parts.education) == (10, 5) and j.score == 85


def test_app_dev_and_other_skills_capped_at_15(tmp_path):
    for role in ("app_dev", "other"):
        j = _one(tmp_path, _pjob(role_type=role))
        assert j.score_parts.skills == 15 and j.score == 77 - 15, role


def test_dealbreaker_first_gap_zeroes_score(tmp_path):
    j = _one(tmp_path, _pjob(gaps=["DEALBREAKER: contract role — \"6-month contract\"", "x"]))
    assert j.score == 0 and j.model_score == 90


def test_part_and_enum_near_misses_are_coerced(tmp_path):
    parts = {"skills": 29.6, "seniority": "15", "compensation": 10.0, "pto_flex": 7, "remote": 10, "education": 5}
    j = _one(tmp_path, _pjob(role_type="App Dev", score_parts=parts))
    assert j.role_type == "app_dev" and j.score_parts.skills == 15 and j.score_parts.seniority == 15
    j = _one(tmp_path, _pjob(role_type="IT_Leadership"))
    assert j.role_type == "it_leadership" and j.score == 77


def test_unknown_role_type_becomes_other(tmp_path, caplog):
    j = _one(tmp_path, _pjob(role_type="wizard"))
    assert j.role_type == "other" and j.score_parts.skills == 15 and "wizard" in caplog.text


def test_missing_parts_falls_back_to_model_score(tmp_path, caplog):
    j = _one(tmp_path, _pjob(score_parts=None, score=88))
    assert j.score == 88 and j.score_parts is None and "score_parts" in caplog.text


def test_invalid_parts_fallback_caps_app_dev_at_75(tmp_path):
    j = _one(tmp_path, _pjob(role_type="app_dev", score_parts={"skills": "lots"}, score=88))
    assert j.score == 75 and j.model_score == 88


def test_fallback_still_applies_dealbreaker(tmp_path):
    j = _one(tmp_path, _pjob(score_parts=None, score=88, gaps=["DEALBREAKER: onsite — \"in office 5 days\""]))
    assert j.score == 0


def test_fallback_without_any_score_drops_entry(tmp_path):
    e = _pjob(score_parts=None)
    del e["score"]
    _write(tmp_path, {"jobs": [e, _pjob("b")], "companies": {}})
    assert [j.id for j in rank.load_ranked(tmp_path, ["a", "b"]).jobs] == ["b"]


def test_parts_valid_but_model_score_garbage_still_ok(tmp_path):
    j = _one(tmp_path, _pjob(score="high"))
    assert j.score == 77 and j.model_score is None


def test_schema_for_claude_requires_role_and_parts_and_hides_model_score(tmp_path):
    profile = tmp_path / "p.md"
    profile.write_text("P", encoding="utf-8")
    rank.prepare_run_dir(tmp_path / "run", [J(1)], profile, {}, chars=100, per_file=10)
    schema = json.loads((tmp_path / "run" / "ranked.schema.json").read_text(encoding="utf-8"))
    job = schema["$defs"]["RankedJob"]
    assert "model_score" not in job["properties"]
    assert {"role_type", "score_parts", "score"} <= set(job["required"])
    assert set(job["properties"]["role_type"]["enum"]) == set(rank.ROLE_TYPES)
    parts = schema["$defs"]["ScoreParts"]["properties"]
    assert {k: v["maximum"] for k, v in parts.items()} == rank.PART_MAX


def test_missing_role_type_is_logged_and_not_capped(tmp_path, caplog):
    e = _pjob()
    del e["role_type"]
    j = _one(tmp_path, e)
    assert j.role_type is None and j.score == 77 and "no role_type" in caplog.text


def test_fallback_out_of_range_model_score_drops_entry(tmp_path):
    _write(tmp_path, {"jobs": [_pjob("a", score_parts=None, score=150, role_type="app_dev"), _pjob("b")],
                      "companies": {}})
    assert [j.id for j in rank.load_ranked(tmp_path, ["a", "b"]).jobs] == ["b"]


def test_coerce_string_gaps_and_missing_pay_estimated(tmp_path):
    a = _job("a", gaps="No Splunk experience", pay="~$190k est. from comparable roles")
    b = _job("b", pay="$180k stated")
    c = _job("c", gaps="")
    for j in (a, b, c):
        del j["pay_estimated"]
    _write(tmp_path, {"jobs": [a, b, c], "companies": {}})
    out = {j.id: j for j in rank.load_ranked(tmp_path, ["a", "b", "c"]).jobs}
    assert out["a"].gaps == ["No Splunk experience"] and out["a"].pay_estimated is True
    assert out["b"].pay_estimated is False and out["c"].gaps == []


def test_coerce_keeps_explicit_pay_estimated(tmp_path):
    _write(tmp_path, {"jobs": [_job("a", pay="est. $200k", pay_estimated=False)], "companies": {}})
    assert rank.load_ranked(tmp_path, ["a"]).jobs[0].pay_estimated is False


def test_run_ranker_timeout_override(tmp_path, monkeypatch):
    _patch_popen(monkeypatch, stdout_bytes=OK_RESULT)
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30), timeout_s=123)
    assert FakePopen.instances[-1].waits[0] == 123


def test_configured_timeout_s():
    assert rank.configured_timeout_s({"timeout_minutes": 2}) == 120
    assert rank.configured_timeout_s({}) == 45 * 60


# ---- degree: per-job degree requirement summary ----

DEG = {"level": "bachelor", "requirement": "required", "equivalent_experience": "yes",
       "quote": "Bachelor's degree or equivalent experience"}


def test_degree_object_is_parsed(tmp_path):
    j = _one(tmp_path, _pjob(degree=dict(DEG)))
    assert j.degree.level == "bachelor" and j.degree.requirement == "required"
    assert j.degree.equivalent_experience == "yes" and j.degree.quote.startswith("Bachelor's")


def test_missing_degree_is_none(tmp_path):
    assert _one(tmp_path, _pjob()).degree is None
    assert _one(tmp_path, _pjob(degree=None)).degree is None


@pytest.mark.parametrize("raw,want", [
    ("Bachelors", "bachelor"), ("BS", "bachelor"), ("B.A.", "bachelor"), ("Bachelor's degree", "bachelor"),
    ("masters", "master"), ("MS", "master"), ("Master's", "master"), ("MBA", "master"),
    ("doctorate", "phd"), ("Ph.D.", "phd"), ("PHD", "phd"),
    ("Associate's", "associate"), ("NONE", "none"), ("no degree", "none"), ("Unspecified", "unspecified"),
])
def test_degree_level_synonyms(tmp_path, raw, want):
    assert _one(tmp_path, _pjob(degree={**DEG, "level": raw})).degree.level == want


@pytest.mark.parametrize("raw,want", [
    ("Required", "required"), ("must", "required"), ("minimum", "required"),
    ("PREFERRED", "preferred"), ("nice to have", "preferred"), ("a plus", "preferred"),
    ("not mentioned", "not_mentioned"), ("Not_Mentioned", "not_mentioned"), ("none", "not_mentioned"),
])
def test_degree_requirement_synonyms(tmp_path, raw, want):
    assert _one(tmp_path, _pjob(degree={**DEG, "requirement": raw})).degree.requirement == want


@pytest.mark.parametrize("raw,want", [
    (True, "yes"), (False, "no"), ("YES", "yes"), ("No", "no"), ("true", "yes"), ("false", "no"),
    ("Unknown", "unknown"), ("unclear", "unknown"), (None, "unknown"),
])
def test_degree_equivalent_experience_synonyms(tmp_path, raw, want):
    j = _one(tmp_path, _pjob(degree={**DEG, "equivalent_experience": raw}))
    assert j.degree.equivalent_experience == want


def test_degree_missing_optional_parts_default(tmp_path):
    j = _one(tmp_path, _pjob(degree={"level": "master", "requirement": "preferred"}))
    assert j.degree.equivalent_experience == "unknown" and j.degree.quote == ""


def test_degree_quote_is_capped_at_200(tmp_path):
    j = _one(tmp_path, _pjob(degree={**DEG, "quote": "x" * 500}))
    assert len(j.degree.quote) <= 200
    assert _one(tmp_path, _pjob(degree={**DEG, "quote": None})).degree.quote == ""


@pytest.mark.parametrize("bad", [
    "bachelor", ["bachelor"], {**DEG, "level": "wizard"}, {**DEG, "requirement": "sometimes"},
    {**DEG, "equivalent_experience": "maybe so"}, {"requirement": "required"}, {"level": "bachelor"},
    {**DEG, "quote": 42},
])
def test_invalid_degree_is_dropped_but_job_kept(tmp_path, caplog, bad):
    j = _one(tmp_path, _pjob(degree=bad))
    assert j.degree is None and j.score == 77
    assert "degree" in caplog.text


def test_schema_for_claude_requires_degree(tmp_path):
    schema = rank.claude_schema()
    job = schema["$defs"]["RankedJob"]
    assert "degree" in job["required"]
    assert job["properties"]["degree"] == {"$ref": "#/$defs/DegreeInfo"}
    deg = schema["$defs"]["DegreeInfo"]
    assert set(deg["required"]) == {"level", "requirement", "equivalent_experience", "quote"}
    assert set(deg["properties"]["level"]["enum"]) == {"none", "associate", "bachelor", "master", "phd",
                                                       "unspecified"}
    assert set(deg["properties"]["requirement"]["enum"]) == {"required", "preferred", "not_mentioned"}
    assert set(deg["properties"]["equivalent_experience"]["enum"]) == {"yes", "no", "unknown"}
    assert deg["properties"]["quote"]["maxLength"] == 200


# ---- pay_min / pay_max: annual USD, tolerant coercion ----

@pytest.mark.parametrize("raw,want", [
    (110000, 110000), (110000.6, 110001), ("110000", 110000), ("110,000", 110000), ("$110,000", 110000),
    ("$110k", 110000), ("110K", 110000), ("110.5k", 110500), (" $ 95k ", 95000), ("1.2M", 1200000),
    (None, None), ("", None), ("n/a", None), ("unknown", None), (-150000, None), ("-150k", None),
    (9999, None), (85, None), (2_000_001, None), ("3M", None), (True, None), ([110000], None), ({}, None),
])
def test_pay_amount_coercion(tmp_path, raw, want):
    j = _one(tmp_path, _pjob(pay_min=raw, pay_max=raw))
    assert j.pay_min == want and j.pay_max == want
    assert j.score == 77  # an invalid pay value never invalidates the job


def test_invalid_pay_amount_is_logged(tmp_path, caplog):
    j = _one(tmp_path, _pjob(pay_min="lots", pay_max=150000))
    assert j.pay_min is None and j.pay_max == 150000
    assert "pay_min" in caplog.text


def test_pay_min_max_swapped_when_reversed(tmp_path):
    j = _one(tmp_path, _pjob(pay_min=140000, pay_max="110k"))
    assert (j.pay_min, j.pay_max) == (110000, 140000)


def test_missing_pay_min_max_is_none(tmp_path):
    j = _one(tmp_path, _pjob())
    assert j.pay_min is None and j.pay_max is None


def test_schema_for_claude_requires_nullable_pay_min_max():
    job = rank.claude_schema()["$defs"]["RankedJob"]
    assert {"pay_min", "pay_max"} <= set(job["required"])
    for k in ("pay_min", "pay_max"):
        types = {t.get("type") for t in job["properties"][k]["anyOf"]}
        assert types == {"integer", "null"}, k


# ---- pay target in the task line, budget cap ----

def test_build_command_passes_pay_target_never_more(tmp_path):
    cmd = rank.build_command("claude.exe", "sonnet", tmp_path / "s.json", date(2026, 9, 30), 175000)
    assert "Pay target: $175,000 (annual USD)." in cmd[2]
    none = rank.build_command("claude.exe", "sonnet", tmp_path / "s.json", date(2026, 9, 30), None)
    assert "Pay target" not in none[2]


def test_build_command_caps_budget(tmp_path):
    cmd = rank.build_command("claude.exe", "sonnet", tmp_path / "s.json", date(2026, 9, 30), None)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "8"


def test_run_ranker_passes_pay_target(tmp_path, monkeypatch):
    _patch_popen(monkeypatch, stdout_bytes=OK_RESULT)
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30), pay_target=200000.0)
    assert "Pay target: $200,000 (annual USD)." in FakePopen.instances[-1].cmd[2]


def test_run_ranker_budget_stop_with_ranked_proceeds(tmp_path, monkeypatch, caplog):
    res = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True}).encode()
    _patch_popen(monkeypatch, stdout_bytes=res, rc=1, writes_ranked={"jobs": []})
    rank.run_ranker(tmp_path, CFG, date(2026, 9, 30))
    assert "stopped early (error_max_budget_usd)" in caplog.text


def test_ranker_settings_webfetch_is_domain_restricted():
    from jobsearch.config import SCHEDULER_DIR
    allow = json.loads((SCHEDULER_DIR / "ranker-settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "WebFetch" not in allow and "WebSearch" in allow
    fetch = {r for r in allow if r.startswith("WebFetch(")}
    assert "WebFetch(domain:glassdoor.com)" in fetch and "WebFetch(domain:www.levels.fyi)" in fetch
    assert all(r.startswith("WebFetch(domain:") and r.endswith(")") for r in fetch)
    assert len(fetch) == 17


# ---- section-aware description trimming ----

EEO = ("Acme is an equal opportunity employer. All qualified applicants will receive consideration without "
       "regard to race, color, religion, sex or national origin. ") * 4
PAY = "The base salary range for this role is $180,000 - $220,000 plus equity."


def test_trim_short_text_untouched():
    text = "About us\n\n" + EEO
    assert rank.trim_description(text, 6000) == (text, False)


def test_trim_drops_boilerplate_keeps_pay_paragraph():
    body = "Responsibilities\n" + "Build detections in Sentinel. " * 100
    eeo_pay = "We are an equal opportunity employer. " + PAY
    text = "\n\n".join([body, EEO, eeo_pay, "We use E-Verify to confirm eligibility.",
                        "Reasonable accommodation is available on request."])
    out, truncated = rank.trim_description(text, 3500)
    assert "regard to race" not in out and "E-Verify" not in out and "accommodation" not in out
    assert PAY in out and "Build detections" in out
    assert truncated is False  # only boilerplate was removed


def test_trim_keeps_head_and_heading_window():
    head = "About Acme. " * 300                 # ~3600 chars of company marketing
    reqs = "Requirements\n" + "- 10+ years with Microsoft Sentinel and KQL.\n" * 40
    text = head + "\n\n" + reqs
    out, truncated = rank.trim_description(text, 3000)
    assert truncated is True
    assert out.startswith(head[:1500])
    assert "Requirements\n- 10+ years with Microsoft Sentinel" in out
    assert len(out) <= 3000 + 2 * len(rank.TRUNCATION_MARK) + 4


def test_trim_without_heading_cuts_like_before():
    out, truncated = rank.trim_description("d" * 5000, 4000)
    assert (out, truncated) == ("d" * 4000 + rank.TRUNCATION_MARK, True)


def test_trim_heading_window_fills_from_head_when_short():
    head = "About Acme. " * 400                 # 4800 chars
    reqs = "\n\nQualifications\n- Entra ID\n- Intune\n"
    out, truncated = rank.trim_description(head + reqs, 3000)
    assert truncated is True and "Qualifications\n- Entra ID\n- Intune" in out
    assert len(out) >= 2900  # the unused window budget goes back to the head


def test_candidate_row_uses_trim():
    j = Job(source="s", url="https://x/1", title="t", company="c", description="x" * 10 + "\n\n" + EEO * 20)
    row = rank._candidate_row(j, 500)
    assert row["description"] == "x" * 10 and row["description_truncated"] is False
