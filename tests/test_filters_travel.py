import time

import pytest

from jobsearch.exclusions import Exclusions
from jobsearch.filters import apply_filters, travel_mentions
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
    "Travel up to 25% to customer sites",
    "This role requires 10% travel.",
    "Some occasional travel to data center",
    "You will do on-site visits with partners.",
    "Travel up to 50%; expenses are reimbursed.",
    "Annual summit, plus travel up to 20% to client sites.",
    "Travel to conferences is part of the job.",
])
def test_travel_mentions_flags_work_travel(desc):
    out = travel_mentions(desc)
    assert out
    assert all(len(s) <= 140 for s in out)


@pytest.mark.parametrize("desc", [
    "No travel required.",
    "0% travel",
    "Travel not required",
    "Fully remote, without travel",
    "This role does not require travel.",
    "Travel-free role",
    "Travel reimbursement and home office stipend",
    "Annual team summit (travel covered)",
    "We meet in person twice a year at our company offsite",
    "Travel to our annual company offsite twice a year.",
    "Optional travel to the all-hands retreat, per diem provided",
    "",
])
def test_travel_mentions_ignores_allowed_or_negated(desc):
    assert travel_mentions(desc) == []


def test_travel_mentions_caps_at_two_and_collapses_whitespace():
    desc = "Travel   up to\t30%.\nTravel to customer sites.\nTravel 5% more."
    out = travel_mentions(desc)
    assert len(out) == 2
    assert out[0] == "Travel up to 30%"


def test_travel_mentions_long_clause_snippet_is_short():
    desc = "x " * 300 + "requires 20% travel " + "y " * 300
    out = travel_mentions(desc)
    assert len(out) == 1 and len(out[0]) <= 140 and "20% travel" in out[0]


@pytest.mark.parametrize("make", [
    lambda: "travel " * 20000,
    lambda: "no " * 40000,
    lambda: "a " * 60000 + "travel 25%",
    lambda: "customer " * 15000,
], ids=["travel-repeat", "no-repeat", "late-mention", "customer-repeat"])
def test_travel_mentions_is_linear(make):
    desc = make()
    t = time.perf_counter()
    travel_mentions(desc)
    assert time.perf_counter() - t < 2


def test_travel_scan_capped_at_50k_chars():
    assert travel_mentions("a " * 30000 + "requires 25% travel") == []


@pytest.mark.parametrize("kw", [
    dict(title="Security Engineer (25% travel)"),
    dict(title="Security Architect - Travel up to 50%"),
    dict(location="Remote, US - 10% travel"),
    dict(title="Security Engineer", location="United States (travel required)"),
])
def test_title_or_location_travel_is_dropped(kw):
    res = apply_filters([J(**kw)], CFG, EX, since=None)
    assert res.kept == []
    assert res.dropped == {"work travel (title/location)": 1}


@pytest.mark.parametrize("kw", [
    dict(title="Security Engineer (0% travel)"),
    dict(title="Security Engineer - No travel required"),
    dict(location="Remote, US - no travel"),
    dict(title="Security Engineer", location="United States"),
])
def test_title_or_location_no_travel_is_kept(kw):
    res = apply_filters([J(**kw)], CFG, EX, since=None)
    assert len(res.kept) == 1


def test_description_travel_only_flags():
    res = apply_filters([J(description="Travel up to 25% to customer sites. Great team.")], CFG, EX, since=None)
    assert len(res.kept) == 1
    assert res.kept[0].flags == ['travel: "Travel up to 25% to customer sites"']


def test_description_without_travel_adds_no_flag():
    res = apply_filters([J(description="No travel required. Annual company offsite.")], CFG, EX, since=None)
    assert res.kept[0].flags == []


@pytest.mark.parametrize("desc", [
    "You will be on-call and may need to be onsite for major incidents.",
    "Occasional onsite visits to customer data centers",
    "Must be able to be in person for hardware installs.",
    "Travel: 10%",
    "Travel: 0-10%",
])
def test_onsite_presence_duty_and_labelled_travel_flag(desc):
    assert travel_mentions(desc)


@pytest.mark.parametrize("desc", [
    "We meet in person at our annual company offsite.",
    "No onsite presence required for incidents.",
    "Travel: 0%",
    "Travel - 0%",
    "Travel: None",
    "Travel: N/A",
    "Travel - not required",
    "Travel: No",
])
def test_onsite_gathering_and_labelled_no_travel_not_flagged(desc):
    assert travel_mentions(desc) == []


def test_onsite_clause_is_linear():
    desc = "onsite customer " * 3000
    t = time.perf_counter()
    travel_mentions(desc)
    assert time.perf_counter() - t < 2
