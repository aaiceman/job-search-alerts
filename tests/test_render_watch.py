import html as _h
import re
from datetime import date

from jobsearch.models import Job
from jobsearch.rank import RankedJob, RankedOutput
from jobsearch.render import DigestContext, render

TODAY = date(2026, 10, 2)
STATS = {"collected": 1, "unique": 1, "dropped": {}, "excluded": {}, "already_sent": 0, "over_cap": 0,
         "candidates": 0, "window_hours": 24, "warnings": [], "sources": [{"name": "a", "ok": True, "count": 1}]}


def row(**kw):
    base = {"company": "Example Corp", "title": "Principal Security Architect", "url": "https://examplecorp.example/jobs/1",
            "posted": date(2026, 9, 22), "applied": date(2026, 9, 30), "first_checked": "2026-10-01",
            "last_seen_open": "2026-10-02", "closed_on": None, "last_status": "open",
            "last_updated_at": "2026-09-29T08:30:00-04:00"}
    base.update(kw)
    return base


def ctx(*rows, **kw):
    return DigestContext(run_date=TODAY, candidates=kw.pop("candidates", []), stats=STATS, watch=list(rows), **kw)


def test_open_line_html_and_text():
    _, html, text = render(ctx(row()))
    want = ("Example Corp — Principal Security Architect: still open · posted Sep 22 (day 10) · "
            "applied Sep 30 (day 2) · listing last updated Sep 29")
    plain = _h.unescape(re.sub(r"<[^>]+>", "", html))
    assert "Your applications" in html and want in plain
    assert "Your applications" in text and want in text
    assert '<a href="https://examplecorp.example/jobs/1"' in html


def test_closed_line():
    r = row(closed_on="2026-10-03", last_status="closed")
    _, _, text = render(ctx(r))
    assert ("Example Corp — Principal Security Architect: POSTING CLOSED on Oct 3 "
            "(posted Sep 22 → closed after 11 days; applied 3 days before close)") in text


def test_closed_stays_closed_when_today_is_unknown():
    _, _, text = render(ctx(row(closed_on="2026-10-01", last_status="unknown")))
    assert "POSTING CLOSED on Oct 1" in text


def test_unknown_line():
    _, html, text = render(ctx(row(last_status="unknown", last_seen_open=None, last_updated_at=None)))
    assert "Example Corp — Principal Security Architect: status unknown today (check failed)" in text
    assert "status unknown today (check failed)" in html


def test_open_line_without_optional_dates():
    _, _, text = render(ctx(row(posted=None, applied=None, last_updated_at=None)))
    assert "Example Corp — Principal Security Architect: still open" in text
    assert "posted" not in text and "applied" not in text and "last updated" not in text


def test_closed_line_without_dates_and_odd_gaps():
    _, _, text = render(ctx(row(closed_on="2026-10-02", last_status="closed", posted=None, applied=None)))
    assert "POSTING CLOSED on Oct 2" in text and "(" not in text.split("POSTING CLOSED on Oct 2", 1)[1].split("\n")[0]
    _, _, text = render(ctx(row(closed_on="2026-10-02", last_status="closed", applied=date(2026, 10, 2))))
    assert "applied the day it closed" in text
    _, _, text = render(ctx(row(closed_on="2026-10-02", last_status="closed", applied=date(2026, 10, 5))))
    assert "applied after it closed" in text
    _, _, text = render(ctx(row(closed_on="2026-09-23", last_status="closed", applied=date(2026, 9, 22),
                                posted=date(2026, 9, 22))))
    assert "closed after 1 day;" in text


def test_iso_strings_are_accepted_for_dates():
    _, _, text = render(ctx(row(posted="2026-09-22", applied="2026-09-30")))
    assert "posted Sep 22 (day 10)" in text and "applied Sep 30 (day 2)" in text


def test_empty_watch_omits_block():
    _, html, text = render(ctx())
    assert "Your applications" not in html and "Your applications" not in text


def test_block_comes_before_top_picks_and_with_zero_candidates():
    job = Job(source="s", url="https://a/1", title="Detection Engineer", company="Acme")
    ranked = RankedOutput(jobs=[RankedJob(id=job.id, score=91, fit="f", gaps=[], pay="p", pay_estimated=False)])
    _, html, text = render(ctx(row(), candidates=[job], ranked=ranked))
    assert html.index("Your applications") < html.index("Top picks")
    assert text.index("Your applications") < text.index("91  Detection Engineer")
    _, html, text = render(ctx(row()))
    assert "Your applications" in html and "No new matches today." in html


def test_watch_block_shown_on_degraded_day():
    stats = {**STATS, "sources": [{"name": "a", "ok": False, "count": 0, "error": "x"}]}
    subj, html, _ = render(DigestContext(run_date=TODAY, candidates=[], stats=stats, watch=[row()]))
    assert "ALL SOURCES FAILED" in subj and "Your applications" in html


def test_escaping_and_safe_links():
    r = row(company="<b>Evil</b> & Co", title="<script>alert(1)</script>", url="javascript:alert(1)")
    _, html, text = render(ctx(r))
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;Evil" in html
    assert "javascript:" not in html
    assert "<script>alert(1)</script>" in text  # plain text is not HTML, but carries no link


def test_multiple_rows_one_line_each():
    _, html, text = render(ctx(row(), row(company="Other", title="Role", closed_on="2026-10-01",
                                          last_status="closed")))
    assert text.count("still open") == 1 and text.count("POSTING CLOSED") == 1
