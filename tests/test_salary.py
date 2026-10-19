import pytest

from jobsearch.salary import annualize, find_salary_in_description, parse_salary_text


@pytest.mark.parametrize("amount,interval,expected", [
    (180000, "yearly", 180000),
    (90, "hourly", 187200),
    (15000, "monthly", 180000),
    ("100", "hourly", 208000),
    (0, "yearly", None),
    (None, "yearly", None),
    (float("nan"), "yearly", None),
    (100, "fortnightly", None),
])
def test_annualize(amount, interval, expected):
    assert annualize(amount, interval) == expected


@pytest.mark.parametrize("text,expected", [
    ("$175K", (175000, 175000)),
    ("$175,000 - $210,000", (175000, 210000)),
    ("$160K–$190K", (160000, 190000)),
    ("$160-190K", (160000, 190000)),
    ("$211.4K – $290.6K • Offers Equity", (211400, 290600)),
    ("$85/hr", (176800, 176800)),
    ("$85 - $95 per hour", (176800, 197600)),
    ("up to $200k", (None, 200000)),
    ("$175k+", (175000, None)),
    ("€90,000", None),
    ("$1.2M", None),
    ("", None),
])
def test_parse_salary_text(text, expected):
    assert parse_salary_text(text) == expected


def test_description_salary_needs_context():
    desc = "We raised $500,000 in seed money. The base salary range for this role is $180,000 - $220,000."
    assert find_salary_in_description(desc) == (180000, 220000)


def test_description_ignores_unrelated_amounts():
    assert find_salary_in_description("We manage $500,000,000 in assets for $10 billion clients.") is None


def test_description_single_amount_with_keyword():
    assert find_salary_in_description("Compensation: $190,000 base plus bonus.") == (190000, 190000)


def test_description_hourly_range():
    assert find_salary_in_description("Pay: $90 - $100/hr depending on experience") == (187200, 208000)


# ---- regression tests (review fixes) ----

@pytest.mark.parametrize("text,expected", [
    ("$175,000 - $210,000 Key responsibilities", (175000, 210000)),
    ("$150,000 - $200,000 Kansas City", (150000, 200000)),
    ("$175,000 Kubernetes", (175000, 175000)),
    ("$150,000.00 - $200,000.00", (150000, 200000)),
    ("between $150,000 and $180,000", (150000, 180000)),
    ("$180,000 USD - $200,000 USD", (180000, 200000)),
    ("$60/hr - $80/hr", (124800, 166400)),
    ("$60/hr to $80/hr", (124800, 166400)),
    ("$120,000 per year ($58/hr)", (120000, 120000)),
    ("$15,000/month", (180000, 180000)),
    ("$40,000 per month", (480000, 480000)),
    ("$3,500 per week", (182000, 182000)),
    ("$600/day", (156000, 156000)),
    ("C$150,000 - C$180,000", None),
    ("CAD $150,000", None),
    ("$150,000 CAD", None),
    ("$150,000 - $180,000 CAD", None),
    ("AU$150,000", None),
])
def test_parse_salary_text_regressions(text, expected):
    assert parse_salary_text(text) == expected


@pytest.mark.parametrize("desc,expected", [
    ("equity range $300,000 - $400,000 in RSUs; base salary: $120,000 - $150,000", (120000, 150000)),
    ("Pay: $95,000 - $110,000 / $180,000 - $200,000 (Bay Area)", (95000, 200000)),
    ("Salary is competitive. $50,000 sign-on bonus", None),
    ("Salary range: C$150,000 - C$180,000", None),
    ("Base salary: $150,000 - $180,000 CAD", None),
])
def test_find_salary_in_description_regressions(desc, expected):
    assert find_salary_in_description(desc) == expected


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), True, False])
def test_annualize_rejects_nonfinite_and_bool(bad):
    assert annualize(bad, "yearly") is None


# ---- regression tests (round 2) ----

@pytest.mark.parametrize("desc,expected", [
    ("Salary is $190,000 and $50,000 target", (190000, 190000)),
    ("Base salary: $190,000 and $50,000 bonus", (190000, 190000)),
    ("Salary $190,000 and $20,000 relocation", (190000, 190000)),
    ("Salary between $150,000 and $180,000", (150000, 180000)),
    ("equity range $300,000 - $400,000 in RSUs and base salary: $120,000 - $150,000", (120000, 150000)),
    ("Compensation: base $190,000 and bonus $50,000", (190000, 190000)),
    ("Compensation: $50,000 signing bonus and $190,000 base", (190000, 190000)),
    ("Hourly pay: $60 - $70 depending on experience", (124800, 145600)),
])
def test_description_round2(desc, expected):
    assert find_salary_in_description(desc) == expected


def test_description_and_is_not_a_range_with_hourly_neighbor():
    r = find_salary_in_description("salary $180,000 and $40 an hour")
    assert r != (83200, 180000)
    assert r in (None, (180000, 180000))


def test_and_needs_between_and_never_swaps():
    assert parse_salary_text("between $150,000 and $180,000") == (150000, 180000)
    assert parse_salary_text("$190,000 and $50,000") == (190000, 190000)
    assert parse_salary_text("between $180,000 and $150,000") == (180000, 180000)


@pytest.mark.parametrize("text,expected", [
    ("Hourly rate: $60 - $70", (124800, 145600)),
    ("$85 - $95, paid hourly", (176800, 197600)),
    ("US$150,000 - US$180,000", (150000, 180000)),
    ("US$150,000", (150000, 150000)),
    ("$150,000 - 401k match", (150000, 150000)),
    ("$150,000 - 401(k) match", (150000, 150000)),
    ("AU$150,000 - AU$180,000", None),
])
def test_parse_salary_text_round2(text, expected):
    assert parse_salary_text(text) == expected


def test_description_scan_is_bounded():
    import time
    big = "salary $150,000 - $160,000 " * 40_000  # ~1 MB
    t0 = time.perf_counter()
    assert find_salary_in_description(big) == (150000, 160000)
    assert time.perf_counter() - t0 < 2


# ---- regression tests (round 3) ----

@pytest.mark.parametrize("desc,expected", [
    ("Base salary: $180,000 • Equity • 401(k)", (180000, 180000)),
    ("Compensation: $150,000 - $180,000 • Bonus eligible", (150000, 180000)),
    ("Salary: $150,000 | 401k match", (150000, 150000)),
    ("Salary: $150,000 - Stock options available", (150000, 150000)),
    ("Pay range: $150,000 - $180,000 Benefits: 401k, medical", (150000, 180000)),
    ("Compensation: $180,000 (base) $60,000 (bonus)", (180000, 180000)),
    ("Salary $150,000 - 401k match", (150000, 150000)),
    ("Salary: $150,000 · Equity offered", (150000, 150000)),
    ("Compensation summary. Equity: $300,000 - $400,000", None),  # ':' stays off the before side
])
def test_description_exclusion_window_cuts(desc, expected):
    assert find_salary_in_description(desc) == expected


def test_parse_salary_text_long_input_is_fast():
    import time
    t0 = time.perf_counter()
    parse_salary_text("hourly " + "$1 - " * 100000)
    assert time.perf_counter() - t0 < 2
