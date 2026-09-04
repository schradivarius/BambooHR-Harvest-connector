"""Plain data structures shared across the tool.

Everything here is frozen and JSON-serialisable. Hours are ``Decimal``, never
``float`` -- a balance adjustment is money-like, and an auditor asking "why is
this 3.9999999999h" is a conversation worth designing out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Action(str, Enum):
    """What the deterministic core decided to do with one request-day.

    Only the two APPLY_* actions ever reach the BambooHR write endpoint.
    """

    APPLY_CREDIT = "apply_credit"
    APPLY_DEBIT = "apply_debit"
    SKIP_WITHIN_TOLERANCE = "skip_within_tolerance"
    SKIP_ALREADY_RECONCILED = "skip_already_reconciled"
    REVIEW_CREDIT_OVER_THRESHOLD = "review_credit_over_threshold"
    REVIEW_DEBIT_OVER_THRESHOLD = "review_debit_over_threshold"
    REVIEW_UNMATCHED_EMPLOYEE = "review_unmatched_employee"
    REVIEW_BLOCKED_UNCERTAIN = "review_blocked_uncertain"
    REVIEW_ORPHAN_HARVEST = "review_orphan_harvest"

    @property
    def is_write(self) -> bool:
        return self in (Action.APPLY_CREDIT, Action.APPLY_DEBIT)

    @property
    def needs_review(self) -> bool:
        return self.value.startswith("review_")


@dataclass(frozen=True)
class Employee:
    """One person, as known to both systems."""

    bamboo_id: str
    email: str | None
    display_name: str
    harvest_id: int | None = None

    @property
    def is_matched(self) -> bool:
        return self.harvest_id is not None


@dataclass(frozen=True)
class RequestDay:
    """A single day of a single approved BambooHR time-off request.

    BambooHR returns a request spanning a date range with a per-day breakdown
    in its ``dates`` field; we reconcile at day granularity because Harvest
    time entries are per-day.
    """

    request_id: str
    bamboo_employee_id: str
    time_off_type_id: str
    time_off_type_name: str
    date: str  # YYYY-MM-DD
    requested_hours: Decimal

    @property
    def ledger_key(self) -> str:
        return f"{self.request_id}:{self.date}:{self.time_off_type_id}"


@dataclass(frozen=True)
class HarvestHours:
    """Actual logged time-off hours for one person on one day for one type."""

    harvest_user_id: int
    date: str
    time_off_type_name: str
    hours: Decimal
    entry_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Decision:
    """The full, reproducible record of one reconciliation decision.

    This is what gets logged, reported and written to the ledger. It carries
    both inputs and the arithmetic so the decision can be re-derived by hand
    from the record alone.
    """

    action: Action
    reason: str
    request_day: RequestDay | None
    employee: Employee
    requested_hours: Decimal
    actual_hours: Decimal
    delta: Decimal
    harvest_entry_ids: tuple[int, ...] = ()
    date: str = ""
    time_off_type_name: str = ""

    def __post_init__(self) -> None:
        # Orphan rows have no request_day, so date/type are carried directly.
        if self.request_day is not None:
            object.__setattr__(self, "date", self.request_day.date)
            object.__setattr__(
                self, "time_off_type_name", self.request_day.time_off_type_name
            )

    @property
    def ledger_key(self) -> str:
        if self.request_day is not None:
            return self.request_day.ledger_key
        return f"orphan:{self.employee.bamboo_id}:{self.date}:{self.time_off_type_name}"

    def audit_sentence(self) -> str:
        """One line an HR person or auditor can read without context."""
        return (
            f"BambooHR showed {self.requested_hours}h approved time off "
            f"({self.time_off_type_name}) on {self.date}; Harvest showed "
            f"{self.actual_hours}h logged. "
            f"{self.requested_hours} - {self.actual_hours} = {self.delta}h."
        )


@dataclass
class RunSummary:
    """Aggregate outcome of one reconciliation run."""

    run_id: str
    start: str
    end: str
    dry_run: bool
    decisions: list[Decision] = field(default_factory=list)
    applied: list[Decision] = field(default_factory=list)
    failed: list[tuple[Decision, str]] = field(default_factory=list)

    def by_action(self, action: Action) -> list[Decision]:
        return [d for d in self.decisions if d.action is action]

    @property
    def needs_review(self) -> list[Decision]:
        return [d for d in self.decisions if d.action.needs_review]

    @property
    def unmatched(self) -> list[Decision]:
        return self.by_action(Action.REVIEW_UNMATCHED_EMPLOYEE)

    @property
    def writes_intended(self) -> list[Decision]:
        return [d for d in self.decisions if d.action.is_write]
