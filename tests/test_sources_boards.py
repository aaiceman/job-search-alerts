import time
from types import SimpleNamespace

import pytest

from jobsearch.sources import himalayas, jobicy, remoteok, remotive, wwr
from jobsearch.sources.base import SourceContext
from jobsearch.sources.http import html_to_text


def test_html_to_text_strips_tags_and_decodes_entities_once():
    assert html_to_text("<p>Hello &amp; bye</p><p>Two</p>") == "Hello & bye\nTwo"
    # no blanket unescape: escaped markup stays literal text
    assert html_to_text("&lt;b&gt;x&lt;/b&gt;") == "<b>x</b>"


def test_html_to_text_is_none_safe_and_capped():
    assert html_to_text(None) == "" and html_to_text(123) == ""
    assert len(html_to_text("a" * 500_000)) == 200_000


def test_remotive_parse():
    data = {"jobs": [{"url": "https://remotive.com/j/1", "title": "Security Engineer", "company_name": "Acme",
                      "job_type": "full_time", "publication_date": "2026-09-28T10:00:00",
                      "candidate_required_location": "USA", "salary": "$180k - $210k",
                      "description": "<p>Do security</p>"}]}
    [j] = remotive.parse(data)
    assert (j.source, j.company, j.location, j.remote) == ("remotive", "Acme", "USA", True)
    assert (j.salary_min, j.salary_max, j.posted) == (180000, 210000, "2026-09-28")
    assert j.description == "Do security"


def test_himalayas_parse_hourly_and_restrictions():
    data = {"jobs": [{"title": "Security Architect", "companyName": "Acme", "employmentType": "Full Time",
                      "minSalary": "100", "maxSalary": "120", "salaryPeriod": "hourly", "currency": "USD",
                      "locationRestrictions": ["United States"], "description": "<p>x</p>",
                      "pubDate": "1790693720", "applicationLink": "https://him/1", "guid": "g"}]}
    [j] = himalayas.parse(data)
    assert (j.salary_min, j.salary_max) == (208000, 249600)
    assert j.location == "United States" and j.url == "https://him/1"
    assert j.posted == "2026-09-29"


def test_himalayas_non_usd_salary_ignored():
    data = {"jobs": [{"title": "T", "companyName": "C", "minSalary": "100000", "maxSalary": "120000",
                      "salaryPeriod": "yearly", "currency": "EUR", "locationRestrictions": [],
                      "description": "", "pubDate": None, "guid": "https://him/2"}]}
    [j] = himalayas.parse(data)
    assert j.salary_min is None and j.location == "Anywhere" and j.url == "https://him/2"


def test_jobicy_parse():
    data = {"jobs": [{"url": "https://jobicy/1", "jobTitle": "Sr. Security Engineer (Detection)",
                      "companyName": "Solace", "jobType": ["Full-Time"], "jobGeo": "USA",
                      "pubDate": "2026-09-28T08:52:06+00:00", "jobDescription": "<h3>About</h3>",
                      "salaryMin": 180000, "salaryMax": 200000, "salaryCurrency": "USD", "salaryPeriod": "yearly"}]}
    [j] = jobicy.parse(data)
    assert (j.company, j.employment_type, j.location) == ("Solace", "Full-Time", "USA")
    assert (j.salary_min, j.salary_max) == (180000, 200000)


def test_remoteok_skips_legal_notice_and_zero_salary():
    data = [{"legal": "notice"},
            {"id": "1", "position": "Security Engineer", "company": "Acme", "url": "https://rok/1",
             "location": "", "salary_min": 0, "salary_max": 0, "date": "2026-09-28T03:36:41+00:00",
             "description": "<p>x</p>"}]
    [j] = remoteok.parse(data)
    assert j.salary_min is None and j.location == "Anywhere" and j.posted == "2026-09-28"


