import html as _h
from datetime import date

import pytest

from jobsearch.models import Job
from jobsearch.rank import CompanyResearch, DegreeInfo, RankedJob, RankedOutput
from jobsearch.render import DigestContext, render

D = date(2026, 9, 30)
STATS = {"collected": 120, "unique": 100, "dropped": {"title": 50}, "excluded": {"Example Corp": 1},
         "already_sent": 3, "over_cap": 0, "candidates": 3, "window_hours": 24, "warnings": ["jobspy: 1 of 22 failed"],
         "sources": [{"name": "remotive", "ok": True, "count": 40, "error": ""},
                     {"name": "jobspy", "ok": False, "count": 0, "error": "RuntimeError: 429"}]}


def J(title, company="Acme", url="https://acme/1"):
    return Job(source="s", url=url, title=title, company=company)


def ranked(jobs, scores):
    return RankedOutput(jobs=[RankedJob(id=j.id, score=s, fit="Strong Sentinel match", gaps=["Splunk"],
                                        pay="$180k–$210k stated · +32% vs current base", pay_estimated=False)
                              for j, s in zip(jobs, scores)])


def test_subject_and_sections():
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta"), J("SOC Analyst", "Gamma")]
    ctx = DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91, 70, 40]))
    subject, html, text = render(ctx)
    assert subject == "Jobs Wed Sep 30 — 2 new, top: Detection Engineer @ Acme (91)"
    assert "Top picks" in html and "Worth a look" in html
    assert "SOC Analyst" not in html  # below 60 omitted
    assert "jobspy" in html and "429" in html  # source health in footer
    assert "Detection Engineer" in text


def test_benefits_block_uses_marks():
    job = J("Detection Engineer")
    research = {"Acme": CompanyResearch.model_validate({
        "company": "Acme", "researched_on": "2026-09-30",
        "benefits": [{"item": "Medical premium", "mark": "worse", "detail": "~$150/mo", "label": "Likely"},
                     {"item": "PTO", "mark": "better", "detail": "unlimited", "label": "Confirmed",
                      "source": "https://acme/benefits"}],
        "reviews": {"summary": "Solid", "glassdoor_rating": 4.1, "red_flags": ["Layoffs Mar 2026"]}})}
    ctx = DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=ranked([job], [88]), research=research)
    _, html, _ = render(ctx)
    assert "⚠️" in html and "✅" in html and "4.1" in html and "Layoffs Mar 2026" in html


def test_escapes_untrusted_text_and_blocks_bad_urls():
    job = J("<script>alert(1)</script>", url="javascript:alert(1)")
    ctx = DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=ranked([job], [80]))
    _, html, _ = render(ctx)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html


def test_no_matches_email():
    subject, html, _ = render(DigestContext(run_date=D, candidates=[], stats=STATS))
    assert subject == "Jobs Wed Sep 30 — no new matches" and "No new matches" in html


def test_ranking_failed_lists_unranked():
    jobs = [J("Detection Engineer")]
    subject, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranking_error="timeout"))
    assert subject == "Jobs Wed Sep 30 — 1 unranked (ranking failed)"
    assert "Ranking failed" in html and "Detection Engineer" in html


def test_late_prefix():
    subject, _, _ = render(DigestContext(run_date=D, candidates=[], stats=STATS, late=True))
    assert subject.startswith("[late] ")


def test_benefit_and_review_source_urls_are_safe():
    job = J("Detection Engineer")
    research = {"Acme": CompanyResearch.model_validate({
        "company": "Acme", "researched_on": "2026-09-30",
        "benefits": [{"item": "PTO", "mark": "better", "detail": "unlimited", "label": "Confirmed",
                      "source": "javascript:alert(1)"},
                     {"item": "401k", "mark": "same", "detail": "4%", "label": "Confirmed",
                      "source": "https://acme/401k"}],
        "reviews": {"summary": "ok", "sources": ["javascript:alert(2)", "https://glassdoor/acme"]}})}
    ctx = DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=ranked([job], [88]), research=research)
    _, html, text = render(ctx)
    assert "javascript:" not in html and "javascript:" not in text
    assert 'href="https://acme/401k"' in html and 'href="https://glassdoor/acme"' in html


