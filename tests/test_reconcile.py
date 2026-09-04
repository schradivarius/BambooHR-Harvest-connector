"""Tests for the deterministic core.

These cover the scenarios the tool exists to get right, and the ones that
would be dangerous to get wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pto_recon.models import Action, Employee, HarvestHours, RequestDay
from pto_recon.reconcile import (
    build_adjustment_note,
    compute_delta,
    decide,
    find_orphan_harvest_entries,
    quantize_hours,
)


# -- the arithmetic itself ---------------------------------------------------


@pytest.mark.parametrize(
    "requested,actual,expected",
    [
        ("8", "8", "0.00"),
        ("8", "4", "4.00"),  # half day taken -> 4h back
        ("8", "0", "8.00"),  # no timesheet entry -> whole day back
        ("4", "8", "-4.00"),  # took more than requested -> debit
        ("7.5", "7.25", "0.25"),
        ("8", "8.004", "0.00"),  # sub-cent noise rounds away
    ],
)
def test_compute_delta(requested, actual, expected):
    assert compute_delta(Decimal(requested), Decimal(actual)) == Decimal(expected)


def test_delta_is_reproducible():
    """The same two numbers must always give the same answer."""
    for _ in range(100):
        assert compute_delta(Decimal("8"), Decimal("6.66")) == Decimal("1.34")


def test_quantize_rounds_half_up_not_bankers():
    # Banker's rounding would give 0.12 here; a person with a calculator
    # gives 0.13, and the report has to match what they'd check it against.
    assert quantize_hours(Decimal("0.125")) == Decimal("0.13")


# -- exact match -------------------------------------------------------------


def test_exact_match_is_skipped(request_day, matched_employee, thresholds):
    decision = decide(
        request_day, matched_employee, Decimal("8.00"), **thresholds
    )
    assert decision.action is Action.SKIP_WITHIN_TOLERANCE
    assert decision.delta == Decimal("0.00")


# -- actual < requested (credit) --------------------------------------------


def test_actual_less_than_requested_credits(
    request_day, matched_employee, thresholds
):
    decision = decide(request_day, matched_employee, Decimal("4.00"), **thresholds)
    assert decision.action is Action.APPLY_CREDIT
    assert decision.delta == Decimal("4.00")
    assert decision.action.is_write


def test_no_harvest_entry_at_all_credits_whole_day(
    request_day, matched_employee, thresholds
):
    """A matched employee with no time entry logged zero. That is a real
    finding, not missing data -- the whole request comes back."""
    thresholds = {**thresholds, "max_auto_credit_hours": Decimal("8.0")}
    decision = decide(request_day, matched_employee, Decimal("0"), **thresholds)
    assert decision.action is Action.APPLY_CREDIT
    assert decision.delta == Decimal("8.00")


# -- actual > requested (debit) ---------------------------------------------


def test_actual_more_than_requested_debits(matched_employee, thresholds):
    request_day = RequestDay(
        request_id="5002",
        bamboo_employee_id="142",
        time_off_type_id="78",
        time_off_type_name="PTO",
        date="2026-08-14",
        requested_hours=Decimal("4.00"),
    )
    decision = decide(request_day, matched_employee, Decimal("5.50"), **thresholds)
    assert decision.action is Action.APPLY_DEBIT
    assert decision.delta == Decimal("-1.50")


def test_debit_threshold_is_separate_and_tighter(matched_employee, thresholds):
    """A 3h delta auto-applies as a credit but is held for review as a debit."""
    credit_day = RequestDay(
        request_id="1", bamboo_employee_id="142", time_off_type_id="78",
        time_off_type_name="PTO", date="2026-08-14",
        requested_hours=Decimal("8.00"),
    )
    assert decide(
        credit_day, matched_employee, Decimal("5.00"), **thresholds
    ).action is Action.APPLY_CREDIT

    debit_day = RequestDay(
        request_id="2", bamboo_employee_id="142", time_off_type_id="78",
        time_off_type_name="PTO", date="2026-08-15",
        requested_hours=Decimal("5.00"),
    )
    assert decide(
        debit_day, matched_employee, Decimal("8.00"), **thresholds
    ).action is Action.REVIEW_DEBIT_OVER_THRESHOLD


def test_zero_debit_threshold_sends_every_debit_to_review(
    matched_employee, thresholds
):
    """MAX_AUTO_DEBIT_HOURS=0 is the full human-in-the-loop setting."""
    thresholds = {**thresholds, "max_auto_debit_hours": Decimal("0")}
    day = RequestDay(
        request_id="3", bamboo_employee_id="142", time_off_type_id="78",
        time_off_type_name="PTO", date="2026-08-14",
        requested_hours=Decimal("8.00"),
    )
    decision = decide(day, matched_employee, Decimal("8.25"), **thresholds)
    assert decision.action is Action.REVIEW_DEBIT_OVER_THRESHOLD


# -- tolerance ---------------------------------------------------------------


def test_delta_under_tolerance_is_ignored(request_day, matched_employee, thresholds):
    decision = decide(request_day, matched_employee, Decimal("7.995"), **thresholds)
    assert decision.action is Action.SKIP_WITHIN_TOLERANCE


def test_delta_exactly_at_tolerance_is_ignored(matched_employee, thresholds):
    day = RequestDay(
        request_id="4", bamboo_employee_id="142", time_off_type_id="78",
        time_off_type_name="PTO", date="2026-08-14",
        requested_hours=Decimal("8.00"),
    )
    decision = decide(day, matched_employee, Decimal("7.99"), **thresholds)
    assert decision.action is Action.SKIP_WITHIN_TOLERANCE


# -- review threshold --------------------------------------------------------


def test_delta_over_credit_threshold_goes_to_review(
    request_day, matched_employee, thresholds
):
    decision = decide(request_day, matched_employee, Decimal("1.00"), **thresholds)
    assert decision.action is Action.REVIEW_CREDIT_OVER_THRESHOLD
    assert not decision.action.is_write
    assert decision.action.needs_review


def test_delta_exactly_at_credit_threshold_still_applies(
    request_day, matched_employee, thresholds
):
    """4.00h with a 4.0h limit applies; the limit is exceeded, not reached."""
    decision = decide(request_day, matched_employee, Decimal("4.00"), **thresholds)
    assert decision.action is Action.APPLY_CREDIT


# -- unmatched employee ------------------------------------------------------


def test_unmatched_employee_is_never_adjusted(
    request_day, unmatched_employee, thresholds
):
    """The dangerous case: treating 'no Harvest user' as 'logged 0 hours'
    would credit back the employee's entire request."""
    decision = decide(request_day, unmatched_employee, Decimal("0"), **thresholds)
    assert decision.action is Action.REVIEW_UNMATCHED_EMPLOYEE
    assert decision.delta == Decimal("0.00")
    assert not decision.action.is_write


