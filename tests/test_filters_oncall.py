import time

import pytest

from jobsearch.exclusions import Exclusions
from jobsearch.filters import apply_filters, oncall_in_title_or_location, oncall_mentions
from jobsearch.models import Job

CFG = {
    "salary_floor": 175000,
    "home_state_abbr": "TX",
    "title_include": [r"secur", r"architect"],
    "title_exclude": [],
}
EX = Exclusions({"current_employer": [{"name": "Example Corp"}]})


def J(**kw):
    base = dict(source="t", url="u", title="Security Engineer", company="Acme", remote=True,
                location="United States", posted="2026-09-29", salary_min=180000, salary_max=220000)
    base.update(kw)
    return Job(**base)


@pytest.mark.parametrize("desc", [
    "Participate in a weekly on-call rotation",
    "24/7 on-call support for production incidents",
    "PagerDuty rotation with the SRE team",
    "after-hours on-call as needed",
    "Weekend on-call coverage one week per month.",
    "You will carry the pager every sixth week.",
    "Oncall duties are shared across the team",
    "Be available for pager duty.",
    "May occasionally assist after hours with critical incidents.",
])
def test_oncall_mentions_flags(desc):
    out = oncall_mentions(desc)
    assert out
    assert all(len(s) <= 140 for s in out)


@pytest.mark.parametrize("desc", [
    "No on-call required.",
    "This role has no pager duty",
    "This is not an on-call role",
    "On-call is not required",
    "On-call not required for this position",
    "Fully remote, without on-call",
    "We do not expect weekend on-call.",
    "No after-hours work.",
    "Escalations are handled only during business hours.",
    "Experience with PagerDuty, Datadog and Grafana.",
    "Our call center platform team",
    "",
])
def test_oncall_mentions_ignores_negated_or_unrelated(desc):
    assert oncall_mentions(desc) == []


def test_oncall_mentions_caps_at_two_and_collapses_whitespace():
    desc = "Join   the\ton-call rotation.\nCarry the pager.\nWeekend on-call too."
    out = oncall_mentions(desc)
    assert len(out) == 2
    assert out[0] == "Join the on-call rotation"


def test_oncall_long_clause_snippet_is_short():
    desc = "x " * 300 + "participate in the on-call rotation " + "y " * 300
    out = oncall_mentions(desc)
    assert len(out) == 1 and len(out[0]) <= 140 and "on-call rotation" in out[0]


@pytest.mark.parametrize("make", [
    lambda: "on-call " * 12500,
    lambda: "no " * 40000,
    lambda: "a" * 100_000,
    lambda: "x " * 50_000,
    lambda: "after hours no " * 7000,
], ids=["oncall-repeat", "no-repeat", "single-token", "single-line", "afterhours-no"])
def test_oncall_mentions_is_linear(make):
    desc = make()
    assert len(desc) >= 70_000
    t = time.perf_counter()
    oncall_mentions(desc)
    assert time.perf_counter() - t < 2


def test_oncall_scan_capped_at_50k_chars():
    assert oncall_mentions("a " * 30000 + "join the on-call rotation") == []


@pytest.mark.parametrize("kw", [
    dict(title="Security Engineer (On-Call)"),
    dict(title="Security Engineer - on-call rotation"),
    dict(title="On-Call Security Engineer"),
    dict(location="Remote, US - 24/7 on-call"),
    dict(title="Security Architect", location="United States (on-call required)"),
])
def test_title_or_location_oncall_is_dropped(kw):
    assert oncall_in_title_or_location(J(**kw))
    res = apply_filters([J(**kw)], CFG, EX, since=None)
    assert res.kept == []
    assert res.dropped == {"on-call (title/location)": 1}


@pytest.mark.parametrize("kw", [
    dict(title="Security Engineer (No On-Call)"),
    dict(title="Security Engineer - no on-call rotation"),
    dict(location="Remote, US - no on-call"),
    dict(title="Security Engineer", location="United States"),
    dict(title="Security Engineer, Call Center Platforms"),
])
def test_title_or_location_without_oncall_is_kept(kw):
    res = apply_filters([J(**kw)], CFG, EX, since=None)
    assert len(res.kept) == 1


def test_description_oncall_only_flags():
    res = apply_filters([J(description="Participate in a weekly on-call rotation. Great team.")],
                        CFG, EX, since=None)
    assert len(res.kept) == 1
    assert res.kept[0].flags == ['on-call: "Participate in a weekly on-call rotation"']


def test_description_negated_oncall_adds_no_flag():
    res = apply_filters([J(description="No on-call required. Great team.")], CFG, EX, since=None)
    assert res.kept[0].flags == []