def test_escapes_every_job_derived_field():
    x = "<img src=x onerror=alert(1)>"
    job = Job(source="s", url="https://acme/1", title="T", company=x, location=x, flags=[x])
    r = RankedOutput(jobs=[RankedJob(id=job.id, score=90, fit=x, gaps=[x], pay=x, pay_estimated=False)])
    research = {x: CompanyResearch.model_validate({
        "company": x, "researched_on": "2026-09-30",
        "benefits": [{"item": x, "mark": "worse", "detail": x, "label": "Likely"}],
        "reviews": {"summary": x, "pros": [x], "cons": [x], "red_flags": [x], "glassdoor_trend": x}})}
    stats = {**STATS, "warnings": [x], "sources": [{"name": x, "ok": False, "count": 0, "error": x}],
             "dropped": {x: 1}, "excluded": {x: 1}}
    _, html, text = render(DigestContext(run_date=D, candidates=[job], stats=stats, ranked=r, research=research))
    assert "<img" not in html
    esc = "&lt;img src=x onerror=alert(1)&gt;"
    assert html.count(esc) >= 10
    for spot in (f"{esc}</div>",  # fit / pay / location / flags / gaps / etc. all land escaped
                 f"Flags: {esc}", f"Gaps: {esc}", f"@ {esc}", f"<b>Pay:</b> {esc}", f"Warnings: {esc}"):
        assert spot in html, spot


def test_dealbreaker_jobs_removed_and_counted():
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta"), J("Cloud Engineer", "Gamma")]
    r = RankedOutput(jobs=[
        RankedJob(id=jobs[0].id, score=91, fit="f", gaps=["DEALBREAKER: onsite"], pay="p", pay_estimated=False),
        RankedJob(id=jobs[1].id, score=70, fit="f", gaps=["dealbreaker - contract"], pay="p", pay_estimated=False),
        RankedJob(id=jobs[2].id, score=80, fit="f", gaps=["Splunk"], pay="p", pay_estimated=False)])
    subject, html, text = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=r))
    assert "Top picks" in html and html.count("Detection Engineer") == 1  # only in the footer block, no card
    assert "Cloud Engineer" in html and "dealbreakers 2" in html
    assert subject == "Jobs Wed Sep 30 — 1 new, top: Cloud Engineer @ Gamma (80)"
    assert "Detection Engineer" not in text


def test_subject_is_single_line():
    job = J("Line1\r\nBcc: evil@x.com")
    subject, _, _ = render(DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=ranked([job], [80])))
    assert "\n" not in subject and "\r" not in subject


def test_research_lookup_is_normalized_and_ranked_deduped():
    job = J("Detection Engineer", company="Acme, Inc.")
    cr = CompanyResearch.model_validate({
        "company": "ACME", "researched_on": "2026-09-30", "benefits": [], "reviews": {"summary": "Great place"}})
    r1 = RankedJob(id=job.id, score=90, fit="first", pay="p", pay_estimated=False)
    r2 = RankedJob(id=job.id, score=95, fit="second", pay="p", pay_estimated=False)
    ctx = DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=RankedOutput(jobs=[r1, r2]),
                        research={"ACME": cr})
    subject, html, _ = render(ctx)
    assert "Great place" in html  # "ACME" matched "Acme, Inc." by normalized name
    assert html.count("Detection Engineer") == 2 and "second" not in html  # first duplicate wins (h1 + card)
    assert "ranked 1" in html and "(90)" in subject


def test_footer_ranked_count_is_jobs_returned_not_candidates():
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta")]
    ctx = DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs[:1], [80]))
    _, html, _ = render(ctx)
    assert "ranked 1" in html and "candidates 3" in html


def test_footer_tolerates_missing_keys():
    stats = {"sources": [{}, {"name": "x"}, {"ok": True}], "dropped": {}, "excluded": {}}
    _, html, _ = render(DigestContext(run_date=D, candidates=[], stats=stats))
    assert "No new matches" in html
    _, html, _ = render(DigestContext(run_date=D, candidates=[], stats={}))
    assert "No new matches" in html


def test_skip_rank_label():
    jobs = [J("Detection Engineer")]
    subject, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranking_skipped=True,
                                            ranking_error="ranking skipped"))
    assert "Ranking skipped (--skip-rank)" in html and "Ranking failed" not in html
    assert "ranking failed" not in subject


