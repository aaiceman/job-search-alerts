import re

from jobsearch import filters, mentions
from jobsearch.mentions import clause_mentions

WORD = re.compile(r"\bfoo\b", re.I)


def test_caps_at_max_snippets_and_default_is_two():
    desc = "foo one. foo two. foo three. foo four"
    assert clause_mentions(desc, WORD) == ["foo one", "foo two"]
    assert clause_mentions(desc, WORD, max_snippets=3) == ["foo one", "foo two", "foo three"]
    assert clause_mentions(desc, WORD, max_snippets=1) == ["foo one"]


def test_dedupes_after_whitespace_collapse():
    desc = "foo   same.\nfoo same;  foo\tsame • foo other"
    assert clause_mentions(desc, WORD) == ["foo same", "foo other"]


def test_splits_on_period_newline_semicolon_and_bullet_only():
    assert clause_mentions("a foo, b foo", WORD) == ["a foo, b foo"]
    assert clause_mentions("x foo.y foo\nz foo;w foo", WORD, max_snippets=9) == ["x foo", "y foo", "z foo", "w foo"]
    assert clause_mentions("a foo•b foo", WORD) == ["a foo", "b foo"]


def test_empty_and_none_input():
    assert clause_mentions("", WORD) == []
    assert clause_mentions(None, WORD) == []
    assert clause_mentions("nothing here", WORD) == []


def test_long_clause_is_windowed_around_the_match():
    desc = "a" * 200 + " foo " + "b" * 200
    (snip,) = clause_mentions(desc, WORD, limit=100)
    assert len(snip) <= 100
    assert "foo" in snip
    assert snip.startswith("a" * 50)  # 60 chars of lead context, trimmed by strip()


def test_limit_controls_the_window_and_short_clauses_are_untouched():
    desc = "a" * 100 + " foo " + "b" * 100
    assert len(clause_mentions(desc, WORD, limit=40)[0]) <= 40
    assert clause_mentions("short foo", WORD, limit=40) == ["short foo"]


def test_skip_hook_receives_clause_and_match():
    seen = []

    def skip(clause, m):
        seen.append((clause, m.group(0)))
        return "no " in clause

    assert clause_mentions("no foo. real foo", WORD, skip=skip) == ["real foo"]
    assert seen == [("no foo", "foo"), (" real foo", "foo")]


def test_skipped_clauses_do_not_count_toward_the_cap():
    desc = "no foo. no foo. foo a. foo b. foo c"
    assert clause_mentions(desc, WORD, skip=lambda c, m: c.strip().startswith("no")) == ["foo a", "foo b"]


def test_prepare_hook_runs_after_truncation_and_before_split():
    # "e.g." would split the clause at its dots; prepare collapses it first.
    assert clause_mentions("see e.g. foo bar", WORD) == ["foo bar"]
    prep = lambda t: t.replace("e.g.", "eg")
    assert clause_mentions("see e.g. foo bar", WORD, prepare=prep) == ["see eg foo bar"]


def test_only_first_50k_chars_are_scanned():
    desc = "x" * mentions.MAX_DESC + ". foo late"
    assert clause_mentions(desc, WORD) == []
    seen = []
    clause_mentions("foo", WORD, prepare=lambda t: seen.append(len(t)) or t)
    assert seen == [3]
    clause_mentions("y" * (mentions.MAX_DESC + 10), WORD, prepare=lambda t: seen.append(len(t)) or t)
    assert seen[-1] == mentions.MAX_DESC


def test_term_may_be_a_callable_returning_a_match():
    def term(clause):
        return re.search(r"\d+", clause)

    assert clause_mentions("room 12. none. 7 days", term) == ["room 12", "7 days"]


def test_filters_reexports_moved_names():
    for name in ("travel_mentions", "oncall_mentions", "degree_mentions", "clearance_required",
                 "clearance_mentioned", "education_check", "contract_terms_mentioned",
                 "office_attendance_mentioned", "clause_mentions"):
        assert getattr(filters, name) is getattr(mentions, name)


def test_clearance_mentioned_matches_whole_words_only():
    assert mentions.clearance_mentioned("Security clearance is a plus")
    assert mentions.clearance_mentioned("TS/SCI")
    assert not mentions.clearance_mentioned("foreclearance")
    assert not mentions.clearance_mentioned("")
