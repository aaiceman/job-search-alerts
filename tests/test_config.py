from jobsearch import config


def test_data_path_uses_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert config.data_path("runs", "2026-09-30") == tmp_path / "runs" / "2026-09-30"


def test_load_yaml_empty_file_returns_dict(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("", encoding="utf-8")
    assert config.load_yaml(p) == {}


def test_app_dir_contains_package():
    assert (config.APP_DIR / "jobsearch" / "config.py").exists()


def test_load_yaml_non_dict_top_level_returns_dict(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    assert config.load_yaml(p) == {}


# ---- owner settings and pay target (owner values never committed: tests use made-up numbers) ----

import logging

import pytest


def _owner_file(tmp_path, monkeypatch, text):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    if text is not None:
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "owner.yaml").write_text(text, encoding="utf-8")


def test_load_owner_reads_data_dir_owner_yaml(tmp_path, monkeypatch):
    _owner_file(tmp_path, monkeypatch, "current_base_salary: 120000\nmin_raise_pct: 30\n")
    assert config.load_owner() == {"current_base_salary": 120000, "min_raise_pct": 30}


@pytest.mark.parametrize("text", [None, "", "- a\n- b\n", "key: [unclosed\n"])
def test_load_owner_missing_or_invalid_is_empty_with_warning(tmp_path, monkeypatch, caplog, text):
    _owner_file(tmp_path, monkeypatch, text)
    with caplog.at_level(logging.WARNING):
        assert config.load_owner() == {}
    assert "owner.yaml" in caplog.text


def test_pay_target_floor_dominates():
    # 130000 * 1.25 = 162500 < 175000
    assert config.pay_target({"salary_floor": 175000}, {"current_base_salary": 130000, "min_raise_pct": 25}) == 175000


def test_pay_target_base_derived_dominates_when_higher():
    assert config.pay_target({"salary_floor": 175000}, {"current_base_salary": 160000, "min_raise_pct": 25}) == 200000
    assert config.pay_target({"salary_floor": 175000}, {"current_base_salary": 150000, "min_raise_pct": 30}) == 195000


def test_pay_target_raise_pct_defaults_to_25():
    assert config.pay_target({"salary_floor": 100000}, {"current_base_salary": 160000}) == 200000


def test_pay_target_missing_owner_is_floor(tmp_path, monkeypatch):
    _owner_file(tmp_path, monkeypatch, None)
    assert config.pay_target({"salary_floor": 175000}, config.load_owner()) == 175000
    assert config.pay_target({"salary_floor": 175000}, {}) == 175000


@pytest.mark.parametrize("base", ["lots", -5, 0, True, None, [1]])
def test_pay_target_invalid_base_falls_back_to_floor(base):
    assert config.pay_target({"salary_floor": 175000}, {"current_base_salary": base, "min_raise_pct": 25}) == 175000


def test_pay_target_invalid_raise_pct_uses_default():
    assert config.pay_target({"salary_floor": 1}, {"current_base_salary": 160000, "min_raise_pct": "x"}) == 200000


def test_pay_target_without_floor():
    assert config.pay_target({}, {"current_base_salary": 160000, "min_raise_pct": 25}) == 200000
    assert config.pay_target({}, {}) is None
    assert config.pay_target({"ranker": {}}, {}) is None


def test_pay_rule_label_never_contains_base():
    assert config.pay_rule({"current_base_salary": 160000, "min_raise_pct": 25}) == "max(floor, base+25%)"
    assert config.pay_rule({"current_base_salary": 160000, "min_raise_pct": 30}) == "max(floor, base+30%)"
    assert config.pay_rule({}) == "floor"