def test_failed_sources_banner():
    stats = {**STATS, "sources": [{"name": "remotive", "ok": True, "count": 4, "error": ""},
                                  {"name": "jobspy <b>", "ok": False, "count": 0, "error": "429"}]}
    jobs = [J("Detection Engineer")]
    subject, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=stats, ranked=ranked(jobs, [80])))
    assert "⚠ 1 of 2 sources failed today: jobspy &lt;b&gt;" in html
    assert "ALL SOURCES FAILED" not in subject


def test_all_sources_failed_subject():
    stats = {**STATS, "sources": [{"name": "a", "ok": False, "count": 0, "error": "x"},
                                  {"name": "b", "ok": False, "count": 0, "error": "y"}]}
    subject, html, _ = render(DigestContext(run_date=D, candidates=[], stats=stats))
    assert subject == "Jobs Wed Sep 30 — ALL SOURCES FAILED"
    assert "2 of 2 sources failed today: a, b" in html
    late, _, _ = render(DigestContext(run_date=D, candidates=[], stats=stats, late=True))
    assert late == "[late] Jobs Wed Sep 30 — ALL SOURCES FAILED"


def test_late_banner_text():
    _, html, _ = render(DigestContext(run_date=D, candidates=[], stats=STATS, late=True))
    assert "This digest started late (the PC was off or asleep at the scheduled 05:45 run)." in html


# ---- omitted / unranked jobs, dealbreaker visibility, research note ----------------------------

def test_ranker_omitted_jobs_get_unranked_section():
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta", url="https://beta/2"),
            J("<b>Omitted</b>", "Gamma", url="javascript:alert(1)")]
    ctx = DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs[:1], [91]))
    subject, html, text = render(ctx)
    assert "Unranked (ranker omitted or invalid)" in html
    assert "Security Architect" in html and "https://beta/2" in html
    assert "&lt;b&gt;Omitted&lt;/b&gt;" in html and "<b>Omitted</b>" not in html and "javascript:" not in html
    assert "unranked 2" in html and "ranked 1" in html
    assert "Security Architect" in text and "javascript:" not in text
    assert subject == "Jobs Wed Sep 30 — 1 new, top: Detection Engineer @ Acme (91)"


def test_no_unranked_section_when_ranker_covers_everything():
    jobs = [J("Detection Engineer")]
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91])))
    assert "Unranked (ranker omitted" not in html


def test_unranked_shown_even_when_nothing_ranked_scores_high_enough():
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta")]
    subject, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs[:1], [10])))
    assert "Security Architect" in html and "No new matches today" not in html
    assert subject == "Jobs Wed Sep 30 — 1 unranked (ranker omitted)"


def test_dealbreaker_block_lists_title_company_first_gap_escaped_and_truncated():
    jobs = [J("Detection Engineer"), J("<i>Architect</i>", "Beta"), J("Cloud Engineer", "Gamma")]
    long_gap = "DEALBREAKER: onsite " + "x" * 300
    r = RankedOutput(jobs=[
        RankedJob(id=jobs[0].id, score=0, fit="f", gaps=[long_gap, "second gap"], pay="p", pay_estimated=False),
        RankedJob(id=jobs[1].id, score=0, fit="f", gaps=["DEALBREAKER: <b>clearance</b>"], pay="p",
                  pay_estimated=False),
        RankedJob(id=jobs[2].id, score=80, fit="f", gaps=["Splunk"], pay="p", pay_estimated=False)])
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=r))
    assert "Dealbreakers (auto-excluded):" in html
    assert f"Detection Engineer @ Acme — {long_gap[:160]}" in html and long_gap[:161] not in html
    assert "second gap" not in html
    assert "&lt;i&gt;Architect&lt;/i&gt; @ Beta — DEALBREAKER: &lt;b&gt;clearance&lt;/b&gt;" in html
    assert "Cloud Engineer @ Gamma" in html and "Splunk" in html  # ordinary card still there
    _, html2, _ = render(DigestContext(run_date=D, candidates=jobs[2:], stats=STATS, ranked=ranked(jobs[2:], [80])))
    assert "Dealbreakers (auto-excluded)" not in html2


