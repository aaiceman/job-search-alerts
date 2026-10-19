import pytest

from jobsearch.exclusions import Exclusions
from jobsearch.filters import (apply_filters, clearance_required, education_check, employment_ok,
                               salary_check, title_ok)
from jobsearch.models import Job

INCLUDE = [r"secur", r"architect", r"\bai\b"]
EXCLUDE = [r"\b(junior|jr\.?|intern(ship)?)\b", r"\b(manager|director)\b"]


def J(**kw):
    base = dict(source="t", url="u", title="Security Engineer", company="Acme")
    base.update(kw)
    return Job(**base)


@pytest.mark.parametrize("title,ok", [
    ("Senior Security Engineer", True),
    ("Cloud Security Architect", True),
    ("Junior Security Analyst", False),
    ("Security Engineering Manager", False),
    ("Accountant", False),
    ("Internal Tools Security Engineer", True),
])
def test_title_ok(title, ok):
    assert title_ok(title, INCLUDE, EXCLUDE) is ok


@pytest.mark.parametrize("kw,ok", [
    (dict(employment_type="fulltime"), True),
    (dict(employment_type="FullTime"), True),
    (dict(employment_type="part_time"), False),
    (dict(employment_type="Contractor"), False),
    (dict(employment_type="PartTime"), False),
    (dict(title="Security Engineer (Contract)"), False),
    (dict(description="This is a 6-month contract with possible extension."), False),
    (dict(description="Contract-to-hire opportunity."), False),
    (dict(description="W2 or C2C accepted."), False),
    (dict(description="Full-time role with great benefits."), True),
])
def test_employment_ok(kw, ok):
    assert employment_ok(J(**kw))[0] is ok


@pytest.mark.parametrize("desc,expected", [
    ("Bachelor's degree or equivalent experience.", "ok"),
    ("Master's degree in Computer Science required.", "drop"),
    ("Bachelor’s or Master’s degree required.", "ok"),
    ("Master's degree preferred.", "penalty"),
    ("PhD in Security required", "drop"),
    ("Ph.D. required.", "drop"),
    ("MBA a plus.", "penalty"),
    ("Bachelor's degree required; Master's preferred.", "penalty"),
    ("Experience with MS Office and Microsoft 365.", "ok"),
    ("", "ok"),
])
def test_education_check(desc, expected):
    assert education_check(desc) == expected


def test_clearance_required():
    assert clearance_required("Active TS/SCI clearance required.") is True
    assert clearance_required("Must hold a Top Secret security clearance.") is True
    assert clearance_required("Ability to obtain a Public Trust is a plus.") is False
    assert clearance_required("We protect customers with strong security.") is False


@pytest.mark.parametrize("lo,hi,status,reason", [
    (None, None, "flag", "salary not listed"),
    (150000, 170000, "drop", "salary midpoint below floor"),
    (125000, 224000, "drop", "salary midpoint below floor"),           # mid 174.5k
    (143000, 207000, "flag", "salary range straddles floor (midpoint ok)"),  # mid exactly 175k passes
    (150000, 210000, "flag", "salary range straddles floor (midpoint ok)"),
    (168000, 286000, "flag", "salary range straddles floor (midpoint ok)"),
    (160000, 200000, "flag", "salary range straddles floor (midpoint ok)"),
    (180000, 220000, "ok", ""),
    (175000, 200000, "ok", ""),
    (200000, 200000, "ok", ""),                                        # single stated value
    (170000, 170000, "drop", "salary midpoint below floor"),
    (None, 170000, "drop", "salary below floor"),                      # "up to $170k"
    (None, 200000, "flag", "salary max only (midpoint unknown)"),      # "up to $200k"
    (150000, None, "flag", "salary from $150k (no max listed)"),
    (190000, None, "ok", ""),
])
def test_salary_check(lo, hi, status, reason):
    assert salary_check(J(salary_min=lo, salary_max=hi), 175000) == (status, reason)


# ---- fix round: drops only on explicit signals ------------------------------------------------
@pytest.mark.parametrize("d", [
    "Security clearance is not required",
    "No security clearance required.",
    "You will not need a security clearance",
    "Security clearance (not required)",
    "polygraph not required",
    "Security clearance: none",
    "Ability to obtain a security clearance",
    "Security clearance will be sponsored",
    "Eligible to obtain a Top Secret clearance is required.",
    "Active security clearance preferred.",
])
def test_clearance_not_required_probes(d):
    assert clearance_required(d) is False


