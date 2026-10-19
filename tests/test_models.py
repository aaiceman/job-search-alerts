from jobsearch.models import Job, norm_company, norm_title


def test_norm_company_strips_suffixes_and_punctuation():
    assert norm_company("Acme, Inc.") == "acme"
    assert norm_company("Robert Half Technology LLC") == "robert half technology"
    assert norm_company("") == ""


def test_norm_title_canonicalizes():
    assert norm_title("Sr. Security Engineer (Remote)") == "senior security engineer"
    assert norm_title("Senior Security Engineer - Remote, US") == "senior security engineer"


def test_same_job_different_sources_share_key_and_id():
    a = Job(source="linkedin", url="u1", title="Sr. Security Engineer", company="Acme Inc")
    b = Job(source="greenhouse", url="u2", title="Senior Security Engineer (Remote)", company="ACME")
    assert a.key == b.key == "acme|senior security engineer"
    assert a.id == b.id and len(a.id) == 10


def test_round_trip_dict():
    j = Job(source="x", url="u", title="T", company="C", flags=["f"])
    d = j.to_dict()
    assert d["key"] == j.key and d["id"] == j.id
    assert Job.from_dict(d) == j


# ---- regression tests (review fixes) ----

def test_norm_title_keeps_meaningful_parentheticals():
    assert norm_title("Security Engineer (Python)") != norm_title("Security Engineer (Java)")
    assert norm_title("Security Engineer (Python)") == "security engineer python"


def test_norm_title_strips_location_parentheticals():
    assert norm_title("Sr. Security Engineer (Remote)") == "senior security engineer"
    for loc in ("US", "U.S.", "USA", "United States", "Hybrid", "Anywhere", "Americas",
                "North America", "Texas", "TX", "Remote - US", "Remote, TX", ""):
        assert norm_title(f"Security Engineer ({loc})") == "security engineer", loc


def test_norm_title_us_variants_and_trailing_regions():
    assert norm_title("Security Engineer U.S.") == "security engineer"
    assert norm_title("Security Engineer - Americas") == "security engineer"
    assert norm_title("Security Engineer, North America") == "security engineer"
    assert norm_title("Security Engineer (Remote-First)") == "security engineer"
    assert norm_title("Security Engineer - Remote First") == "security engineer"


def test_norm_company_variants():
    assert norm_company("Johnson & Johnson") == norm_company("Johnson and Johnson")
    assert norm_company("The Home Depot") == norm_company("Home Depot") == "home depot"
    assert norm_company("Amazon.com, Inc.") == norm_company("Amazon") == "amazon"
    assert norm_company("Zürich Insurance") == "zurich insurance"
    assert norm_company("株式会社") != ""


def test_key_falls_back_to_url_when_part_empty():
    a = Job(source="x", url="https://a/1", title="", company="Acme")
    b = Job(source="x", url="https://a/2", title="", company="Acme")
    c = Job(source="x", url="https://a/3", title="Engineer", company="")
    assert a.key == "url|https://a/1" and a.key != b.key and a.id != b.id
    assert c.key == "url|https://a/3"


def test_from_dict_ignores_unknown_keys():
    j = Job(source="x", url="u", title="T", company="C")
    d = j.to_dict()
    d["surprise"] = 1
    assert Job.from_dict(d) == j


def test_norm_company_dot_com_only_as_word():
    assert norm_company("Foo.community") == "foo community"
    assert norm_company("Amazon.com, Inc.") == "amazon"


def test_norm_title_folds_accents():
    assert norm_title("Ingénieur Sécurité") == "ingenieur securite"