def _cr(name):
    return CompanyResearch.model_validate({"company": name, "researched_on": "2026-09-30", "benefits": [],
                                           "reviews": {"summary": "s"}})


NOTE = "Company research incomplete today (ranker stopped early)"


def test_incomplete_research_note_when_no_companies_and_no_cache():
    jobs = [J("Detection Engineer")]
    _, html, text = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91])))
    assert NOTE in html and html.index("Top picks") < html.index(NOTE) < html.index("<b>Detection Engineer</b>")
    assert NOTE in text


def test_no_incomplete_note_when_cache_covers_or_ranker_returned_companies():
    jobs = [J("Detection Engineer")]
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91]),
                                      research={"Acme": _cr("Acme")}))
    assert NOTE not in html
    r = ranked(jobs, [91])
    r.companies = {"Acme": _cr("Acme")}
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=r))
    assert NOTE not in html
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [30])))
    assert NOTE not in html  # nothing top/worth: nothing to research


def test_no_incomplete_note_for_unresearched_worth_a_look_rows():
    # Pass B only researches >= 75 jobs, so a cached Top pick plus unresearched Worth-a-look rows is complete
    # (regression: 2026-10-15 digest showed the note although its only Top pick was cached).
    jobs = [J("Detection Engineer"), J("Security Architect", "Beta"), J("IT Director", "Gamma")]
    ctx = DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91, 70, 65]),
                        research={"Acme": _cr("Acme")})
    _, html, text = render(ctx)
    assert NOTE not in html and NOTE not in text
    assert "Worth a look" in html
    # ...but a Top pick with no research at all still gets the note
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked(jobs, [91, 70, 65])))
    assert NOTE in html


# ---- degree line on every job ----

def _deg(level="bachelor", requirement="required", eq="yes", quote="Bachelor's degree or equivalent experience"):
    return DegreeInfo(level=level, requirement=requirement, equivalent_experience=eq, quote=quote)


def _render_one(score, degree=None, flags=None):
    job = J("Detection Engineer")
    job.flags = list(flags or [])
    r = RankedOutput(jobs=[RankedJob(id=job.id, score=score, fit="fit", gaps=[], pay="$200k stated",
                                     pay_estimated=False, degree=degree)])
    return render(DigestContext(run_date=D, candidates=[job], stats=STATS, ranked=r))



@pytest.mark.parametrize("deg,line", [
    (_deg(), "Degree: Bachelor's required — equivalent experience accepted"),
    (_deg(eq="no"), "Degree: Bachelor's required — no equivalency stated ⚠"),
    (_deg(eq="unknown"), "Degree: Bachelor's required — equivalency unclear"),
    (_deg(level="master", requirement="preferred"), "Degree: Master's preferred"),
    (_deg(level="phd", requirement="preferred"), "Degree: PhD preferred"),
    (_deg(level="associate", requirement="required"), "Degree: Associate's required — equivalent experience accepted"),
    (_deg(level="none", requirement="not_mentioned", quote=""), "Degree: not mentioned"),
    (_deg(level="none", requirement="required", quote="No degree required"), "Degree: not mentioned"),
    (_deg(level="bachelor", requirement="not_mentioned", quote=""), "Degree: not mentioned"),
])
@pytest.mark.parametrize("score", [88, 65], ids=["card", "worth-row"])
def test_degree_line_variants_in_card_row_and_text(deg, line, score):
    _, html, text = _render_one(score, deg)
    assert _html_escape(line) in html
    assert line in text


def _html_escape(s):
    return _h.escape(s, quote=True)


def test_card_shows_quote_muted_and_truncated():
    _, html, text = _render_one(88, _deg(quote="Q" * 190))
    assert "Q" * 120 not in html and "Q" * 100 in html
    _, html, _ = _render_one(88, _deg(quote="BS in CS or equivalent"))
    assert "color:#6b7280" in html.split("BS in CS or equivalent")[0].rsplit("<div", 1)[1]


def test_degree_none_falls_back_to_flag_snippet():
    _, html, text = _render_one(88, None, flags=["salary not listed", 'degree: "BS in CS or equivalent"'])
    assert _html_escape('Degree: see posting — "BS in CS or equivalent"') in html
    assert 'Degree: see posting — "BS in CS or equivalent"' in text


