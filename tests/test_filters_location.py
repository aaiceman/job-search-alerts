import time

import pytest

from jobsearch.filters import classify_location
from jobsearch.models import Job


def J(**kw):
    base = dict(source="t", url="u", title="Security Engineer", company="Acme", remote=True, location="United States")
    base.update(kw)
    return Job(**base)


def test_plain_us_remote_ok():
    assert classify_location(J()) == ("ok", "")


def test_hybrid_without_remote_dropped():
    assert classify_location(J(location="Austin, TX (Hybrid)", remote=None)) == ("drop", "hybrid/on-site")


def test_remote_or_hybrid_flagged():
    status, why = classify_location(J(location="Remote or Hybrid - Denver"))
    assert status == "flag" and "ambiguity" in why


def test_not_remote_dropped():
    assert classify_location(J(remote=False, location="Chicago, IL"))[0] == "drop"


def test_unknown_remote_requires_remote_in_title_or_location():
    assert classify_location(J(remote=None, location="San Francisco"))[0] == "drop"
    assert classify_location(J(remote=None, location="Remote - US"))[0] == "ok"


def test_non_us_dropped_but_us_or_canada_kept():
    assert classify_location(J(location="Remote (Canada)")) == ("drop", "outside US")
    assert classify_location(J(location="Remote - US or Canada"))[0] == "ok"


def test_location_state_list_without_tx_dropped():
    assert classify_location(J(location="Remote - CA, NY, WA"))[0] == "drop"
    assert classify_location(J(location="Remote - CA, TX, WA"))[0] == "ok"


def test_single_state_hq_location_is_not_a_restriction():
    assert classify_location(J(location="Orlando, FL"))[0] == "ok"


def test_residency_list_without_texas_dropped():
    d = "Candidates must reside in one of the following states: Arizona, Colorado, Florida, Georgia."
    assert classify_location(J(description=d))[0] == "drop"


def test_residency_list_with_texas_ok():
    d = "We can hire in the following states: Arizona, Texas, Florida."
    assert classify_location(J(description=d)) == ("ok", "")


def test_residency_exclusion_not_naming_texas_ok():
    d = "We can hire in all US states except California and New York."
    assert classify_location(J(description=d)) == ("ok", "")


def test_residency_exclusion_naming_texas_dropped():
    d = "We are unable to hire in Texas or Florida at this time."
    assert classify_location(J(description=d))[0] == "drop"


def test_office_days_flagged():
    status, why = classify_location(J(description="You will work 3 days per week in the office."))
    assert status == "flag" and "office" in why


# ---- fix round: drops only on explicit signals ------------------------------------------------
@pytest.mark.parametrize("d", [
    "Acme is a company based in San Francisco, California and New York, New York.",
    "For residents of California, Colorado and Illinois, please see our privacy notice.",
    "We have team members located in Colorado, California and Washington who work remotely across the US.",
    "Pay range for candidates based in California, New York City, Colorado: $190k-$230k",
    "Candidates residing in California, Colorado, New York, or Washington may see a range of $180,000 - $220,000.",
])
def test_boilerplate_state_lists_never_drop(d):
    assert classify_location(J(description=d))[0] != "drop"


def test_explicit_restriction_phrases_drop():
    d = "Candidates must reside in one of the following states: Arizona, Colorado, Florida, Georgia."
    assert classify_location(J(description=d))[0] == "drop"
    assert classify_location(J(description="We are only accepting candidates in Arizona, Colorado or Utah."))[0] == "drop"
    assert classify_location(J(description="This role is open only to residents of Ohio, Utah and Idaho."))[0] == "drop"


@pytest.mark.parametrize("loc", [
    "San Francisco, CA; New York, NY; Remote - US",
    "Remote - USA | Boston, MA | Seattle, WA",
    "Remote - Kansas City, Missouri",
    "Remote - Washington, DC",
    "Remote - US (Not available in NY, NJ)",
    "Remote US, excluding CA, NY",
    "United States (Remote) - not CA/NY",
    "Remote - New Mexico",
    "Remote - Nationwide (CA, NY, WA hubs)",
])
def test_location_field_false_drop_probes_ok(loc):
    assert classify_location(J(location=loc))[0] == "ok"


def test_location_exclusion_naming_home_state_dropped():
    assert classify_location(J(location="Remote - US (excluding TX, CA)"))[0] == "drop"


def test_new_york_city_still_counts_as_new_york():
    assert classify_location(J(location="Remote - New York City, California"))[0] == "drop"


def test_classify_location_is_linear_on_huge_description():
    d = "lorem, ipsum " * 8000  # ~104k chars on one line, no sentence breaks
    t = time.perf_counter()
    assert classify_location(J(description=d))[0] == "ok"
    assert time.perf_counter() - t < 2


@pytest.mark.parametrize("loc", [
    "Boston, MA or New York, NY",
    "Denver, CO; Boston, MA",
    "Denver, Colorado / Seattle, Washington",
])
def test_multi_office_location_without_home_state_is_flagged_not_dropped(loc):
    assert classify_location(J(location=loc)) == ("flag", "multi-office location, TX not listed")


