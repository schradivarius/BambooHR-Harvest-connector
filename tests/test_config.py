from __future__ import annotations

from decimal import Decimal

import pytest

from pto_recon.config import ConfigError, load_config, redacted

BASE_ENV = {
    "BAMBOO_SUBDOMAIN": "acme",
    "BAMBOO_API_KEY": "secret-key",
    "HARVEST_ACCOUNT_ID": "12345",
    "HARVEST_TOKEN": "secret-token",
    "TIMEOFF_TYPES": "PTO",
    "HARVEST_TASK_NAMES": "PTO",
    "TASK_TYPE_MAP": "PTO=PTO",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(BASE_ENV) + [
        "TOLERANCE_HOURS",
        "MAX_AUTO_CREDIT_HOURS",
        "MAX_AUTO_DEBIT_HOURS",
        "HARVEST_PROJECT_NAMES",
        "LEDGER_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)


def apply_env(monkeypatch, **overrides):
    for key, value in {**BASE_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


@pytest.mark.parametrize("missing", list(BASE_ENV)[:4])
def test_missing_credential_fails_at_startup(monkeypatch, missing):
    apply_env(monkeypatch, **{missing: None})
    with pytest.raises(ConfigError, match=missing):
        load_config()


def test_all_missing_credentials_are_named_at_once(monkeypatch):
    apply_env(
        monkeypatch,
        BAMBOO_API_KEY=None,
        HARVEST_TOKEN=None,
    )
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "BAMBOO_API_KEY" in str(exc.value)
    assert "HARVEST_TOKEN" in str(exc.value)


def test_defaults_are_applied(monkeypatch):
    apply_env(monkeypatch)
    config = load_config()
    assert config.tolerance_hours == Decimal("0.01")
    assert config.max_auto_credit_hours == Decimal("4.0")
    assert config.max_auto_debit_hours == Decimal("2.0")
    assert config.bamboo_base == "https://acme.bamboohr.com/api/v1"


def test_multiple_types_require_an_explicit_map(monkeypatch):
    apply_env(
        monkeypatch,
        TIMEOFF_TYPES="PTO,Vacation",
        HARVEST_TASK_NAMES="PTO,Vacation",
        TASK_TYPE_MAP=None,
    )
    with pytest.raises(ConfigError, match="TASK_TYPE_MAP is required"):
        load_config()


def test_single_type_infers_the_map(monkeypatch):
    apply_env(monkeypatch, TASK_TYPE_MAP=None)
    assert load_config().task_type_map == {"pto": "PTO"}


def test_unmapped_harvest_task_is_rejected(monkeypatch):
    apply_env(
        monkeypatch,
        TIMEOFF_TYPES="PTO,Vacation",
        HARVEST_TASK_NAMES="PTO,Vacation Day",
        TASK_TYPE_MAP="PTO=PTO",
    )
    with pytest.raises(ConfigError, match="no TASK_TYPE_MAP entry"):
        load_config()


def test_map_pointing_at_an_unlisted_type_is_rejected(monkeypatch):
    apply_env(
        monkeypatch,
        TIMEOFF_TYPES="PTO",
        HARVEST_TASK_NAMES="PTO",
        TASK_TYPE_MAP="PTO=Sick",
    )
    with pytest.raises(ConfigError, match="missing from"):
        load_config()


def test_malformed_map_entry_is_rejected(monkeypatch):
    apply_env(monkeypatch, TASK_TYPE_MAP="PTO")
    with pytest.raises(ConfigError, match="not in"):
        load_config()


def test_negative_threshold_is_rejected(monkeypatch):
    apply_env(monkeypatch, MAX_AUTO_CREDIT_HOURS="-1")
    with pytest.raises(ConfigError, match="must not be negative"):
        load_config()


def test_non_numeric_threshold_is_rejected(monkeypatch):
    apply_env(monkeypatch, TOLERANCE_HOURS="four")
    with pytest.raises(ConfigError, match="must be a number"):
        load_config()


def test_zero_debit_threshold_is_allowed(monkeypatch):
    """This is the documented full human-in-the-loop setting."""
    apply_env(monkeypatch, MAX_AUTO_DEBIT_HOURS="0")
    assert load_config().max_auto_debit_hours == 0


def test_secrets_are_redacted_for_logging(monkeypatch):
    apply_env(monkeypatch)
    dump = redacted(load_config())
    assert "secret-key" not in str(dump)
    assert "secret-token" not in str(dump)
    assert dump["bamboo_subdomain"] == "acme"
