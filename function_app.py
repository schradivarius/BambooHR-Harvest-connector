"""Azure Functions entry points.

Two triggers:

  reconcile_timer  -- scheduled monthly run (see PTO_SCHEDULE)
  reconcile_manual -- HTTP trigger for an on-demand dry run

The timer runs in DRY RUN mode unless the app setting ``PTO_APPLY`` is
exactly "true". Deploying the function does not, by itself, give it the
ability to change anyone's balance -- that takes a second, deliberate setting
change. The HTTP trigger can never apply; it is a preview only.

The advisory tool (``pto_advisory``) is deliberately NOT deployed here. It
belongs in a separate Function App without BambooHR credentials -- see the
README's deployment section.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

import azure.functions as func

from pto_recon.config import ConfigError, load_config
from pto_recon.ledger import Ledger
from pto_recon.logging_setup import setup_logging
from pto_recon.matching import load_overrides
from pto_recon.report import render_json_report, render_text_report
from pto_recon.runner import RunAborted, new_run_id, run_reconciliation

app = func.FunctionApp()

# NCRONTAB: {second} {minute} {hour} {day} {month} {day-of-week}
# Default: 06:00 UTC on the 5th of each month -- a few days after a monthly
# pay period closes, so Harvest timesheets are actually submitted.
SCHEDULE = os.environ.get("PTO_SCHEDULE", "0 0 6 5 * *")


def previous_month(today: date) -> tuple[str, str]:
    """First and last day of the calendar month before ``today``."""
    first_of_this_month = today.replace(day=1)
    last_of_previous = first_of_this_month - timedelta(days=1)
    return (
        last_of_previous.replace(day=1).isoformat(),
        last_of_previous.isoformat(),
    )


def _execute(dry_run: bool) -> dict:
    config = load_config()
    run_id = new_run_id()
    logger, _ = setup_logging(config.log_dir, run_id)
    start, end = previous_month(date.today())

    overrides = load_overrides(
        config.ledger_path.parent / "overrides.json"
    )

    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(run_id, start, end, dry_run=dry_run)
        try:
            summary, match = run_reconciliation(
                config,
                start,
                end,
                dry_run=dry_run,
                run_id=run_id,
                ledger=ledger,
                overrides=overrides,
            )
        except RunAborted as exc:
            ledger.finish_run(run_id, "aborted", {}, str(exc))
            raise

        report = render_json_report(summary, match)
        ledger.finish_run(run_id, "completed", report["counts"])

        config.report_dir.mkdir(parents=True, exist_ok=True)
        suffix = "dryrun" if dry_run else "live"
        (config.report_dir / f"report-{run_id}-{suffix}.txt").write_text(
            render_text_report(summary, match), encoding="utf-8"
        )
        (config.report_dir / f"report-{run_id}-{suffix}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )

    logging.info(
        "PTO reconciliation %s complete: %s", run_id, json.dumps(report["counts"])
    )
    return report


@app.timer_trigger(
    schedule=SCHEDULE, arg_name="timer", run_on_startup=False, use_monitor=True
)
def reconcile_timer(timer: func.TimerRequest) -> None:
    # Opt in explicitly. An accidental deploy reconciles nothing.
    dry_run = os.environ.get("PTO_APPLY", "").strip().lower() != "true"
    if timer.past_due:
        logging.warning("PTO reconciliation timer is past due")

    try:
        report = _execute(dry_run=dry_run)
    except ConfigError as exc:
        # Fail loudly: a misconfigured scheduled job that logs and returns is
        # a job nobody notices has stopped working.
        logging.error("PTO reconciliation misconfigured: %s", exc)
        raise
    except RunAborted as exc:
        logging.error("PTO reconciliation aborted: %s", exc)
        raise

    if report["counts"]["needs_review"]:
        logging.warning(
            "PTO reconciliation %s: %d item(s) need human review",
            report["run_id"],
            report["counts"]["needs_review"],
        )


@app.route(route="reconcile/preview", auth_level=func.AuthLevel.FUNCTION)
def reconcile_manual(req: func.HttpRequest) -> func.HttpResponse:
    """On-demand dry run. Never writes, regardless of PTO_APPLY."""
    try:
        report = _execute(dry_run=True)
    except (ConfigError, RunAborted) as exc:
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500,
            mimetype="application/json",
        )
    return func.HttpResponse(
        json.dumps(report, indent=2, default=str),
        status_code=200,
        mimetype="application/json",
    )