def test_degree_none_without_flags_omits_line():
    _, html, text = _render_one(88, None, flags=["degree: advanced degree mentioned"])
    assert "Degree:" not in html and "Degree:" not in text


def test_unranked_rows_show_degree_flag_line():
    ranked_job, omitted = J("Detection Engineer"), J("Cloud Architect", "Beta", url="https://beta/1")
    omitted.flags = ['degree: "Master\'s degree preferred"']
    r = RankedOutput(jobs=[RankedJob(id=ranked_job.id, score=80, fit="f", gaps=[], pay="p", pay_estimated=False)])
    _, html, text = render(DigestContext(run_date=D, candidates=[ranked_job, omitted], stats=STATS, ranked=r))
    unranked_html = html.split("Unranked (ranker omitted or invalid)")[1]
    assert "Degree: see posting" in unranked_html
    assert 'Degree: see posting — "Master\'s degree preferred"' in text.split("Unranked")[1]


def test_ranking_failed_rows_show_degree_flag_line():
    job = J("Detection Engineer")
    job.flags = ['degree: "BS required"']
    _, html, text = render(DigestContext(run_date=D, candidates=[job], stats=STATS, ranking_error="boom"))
    assert "Degree: see posting" in html and "Degree: see posting" in text


def test_degree_text_is_escaped():
    _, html, _ = _render_one(88, _deg(quote="<b>BS</b> & <script>x</script>"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    _, html, _ = _render_one(65, None, flags=['degree: "<img src=x onerror=alert(1)>"'])
    assert "<img" not in html and "&lt;img" in html


# ---- pay-target gate: stated salary first, else the ranker's pay_min/pay_max; unknown pay is kept ----------
# Made-up owner numbers: base 151234 * 1.25 = 189042.5 (the base itself must never be rendered).
TARGET = 189042.5
RULE = "max(floor, base+25%)"


def SJ(title, company="Acme", lo=None, hi=None, text=""):
    return Job(source="s", url=f"https://x/{title.replace(' ', '')}", title=title, company=company,
               salary_min=lo, salary_max=hi, salary_text=text)


def RJ(job, score=80, pay="p", lo=None, hi=None, gaps=None, est=True):
    return RankedJob(id=job.id, score=score, fit="fit", gaps=gaps or [], pay=pay, pay_estimated=est,
                     pay_min=lo, pay_max=hi)


def _gate(jobs, rjobs, target=TARGET, **kw):
    ranked_out = RankedOutput(jobs=rjobs) if rjobs is not None else None
    ctx = DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=ranked_out, pay_target=target,
                        pay_rule=RULE, **kw)
    return render(ctx)


def test_michigan_medicine_estimate_below_target_is_excluded():
    mm = SJ("Systems Architect - Senior", "Michigan Medicine")
    ok = SJ("Security Architect", "Beta")
    subject, html, text = _gate([mm, ok], [
        RJ(mm, 82, "~$110k–$140k est. · -27% to -7% vs current base", 110000, 140000),
        RJ(ok, 78, "~$180k–$220k est. · +19% to +45% vs current base", 180000, 220000)])
    assert subject == "Jobs Wed Sep 30 — 1 new, top: Security Architect @ Beta (78)"
    assert "Below pay target ($189k = max(floor, base+25%)):" in html
    assert ("Systems Architect - Senior @ Michigan Medicine — ~$110k–$140k est. · -27% to -7% vs current base"
            in _h.unescape(html))
    assert html.count("Systems Architect - Senior") == 1  # footer only: no card, no row
    assert "Top picks" in html and "Security Architect" in html
    assert "below pay target 1" in html
    body = text.split("Below pay target", 1)[0]
    assert "Michigan Medicine" not in body and "Security Architect @ Beta" in body
    assert "Systems Architect - Senior @ Michigan Medicine" in text.split("Below pay target", 1)[1]


def test_estimated_180_220_is_kept():
    j = SJ("Security Architect")
    subject, html, _ = _gate([j], [RJ(j, 70, "~$180k–$220k est.", 180000, 220000)])  # midpoint 200k
    assert "Worth a look" in html and "Below pay target" not in html and "1 new" in subject


def test_stated_salary_wins_over_ranker_estimate():
    low_stated = SJ("Cloud Architect", lo=150000, hi=190000)        # midpoint 170k < target
    high_stated = SJ("Security Architect", "Beta", lo=190000, hi=230000)  # midpoint 210k
    _, html, _ = _gate([low_stated, high_stated], [
        RJ(low_stated, 80, "$150k–$190k stated", 250000, 300000, est=False),
        RJ(high_stated, 80, "$190k–$230k stated", 100000, 120000, est=False)])
    below = html.split("Below pay target", 1)[1]
    assert "Cloud Architect" in below and "Security Architect" not in below
    assert "Security Architect" in html.split("Below pay target", 1)[0]


@pytest.mark.parametrize("lo,hi,excluded", [
    (None, 180000, True), (180000, None, True), (None, 200000, False), (200000, None, False),
    (170000, 208085, False),  # midpoint 189042.5 == target -> kept
])
def test_one_sided_stated_salary_uses_that_value(lo, hi, excluded):
    j = SJ("Cloud Architect", lo=lo, hi=hi)
    _, html, _ = _gate([j], [RJ(j, 80, "stated")])
    assert ("Below pay target" in html) is excluded


def test_one_sided_ranker_estimate_uses_that_value():
    a, b = SJ("Cloud Architect"), SJ("Security Architect", "Beta")
    _, html, _ = _gate([a, b], [RJ(a, 80, "est", None, 150000), RJ(b, 80, "est", 200000, None)])
    below = html.split("Below pay target", 1)[1]
    assert "Cloud Architect" in below and "Security Architect" not in below


def test_unknown_pay_is_kept():
    j = SJ("Security Architect")
    subject, html, _ = _gate([j], [RJ(j, 80, "not stated")])
    assert "Top picks" in html and "Below pay target" not in html and "1 new" in subject


def test_no_target_means_no_gate():
    j = SJ("Security Architect", lo=100000, hi=120000)
    subject, html, _ = render(DigestContext(run_date=D, candidates=[j], stats=STATS,
                                            ranked=RankedOutput(jobs=[RJ(j, 80, "$100k–$120k", 100000, 120000)])))
    assert "Top picks" in html and "Below pay target" not in html and "1 new" in subject


def test_below_target_jobs_that_would_not_show_anyway_are_not_listed():
    low_score, dealbreaker = SJ("Cloud Architect"), SJ("Security Architect", "Beta")
    _, html, _ = _gate([low_score, dealbreaker], [
        RJ(low_score, 40, "~$100k est.", 100000, 110000),
        RJ(dealbreaker, 0, "~$100k est.", 100000, 110000, gaps=["DEALBREAKER: onsite"])])
    assert "Below pay target" not in html
    assert "Dealbreakers (auto-excluded):" in html and "Security Architect @ Beta" in html


def test_all_shown_jobs_below_target_subject_says_no_new_matches():
    j = SJ("Cloud Architect")
    subject, html, text = _gate([j], [RJ(j, 90, "~$120k est.", 110000, 130000)])
    assert subject == "Jobs Wed Sep 30 — no new matches"
    assert "Top picks" not in html and "Below pay target" in html and "No new matches today" in html


def test_below_target_block_is_escaped_and_truncated():
    j = SJ("<b>Architect</b>", "Evil & Co")
    long_pay = "<script>x</script> ~$100k est. " + "y" * 300
    _, html, text = _gate([j], [RJ(j, 80, long_pay, 100000, 110000)])
    assert "<script>" not in html and "<b>Architect</b>" not in html
    line = f"<b>Architect</b> @ Evil & Co — {long_pay}"
    assert _h.escape(line[:160], quote=True) in html and _h.escape(line[:161], quote=True) not in html
    assert f"- {line[:160]}" in text


def test_ranking_failed_rows_gated_on_stated_salary_only():
    low = SJ("Cloud Architect", lo=120000, hi=150000, text="$120k–$150k")
    unknown = SJ("Security Architect", "Beta")
    high = SJ("Identity Architect", "Gamma", lo=200000, hi=240000)
    subject, html, text = _gate([low, unknown, high], None, ranking_error="RuntimeError: boom")
    assert subject == "Jobs Wed Sep 30 — 2 unranked (ranking failed)"
    below = html.split("Below pay target", 1)[1]
    assert "Cloud Architect @ Acme — $120k–$150k" in below
    top = html.split("Below pay target", 1)[0]
    assert "Security Architect" in top and "Identity Architect" in top and "Cloud Architect" not in top
    assert "Cloud Architect" not in text.split("Below pay target", 1)[0]


def test_ranking_failed_all_below_target():
    low = SJ("Cloud Architect", lo=120000, hi=150000)
    subject, html, _ = _gate([low], None, ranking_error="RuntimeError: boom")
    assert subject == "Jobs Wed Sep 30 — no new matches (ranking failed)"
    assert "Ranking failed" in html and "$120k–$150k stated" in html.split("Below pay target", 1)[1]


def test_unranked_omitted_rows_gated_on_stated_salary_only():
    ranked_job = SJ("Security Architect")
    omitted_low = SJ("Cloud Architect", "Beta", lo=120000, hi=150000)
    omitted_unknown = SJ("Identity Architect", "Gamma")
    subject, html, _ = _gate([ranked_job, omitted_low, omitted_unknown], [RJ(ranked_job, 30, "est", 90000, 100000)])
    assert subject == "Jobs Wed Sep 30 — 1 unranked (ranker omitted)"
    unranked = html.split("Unranked (ranker omitted or invalid)", 1)[1].split("Below pay target", 1)[0]
    assert "Identity Architect" in unranked and "Cloud Architect" not in unranked
    assert "Cloud Architect @ Beta" in html.split("Below pay target", 1)[1]
    assert "unranked 1" in html


def test_base_salary_never_rendered():
    jobs = [SJ("Cloud Architect", lo=120000, hi=150000), SJ("Security Architect", "Beta"),
            SJ("Identity Architect", "Gamma")]
    for rjobs in ([RJ(jobs[0], 80, "$120k–$150k"), RJ(jobs[1], 80, "~$110k est.", 100000, 120000),
                   RJ(jobs[2], 80, "~$200k est.", 190000, 210000)], None):
        _, html, text = _gate(jobs, rjobs, ranking_error="x")
        for out in (html, text):
            assert "Below pay target ($189k" in out
            for leak in ("151234", "151,234", "$151k", "151.2", "$151"):
                assert leak not in out


# ---- batch B: pay vs base in Python, grouped dealbreakers, card flags, benefits, source timing -----------
# Made-up base 151234: it must never appear in the digest.
BASE = 151234


def _pay_ctx(rjobs, jobs, base=BASE):
    return DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=RankedOutput(jobs=rjobs), pay_base=base)


