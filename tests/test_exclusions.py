import pytest

from jobsearch.exclusions import Exclusions

DATA = {
    "current_employer": [{"name": "Example Corp", "aliases": ["ExampleCo"]}],
    "pe_owner_portfolio": [{"name": "Flute", "aliases": ["Aurora Payments"]}],
    "staffing_agencies": [{"name": "Robert Half"}],
}


def test_exact_and_alias_match():
    ex = Exclusions(DATA)
    assert ex.match("Example Corp, Inc.") == "Example Corp"
    assert ex.match("ExampleCo") == "Example Corp"
    assert ex.match("Aurora Payments LLC") == "Flute"


def test_portfolio_is_exact_not_prefix():
    assert Exclusions(DATA).match("Flute Health") is None


def test_staffing_prefix_match():
    assert Exclusions(DATA).match("Robert Half Technology") == "Robert Half"


def test_staffing_heuristic_text():
    assert Exclusions(DATA).match("Acme", "On behalf of our client, a fintech...") == "staffing agency (our client)"


def test_confidential_company():
    assert Exclusions(DATA).match("Confidential") == "confidential company"


def test_no_match_and_empty_company():
    ex = Exclusions(DATA)
    assert ex.match("Cloudflare") is None
    assert ex.match("") is None


def test_load_missing_file_is_empty(tmp_path):
    assert Exclusions.load(tmp_path / "nope.yaml").match("Anything") is None


def test_load_real_format(tmp_path):
    p = tmp_path / "ex.yaml"
    p.write_text("current_employer:\n  - name: Example Corp\n", encoding="utf-8")
    assert Exclusions.load(p).match("example corp") == "Example Corp"


def test_our_client_heuristic_needs_staffing_phrasing():
    ex = Exclusions(DATA)
    assert ex.match("Acme", "Our client, a leading fintech, is hiring.") == "staffing agency (our client)"
    assert ex.match("Acme", "Our client is a leading bank.") == "staffing agency (our client)"
    assert ex.match("Acme", "Recruiting for a confidential client.") == "staffing agency (our client)"
    for d in ("You will join our client success team.", "You will work with our client teams.",
              "You will keep confidential client data private.", "confidential company culture"):
        assert ex.match("Acme", d) is None


def test_our_client_heuristic_only_scans_first_300_chars():
    assert Exclusions(DATA).match("Acme", "x" * 400 + " On behalf of our client, a fintech") is None


def test_string_entries_are_names():
    assert Exclusions({"current_employer": ["Example Corp"]}).match("Example Corp") == "Example Corp"


def test_entry_without_name_raises():
    with pytest.raises(ValueError, match="missing 'name'"):
        Exclusions({"current_employer": [{"aliases": ["x"]}]})


@pytest.mark.parametrize("name", [
    "Acme Staffing", "Blue Ridge Recruiting", "Apex Talent Solutions", "Global Workforce Solutions",
    "Prime IT Resources", "Summit Search Group", "TechPlacements", "Acme Staffing, Inc.",
    "Northstar Recruitment", "Kelly Personnel Services", "Bay Employment Agency",
])
def test_staffing_name_pattern_matches(name):
    assert Exclusions({}).match(name) == "staffing agency (name pattern)"


@pytest.mark.parametrize("name", [
    "Cloudflare", "Recruit Holdings", "Recruit Holdings Co., Ltd.", "Displacement Labs",
    "Okta", "Human Resources Corp", "Acme Security",
])
def test_staffing_name_pattern_no_false_positive(name):
    assert Exclusions({}).match(name) is None


def test_named_exclusion_wins_over_name_pattern():
    ex = Exclusions({"staffing_agencies": [{"name": "Robert Half"}]})
    assert ex.match("Robert Half Staffing") == "Robert Half"


def test_name_pattern_runs_before_description_heuristic():
    ex = Exclusions({})
    assert ex.match("Acme Staffing", "On behalf of our client, a bank") == "staffing agency (name pattern)"


def test_custom_staffing_name_patterns_merge_with_defaults():
    ex = Exclusions({"staffing_name_patterns": [r"\bconsulting bench\b"]})
    assert ex.match("Zeta Consulting Bench") == "staffing agency (name pattern)"
    assert ex.match("Acme Staffing") == "staffing agency (name pattern)"
    assert Exclusions({}).match("Zeta Consulting Bench") is None


def test_invalid_staffing_name_pattern_names_the_pattern():
    with pytest.raises(ValueError, match=r"staffing_name_patterns.*\(unclosed"):
        Exclusions({"staffing_name_patterns": ["(unclosed"]})


def test_staffing_name_patterns_key_is_not_an_entry_group():
    ex = Exclusions({"staffing_name_patterns": ["x+"], "current_employer": ["Example Corp"]})
    assert ex.match("Example Corp") == "Example Corp"
    assert ex.exact == {"example": "Example Corp"}
