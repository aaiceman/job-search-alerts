import time

import pytest

from jobsearch.exclusions import Exclusions
from jobsearch.filters import apply_filters, degree_mentions
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
    "Bachelor's degree in Computer Science or equivalent experience",
    "BS/MS in Computer Science",
    "MS in Information Security preferred",
    "BS degree required",
    "Master's degree required",
    "PhD a plus",
    "Ph.D. in a related field",
    "B.S. in Engineering",
    "Equivalent work experience will be considered",
    "Associate's degree and 5 years of experience",
    "Four-year degree or 8 years of experience",
    "No degree required",
    "MBA preferred",
])
def test_degree_mentions_flags(desc):
    out = degree_mentions(desc)
    assert out
    assert all(len(s) <= 160 for s in out)


@pytest.mark.parametrize("desc", [
    "Experience with MS Intune, MS Defender and MS 365",
    "Deep MS Sentinel knowledge",
    "Work with BAs and product owners",
    "A high degree of autonomy",
    "360-degree view of the customer",
    "Scrum Master certification",
    "Master data management experience",
    "",
])
def test_degree_mentions_ignores_non_degree_text(desc):
    assert degree_mentions(desc) == []


def test_degree_mentions_quotes_exact_phrase_and_caps_at_two():
    desc = ("About us.\nBachelor's   degree in CS or equivalent experience.\nMaster's degree preferred.\n"
            "PhD a plus.")
    out = degree_mentions(desc)
    assert out == ["Bachelor's degree in CS or equivalent experience", "Master's degree preferred"]


def test_degree_mentions_abbreviation_dots_do_not_split_clause():
    out = degree_mentions("B.S. or M.S. in Computer Science, or equivalent experience.")
    assert len(out) == 1 and "Computer Science" in out[0] and "equivalent" in out[0]


def test_degree_long_clause_snippet_is_short():
    desc = "x " * 300 + "bachelor's degree required " + "y " * 300
    out = degree_mentions(desc)
    assert len(out) == 1 and len(out[0]) <= 160 and "degree required" in out[0]


@pytest.mark.parametrize("make", [
    lambda: "degree " * 15000,
    lambda: "MS " * 35000,
    lambda: "a" * 100_000,
    lambda: "x " * 50_000,
    lambda: "BS/" * 35000,
], ids=["degree-repeat", "ms-repeat", "single-token", "single-line", "bs-slash"])
def test_degree_mentions_is_linear(make):
    desc = make()
    assert len(desc) >= 70_000
    t = time.perf_counter()
    degree_mentions(desc)
    assert time.perf_counter() - t < 2


def test_degree_scan_capped_at_50k_chars():
    assert degree_mentions("a " * 30000 + "bachelor's degree required") == []


def test_apply_filters_adds_degree_snippet_flags():
    res = apply_filters([J(description="Great team. Bachelor's degree or equivalent experience.")],
                        CFG, EX, since=None)
    assert res.kept[0].flags == ['degree: "Bachelor\'s degree or equivalent experience"']


def test_apply_filters_keeps_existing_advanced_degree_flag():
    res = apply_filters([J(description="Master's degree required.")], CFG, EX, since=None)
    assert res.kept[0].flags == ["degree: Master's/PhD may be required", 'degree: "Master\'s degree required"']