def test_unmatched_beats_a_large_apparent_delta(
    request_day, unmatched_employee, thresholds
):
    decision = decide(request_day, unmatched_employee, Decimal("8"), **thresholds)
    assert decision.action is Action.REVIEW_UNMATCHED_EMPLOYEE


# -- idempotency and blocking ------------------------------------------------


def test_already_reconciled_is_skipped(request_day, matched_employee, thresholds):
    decision = decide(
        request_day,
        matched_employee,
        Decimal("4.00"),
        already_reconciled=True,
        **thresholds,
    )
    assert decision.action is Action.SKIP_ALREADY_RECONCILED
    assert not decision.action.is_write


def test_uncertain_key_blocks_and_outranks_everything(
    request_day, matched_employee, thresholds
):
    decision = decide(
        request_day,
        matched_employee,
        Decimal("4.00"),
        already_reconciled=False,
        blocked_uncertain=True,
        **thresholds,
    )
    assert decision.action is Action.REVIEW_BLOCKED_UNCERTAIN
    assert not decision.action.is_write


# -- orphan Harvest entries --------------------------------------------------


def test_harvest_hours_with_no_request_are_flagged(matched_employee):
    orphans = find_orphan_harvest_entries(
        harvest_hours=[
            HarvestHours(
                harvest_user_id=987654,
                date="2026-08-20",
                time_off_type_name="PTO",
                hours=Decimal("8.00"),
                entry_ids=(111, 112),
            )
        ],
        request_days=[],
        employees_by_harvest_id={987654: matched_employee},
        tolerance_hours=Decimal("0.01"),
    )
    assert len(orphans) == 1
    assert orphans[0].action is Action.REVIEW_ORPHAN_HARVEST
    assert not orphans[0].action.is_write
    assert orphans[0].harvest_entry_ids == (111, 112)


def test_covered_harvest_hours_are_not_flagged(matched_employee, request_day):
    orphans = find_orphan_harvest_entries(
        harvest_hours=[
            HarvestHours(
                harvest_user_id=987654,
                date="2026-08-14",
                time_off_type_name="PTO",
                hours=Decimal("8.00"),
            )
        ],
        request_days=[request_day],
        employees_by_harvest_id={987654: matched_employee},
        tolerance_hours=Decimal("0.01"),
    )
    assert orphans == []


# -- the audit trail ---------------------------------------------------------


def test_note_states_the_arithmetic(request_day, matched_employee, thresholds):
    decision = decide(request_day, matched_employee, Decimal("4.00"), **thresholds)
    note = build_adjustment_note(decision, run_id="20260904T120000Z-abcd1234")
    assert "8.00" in note and "4.00" in note
    assert "5001" in note  # the BambooHR request id
    assert "20260904T120000Z-abcd1234" in note


def test_ledger_key_is_stable(request_day):
    assert request_day.ledger_key == "5001:2026-08-14:78"
