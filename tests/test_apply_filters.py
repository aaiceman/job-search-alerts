import time

import pytest

from jobsearch.exclusions import Exclusions
from jobsearch.filters import apply_filters, classify_location, residency_check
from jobsearch.models import Job

CFG = {
    "salary_floor": 175000,
    "home_state_abbr": "TX",
    "drop_clearance_required": True,
    "title_include": [r"secur", r"architect"],
    "title_exclude": [r"\bjunior\b"],
}
EX = Exclusions({"current_employer": [{"name": "Example Corp"}]})


def J(**kw):
    base = dict(source="t", url="u", title="Security Engineer", company="Acme", remote=True,
                location="United States", posted="2026-09-29", salary_min=180000, salary_max=220000)
    base.update(kw)
    return Job(**base)


def test_each_rule_counts_its_drop():
    jobs = [
        J(title="Accountant"),
        J(title="Junior Security Engineer"),
        J(company="Example Corp"),
        J(employment_type="contract"),
        J(remote=False, location="Chicago, IL"),
        J(salary_min=120000, salary_max=150000),
        J(description="Master's degree required."),
        J(description="Active TS/SCI clearance required."),
        J(posted="2026-01-01"),
        J(title="Security Architect"),
    ]
    res = apply_filters(jobs, CFG, EX, since="2026-09-27")
    # degree and clearance requirements come from free text, so those two jobs are kept and flagged
    assert [j.title for j in res.kept] == ["Security Engineer", "Security Engineer", "Security Architect"]
    assert [j.flags for j in res.kept] == [
        ["degree: Master's/PhD may be required", 'degree: "Master\'s degree required"'],
        ["clearance may be required"], []]
    assert res.dropped == {
        "title": 2, "excluded company": 1, "not full-time": 1, "not remote": 1,
        "salary midpoint below floor": 1, "too old": 1,
    }
    assert res.excluded == {"Example Corp": 1}


def test_salary_parsed_from_description_when_missing():
    j = J(salary_min=None, salary_max=None, description="The base salary range is $150,000 - $165,000.")
    res = apply_filters([j], CFG, EX, since=None)
    assert res.kept == [] and res.dropped == {"salary midpoint below floor": 1}


def test_flags_accumulate_on_kept_jobs():
    j = J(salary_min=None, salary_max=None, description="Master's degree preferred.")
    res = apply_filters([j], CFG, EX, since=None)
    assert res.kept[0].flags == ["salary not listed", "degree: advanced degree mentioned",
                                 'degree: "Master\'s degree preferred"']


def test_unknown_posted_date_kept():
    assert len(apply_filters([J(posted="")], CFG, EX, since="2026-09-27").kept) == 1


# ---- refactor: description-derived rules flag instead of drop -----------------------------------
def _kept_flags(job, cfg=CFG, since=None):
    res = apply_filters([job], cfg, EX, since=since)
    assert res.dropped == {}, res.dropped
    return res.kept[0].flags


def test_description_degree_requirement_flags_instead_of_dropping():
    assert _kept_flags(J(description="Master's degree in Computer Science required.")) == [
        "degree: Master's/PhD may be required", 'degree: "Master\'s degree in Computer Science required"']


def test_description_clearance_requirement_flags_instead_of_dropping():
    assert _kept_flags(J(description="Active TS/SCI clearance required.")) == ["clearance may be required"]


def test_clearance_flag_is_not_duplicated_and_drop_key_is_tolerated():
    cfg = dict(CFG, drop_clearance_required=False)
    assert _kept_flags(J(description="Active TS/SCI clearance required."), cfg) == ["clearance may be required"]
    assert _kept_flags(J(description="Clearance is not required."), cfg) == ["clearance mentioned"]


def test_description_residency_flags_instead_of_dropping():
    d = "Candidates must reside in one of the following states: Arizona, Colorado, Florida, Georgia."
    assert _kept_flags(J(description=d)) == ["residency: state-restricted (no TX)"]
    d = "We are unable to hire in Texas or Florida at this time."
    assert _kept_flags(J(description=d)) == ["residency: residency excludes TX"]


def test_description_office_attendance_still_flagged():
    assert _kept_flags(J(description="You will work 3 days per week in the office.")) == [
        "office attendance mentioned"]


def test_description_contract_terms_flag_instead_of_dropping():
    assert _kept_flags(J(description="This is a 6-month contract with possible extension.")) == [
        "contract terms mentioned"]


@pytest.mark.parametrize("kw,reason", [
    (dict(employment_type="Contractor"), "not full-time"),
    (dict(title="Security Engineer (Contract)"), "not full-time"),
    (dict(employment_type="CONTRACT_TO_HIRE"), "not full-time"),
    (dict(location="Chicago, IL (Hybrid)", remote=None), "hybrid/on-site"),
    (dict(location="Chicago, IL", remote=False), "not remote"),
    (dict(location="Remote (Canada)"), "outside US"),
    (dict(location="Remote - CA, NY, WA"), "state-restricted (no TX)"),
    (dict(location="Remote - Texas excluded"), "location excludes TX"),
    (dict(location="Remote - US (excluding TX, CA)"), "location excludes TX"),
    (dict(salary_min=100000, salary_max=120000), "salary midpoint below floor"),
    (dict(salary_min=None, salary_max=150000), "salary below floor"),
])
def test_structured_signals_still_drop(kw, reason):
    res = apply_filters([J(**kw)], CFG, EX, since=None)
    assert res.kept == [] and res.dropped == {reason: 1}


def test_classify_location_include_description_false_ignores_description():
    d = "Candidates must reside in one of the following states: Arizona, Colorado, Florida, Georgia."
    assert classify_location(J(description=d))[0] == "drop"
    assert classify_location(J(description=d), include_description=False) == ("ok", "")
    assert classify_location(J(location="Remote - CA, NY, WA", description=d), include_description=False)[0] == "drop"


def test_residency_check_helper_is_description_only():
    d = "Candidates must reside in one of the following states: Arizona, Colorado, Florida, Georgia."
    assert residency_check(d, "TX") == ("drop", "state-restricted (no TX)")
    assert residency_check("We can hire in the following states: Arizona, Texas, Florida.", "TX") == ("ok", "")
    assert residency_check("Our team is based in Colorado.")[0] == "flag"
    assert residency_check("") == ("ok", "")


def test_location_field_is_capped_so_huge_strings_are_fast():
    j = J(location="Remote - " + "TX, " * 5000)
    t = time.perf_counter()
    classify_location(j)
    assert time.perf_counter() - t < 2


def test_merged_cross_region_duplicates_survive_filters():
    from jobsearch.dedupe import merge_duplicates
    a = J(source="linkedin", location="Toronto, Canada", description="short")
    b = J(source="greenhouse", url="https://gh/1", location="Austin, TX", description="a much longer description")
    [m] = merge_duplicates([a, b])
    assert m.location == "Austin, TX; Toronto, Canada"
    res = apply_filters([m], CFG, EX, None)
    assert [j.url for j in res.kept] == ["https://gh/1"]
    assert "multi-region posting; kept US-eligible location" in res.kept[0].flags


def test_contract_to_hire_at_staffing_named_company_dropped_as_excluded():
    j = J(company="Blue Ridge Recruiting",
          description="12-month contract-to-hire with possible conversion. Remote, US.")
    res = apply_filters([j], CFG, Exclusions({}), since=None)
    assert res.kept == []
    assert res.dropped == {"excluded company": 1}
    assert res.excluded == {"staffing agency (name pattern)": 1}
