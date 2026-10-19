import pytest

from jobsearch.config import load_config
from jobsearch.filters import title_ok

CFG = load_config("filters.yaml")


def ok(title):
    return title_ok(title, CFG["title_include"], CFG["title_exclude"])


@pytest.mark.parametrize("title", [
    "Head of Business Technology Engineering", "Director Cloud Services", "Cloud Operations Engineer",
    "Director, Site Reliability Engineering", "Senior Software Engineer, PKI & Cryptographic Systems",
    "Staff Engineer, Endpoint DLP",
    "Director of IT", "IT Manager", "Head of Security", "Security Engineer", "Senior Systems Engineer",
    "Director, Enterprise Systems & Architecture", "Senior Security Architect", "Cloud Security Architect",
    "Director of Information Security", "Microsoft Sentinel Detection Engineer",
    "Detection Engineer", "AI Platform Engineer", "Identity Architect", "Security Architect",
])
def test_wanted_titles_pass(title):
    assert ok(title), title


@pytest.mark.parametrize("title", [
    "Principal Structural Engineer – Data Center Infrastructure", "Missile Defense Systems Architect, Senior",
    "Mechanical Systems Engineer", "Civil Engineer, Infrastructure", "IT Help Desk Senior Coordinator",
    "Physical Security Officer", "Junior Security Analyst", "Enterprise Sales Director", "Product Designer",
    "Endpoint Support Technician", "Landscape Architect", "AI Content Writer", "AI Research Scientist",
    "Cybersecurity Instructor", "Social Security Claims Specialist", "Integration Specialist – Payroll",
    "Infrastructure Project Manager – Construction", "Talent Partner, IT", "Recruiting Coordinator, Security",
])
def test_unwanted_titles_fail(title):
    assert not ok(title), title