def test_pay_vs_base_computed_in_python_and_base_never_shown():
    a, b = SJ("Security Architect"), SJ("Cloud Architect", "Beta")
    _, html, text = render(_pay_ctx([RJ(a, 90, "$180k–$210k stated", 180000, 210000, est=False),
                                     RJ(b, 70, "~$200k est.", 200000, None)], [a, b]))
    assert "$180k–$210k stated · +19% to +39% vs current base" in html
    assert "~$200k est. · +32% vs current base" in html
    assert "$180k–$210k stated · +19% to +39% vs current base" in text
    for leak in ("151234", "151,234", "$151k", "151k"):
        assert leak not in html and leak not in text


def test_pay_vs_base_skipped_without_base_or_amounts_or_when_already_there():
    a, b = SJ("Security Architect"), SJ("Cloud Architect", "Beta")
    _, html, _ = render(_pay_ctx([RJ(a, 90, "not stated"),
                                  RJ(b, 90, "~$190k est. · +5% vs current base", 190000, 190000)], [a, b]))
    assert "vs current base" in html and html.count("vs current base") == 1
    _, html, _ = render(_pay_ctx([RJ(a, 90, "$180k stated", 180000, 180000)], [a], base=None))
    assert "vs current base" not in html


def test_negative_pay_vs_base_is_signed():
    a = SJ("Security Architect")
    _, html, _ = render(_pay_ctx([RJ(a, 90, "~$140k–$160k est.", 140000, 160000)], [a]))
    assert "-7% to +6% vs current base" in html


