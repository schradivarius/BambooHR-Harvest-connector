"""SQLite idempotency ledger and audit trail.

Replaces the PoC's flat JSON file, which was only written once at the very end
of a run: a crash after the twelfth of thirty adjustments left nothing on
disk, and the next run reapplied all twelve.

Here each adjustment is recorded ``pending`` *before* the HTTP call and
updated to ``applied``/``failed``/``uncertain`` immediately after, each in its
own committed transaction. An interrupted run therefore leaves an accurate
record of exactly how far it got.

Status meanings:
  pending    -- write started, outcome not yet recorded (only seen if the
                process was killed mid-write; treated as uncertain)
  applied    -- BambooHR confirmed 2xx. Never retried.
  failed     -- BambooHR rejected it (4xx). Nothing applied; safe to retry.
  uncertain  -- outcome unknown. BLOCKS the key until a human clears it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .models import Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS adjustments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_key          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    request_id          TEXT,
    date                TEXT    NOT NULL,
    bamboo_employee_id  TEXT    NOT NULL,
    employee_name       TEXT,
    employee_email      TEXT,
    time_off_type_id    TEXT,
    time_off_type_name  TEXT,
    requested_hours     TEXT    NOT NULL,
    actual_hours        TEXT    NOT NULL,
    delta               TEXT    NOT NULL,
    action              TEXT    NOT NULL,
    reason              TEXT,
    harvest_entry_ids   TEXT,
    note                TEXT,
    status              TEXT    NOT NULL,
    dry_run             INTEGER NOT NULL,
    error               TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

-- One *effective* live adjustment per request-day, ever.
--
-- The index deliberately excludes 'failed' rows. A 4xx rejection means the
-- balance never moved, so that key must be free for a later run to retry --
-- but the row stays in the table as audit history. Covering failed rows here
-- would silently block the retry that `clear-uncertain --resolution failed`
-- promises. Dry runs are exempt so previews can be re-run freely.
DROP INDEX IF EXISTS ux_adjustments_live;
CREATE UNIQUE INDEX IF NOT EXISTS ux_adjustments_live
    ON adjustments (ledger_key)
    WHERE dry_run = 0 AND status IN ('pending', 'applied', 'uncertain');

CREATE INDEX IF NOT EXISTS ix_adjustments_employee_date
    ON adjustments (bamboo_employee_id, date);

CREATE INDEX IF NOT EXISTS ix_adjustments_run ON adjustments (run_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    dry_run      INTEGER NOT NULL,
    status       TEXT NOT NULL,
    summary_json TEXT,
    error        TEXT
);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class Ledger:
    """Thin wrapper over SQLite. One instance per run."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps readers unblocked and survives an abrupt process exit far
        # better than the default rollback journal.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE takes the write lock up front, so two concurrent
        runs serialise cleanly instead of one dying on a mid-transaction
        upgrade."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- run bookkeeping ---------------------------------------------------

    def start_run(
        self, run_id: str, period_start: str, period_end: str, dry_run: bool
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, period_start, period_end,"
                " dry_run, status) VALUES (?, ?, ?, ?, ?, 'running')",
                (run_id, _now(), period_start, period_end, int(dry_run)),
            )

    def finish_run(
        self, run_id: str, status: str, summary: dict, error: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, summary_json = ?,"
                " error = ? WHERE run_id = ?",
                (_now(), status, json.dumps(summary, default=str), error, run_id),
            )

    # -- idempotency -------------------------------------------------------

    def reconciled_keys(self) -> set[str]:
        """Keys already applied live. These are never touched again."""
        rows = self.conn.execute(
            "SELECT ledger_key FROM adjustments WHERE dry_run = 0 AND status = 'applied'"
        ).fetchall()
        return {row["ledger_key"] for row in rows}

    def blocked_keys(self) -> set[str]:
        """Keys whose outcome is unknown. Blocked pending human confirmation."""
        rows = self.conn.execute(
            "SELECT ledger_key FROM adjustments WHERE dry_run = 0 "
            "AND status IN ('uncertain', 'pending')"
        ).fetchall()
        return {row["ledger_key"] for row in rows}

    # -- adjustment lifecycle ---------------------------------------------

    def record_pending(self, decision: Decision, run_id: str, note: str) -> int:
        """Insert the row *before* the HTTP call. Returns the row id.

        A UNIQUE violation here means another run already claimed this key --
        that is the concurrency guard, and the caller should skip the write.
        """
        rd = decision.request_day
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO adjustments (
                    ledger_key, run_id, request_id, date, bamboo_employee_id,
                    employee_name, employee_email, time_off_type_id,
                    time_off_type_name, requested_hours, actual_hours, delta,
                    action, reason, harvest_entry_ids, note, status, dry_run,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,?)
                """,
                (
                    decision.ledger_key,
                    run_id,
                    rd.request_id if rd else None,
                    decision.date,
                    decision.employee.bamboo_id,
                    decision.employee.display_name,
                    decision.employee.email,
                    rd.time_off_type_id if rd else None,
                    decision.time_off_type_name,
                    str(decision.requested_hours),
                    str(decision.actual_hours),
                    str(decision.delta),
                    decision.action.value,
                    decision.reason,
                    json.dumps(list(decision.harvest_entry_ids)),
                    note,
                    _now(),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def mark(self, row_id: int, status: str, error: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE adjustments SET status = ?, error = ?, updated_at = ?"
                " WHERE id = ?",
                (status, error, _now(), row_id),
            )

    def record_dry_run(self, decision: Decision, run_id: str, note: str) -> None:
        """Record what a dry run *would* have done. Never blocks a live run."""
        rd = decision.request_day
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO adjustments (
                    ledger_key, run_id, request_id, date, bamboo_employee_id,
                    employee_name, employee_email, time_off_type_id,
                    time_off_type_name, requested_hours, actual_hours, delta,
                    action, reason, harvest_entry_ids, note, status, dry_run,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'preview',1,?,?)
                """,
                (
                    decision.ledger_key,
                    run_id,
                    rd.request_id if rd else None,
                    decision.date,
                    decision.employee.bamboo_id,
                    decision.employee.display_name,
                    decision.employee.email,
                    rd.time_off_type_id if rd else None,
                    decision.time_off_type_name,
                    str(decision.requested_hours),
                    str(decision.actual_hours),
                    str(decision.delta),
                    decision.action.value,
                    decision.reason,
                    json.dumps(list(decision.harvest_entry_ids)),
                    note,
                    _now(),
                    _now(),
                ),
            )

    # -- operator queries --------------------------------------------------

    def uncertain_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM adjustments WHERE dry_run = 0 AND status IN "
            "('uncertain', 'pending') ORDER BY date, bamboo_employee_id"
        ).fetchall()

    def clear_uncertain(self, ledger_key: str, resolution: str, operator: str) -> int:
        """Human confirmation that an uncertain write did or did not apply.

        ``resolution`` is 'applied' (BambooHR shows the adjustment -- leave the
        balance alone) or 'failed' (it does not -- the key frees up for the
        next run to retry).
        """
        if resolution not in ("applied", "failed"):
            raise ValueError("resolution must be 'applied' or 'failed'")
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE adjustments SET status = ?, updated_at = ?,"
                " error = COALESCE(error, '') || ? WHERE ledger_key = ?"
                " AND dry_run = 0 AND status IN ('uncertain', 'pending')",
                (
                    resolution,
                    _now(),
                    f" | manually resolved as {resolution} by {operator} at {_now()}",
                    ledger_key,
                ),
            )
            return cursor.rowcount

    def history_for_employee(
        self, bamboo_employee_id: str
    ) -> list[sqlite3.Row]:
        """Everything ever done to one person's balance -- the query to run
        when HR asks why a balance changed."""
        return self.conn.execute(
            "SELECT * FROM adjustments WHERE bamboo_employee_id = ? AND"
            " dry_run = 0 ORDER BY date, created_at",
            (bamboo_employee_id,),
        ).fetchall()

    def total_applied_delta(self, run_id: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT delta FROM adjustments WHERE run_id = ? AND status = 'applied'",
            (run_id,),
        ).fetchall()
        return sum((Decimal(r["delta"]) for r in rows), Decimal("0.00"))
