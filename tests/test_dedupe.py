from jobsearch.dedupe import merge_duplicates
from jobsearch.models import Job


def test_merges_same_job_keeps_richest_and_all_links():
    a = Job(source="linkedin", url="https://li/1", title="Sr. Security Engineer", company="Acme Inc", description="short")
    b = Job(source="greenhouse", url="https://gh/1", title="Senior Security Engineer", company="Acme",
            description="a much longer description", salary_min=180000, salary_max=200000)
    c = Job(source="indeed", url="https://in/1", title="Senior Security Engineer (Remote)", company="ACME", description="mid")
    out = merge_duplicates([a, b, c])
    assert len(out) == 1
    m = out[0]
    assert m.url == "https://gh/1"
    assert set(m.extra_urls) == {"https://li/1", "https://in/1"}
    assert m.salary_max == 200000
    assert set(m.source.split("+")) == {"linkedin", "greenhouse", "indeed"}


def test_distinct_jobs_untouched():
    a = Job(source="x", url="1", title="Security Engineer", company="Acme")
    b = Job(source="x", url="2", title="Security Architect", company="Acme")
    assert len(merge_duplicates([a, b])) == 2


def test_merge_never_loses_a_region_and_prefers_direct_source():
    a = Job(source="linkedin", url="https://www.linkedin.com/jobs/view/1", title="Senior DevOps Engineer",
            company="Acme", location="Remote - US", remote=True, description="short", posted="2026-09-27")
    b = Job(source="indeed", url="https://in.indeed.com/viewjob?jk=2", title="Senior DevOps Engineer",
            company="Acme", location="Bangalore, India", remote=None, description="a much much longer description",
            posted="2026-09-29")
    out = merge_duplicates([a, b])
    assert len(out) == 1
    m = out[0]
    assert "Remote - US" in m.location and "Bangalore, India" in m.location
    assert m.remote is True
    assert m.posted == "2026-09-29"


def test_primary_prefers_ats_over_longer_aggregator_description():
    li = Job(source="linkedin", url="https://www.linkedin.com/jobs/view/1", title="Security Engineer",
             company="Acme", description="x" * 500, location="Remote")
    gh = Job(source="greenhouse", url="https://boards.greenhouse.io/acme/1", title="Security Engineer",
             company="Acme", description="short", location="US")
    [m] = merge_duplicates([li, gh])
    assert m.url == "https://boards.greenhouse.io/acme/1"
    assert m.location == "US; Remote"
    assert m.description == "short"
    assert m.extra_urls == ["https://www.linkedin.com/jobs/view/1"]


def test_merge_salary_field_by_field_and_remote_false_beats_none():
    a = Job(source="x", url="1", title="Security Engineer", company="Acme", salary_min=150000, remote=False,
            description="aaa")
    b = Job(source="y", url="2", title="Security Engineer", company="Acme", salary_max=220000, salary_text="$220k",
            remote=None)
    c = Job(source="z", url="3", title="Security Engineer", company="Acme", salary_min=170000, salary_max=200000)
    [m] = merge_duplicates([a, b, c])
    assert (m.salary_min, m.salary_max, m.salary_text, m.remote) == (150000, 220000, "$220k", False)


def test_merge_remote_none_when_all_unknown():
    a = Job(source="x", url="1", title="Security Engineer", company="Acme")
    b = Job(source="y", url="2", title="Security Engineer", company="Acme")
    assert merge_duplicates([a, b])[0].remote is None


def test_merge_does_not_mutate_inputs():
    a = Job(source="x", url="1", title="Security Engineer", company="Acme", description="longer one", location="A")
    b = Job(source="y", url="2", title="Security Engineer", company="Acme", location="B", flags=["f"])
    merge_duplicates([a, b])
    assert (a.extra_urls, a.source, a.location, a.flags) == ([], "x", "A", [])
    assert (b.extra_urls, b.source, b.location, b.flags) == ([], "y", "B", ["f"])