def test_dealbreakers_grouped_by_canonical_key():
    jobs = [SJ(f"Job {i}", f"Co{i}") for i in range(6)]
    gaps = ["DEALBREAKER: travel — 25% travel", "DEALBREAKER: on-call — 24/7 rotation",
            "DEALBREAKER: travel — client visits", "DEALBREAKER: travel — data center",
            "DEALBREAKER: hybrid-ish — 3 days in office", "DEALBREAKER: onsite — Austin office"]
    rj = [RJ(j, 0, gaps=[g]) for j, g in zip(jobs, gaps)]
    _, html, _ = render(DigestContext(run_date=D, candidates=jobs, stats=STATS, ranked=RankedOutput(jobs=rj)))
    import re
    plain = _h.unescape(re.sub(r"<[^>]+>", "", html))
    assert "Dealbreakers (auto-excluded): travel 3 · on-call 1 · onsite 1 · other 1" in plain
    block = html.split("Dealbreakers (auto-excluded)", 1)[1]
    order = [block.index(f"Job {i} @") for i in (0, 2, 3, 1, 5, 4)]
    assert order == sorted(order)  # lines follow the groups


def test_card_hides_snippet_flags_keeps_others():
    j = Job(source="s", url="https://x/1", title="Security Architect", company="Acme",
            flags=['degree: "BS in CS required"', 'travel: "up to 10% travel"', 'on-call: "on-call rotation"',
                   "salary range straddles floor"])
    _, html, _ = render(DigestContext(run_date=D, candidates=[j], stats=STATS,
                                      ranked=RankedOutput(jobs=[RJ(j, 90)])))
    card = html.split("Top picks", 1)[1]
    assert "Flags: salary range straddles floor" in card
    assert "up to 10% travel" not in card and "on-call rotation" not in card
    j2 = Job(source="s", url="https://x/2", title="Cloud Architect", company="Acme", flags=['travel: "x"'])
    _, html, _ = render(DigestContext(run_date=D, candidates=[j2], stats=STATS,
                                      ranked=RankedOutput(jobs=[RJ(j2, 90)])))
    assert "Flags:" not in html


