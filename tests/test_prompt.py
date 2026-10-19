from jobsearch.config import PROMPTS_DIR

PROMPT = (PROMPTS_DIR / "rank.md").read_text(encoding="utf-8")


def _dealbreakers() -> str:
    return PROMPT.split("## Dealbreakers", 1)[1].split("## Pass A", 1)[0]


def test_oncall_rotation_is_a_dealbreaker():
    d = _dealbreakers()
    assert "requires an on-call rotation" in d and "`on-call:` flags" in d
    assert "Occasional after-hours help without a rotation is NOT a dealbreaker" in d


def test_staffing_agency_posting_is_a_dealbreaker():
    d = _dealbreakers()
    assert "staffing agency, recruiter, or job-reposting platform" in d
    assert "Swooped" in d and "Jobgether" in d


def test_degree_field_is_documented():
    produce = PROMPT.split("For each job produce", 1)[1].split("## Pass B", 1)[0]
    assert "`degree`" in produce
    for v in ("none", "associate", "bachelor", "master", "phd", "unspecified", "required", "preferred",
              "not_mentioned", "yes", "no", "unknown"):
        assert f"`{v}`" in produce, v
    assert "pay_estimated, pay_min, pay_max, degree}" in PROMPT.split("## Output", 1)[1]
    assert "A Master's degree or PhD is **required**" in _dealbreakers()


def test_pay_min_max_documented():
    produce = PROMPT.split("For each job produce", 1)[1].split("## Pass B", 1)[0]
    assert "`pay_min`" in produce and "`pay_max`" in produce
    assert "annual USD" in produce and "annualize" in produce
    assert "stated range" in produce and "estimate" in produce


def _section(start: str, end: str) -> str:
    return PROMPT.split(start, 1)[1].split(end, 1)[0]


def test_pay_text_has_no_base_comparison():
    produce = _section("For each job produce", "## Pass B")
    pay_line = next(line for line in produce.splitlines() if line.startswith("- `pay`:"))
    assert '"$180k–$210k stated"' in pay_line and '"~$170k–$200k est."' in pay_line
    assert "vs current base" not in PROMPT
    assert "base salary" not in _section("## Inputs", "## Security rule")
    assert "$175k" not in _section("## Pass A", "For each job produce")
    assert "the pay target given in the task message" in _section("## Pass A", "For each job produce")


def test_pass_b_is_scoped_to_shown_jobs():
    b = _section("## Pass B", "## Output")
    for s in ("≥ 75", "DEALBREAKER", "`app_dev`", "`other`", "pay target", "At most 8 companies",
              "company_cache.json", "at most 6 web calls per company"):
        assert s in b, s
    assert "≥ 60" not in b and "up to 15" not in b


def test_confirmed_label_allows_authoritative_snippets():
    b = _section("## Pass B", "## Output")
    for d in ("levels.fyi", "glassdoor.com", "indeed.com", "comparably.com", "builtin.com"):
        assert d in b, d
    assert "careers/benefits page" in b and "`source`" in b
    assert "facts seen only in search-result snippets are `Likely`" not in b


def test_dealbreaker_keys_are_canonical():
    d = _dealbreakers()
    assert '"DEALBREAKER: <key> — <short quote>"' in d
    for k in ("travel", "on-call", "onsite", "clearance", "degree", "contract", "staffing", "residency"):
        assert f"`{k}`" in d, k
