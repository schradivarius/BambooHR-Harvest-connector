"""Tests for idempotency and the crash-safety properties the JSON file lacked."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from pto_recon.ledger import Ledger
from pto_recon.models import Action, Decision, Employee, RequestDay


def make_decision(date="2026-08-14", request_id="5001", delta="4.00") -> Decision:
    employee = Employee(
        bamboo_id="142",
        email="jsmith@example.com",
        display_name="Jonathan Smith",
        harvest_id=987654,
    )
    request_day = RequestDay(
        request_id=request_id,
        bamboo_employee_id="142",
        time_off_type_id="78",
        time_off_type_name="PTO",
        date=date,
        requested_hours=Decimal("8.00"),
    )
    return Decision(
        action=Action.APPLY_CREDIT,
        reason="test",
        request_day=request_day,
        employee=employee,
        requested_hours=Decimal("8.00"),
        actual_hours=Decimal("4.00"),
        delta=Decimal(delta),
        harvest_entry_ids=(111,),
    )


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def test_applied_key_is_never_reconciled_twice(ledger):
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "applied")
    assert decision.ledger_key in ledger.reconciled_keys()


def test_pending_row_survives_a_crash_mid_run(tmp_path):
    """The PoC's failure mode: the run dies after writing, before the ledger
    is saved. Here the row is committed before the HTTP call, so it is still
    there afterwards -- and it blocks, rather than silently re-applying."""
    path = tmp_path / "ledger.db"
    decision = make_decision()

    led = Ledger(path)
    led.record_pending(decision, "run-1", "note")
    led.conn.close()  # simulate the process being killed mid-write

    with Ledger(path) as reopened:
        assert decision.ledger_key in reopened.blocked_keys()
        assert decision.ledger_key not in reopened.reconciled_keys()


def test_uncertain_key_blocks_until_cleared(ledger):
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "uncertain", "timeout")
    assert decision.ledger_key in ledger.blocked_keys()

    ledger.clear_uncertain(decision.ledger_key, "failed", "cschrader")
    assert decision.ledger_key not in ledger.blocked_keys()
    assert decision.ledger_key not in ledger.reconciled_keys()


def test_clearing_as_applied_prevents_retry(ledger):
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "uncertain", "timeout")
    ledger.clear_uncertain(decision.ledger_key, "applied", "cschrader")
    assert decision.ledger_key in ledger.reconciled_keys()
    assert decision.ledger_key not in ledger.blocked_keys()


def test_clear_uncertain_records_the_operator(ledger):
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "uncertain", "timeout")
    ledger.clear_uncertain(decision.ledger_key, "applied", "cschrader")
    row = ledger.conn.execute(
        "SELECT error FROM adjustments WHERE id = ?", (row_id,)
    ).fetchone()
    assert "cschrader" in row["error"]


def test_rejected_write_frees_the_key_for_retry(ledger):
    """A 4xx means nothing was applied, so a later run should try again.

    Regression: the unique index used to cover every live row, so the failed
    row kept occupying the key and the retry died on an IntegrityError --
    silently, because the caller treats that as a concurrent claim.
    """
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "failed", "HTTP 400")
    assert decision.ledger_key not in ledger.reconciled_keys()
    assert decision.ledger_key not in ledger.blocked_keys()

    # The retry must actually be able to claim the key.
    retry_id = ledger.record_pending(decision, "run-2", "note")
    ledger.mark(retry_id, "applied")
    assert decision.ledger_key in ledger.reconciled_keys()


def test_clearing_uncertain_as_failed_allows_a_real_retry(ledger):
    """The recovery path the CLI promises: cleared as failed -> retried."""
    decision = make_decision()
    row_id = ledger.record_pending(decision, "run-1", "note")
    ledger.mark(row_id, "uncertain", "timeout")
    ledger.clear_uncertain(decision.ledger_key, "failed", "cschrader")

    retry_id = ledger.record_pending(decision, "run-2", "note")
    ledger.mark(retry_id, "applied")
    assert decision.ledger_key in ledger.reconciled_keys()

    # Both attempts survive as audit history.
    rows = ledger.history_for_employee("142")
    assert [r["status"] for r in rows] == ["failed", "applied"]


def test_an_applied_key_still_cannot_be_claimed_twice(ledger):
    decision = make_decision()
    ledger.mark(ledger.record_pending(decision, "run-1", "note"), "applied")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_pending(decision, "run-2", "note")


def test_concurrent_claim_of_the_same_key_is_rejected(ledger):
    decision = make_decision()
    ledger.record_pending(decision, "run-1", "note")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_pending(decision, "run-2", "note")


def test_dry_runs_do_not_consume_the_key(ledger):
    decision = make_decision()
    ledger.record_dry_run(decision, "run-1", "note")
    ledger.record_dry_run(decision, "run-2", "note")  # repeatable
    assert decision.ledger_key not in ledger.reconciled_keys()
    assert decision.ledger_key not in ledger.blocked_keys()
    # ...and a live run afterwards is still free to write it.
    ledger.record_pending(decision, "run-3", "note")


def test_different_days_of_one_request_are_separate_keys(ledger):
    first = make_decision(date="2026-08-14")
    second = make_decision(date="2026-08-15")
    ledger.mark(ledger.record_pending(first, "run-1", "n"), "applied")
    ledger.mark(ledger.record_pending(second, "run-1", "n"), "applied")
    assert len(ledger.reconciled_keys()) == 2


def test_history_returns_only_live_rows(ledger):
    applied = make_decision(date="2026-08-14")
    preview = make_decision(date="2026-08-20", request_id="5002")
    ledger.mark(ledger.record_pending(applied, "run-1", "n"), "applied")
    ledger.record_dry_run(preview, "run-2", "n")
    history = ledger.history_for_employee("142")
    assert [row["date"] for row in history] == ["2026-08-14"]


def test_total_applied_delta(ledger):
    for i, delta in enumerate(["4.00", "-1.50", "2.25"]):
        decision = make_decision(date=f"2026-08-1{i}", delta=delta)
        ledger.mark(ledger.record_pending(decision, "run-1", "n"), "applied")
    assert ledger.total_applied_delta("run-1") == Decimal("4.75")


def test_run_bookkeeping(ledger):
    ledger.start_run("run-1", "2026-08-01", "2026-08-31", dry_run=True)
    ledger.finish_run("run-1", "completed", {"examined": 3})
    row = ledger.conn.execute(
        "SELECT * FROM runs WHERE run_id = 'run-1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert row["dry_run"] == 1
    assert row["finished_at"] is not None
