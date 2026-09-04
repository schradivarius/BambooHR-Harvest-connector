"""Human-readable and machine-readable run reports.

The dry-run report is the thing someone actually reads before promoting to
``--apply``, so it is organised per employee -- requested vs. actual vs. delta
-- rather than as a stream of per-decision lines.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .matching import MatchResult
from .models import Action, Decision, RunSummary

RULE = "=" * 78
THIN = "-" * 78


def _fmt(value: Decimal) -> str:
    # Width matches the column headers in the per-employee table.
    return f"{value:>10.2f}"


def _signed(value: Decimal) -> str:
    return f"{value:+.2f}"


def render_text_report(summary: RunSummary, match: MatchResult) -> str:
    """The report a human reads to decide whether to promote to --apply."""
    mode = "DRY RUN -- nothing was written" if summary.dry_run else "LIVE"
    lines: list[str] = [
        RULE,
        f"PTO RECONCILIATION  |  {summary.start} to {summary.end}  |  {mode}",
        f"run id: {summary.run_id}",
        RULE,
        "",
    ]

    writes = summary.writes_intended
    review = summary.needs_review
    verb = "would be" if summary.dry_run else "were"

    lines += [
        "SUMMARY",
        THIN,
        f"  Request-days examined      {len(summary.decisions):>5}",
        f"  Adjustments that {verb:<9} {len(writes):>5}",
        f"    credits (hours back)     {len(summary.by_action(Action.APPLY_CREDIT)):>5}",
        f"    debits  (hours taken)    {len(summary.by_action(Action.APPLY_DEBIT)):>5}",
        f"  Needs human review         {len(review):>5}",
        f"  Within tolerance, ignored  "
        f"{len(summary.by_action(Action.SKIP_WITHIN_TOLERANCE)):>5}",
        f"  Already reconciled         "
        f"{len(summary.by_action(Action.SKIP_ALREADY_RECONCILED)):>5}",
    ]
    if not summary.dry_run:
        lines.append(f"  Applied successfully       {len(summary.applied):>5}")
        lines.append(f"  Failed                     {len(summary.failed):>5}")

    net = sum((d.delta for d in writes), Decimal("0.00"))
    lines += [f"  Net hours moved            {net:>+8.2f}", ""]

    # -- per-employee detail ------------------------------------------------
    by_employee: dict[str, list[Decision]] = defaultdict(list)
    for decision in summary.decisions:
        if decision.action in (
            Action.SKIP_ALREADY_RECONCILED,
            Action.SKIP_WITHIN_TOLERANCE,
        ):
            continue
        by_employee[decision.employee.display_name].append(decision)

    if by_employee:
        lines += ["PER EMPLOYEE", THIN]
        for name in sorted(by_employee):
            decisions = sorted(by_employee[name], key=lambda d: d.date)
            employee = decisions[0].employee
            lines.append(f"  {name}  <{employee.email or 'no work email'}>")
            lines.append(
                f"    {'date':<12}{'type':<12}{'requested':>10}"
                f"{'actual':>10}{'delta':>10}  outcome"
            )
            for d in decisions:
                lines.append(
                    f"    {d.date:<12}{d.time_off_type_name[:11]:<12}"
                    f"{_fmt(d.requested_hours)}{_fmt(d.actual_hours)}"
                    f"{_signed(d.delta):>10}  {d.action.value}"
                )
            subtotal = sum(
                (d.delta for d in decisions if d.action.is_write), Decimal("0.00")
            )
            lines.append(f"    {'':<34}{'net:':>10}{subtotal:>+10.2f}")
            lines.append("")

    # -- needs review -------------------------------------------------------
    lines += ["NEEDS REVIEW", THIN]
    if not review:
        lines.append("  Nothing flagged.")
    else:
        lines.append(
            "  These were NOT written. Each needs a person to look before the"
        )
        lines.append("  balance can be corrected.")
        lines.append("")
        for action in (
            Action.REVIEW_BLOCKED_UNCERTAIN,
            Action.REVIEW_CREDIT_OVER_THRESHOLD,
            Action.REVIEW_DEBIT_OVER_THRESHOLD,
            Action.REVIEW_ORPHAN_HARVEST,
            Action.REVIEW_UNMATCHED_EMPLOYEE,
        ):
            group = summary.by_action(action)
            if not group:
                continue
            lines.append(f"  [{action.value}]  {len(group)}")
            for d in sorted(group, key=lambda x: (x.employee.display_name, x.date)):
                lines.append(
                    f"    {d.date}  {d.employee.display_name:<24} "
                    f"requested {d.requested_hours:>6.2f}  "
                    f"actual {d.actual_hours:>6.2f}  "
                    f"delta {d.delta:>+7.2f}"
                )
                lines.append(f"        {d.reason}")
                if d.harvest_entry_ids:
                    lines.append(
                        "        harvest entries: "
                        + ", ".join(str(i) for i in d.harvest_entry_ids)
                    )
            lines.append("")
    lines.append("")

    # -- unmatched people ---------------------------------------------------
    lines += ["UNMATCHED EMPLOYEES", THIN]
    if not match.unmatched_bamboo and not match.unmatched_harvest:
        lines.append("  Every employee matched on work email.")
    else:
        if match.unmatched_bamboo:
            lines.append(
                f"  {len(match.unmatched_bamboo)} BambooHR employee(s) with no "
                "Harvest user. Their PTO cannot be"
            )
            lines.append("  reconciled until this is resolved:")
            for e in sorted(match.unmatched_bamboo, key=lambda x: x.display_name):
                lines.append(
                    f"    bamboo id {e.bamboo_id:<8} {e.display_name:<28} "
                    f"<{e.email or 'NO WORK EMAIL'}>"
                )
            lines.append("")
        if match.unmatched_harvest:
            lines.append(
                f"  {len(match.unmatched_harvest)} Harvest user(s) with no "
                "BambooHR employee:"
            )
            for harvest_id, label in sorted(
                match.unmatched_harvest.items(), key=lambda kv: kv[1]
            ):
                lines.append(f"    harvest id {harvest_id:<10} {label}")
            lines.append("")
        lines.append(
            "  To resolve: confirm the correct pairing yourself, then add it to"
        )
        lines.append(
            "  overrides.json (see README). Matching is never guessed."
        )
    lines.append("")

    if summary.failed:
        lines += ["FAILURES", THIN]
        for decision, error in summary.failed:
            lines.append(
                f"  {decision.date}  {decision.employee.display_name}: {error}"
            )
        lines.append("")

    lines += [RULE]
    if summary.dry_run:
        lines.append(
            "Dry run. Re-run with --apply to write the adjustments listed above."
        )
    lines.append(RULE)
    return "\n".join(lines)


def render_json_report(summary: RunSummary, match: MatchResult) -> dict:
    """Machine-readable output. This is the Phase 2 advisory tool's input."""
    return {
        "run_id": summary.run_id,
        "period": {"start": summary.start, "end": summary.end},
        "dry_run": summary.dry_run,
        "counts": {
            "examined": len(summary.decisions),
            "writes_intended": len(summary.writes_intended),
            "needs_review": len(summary.needs_review),
            "applied": len(summary.applied),
            "failed": len(summary.failed),
        },
        "decisions": [
            {
                "action": d.action.value,
                "reason": d.reason,
                "date": d.date,
                "time_off_type": d.time_off_type_name,
                "request_id": d.request_day.request_id if d.request_day else None,
                "employee": {
                    "bamboo_id": d.employee.bamboo_id,
                    "harvest_id": d.employee.harvest_id,
                    "name": d.employee.display_name,
                    "email": d.employee.email,
                },
                "requested_hours": str(d.requested_hours),
                "actual_hours": str(d.actual_hours),
                "delta": str(d.delta),
                "harvest_entry_ids": list(d.harvest_entry_ids),
            }
            for d in summary.decisions
        ],
        "unmatched_bamboo": [
            {
                "bamboo_id": e.bamboo_id,
                "name": e.display_name,
                "email": e.email,
            }
            for e in match.unmatched_bamboo
        ],
        "unmatched_harvest": [
            {"harvest_id": hid, "label": label}
            for hid, label in match.unmatched_harvest.items()
        ],
        "failures": [
            {
                "date": d.date,
                "bamboo_id": d.employee.bamboo_id,
                "delta": str(d.delta),
                "error": err,
            }
            for d, err in summary.failed
        ],
    }


def write_reports(
    report_dir: Path, summary: RunSummary, match: MatchResult
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "dryrun" if summary.dry_run else "live"
    text_path = report_dir / f"report-{summary.run_id}-{suffix}.txt"
    json_path = report_dir / f"report-{summary.run_id}-{suffix}.json"
    text_path.write_text(render_text_report(summary, match), encoding="utf-8")
    json_path.write_text(
        json.dumps(render_json_report(summary, match), indent=2, default=str),
        encoding="utf-8",
    )
    return text_path, json_path
