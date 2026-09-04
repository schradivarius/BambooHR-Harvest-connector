from __future__ import annotations

import json

import pytest

from pto_recon.matching import (
    OverrideError,
    load_overrides,
    match_employees,
    normalise_email,
)
from pto_recon.models import Employee


def bamboo(bamboo_id, email, name):
    return Employee(bamboo_id=bamboo_id, email=email, display_name=name)


def test_matches_on_lowercased_email():
    result = match_employees(
        [bamboo("142", "jsmith@example.com", "Jonathan Smith")],
        {987654: "JSmith@Example.com"},
    )
    assert len(result.matched) == 1
    assert result.matched[0].harvest_id == 987654
    assert result.unmatched_bamboo == []


def test_whitespace_is_tolerated():
    result = match_employees(
        [bamboo("142", "  jsmith@example.com  ", "Jonathan Smith")],
        {987654: "jsmith@example.com"},
    )
    assert len(result.matched) == 1


def test_different_emails_do_not_match():
    result = match_employees(
        [bamboo("142", "jonathan.smith@example.com", "Jonathan Smith")],
        {987654: "jsmith@example.com"},
    )
    assert result.matched == []
    assert [e.bamboo_id for e in result.unmatched_bamboo] == ["142"]
    assert 987654 in result.unmatched_harvest


def test_names_are_never_used_to_match():
    """"Jon Smith" and "Jonathan Smith" must NOT be paired automatically."""
    result = match_employees(
        [bamboo("142", None, "Jonathan Smith")],
        {987654: "jon.smith@example.com"},
        harvest_names={987654: "Jon Smith"},
    )
    assert result.matched == []
    assert len(result.unmatched_bamboo) == 1


def test_employee_with_no_work_email_is_unmatched():
    result = match_employees([bamboo("207", None, "Dana Wu")], {1: "a@b.com"})
    assert [e.bamboo_id for e in result.unmatched_bamboo] == ["207"]


def test_duplicate_harvest_emails_match_nobody():
    """Two Harvest users on one address: refuse rather than pick one."""
    result = match_employees(
        [bamboo("142", "shared@example.com", "Jonathan Smith")],
        {111: "shared@example.com", 222: "shared@example.com"},
    )
    assert result.matched == []
    assert len(result.unmatched_bamboo) == 1


def test_unmatched_harvest_users_are_reported():
    result = match_employees(
        [bamboo("142", "jsmith@example.com", "Jonathan Smith")],
        {987654: "jsmith@example.com", 111: "contractor@example.com"},
        harvest_names={987654: "Jonathan Smith", 111: "Ada Contractor"},
    )
    assert result.unmatched_harvest == {111: "Ada Contractor"}


# -- overrides ---------------------------------------------------------------


def write_overrides(tmp_path, entries):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"manual_matches": entries}), encoding="utf-8")
    return path


def test_override_pins_a_match(tmp_path):
    path = write_overrides(
        tmp_path,
        [
            {
                "bamboo_employee_id": "142",
                "harvest_user_id": 987654,
                "confirmed_by": "cschrader",
            }
        ],
    )
    overrides = load_overrides(path)
    result = match_employees(
        [bamboo("142", "different@example.com", "Jonathan Smith")],
        {987654: "jon.smith@example.com"},
        overrides=overrides,
    )
    assert len(result.matched) == 1
    assert result.matched[0].harvest_id == 987654


def test_override_beats_email():
    result = match_employees(
        [bamboo("142", "jsmith@example.com", "Jonathan Smith")],
        {987654: "jsmith@example.com", 555: "other@example.com"},
        overrides={"142": 555},
    )
    assert result.matched[0].harvest_id == 555


def test_override_requires_confirmed_by(tmp_path):
    path = write_overrides(
        tmp_path, [{"bamboo_employee_id": "142", "harvest_user_id": 987654}]
    )
    with pytest.raises(OverrideError, match="confirmed_by"):
        load_overrides(path)


def test_override_rejects_double_mapping(tmp_path):
    path = write_overrides(
        tmp_path,
        [
            {"bamboo_employee_id": "142", "harvest_user_id": 1, "confirmed_by": "x"},
            {"bamboo_employee_id": "143", "harvest_user_id": 1, "confirmed_by": "x"},
        ],
    )
    with pytest.raises(OverrideError, match="mapped to both"):
        load_overrides(path)


def test_override_to_nonexistent_harvest_user_is_unmatched():
    result = match_employees(
        [bamboo("142", "jsmith@example.com", "Jonathan Smith")],
        {987654: "jsmith@example.com"},
        overrides={"142": 999999},
    )
    assert result.matched == []
    assert len(result.unmatched_bamboo) == 1


def test_missing_overrides_file_is_fine(tmp_path):
    assert load_overrides(tmp_path / "nope.json") == {}


def test_malformed_overrides_file_raises(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(OverrideError):
        load_overrides(path)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  A@B.COM ", "a@b.com"),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_normalise_email(raw, expected):
    assert normalise_email(raw) == expected