@pytest.mark.parametrize("d", [
    "A Master's degree is not required.",
    "Master's degree not required",
    "No Master's or PhD required",
    "Advanced degrees (Master's, PhD) are not required",
    "we don't require a Master's degree or a PhD",
    "we don’t require a Master’s degree",
])
def test_education_negation_ok(d):
    assert education_check(d) == "ok"


@pytest.mark.parametrize("d", [
    "Master's degree is a nice-to-have; minimum 10 years experience",
    "Must have 10 years; Master's degree is beneficial",
    "Master's degree is highly desirable, minimum 5 years",
    "A Master's degree is an asset.",
    "Master's degree is advantageous.",
    "Master's degree is ideal.",
    "Master's degree favoured.",
])
def test_education_soft_words_penalty(d):
    assert education_check(d) == "penalty"


def test_education_requirement_in_other_clause_is_not_a_drop():
    assert education_check("Master's degree; must have 5 years experience") != "drop"
    assert education_check("Master's degree required") == "drop"


@pytest.mark.parametrize("d", [
    "No C2C or 1099 candidates please. This is a full-time W2 role.",
    "You will receive a W-2 (not 1099).",
    "This is not a contract role.",
    "This is not contract-to-hire.",
    "You will generate 1099 forms for vendors.",
])
def test_employment_negated_terms_ok(d):
    assert employment_ok(J(description=d))[0] is True


def test_employment_1099_with_job_context_dropped():
    assert employment_ok(J(description="This is a 1099 contractor position."))[0] is False
    assert employment_ok(J(description="1099 only."))[0] is False


@pytest.mark.parametrize("et", ["Full-time (International)", "internal", "FULL_TIME"])
def test_employment_type_word_boundaries(et):
    assert employment_ok(J(employment_type=et)) == (True, "")


@pytest.mark.parametrize("et", ["Full-Time, Part-Time", "Full-time / Contract"])
def test_employment_multivalued_type_is_ambiguous_not_dropped(et):
    assert employment_ok(J(employment_type=et)) == (True, "employment type ambiguous")


@pytest.mark.parametrize("title,ok", [
    ("Contract Management Systems Engineer", True),
    ("Engineer, Contract Lifecycle Management", True),
    ("Temp Sensor Engineer", True),
    ("Security Engineer (Contract)", False),
    ("Security Engineer - Contract", False),
    ("Security Engineer, Contract", False),
    ("Security Engineer (Temp)", False),
    ("Security Engineer Contract-to-hire", False),
    ("Contract Role: Security Engineer", False),
])
def test_employment_title_contract_needs_engagement_shape(title, ok):
    assert employment_ok(J(title=title))[0] is ok


def test_title_ok_empty_include_keeps_all():
    assert title_ok("Accountant", [], EXCLUDE) is True
    assert title_ok("Junior Accountant", [], EXCLUDE) is False


_CFG = {"salary_floor": 175000, "home_state_abbr": "TX", "title_include": [r"secur"], "title_exclude": []}
_EX = Exclusions({})


def _AJ(**kw):
    base = dict(source="t", url="u", title="Security Engineer", company="Acme", remote=True,
                location="United States", salary_min=180000, salary_max=220000)
    base.update(kw)
    return Job(**base)


def test_apply_filters_ambiguous_employment_type_becomes_flag():
    res = apply_filters([_AJ(employment_type="Full-time / Contract")], _CFG, _EX, since=None)
    assert res.kept[0].flags == ["employment type ambiguous"]


@pytest.mark.parametrize("posted", ["Yesterday", "3 days ago", "09/29/2026", "", "2026"])
def test_apply_filters_since_only_applies_to_iso_dates(posted):
    assert len(apply_filters([_AJ(posted=posted)], _CFG, _EX, since="2026-09-27").kept) == 1


def test_apply_filters_missing_title_lists_default_to_empty():
    res = apply_filters([_AJ(title="Accountant")], {"salary_floor": 175000}, _EX, since=None)
    assert len(res.kept) == 1