def _research(rows):
    return {"Acme": CompanyResearch.model_validate({"company": "Acme", "researched_on": "2026-09-30",
                                                    "benefits": rows, "reviews": {"summary": "s"}})}


def test_unknown_benefit_rows_collapse_into_one_muted_line():
    j = SJ("Security Architect")
    rows = [{"item": "Medical premium", "mark": "unknown", "detail": "?", "label": "Unknown"},
            {"item": "PTO", "mark": "better", "detail": "unlimited", "label": "Likely"},
            {"item": "HSA / employer contribution", "mark": "same", "detail": "?", "label": "Unknown"},
            {"item": "401(k) match", "mark": "unknown", "detail": "?", "label": "Likely"}]
    _, html, _ = render(DigestContext(run_date=D, candidates=[j], stats=STATS,
                                      ranked=RankedOutput(jobs=[RJ(j, 90)]), research=_research(rows)))
    assert "No public data: Medical premium, HSA / employer contribution, 401(k) match" in html
    assert html.count("<tr>") == 1 and "unlimited" in html


def test_all_unknown_benefits_render_only_the_muted_line():
    j = SJ("Security Architect")
    rows = [{"item": "Medical premium", "mark": "unknown", "detail": "?", "label": "Unknown"},
            {"item": "PTO", "mark": "unknown", "detail": "?", "label": "Unknown"}]
    _, html, _ = render(DigestContext(run_date=D, candidates=[j], stats=STATS,
                                      ranked=RankedOutput(jobs=[RJ(j, 90)]), research=_research(rows)))
    assert "No public data: Medical premium, PTO" in html and "<table" not in html


def test_footer_shows_source_minutes_when_present():
    stats = {**STATS, "sources": [{"name": "jobspy", "ok": True, "count": 350, "seconds": 1862.4},
                                  {"name": "remotive", "ok": True, "count": 4},
                                  {"name": "himalayas", "ok": False, "error": "429", "seconds": "n/a"},
                                  {"name": "lever", "ok": True, "count": 7, "seconds": 12.0}]}
    _, html, _ = render(DigestContext(run_date=D, candidates=[], stats=stats))
    assert "jobspy ✓350 (31m)" in html and "remotive ✓4," in html and "429 (" not in html
    assert "lever ✓7<br>" in html  # under half a minute: no "(0m)" noise