def test_wwr_parse_rss():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Discord: Senior Software Engineer, Application Security</title>
        <region>Anywhere in the World</region><type>Full-Time</type>
        <pubDate>Mon, 28 Sep 2026 14:00:00 +0000</pubDate>
        <link>https://weworkremotely.com/remote-jobs/discord-1</link>
        <description>&lt;p&gt;Hi&lt;/p&gt;</description></item>
    </channel></rss>"""
    [j] = wwr.parse(xml)
    assert (j.company, j.title) == ("Discord", "Senior Software Engineer, Application Security")
    assert (j.location, j.employment_type, j.posted) == ("Anywhere in the World", "Full-Time", "2026-09-28")


# ---- malformed-input tolerance ------------------------------------------------------------------
PARSERS = [remotive.parse, himalayas.parse, jobicy.parse, remoteok.parse]


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("data", [None, "oops", 7, {}, {"jobs": None}, {"jobs": "x"}, {"error": "rate limited"}, []])
def test_board_parsers_return_empty_on_wrong_root(parse, data):
    assert parse(data) == []


@pytest.mark.parametrize("parse", PARSERS)
def test_board_parsers_skip_bad_items_and_report_them(parse):
    good = {
        remotive.parse: {"url": "https://r/1", "title": "T", "company_name": "C"},
        himalayas.parse: {"applicationLink": "https://h/1", "title": "T", "companyName": "C"},
        jobicy.parse: {"url": "https://j/1", "jobTitle": "T", "companyName": "C"},
        remoteok.parse: {"id": "1", "url": "https://o/1", "position": "T", "company": "C"},
    }[parse]
    items = [None, 5, "str", ["x"], good, {"id": "2", "title": None, "position": None, "jobTitle": None}]
    payload = items if parse is remoteok.parse else {"jobs": items}
    skipped: list = []
    jobs = parse(payload, skipped)
    assert [j.url for j in jobs] == [good.get("url") or good.get("applicationLink")]
    assert len(skipped) == 5


def test_null_strings_never_leak_as_none():
    [j] = remotive.parse({"jobs": [{"url": "https://r/1", "title": "T", "company_name": None, "job_type": None,
                                    "candidate_required_location": None, "salary": None, "description": None,
                                    "publication_date": None}]})
    assert (j.company, j.location, j.employment_type, j.salary_text, j.posted, j.description) == ("", "", "", "", "", "")


def test_himalayas_pubdate_variants():
    def posted(v):
        [j] = himalayas.parse({"jobs": [{"title": "T", "companyName": "C", "guid": "g", "pubDate": v}]})
        return j.posted
    assert posted(1790693720) == "2026-09-29"           # seconds
    assert posted(1790693720000) == "2026-09-29"        # milliseconds
    assert posted("1790693720000") == "2026-09-29"
    assert posted("not a date") == "" and posted(-5e30) == "" and posted([1]) == ""


def test_bad_dates_become_empty_posted():
    [j] = remotive.parse({"jobs": [{"url": "u", "title": "T", "company_name": "C", "publication_date": "yesterday"}]})
    assert j.posted == ""
    [k] = wwr.parse("<rss><channel><item><title>A: B</title><link>https://w/1</link>"
                    "<pubDate>garbage</pubDate></item></channel></rss>")
    assert k.posted == ""


def test_jobicy_geo_list_is_joined():
    [j] = jobicy.parse({"jobs": [{"url": "u", "jobTitle": "T", "companyName": "C", "jobGeo": ["USA", "Canada"]}]})
    assert j.location == "USA, Canada"


def test_wwr_skips_items_without_title_or_link():
    skipped: list = []
    jobs = wwr.parse("<rss><channel><item><description>x</description></item>"
                     "<item><title>A: B</title><link>https://w/1</link></item></channel></rss>", skipped)
    assert len(jobs) == 1 and len(skipped) == 1


# ---- fetch(): per-query isolation, warnings, pacing ---------------------------------------------
EMPTY_RSS = "<rss><channel></channel></rss>"
BAD_RSS = "<rss><channel><item></item></channel></rss>"
FETCH_CASES = [
    (remotive, "remotive_categories", "get_json", {"jobs": []}, {"jobs": [5]}),
    (himalayas, "himalayas_queries", "get_json", {"jobs": []}, {"jobs": [5]}),
    (jobicy, "jobicy_tags", "get_json", {"jobs": []}, {"jobs": [5]}),
    (remoteok, "remoteok_tags", "get_json", [], [5]),
    (wwr, "wwr_feeds", "get", SimpleNamespace(text=EMPTY_RSS), SimpleNamespace(text=BAD_RSS)),
]


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    return calls


@pytest.mark.parametrize("mod,key,getter,ok,bad", FETCH_CASES)
def test_fetch_survives_one_failed_query(monkeypatch, sleeps, mod, key, getter, ok, bad):
    n = {"i": 0}

    def fake(*a, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("boom")
        return ok

    monkeypatch.setattr(mod, getter, fake)
    ctx = SourceContext(cfg={key: ["a", "b", "c"]}, hours_old=24)
    assert mod.fetch(ctx) == []
    assert ctx.warnings == [f"{mod.NAME}: 1 of 3 queries failed"]
    assert len(sleeps) == 2 and all(s == 1 for s in sleeps)  # between queries, not after the last


@pytest.mark.parametrize("mod,key,getter,ok,bad", FETCH_CASES)
def test_fetch_raises_only_when_every_query_fails(monkeypatch, sleeps, mod, key, getter, ok, bad):
    def fake(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(mod, getter, fake)
    ctx = SourceContext(cfg={key: ["a", "b"]}, hours_old=24)
    with pytest.raises(RuntimeError):
        mod.fetch(ctx)
    assert ctx.warnings == [f"{mod.NAME}: 2 of 2 queries failed"]


@pytest.mark.parametrize("mod,key,getter,ok,bad", FETCH_CASES)
def test_fetch_reports_skipped_items(monkeypatch, sleeps, mod, key, getter, ok, bad):
    monkeypatch.setattr(mod, getter, lambda *a, **kw: bad)
    ctx = SourceContext(cfg={key: ["a"]}, hours_old=24)
    assert mod.fetch(ctx) == []
    assert ctx.warnings == [f"{mod.NAME}: 1 items skipped"]
    assert sleeps == []


@pytest.mark.parametrize("mod,key,getter,ok,bad", FETCH_CASES)
def test_fetch_with_no_queries_is_empty(mod, key, getter, ok, bad):
    ctx = SourceContext(cfg={}, hours_old=24)
    assert mod.fetch(ctx) == []
    assert ctx.warnings == []


def _him_job(n):
    return {"title": f"T{n}", "companyName": "C", "guid": f"https://h/{n}"}


def test_himalayas_follows_next_cursor_up_to_three_pages(monkeypatch, sleeps):
    calls = []

    def fake(url, params=None):
        calls.append(dict(params))
        return {"jobs": [_him_job(len(calls))], "nextCursor": f"c{len(calls)}"}

    monkeypatch.setattr(himalayas, "get_json", fake)
    ctx = SourceContext(cfg={"himalayas_queries": ["security"]}, hours_old=24)
    jobs = himalayas.fetch(ctx)
    assert len(jobs) == 3 and len(calls) == 3
    assert "cursor" not in calls[0] and calls[1]["cursor"] == "c1" and calls[2]["cursor"] == "c2"
    assert calls[0]["q"] == "security" and calls[0]["country"] == "US"


def test_himalayas_stops_when_no_cursor_or_empty_page(monkeypatch, sleeps):
    pages = iter([{"jobs": [_him_job(1)], "nextCursor": "c1"}, {"jobs": [], "nextCursor": "c2"}])
    calls = []

    def fake(url, params=None):
        calls.append(params)
        return next(pages)

    monkeypatch.setattr(himalayas, "get_json", fake)
    ctx = SourceContext(cfg={"himalayas_queries": ["q"]}, hours_old=24)
    assert len(himalayas.fetch(ctx)) == 1 and len(calls) == 2

    monkeypatch.setattr(himalayas, "get_json", lambda url, params=None: {"jobs": [_him_job(1)]})
    assert len(himalayas.fetch(SourceContext(cfg={"himalayas_queries": ["q"]}, hours_old=24))) == 1


def test_himalayas_later_page_failure_keeps_earlier_pages(monkeypatch, sleeps):
    calls = []

    def fake(url, params=None):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("cursor expired")
        return {"jobs": [_him_job(1)], "nextCursor": "c1"}

    monkeypatch.setattr(himalayas, "get_json", fake)
    ctx = SourceContext(cfg={"himalayas_queries": ["q"]}, hours_old=24)
    assert len(himalayas.fetch(ctx)) == 1
    assert ctx.warnings == ["himalayas: pagination stopped early for 'q'"]


def test_text_decodes_entities_but_leaves_bare_ampersands():
    from jobsearch.sources.base import text

    assert text("hims &#038; hers") == "hims & hers"
    assert text("R&amp;D &lt;Lead&gt;") == "R&D <Lead>"
    assert text("https://x/y?a=1&copy=2") == "https://x/y?a=1&copy=2"
    assert text(["A &amp; B", "C"]) == "A & B, C"


def test_jobicy_company_entities_decoded():
    from jobsearch.sources import jobicy

    jobs = jobicy.parse({"jobs": [{"url": "https://x/1", "jobTitle": "Security Engineer",
                                   "companyName": "hims &#038; hers", "jobGeo": "USA"}]})
    assert jobs[0].company == "hims & hers"


def test_wwr_entities_decoded():
    from jobsearch.sources import wwr

    xml = ("<rss><channel><item><title>R&amp;amp;D Co: Security &amp;amp; IAM Lead</title>"
           "<link>https://x/1</link></item></channel></rss>")
    j = wwr.parse(xml)[0]
    assert (j.company, j.title) == ("R&D Co", "Security & IAM Lead")


def test_text_strips_control_and_bidi_characters():
    from jobsearch.sources.base import text

    assert text("Sec‮urity\x00 Eng\x07ineer&#x202E;") == "Security Engineer"
    assert text("a\tb\nc‏d⁦e") == "a\tb\ncde"


@pytest.mark.parametrize("mod,key,getter,ok,bad", FETCH_CASES)
def test_fetch_stops_issuing_queries_after_the_deadline(monkeypatch, sleeps, mod, key, getter, ok, bad):
    ctx = SourceContext(cfg={key: ["a", "b", "c"]}, hours_old=24, deadline=10 ** 12)
    n = {"i": 0}

    def fake(*a, **kw):
        n["i"] += 1
        ctx.deadline = 0.0  # budget gone after the first query
        return ok

    monkeypatch.setattr(mod, getter, fake)
    assert mod.fetch(ctx) == []
    assert n["i"] == 1
    assert ctx.warnings == [f"{mod.NAME}: stopped early (time budget); 2 of 3 queries skipped"]


def test_run_queries_deadline_already_passed_is_not_all_failed():
    from jobsearch.sources.base import run_queries
    ctx = SourceContext(cfg={}, hours_old=24, deadline=0.0)
    assert run_queries(ctx, "x", ["a", "b"], lambda q, s: 1 / 0) == []
    assert ctx.warnings == ["x: stopped early (time budget); 2 of 2 queries skipped"]


def test_past_deadline():
    from jobsearch.sources.base import past_deadline
    assert not past_deadline(SourceContext(cfg={}, hours_old=24))
    assert not past_deadline(SourceContext(cfg={}, hours_old=24, deadline=time.monotonic() + 60))
    assert past_deadline(SourceContext(cfg={}, hours_old=24, deadline=time.monotonic() - 1))
