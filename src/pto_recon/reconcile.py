"""The deterministic reconciliation core.

    requested_hours - actual_hours = delta

That subtraction is the entire decision. This module is deliberately kept
free of network calls, disk access, clocks, randomness and any model call --
it is a pure function of its arguments, so the same two numbers always
produce the same adjustment, and any decision can be reproduced from the
logged inputs alone.

Do not add I/O here. Do not add an LLM here. The callers in ``cli.py`` do the
fetching and the writing; this module only decides.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import Action, Decision, Employee, HarvestHours, RequestDay

# Quantum for all hour arithmetic. Two decimal places matches what BambooHR
# and Harvest both display, so a reported delta always matches what a human
# sees in either UI.
CENTS = Decimal("0.01")


def quantize_hours(value: Decimal) -> Decimal:
    """Round half-up to 2dp. Half-up (not banker's rounding) because that is
    what a person doing this by hand on a calculator would produce."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_delta(requested_hours: Decimal, actual_hours: Decimal) -> Decimal:
    """The whole decision, isolated so it can be tested and cited directly.

    Positive delta -> employee was charged for more time off than they took;
                      hours are credited back to their balance.
    Negative delta -> employee took more time off than was approved;
                      hours are debited from their balance.
    """
    return quantize_hours(quantize_hours(requested_hours) - quantize_hours(actual_hours))


def decide(
    request_day: RequestDay,
    employee: Employee,
    actual_hours: Decimal,
    *,
    tolerance_hours: Decimal,
    max_auto_credit_hours: Decimal,
    max_auto_debit_hours: Decimal,
    harvest_entry_ids: tuple[int, ...] = (),
    already_reconciled: bool = False,
    blocked_uncertain: bool = False,
) -> Decision:
    """Decide what to do about one request-day. Pure function.

    Guard order matters and is intentional:

    1. ``blocked_uncertain`` -- a previous run wrote and could not confirm the
       result. Never touch this key again without a human clearing it, or we
       risk double-applying.
    2. ``already_reconciled`` -- the ledger says this key was handled live.
    3. Unmatched employee -- no Harvest identity, so ``actual_hours`` would be
       a fiction. Never treat "no match" as "logged zero hours"; that would
       silently credit back the employee's entire request.
    4. Tolerance -- rounding noise, not a real discrepancy.
    5. Thresholds -- large deltas usually mean a data problem, not a
       correction.
    """
    requested = quantize_hours(request_day.requested_hours)

    def build(action: Action, reason: str, actual: Decimal, delta: Decimal) -> Decision:
        return Decision(
            action=action,
            reason=reason,
            request_day=request_day,
            employee=employee,
            requested_hours=requested,
            actual_hours=actual,
            delta=delta,
            harvest_entry_ids=harvest_entry_ids,
        )

    # (1) A prior write whose outcome we could not confirm.
    if blocked_uncertain:
        return build(
            Action.REVIEW_BLOCKED_UNCERTAIN,
            "A previous run attempted this adjustment but could not confirm "
            "whether BambooHR applied it. Blocked to prevent double-adjusting. "
            "Verify the balance in BambooHR, then clear this key with "
            "`pto-reconcile clear-uncertain`.",
            quantize_hours(actual_hours),
            Decimal("0.00"),
        )

    # (2) Already handled by a live run.
    if already_reconciled:
        return build(
            Action.SKIP_ALREADY_RECONCILED,
            "Already reconciled by an earlier live run.",
            quantize_hours(actual_hours),
            Decimal("0.00"),
        )

    # (3) No Harvest identity -- we do not know the actual hours at all.
    if not employee.is_matched:
        return build(
            Action.REVIEW_UNMATCHED_EMPLOYEE,
            f"No Harvest user matched to BambooHR employee "
            f"{employee.bamboo_id} ({employee.email or 'no work email'}). "
            "Actual hours are unknown, so no adjustment can be computed.",
            Decimal("0.00"),
            Decimal("0.00"),
        )

    actual = quantize_hours(actual_hours)
    delta = compute_delta(requested, actual)

    # (4) Rounding noise.
    if abs(delta) <= quantize_hours(tolerance_hours):
        return build(
            Action.SKIP_WITHIN_TOLERANCE,
            f"Delta of {delta}h is within the {tolerance_hours}h tolerance.",
            actual,
            delta,
        )

    # (5) Thresholds, applied separately per direction.
    if delta > 0:
        if delta > quantize_hours(max_auto_credit_hours):
            return build(
                Action.REVIEW_CREDIT_OVER_THRESHOLD,
                f"Credit of {delta}h exceeds the {max_auto_credit_hours}h "
                "auto-apply limit. A gap this large more often means a missing "
                "or miscategorised timesheet than a real correction.",
                actual,
                delta,
            )
        return build(
            Action.APPLY_CREDIT,
            f"Employee logged {delta}h less time off than approved; "
            "crediting the difference back.",
            actual,
            delta,
        )

    if abs(delta) > quantize_hours(max_auto_debit_hours):
        return build(
            Action.REVIEW_DEBIT_OVER_THRESHOLD,
            f"Debit of {abs(delta)}h exceeds the {max_auto_debit_hours}h "
            "auto-apply limit. Reducing a balance automatically is held to a "
            "tighter limit than crediting one.",
            actual,
            delta,
        )
    return build(
        Action.APPLY_DEBIT,
        f"Employee logged {abs(delta)}h more time off than approved; "
        "debiting the difference.",
        actual,
        delta,
    )


def find_orphan_harvest_entries(
    harvest_hours: list[HarvestHours],
    request_days: list[RequestDay],
    employees_by_harvest_id: dict[int, Employee],
    *,
    tolerance_hours: Decimal,
) -> list[Decision]:
    """Flag time off logged in Harvest with no approved BambooHR request.

    The original script could not see these: it iterated BambooHR requests, so
    a day with hours in Harvest and nothing in BambooHR produced no output at
    all. That is unrequested time off which never gets deducted.

    These are always flagged, never auto-applied -- there is no approved
    request to anchor an adjustment to, and the likeliest causes (wrong task
    selected, PTO logged before the request was filed) are data-entry
    problems a human should look at.
    """
    covered: set[tuple[str, str, str]] = {
        (rd.bamboo_employee_id, rd.date, rd.time_off_type_name.lower())
        for rd in request_days
    }

    orphans: list[Decision] = []
    for hh in sorted(
        harvest_hours, key=lambda h: (h.date, h.harvest_user_id, h.time_off_type_name)
    ):
        employee = employees_by_harvest_id.get(hh.harvest_user_id)
        if employee is None:
            # Harvest user with no BambooHR counterpart -- surfaced by the
            # matching report, not here, to avoid reporting it twice.
            continue
        hours = quantize_hours(hh.hours)
        if hours <= quantize_hours(tolerance_hours):
            continue
        if (employee.bamboo_id, hh.date, hh.time_off_type_name.lower()) in covered:
            continue
        orphans.append(
            Decision(
                action=Action.REVIEW_ORPHAN_HARVEST,
                reason=(
                    f"{hours}h of {hh.time_off_type_name} logged in Harvest on "
                    f"{hh.date} with no approved BambooHR request covering it. "
                    "Not adjusted automatically: there is no request to tie the "
                    "adjustment to."
                ),
                request_day=None,
                employee=employee,
                requested_hours=Decimal("0.00"),
                actual_hours=hours,
                delta=quantize_hours(-hours),
                harvest_entry_ids=hh.entry_ids,
                date=hh.date,
                time_off_type_name=hh.time_off_type_name,
            )
        )
    return orphans


def build_adjustment_note(decision: Decision, run_id: str) -> str:
    """The note written into BambooHR alongside the adjustment.

    This is the text an employee sees when they ask why their balance moved,
    so it states the arithmetic and identifies the run that made it.
    """
    request_id = (
        decision.request_day.request_id if decision.request_day else "n/a"
    )
    return (
        f"Auto-reconciled against Harvest. "
        f"{decision.audit_sentence()} "
        f"[BambooHR request #{request_id}, run {run_id}]"
    )
