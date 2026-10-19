import time

import pytest

from jobsearch.sources import ats
from jobsearch.sources.base import SourceContext


def test_greenhouse_parse():
    data = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "title": "Security Engineer",
                      "company_name": "Acme", "location": {"name": "Remote - US"},
                      "first_published": "2026-09-28T12:52:11-04:00",
                      "content": "&lt;p&gt;Salary range: $180,000 - $220,000&lt;/p&gt;"}]}
    [j] = ats.parse_greenhouse(data, "Fallback")
    assert (j.source, j.company, j.location, j.remote, j.posted) == ("greenhouse", "Acme", "Remote - US", None, "2026-09-28")
    assert "Salary range" in j.description


@pytest.mark.parametrize("raw,expected", [
    ("Rubrik Job Board", "Rubrik"),
    ("Acme Careers", "Acme"),
    ("Acme jobs", "Acme"),
    ("ACME JOB BOARD", "ACME"),
    ("  Acme Job Board  ", "Acme"),
    ("Jobs Unlimited Inc", "Jobs Unlimited Inc"),   # only a trailing suffix is stripped
    ("Careers", "Fallback"),                          # nothing left -> portal name
    ("Job Board", "Fallback"),
    ("", "Fallback"),
])
def test_greenhouse_company_name_suffix_stripped(raw, expected):
    data = {"jobs": [{"absolute_url": "u", "title": "T", "company_name": raw}]}
    [j] = ats.parse_greenhouse(data, "Fallback")
    assert j.company == expected


def test_lever_parse_remote_salary_and_lists():
    data = [{"text": "Detection Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
             "categories": {"location": "United States", "commitment": "Full-time"},
             "workplaceType": "remote", "createdAt": 1790640000000,
             "descriptionPlain": "Intro", "lists": [{"text": "Requirements", "content": "<li>KQL</li>"}],
             "additionalPlain": "Benefits", "salaryRange": {"min": 180000, "max": 210000,
                                                           "currency": "USD", "interval": "per-year-salary"}}]
    [j] = ats.parse_lever(data, "Acme")
    assert (j.remote, j.employment_type, j.salary_min, j.salary_max) == (True, "Full-time", 180000, 210000)
    assert "Requirements\nKQL" in j.description and j.posted == "2026-09-29"


def test_lever_hybrid_is_not_remote():
    [j] = ats.parse_lever([{"text": "T", "hostedUrl": "u", "categories": {}, "workplaceType": "hybrid"}], "Acme")
    assert j.remote is False


def test_ashby_parse_compensation_and_locations():
    data = {"jobs": [{"title": " Security Engineer, Cloud", "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                      "location": "New York, NY (HQ)", "secondaryLocations": [{"location": "Remote (Canada)"}],
                      "isRemote": True, "workplaceType": "Hybrid", "employmentType": "FullTime",
                      "publishedAt": "2026-09-27T17:12:35.753+00:00", "isListed": True,
                      "descriptionPlain": "About", "compensation": {"compensationTierSummary": "$211.4K – $290.6K • Offers Equity"}},
                     {"title": "Hidden", "jobUrl": "x", "isListed": False}]}
    [j] = ats.parse_ashby(data, "Acme")
    assert j.title == "Security Engineer, Cloud"
    assert j.location == "New York, NY (HQ); Remote (Canada)"
    assert (j.salary_min, j.salary_max) == (211400, 290600)
    assert j.remote is False  # workplaceType Hybrid wins over isRemote


def test_greenhouse_unescapes_double_escaped_content():
    data = {"jobs": [{"absolute_url": "https://gh/1", "title": "T",
                      "content": "&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;&lt;p&gt;Two&lt;/p&gt;"}]}
    [j] = ats.parse_greenhouse(data, "Acme")
    assert j.description == "Hello & bye\nTwo"
    assert j.company == "Acme"


def test_ashby_remote_rules():
    def remote(**kw):
        [j] = ats.parse_ashby({"jobs": [{"title": "T", "jobUrl": "https://a/1", **kw}]}, "Acme")
        return j.remote
    assert remote(workplaceType="Remote") is True
    assert remote(isRemote=True) is True
    assert remote(isRemote=True, workplaceType="Hybrid") is False
    assert remote(isRemote=True, workplaceType="OnSite") is False
    assert remote(workplaceType="Hybrid") is False
    assert remote() is None


@pytest.mark.parametrize("parse", [ats.parse_greenhouse, ats.parse_lever, ats.parse_ashby])
@pytest.mark.parametrize("data", [None, "oops", 3, {}, {"jobs": None}, {"jobs": "x"}, [],
                                  {"error": "not found"}, {"status": 404, "ok": False}])
def test_ats_parsers_return_empty_on_wrong_root(parse, data):
    assert parse(data, "Acme") == []


def test_lever_error_dict_root_is_empty():
    assert ats.parse_lever({"ok": False, "error": "Document not found"}, "Acme") == []


