"""Run orchestration: fetch everything, decide everything, then write.

The three phases are deliberately separated. All reads finish before any
decision is made, and all decisions are made before any balance is touched,
so a failure while reading can never leave a half-adjusted period behind.

Nothing in this module decides anything. Every decision comes from
``reconcile.decide``; this module only fetches inputs, hands them over, and
carries out what came back.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date as date_cls
from datetime import datetime, timezone
from decimal import Decimal

from .bamboo import BambooClient
from .config import Config
from .harvest import HarvestClient
from .http import (
    AmbiguousWriteError,
    ApiError,
    build_read_session,
    build_write_session,
)
from .ledger import Ledger
from .logging_setup import log_event
from .matching import MatchResult, match_employees
from .models import Action, Decision, Employee, RunSummary
from .reconcile import build_adjustment_note, decide, find_orphan_harvest_entries

log = logging.getLogger("pto_recon.runner")


class RunAborted(RuntimeError):
    """The run stopped early. The ledger records exactly how far it got."""


def new_run_id() -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def validate_period(start: str, end: str) -> None:
    try:
        start_date = date_cls.fromisoformat(start)
        end_date = date_cls.fromisoformat(end)
    except ValueError as exc:
        raise ValueError(
            f"Dates must be YYYY-MM-DD; got start={start!r} end={end!r}"
        ) from exc
    if end_date < start_date:
        raise ValueError(f"end ({end}) is before start ({start})")


def run_reconciliation(
    config: Config,
    start: str,
    end: str,
    *,
    dry_run: bool,
    run_id: str,
    ledger: Ledger,
    overrides: dict[str, int],
) -> tuple[RunSummary, MatchResult]:
    validate_period(start, end)

    read_session = build_read_session(
        max_retries=config.http_max_retries,
        backoff_seconds=config.http_backoff_seconds,
        user_agent=config.http_user_agent,
    )
    write_session = build_write_session(
        backoff_seconds=config.http_backoff_seconds,
        user_agent=config.http_user_agent,
    )

    bamboo = BambooClient(config, read_session, write_session)
    harvest = HarvestClient(config, read_session)

    # -- phase 1: read -----------------------------------------------------
    log_event(log, logging.INFO, "Fetching reference data", run_id=run_id)

    type_ids = bamboo.time_off_type_ids()
    log_event(
        log,
        logging.INFO,
        f"Resolved {len(type_ids)} BambooHR time-off type(s)",
        type_ids=type_ids,
    )

    task_ids = harvest.time_off_task_ids()
    project_ids = harvest.project_ids()
    log_event(
        log,
        logging.INFO,
        f"Resolved {len(task_ids)} Harvest time-off task(s)",
        tasks=task_ids,
        project_filter=sorted(project_ids) if project_ids else None,
    )

    bamboo_employees = bamboo.employees()
    harvest_users = harvest.users()
    harvest_names = harvest.user_names()

    match = match_employees(
        bamboo_employees, harvest_users, harvest_names, overrides
    )
    log_event(
        log,
        logging.INFO,
        f"Matched {len(match.matched)} employee(s); "
        f"{len(match.unmatched_bamboo)} BambooHR and "
        f"{len(match.unmatched_harvest)} Harvest unmatched",
        matched=len(match.matched),
        unmatched_bamboo=[e.bamboo_id for e in match.unmatched_bamboo],
        unmatched_harvest=sorted(match.unmatched_harvest),
        overrides_applied=len(overrides),
    )

    request_days = bamboo.approved_request_days(start, end, type_ids)
    harvest_hours = harvest.time_off_hours(start, end, task_ids, project_ids)
    log_event(
        log,
        logging.INFO,
        f"Fetched {len(request_days)} approved request-day(s) and "
        f"{len(harvest_hours)} Harvest time-off total(s)",
        request_days=len(request_days),
        harvest_totals=len(harvest_hours),
    )

    # -- phase 2: decide (pure) -------------------------------------------
    actuals = {
        (h.harvest_user_id, h.date, h.time_off_type_name.lower()): h
        for h in harvest_hours
    }
    employees_by_bamboo_id = match.by_bamboo_id
    reconciled = ledger.reconciled_keys()
    blocked = ledger.blocked_keys()

    summary = RunSummary(run_id=run_id, start=start, end=end, dry_run=dry_run)

    for request_day in request_days:
        employee = employees_by_bamboo_id.get(request_day.bamboo_employee_id)
        if employee is None:
            # In a BambooHR request but not in the directory (e.g. terminated
            # mid-period). Treated as unmatched, never as zero hours.
            employee = Employee(
                bamboo_id=request_day.bamboo_employee_id,
                email=None,
                display_name=f"unknown employee {request_day.bamboo_employee_id}",
            )

        hh = None
        if employee.harvest_id is not None:
            hh = actuals.get(
                (
                    employee.harvest_id,
                    request_day.date,
                    request_day.time_off_type_name.lower(),
                )
            )

        summary.decisions.append(
            decide(
                request_day,
                employee,
                hh.hours if hh else Decimal("0"),
                tolerance_hours=config.tolerance_hours,
                max_auto_credit_hours=config.max_auto_credit_hours,
                max_auto_debit_hours=config.max_auto_debit_hours,
                harvest_entry_ids=hh.entry_ids if hh else (),
                already_reconciled=request_day.ledger_key in reconciled,
                blocked_uncertain=request_day.ledger_key in blocked,
            )
        )

    summary.decisions.extend(
        find_orphan_harvest_entries(
            harvest_hours,
            request_days,
            match.by_harvest_id,
            tolerance_hours=config.tolerance_hours,
        )
    )

    for decision in summary.decisions:
        log_event(
            log,
            logging.DEBUG if decision.action.value.startswith("skip") else logging.INFO,
            f"{decision.action.value}: {decision.employee.display_name} "
            f"{decision.date} delta {decision.delta:+.2f}h",
            run_id=run_id,
            action=decision.action.value,
            reason=decision.reason,
            bamboo_employee_id=decision.employee.bamboo_id,
            date=decision.date,
            requested_hours=str(decision.requested_hours),
            actual_hours=str(decision.actual_hours),
            delta=str(decision.delta),
            harvest_entry_ids=list(decision.harvest_entry_ids),
        )

    # -- phase 3: write ----------------------------------------------------
    writes = summary.writes_intended
    if dry_run:
        for decision in writes:
            ledger.record_dry_run(
                decision, run_id, build_adjustment_note(decision, run_id)
            )
        log_event(
            log,
            logging.INFO,
            f"Dry run complete: {len(writes)} adjustment(s) would be applied",
            run_id=run_id,
        )
        return summary, match

    _apply_writes(bamboo, ledger, summary, writes, run_id)
    return summary, match


def _apply_writes(
    bamboo: BambooClient,
    ledger: Ledger,
    summary: RunSummary,
    writes: list[Decision],
    run_id: str,
) -> None:
    """Apply adjustments one at a time, recording each before and after.

    An ambiguous outcome aborts the rest of the run: if the API just failed in
    a way that leaves one balance in an unknown state, pressing on risks
    producing several more.
    """
    import sqlite3

    for decision in writes:
        assert decision.request_day is not None  # only request-days are written
        note = build_adjustment_note(decision, run_id)

        try:
            row_id = ledger.record_pending(decision, run_id, note)
        except sqlite3.IntegrityError:
            # Another run claimed this key between our read and this write.
            # Recorded as a failure so it appears in the report rather than
            # disappearing -- the adjustment did not happen on this run.
            message = (
                "Another run claimed this request-day between this run's "
                "ledger read and its write. No adjustment was made here; "
                "check the other run's outcome."
            )
            summary.failed.append((decision, message))
            log_event(
                log,
                logging.WARNING,
                f"Skipping {decision.ledger_key}: claimed by a concurrent run",
                run_id=run_id,
                ledger_key=decision.ledger_key,
            )
            continue

        try:
            bamboo.apply_adjustment(
                decision.employee.bamboo_id,
                decision.date,
                decision.request_day.time_off_type_id,
                decision.delta,
                note,
            )
        except AmbiguousWriteError as exc:
            ledger.mark(row_id, "uncertain", str(exc))
            log_event(
                log,
                logging.ERROR,
                "Adjustment outcome UNKNOWN -- aborting run",
                run_id=run_id,
                ledger_key=decision.ledger_key,
                bamboo_employee_id=decision.employee.bamboo_id,
                date=decision.date,
                delta=str(decision.delta),
                error=str(exc),
            )
            summary.failed.append((decision, str(exc)))
            raise RunAborted(
                f"Could not confirm whether an adjustment applied for "
                f"{decision.employee.display_name} on {decision.date} "
                f"({decision.delta:+.2f}h). It is recorded as 'uncertain' and "
                f"blocked from retry. Check BambooHR, then run "
                f"`pto-reconcile clear-uncertain`. "
                f"{len(summary.applied)} adjustment(s) were applied before this "
                f"point and are recorded in the ledger. Underlying error: {exc}"
            ) from exc
        except ApiError as exc:
            # Rejected outright: the balance did not move, so the key is freed
            # for a later run to retry.
            ledger.mark(row_id, "failed", str(exc))
            summary.failed.append((decision, str(exc)))
            log_event(
                log,
                logging.ERROR,
                f"Adjustment rejected for {decision.employee.display_name} "
                f"on {decision.date}",
                run_id=run_id,
                ledger_key=decision.ledger_key,
                error=str(exc),
            )
            continue

        ledger.mark(row_id, "applied")
        summary.applied.append(decision)
        log_event(
            log,
            logging.INFO,
            f"Applied {decision.delta:+.2f}h to "
            f"{decision.employee.display_name} on {decision.date}",
            run_id=run_id,
            ledger_key=decision.ledger_key,
            bamboo_employee_id=decision.employee.bamboo_id,
            delta=str(decision.delta),
            note=note,
        )

    if summary.failed:
        log_event(
            log,
            logging.WARNING,
            f"{len(summary.failed)} adjustment(s) were rejected by BambooHR",
            run_id=run_id,
        )