@pytest.mark.parametrize("loc", ["Remote - CA, NY, WA", "Remote: California or New York", "Remote (Oregon / Washington)"])
def test_remote_followed_only_by_states_is_a_stated_restriction(loc):
    assert classify_location(J(location=loc))[0] == "drop"


# ---- fix round 3 ------------------------------------------------------------------------------
@pytest.mark.parametrize("loc", [
    "Remote (not hybrid) - Texas",
    "Remote (No Travel) - Dallas, TX",
    "Remote - Not Hybrid, Texas",
    "Remote, no relocation, Texas",
    "Remote - US, No Travel, Texas",
    "Fully Remote - No Travel Required - Texas",
])
def test_negation_words_not_attached_to_states_do_not_drop(loc):
    assert classify_location(J(location=loc))[0] != "drop"


@pytest.mark.parametrize("loc", [
    "Remote - US (excluding TX, CA)",
    "Remote - Texas excluded",
    "Remote - Texas not eligible",
    "Remote - US, not available in Texas",
])
def test_negation_attached_to_home_state_drops(loc):
    assert classify_location(J(location=loc))[0] == "drop"


@pytest.mark.parametrize("d", [
    "Must reside in the United States, including Alaska and Hawaii.",
    "Must live in one of the 50 states, including Washington, Oregon and California.",
    "Must be located in the US (we have hubs in Colorado, Utah).",
    "Must reside in a state where Acme is registered, including California, New York, Colorado and Washington.",
    "Must be located in the contiguous US, such as New York, Georgia, or Florida time zones.",
    "Must be located in the US, e.g. Oregon or Utah.",
])
def test_explicit_restriction_with_us_wide_or_illustrative_words_flags(d):
    assert classify_location(J(description=d))[0] == "flag"


@pytest.mark.parametrize("d", [
    "Must reside in CA, NY, or WA and cannot work from other states.",
    "Must reside in one of the following states: CA, NY, WA, CO (we are unable to hire elsewhere).",
    "Must live in California or Oregon, not able to work outside those states",
    "We are unable to hire in Texas or Florida at this time.",
    "Must reside in one of the following states: CA, NY, WA, CO, IL - $150k pay range",
])
def test_prohibition_words_do_not_undrop_explicit_restrictions(d):
    assert classify_location(J(description=d))[0] == "drop"


def test_exclusion_words_still_undrop():
    assert classify_location(J(description="We can hire in all US states except California and New York."))[0] == "ok"


@pytest.mark.parametrize("d", ["Must reside in NJ.", "Must live in California.", "MUST BE LOCATED IN Ohio"])
def test_explicit_single_state_restriction_drops(d):
    assert classify_location(J(description=d))[0] == "drop"


@pytest.mark.parametrize("d", [
    "Must reside in Texas.",
    "MUST BE LOCATED IN THE US",
    "MUST LIVE IN OR NEAR THE US",
    "Must reside in the US (HQ in NY).",
])
def test_explicit_single_state_guards(d):
    assert classify_location(J(description=d))[0] != "drop"


def test_single_state_in_non_explicit_clause_is_flag_only():
    assert classify_location(J(description="Our team is based in Colorado.")) == (
        "flag", "may require residence in CO")


@pytest.mark.parametrize("loc", [
    "Toronto, Canada; Austin, TX",
    "London, United Kingdom; New York, NY",
    "Singapore; San Francisco, CA",
    "Remote - Ontario, Canada; Remote - Texas",
    "Bangalore, India; Remote - US",
])
def test_merged_multi_region_location_keeps_us_eligible_segment(loc):
    assert classify_location(J(location=loc))[0] != "drop"


def test_rescued_segment_is_flagged_as_multi_region():
    assert classify_location(J(location="Toronto, Canada; Austin, TX")) == (
        "flag", "multi-region posting; kept US-eligible location")


@pytest.mark.parametrize("loc", [
    "Toronto, Canada; London, UK",
    "Remote - CA, NY, WA; Remote - Canada",
])
def test_merged_location_with_no_us_eligible_segment_still_drops(loc):
    assert classify_location(J(location=loc))[0] == "drop"


def test_merged_location_flag_segment_keeps_its_reason_plus_multi_region_note():
    status, why = classify_location(J(location="Toronto, Canada; Remote or Hybrid - Denver"))
    assert status == "flag" and "ambiguity" in why and "multi-region" in why


def test_ok_segments_need_no_multi_region_flag():
    assert classify_location(J(location="Remote - US; Remote - Texas")) == ("ok", "")


def test_merged_location_still_applies_title_hybrid_check_and_description():
    assert classify_location(J(title="Security Engineer (Hybrid)", location="Toronto, Canada; Austin, TX",
                               remote=None))[0] == "drop"
    d = "You must reside in California."
    assert classify_location(J(location="Toronto, Canada; Austin, TX", description=d))[0] == "drop"
    assert classify_location(J(location="Toronto, Canada; Austin, TX", description=d),
                             include_description=False)[0] != "drop"


def test_location_without_semicolon_is_unchanged():
    assert classify_location(J(location="Toronto, Canada")) == ("drop", "outside US")