def test_ats_parsers_skip_bad_items_and_report():
    skipped: list = []
    gh = ats.parse_greenhouse({"jobs": [None, 4, {"title": None}, {"absolute_url": "u", "title": "T"}]}, "A", skipped)
    assert len(gh) == 1 and len(skipped) == 3
    skipped = []
    lv = ats.parse_lever([None, "x", {"text": "", "hostedUrl": ""}, {"text": "T", "hostedUrl": "u"}], "A", skipped)
    assert len(lv) == 1 and len(skipped) == 3
    skipped = []
    ab = ats.parse_ashby({"jobs": [None, 1, {"title": None}, {"title": "T", "jobUrl": "u"}]}, "A", skipped)
    assert len(ab) == 1 and len(skipped) == 3


def test_ashby_unlisted_jobs_are_dropped_silently():
    skipped: list = []
    assert ats.parse_ashby({"jobs": [{"title": "T", "jobUrl": "u", "isListed": False}]}, "A", skipped) == []
    assert skipped == []


def test_greenhouse_location_as_string_and_non_dict():
    def loc(v):
        [j] = ats.parse_greenhouse({"jobs": [{"absolute_url": "u", "title": "T", "location": v}]}, "A")
        return j.location
    assert (loc("Remote - US"), loc(None), loc(["x"])) == ("Remote - US", "", "")


def test_lever_created_at_variants_and_odd_shapes():
    def lever(**kw):
        [j] = ats.parse_lever([{"text": "T", "hostedUrl": "u", **kw}], "Acme")
        return j
    assert lever(createdAt="1790640000000").posted == "2026-09-29"
    assert lever(createdAt=1790640000000).posted == "2026-09-29"
    assert lever(createdAt="soon").posted == ""
    assert lever(createdAt=None).posted == ""
    j = lever(categories="x", salaryRange="x", lists="x", workplaceType=None)
    assert (j.location, j.salary_min, j.description, j.remote) == ("", None, "", None)
    j = lever(lists=[None, {"text": "Req", "content": "<li>A</li>"}], categories={"location": None, "commitment": None})
    assert "Req\nA" in j.description and j.location == "" and j.employment_type == ""


def test_ashby_odd_shapes_ignored():
    [j] = ats.parse_ashby({"jobs": [{"title": "T", "jobUrl": "u", "location": None,
                                     "secondaryLocations": "x", "compensation": "lots"}]}, "A")
    assert j.location == "" and j.salary_min is None
    [k] = ats.parse_ashby({"jobs": [{"title": "T", "jobUrl": "u", "location": "NY",
                                     "secondaryLocations": [None, {"location": None}, {"location": "Remote"}],
                                     "compensation": None, "publishedAt": None}]}, "A")
    assert k.location == "NY; Remote" and k.posted == ""


def test_ats_fetch_reports_skipped_items_and_failures(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake(url, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dead board")
        return {"jobs": [5, {"absolute_url": "u", "title": "T"}]}

    monkeypatch.setattr(ats, "get_json", fake)
    ctx = SourceContext(cfg={}, hours_old=24, portals=[
        {"ats": "greenhouse", "slug": "a", "name": "A"}, {"ats": "greenhouse", "slug": "b", "name": "B"}])
    assert len(ats.fetch(ctx)) == 1
    assert ctx.warnings == ["ats: 1 of 2 company boards failed", "ats: 1 items skipped"]


def test_lever_list_content_is_html_and_escaped_tags_survive_as_text():
    data = [{"text": "Engineer", "hostedUrl": "https://jobs.lever.co/acme/2", "categories": {},
             "lists": [{"text": "Skills", "content": "<li>Use the &lt;b&gt; tag &amp; more</li>"}]}]
    [j] = ats.parse_lever(data, "Acme")
    assert "<b>" in j.description and "tag & more" in j.description and "<li>" not in j.description


def test_ats_fetch_stops_between_boards_after_the_deadline(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    portals = [{"ats": "greenhouse", "slug": s, "name": s.upper()} for s in ("a", "b", "c", "d")]
    ctx = SourceContext(cfg={}, hours_old=24, portals=portals, deadline=10 ** 12)
    calls = []

    def fake(url, params=None):
        calls.append(url)
        if len(calls) == 2:
            ctx.deadline = 0.0  # budget gone after the second board
        return {"jobs": [{"absolute_url": f"u{len(calls)}", "title": "T"}]}

    monkeypatch.setattr(ats, "get_json", fake)
    assert len(ats.fetch(ctx)) == 2 and len(calls) == 2
    assert ctx.warnings == ["ats: stopped early (time budget); 2 of 4 company boards skipped"]


def test_ats_all_attempted_boards_failed_before_deadline_still_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    portals = [{"ats": "greenhouse", "slug": s, "name": s} for s in ("a", "b", "c")]
    ctx = SourceContext(cfg={}, hours_old=24, portals=portals, deadline=10 ** 12)

    def fake(url, params=None):
        ctx.deadline = 0.0
        raise RuntimeError("dead")

    monkeypatch.setattr(ats, "get_json", fake)
    with pytest.raises(RuntimeError):
        ats.fetch(ctx)