@pytest.mark.parametrize("d", [
    "Master's degree in CS or 10 years experience required",
    "MS/PhD or equivalent practical experience required",
    "Master's degree or 8+ years of relevant experience required",
])
def test_education_experience_alternative_is_ok(d):
    assert education_check(d) == "ok"


@pytest.mark.parametrize("d", ["Master's degree in Computer Science required.", "PhD in Security required"])
def test_education_without_alternative_still_drops(d):
    assert education_check(d) == "drop"


# ---- fix round 3 ------------------------------------------------------------------------------
@pytest.mark.parametrize("d", [
    "Active TS/SCI clearance required, and we do not offer sponsorship.",
    "Must hold an active Secret clearance (no sponsorship available).",
    "Requires an active security clearance (we cannot sponsor).",
    "Active TS/SCI required, no exceptions.",
    "Applicants must currently hold a Top Secret clearance",
    "Candidate must currently possess a Top Secret security clearance",
    "Clearance: Active Secret",
])
def test_clearance_true_probes(d):
    assert clearance_required(d) is True


@pytest.mark.parametrize("d", [
    "No polygraph required.",
    "We do not require a security clearance.",
    "An active clearance is required, or we will sponsor one.",
    "Security clearance can be sponsored.",
])
def test_clearance_false_probes_round3(d):
    assert clearance_required(d) is False


def test_apply_filters_flags_clearance_mentioned_when_not_required():
    j = _AJ(description="Security clearance is not required.")
    res = apply_filters([j], _CFG, _EX, since=None)
    assert res.kept[0].flags == ["clearance mentioned"]
    assert apply_filters([_AJ(description="A pleasant office.")], _CFG, _EX, since=None).kept[0].flags == []


@pytest.mark.parametrize("d", [
    "Master's degree in CS or related field required, with 5+ years of experience",
    "PhD in machine learning or statistics required, plus 5 years of experience",
    "Master's or PhD required, and 8+ years experience",
])
def test_education_or_must_be_followed_directly_by_experience(d):
    assert education_check(d) == "drop"


@pytest.mark.parametrize("et", ["CONTRACT_TO_HIRE", "contract_to_hire", "Part_Time", "Contract_Only"])
def test_employment_type_underscore_is_a_separator(et):
    assert employment_ok(J(employment_type=et))[0] is False


@pytest.mark.parametrize("title", ["Contract Software Engineer", "Temp Software Engineer"])
def test_title_starting_with_contract_or_temp_is_flagged_not_dropped(title):
    assert employment_ok(J(title=title)) == (True, "title suggests contract/temp")


def test_apply_filters_records_each_employment_reason_as_flag():
    res = apply_filters([_AJ(title="Contract Security Engineer", employment_type="Full-time / Contract")],
                        _CFG, _EX, since=None)
    assert res.kept[0].flags == ["employment type ambiguous", "title suggests contract/temp"]


# ---- refactor: wording fixes F1 / F2 ------------------------------------------------------------
@pytest.mark.parametrize("d", [
    "Master's degree or a minimum of 5 years of relevant experience",
    "Master's degree (or a minimum of 8 years of experience) required",
    "Master's degree or significant industry experience required",
    "Master's degree is required unless you have 10 years of experience",
    "Master's degree required, or in lieu of a degree, equivalent experience",
])
def test_education_experience_alternative_with_filler_is_ok(d):
    assert education_check(d) == "ok"


@pytest.mark.parametrize("d", [
    "You do not need to hold a security clearance",
    "We do not require you to hold an active security clearance",
    "Candidates are not required to have or hold an active clearance",
    "There is no requirement to hold a security clearance",
    "Neither a clearance nor a polygraph is required",
    "Security clearance requirement: none",
    "Security clearance required: no",
])
def test_clearance_negated_requirement_phrasings_are_false(d):
    assert clearance_required(d) is False


def test_employment_ok_can_skip_description_text():
    j = J(description="This is a 6-month contract with possible extension.")
    assert employment_ok(j)[0] is False
    assert employment_ok(j, include_description=False) == (True, "")
    assert employment_ok(J(employment_type="Contractor"), include_description=False)[0] is False
