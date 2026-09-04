"""Command line entry point.

    pto-reconcile run 2026-08-01 2026-08-31          # dry run (default)
    pto-reconcile run 2026-08-01 2026-08-31 --apply  # write to BambooHR
    pto-reconcile uncertain                          # list blocked keys
    pto-reconcile clear-uncertain KEY --resolution applied --operator you
    pto-reconcile history 142                        # one employee's audit trail

Exit codes:
    0  clean run
    1  configuration or usage error (nothing was read or written)
    2  the run aborted with adjustments in an unknown state -- needs a human
    3  the run completed but some adjustments were rejected
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config, redacted
from .http import ApiError
from .ledger import Ledger
from .logging_setup import log_event, setup_logging
from .matching import OverrideError, load_overrides
from .report import render_text_report, write_reports
from .runner import RunAborted, new_run_id, run_reconciliation

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_UNCERTAIN = 2
EXIT_PARTIAL_FAILURE = 3

DEFAULT_OVERRIDES = Path("overrides.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pto-reconcile",
        description="Reconcile BambooHR time-off balances against Harvest actuals.",
    )
    parser.add_argument("--env-file", type=Path, default=None, help="path to .env")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="reconcile a period")
    run.add_argument("start", help="period start, YYYY-MM-DD")
    run.add_argument("end", help="period end, YYYY-MM-DD")
    run.add_argument(
        "--apply",
        action="store_true",
        help="actually write adjustments to BambooHR (default: dry run)",
    )
    run.add_argument(
        "--overrides",
        type=Path,
        default=DEFAULT_OVERRIDES,
        help="human-confirmed employee matches (default: overrides.json)",
    )

    unc = sub.add_parser("uncertain", help="list adjustments in an unknown state")
    unc.add_argument("--json", action="store_true")

    clear = sub.add_parser(
        "clear-uncertain",
        help="record a human's verdict on an uncertain adjustment",
    )
    clear.add_argument("ledger_key")
    clear.add_argument(
        "--resolution",
        required=True,
        choices=["applied", "failed"],
        help="'applied' if BambooHR shows the adjustment, 'failed' if it does not",
    )
    clear.add_argument(
        "--operator", required=True, help="who verified it, for the audit trail"
    )

    hist = sub.add_parser("history", help="every adjustment for one employee")
    hist.add_argument("bamboo_employee_id")

    return parser


def _cmd_run(
    args: argparse.Namespace, config, logger: logging.Logger, run_id: str
) -> int:
    mode = "LIVE" if args.apply else "DRY RUN"

    try:
        overrides = load_overrides(args.overrides)
    except OverrideError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    log_event(
        logger,
        logging.INFO,
        f"Starting {mode} reconciliation for {args.start} to {args.end}",
        run_id=run_id,
        mode=mode,
        config=redacted(config),
        overrides_file=str(args.overrides),
        overrides_count=len(overrides),
    )

    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(run_id, args.start, args.end, dry_run=not args.apply)
        try:
            summary, match = run_reconciliation(
                config,
                args.start,
                args.end,
                dry_run=not args.apply,
                run_id=run_id,
                ledger=ledger,
                overrides=overrides,
            )
        except RunAborted as exc:
            ledger.finish_run(run_id, "aborted", {}, str(exc))
            log_event(logger, logging.ERROR, "Run aborted", run_id=run_id)
            print(f"\nRUN ABORTED\n\n{exc}\n", file=sys.stderr)
            return EXIT_UNCERTAIN
        except (ApiError, ValueError) as exc:
            ledger.finish_run(run_id, "failed", {}, str(exc))
            log_event(
                logger, logging.ERROR, "Run failed", run_id=run_id, error=str(exc)
            )
            print(f"\nRUN FAILED: {exc}\n", file=sys.stderr)
            return EXIT_CONFIG

        text_path, json_path = write_reports(config.report_dir, summary, match)
        ledger.finish_run(
            run_id,
            "completed",
            {
                "examined": len(summary.decisions),
                "writes_intended": len(summary.writes_intended),
                "applied": len(summary.applied),
                "failed": len(summary.failed),
                "needs_review": len(summary.needs_review),
                "unmatched_bamboo": len(match.unmatched_bamboo),
            },
        )

    print()
    print(render_text_report(summary, match))
    print()
    print(f"report: {text_path}")
    print(f"json:   {json_path}")

    return EXIT_PARTIAL_FAILURE if summary.failed else EXIT_OK


def _cmd_uncertain(args: argparse.Namespace, config) -> int:
    with Ledger(config.ledger_path) as ledger:
        rows = ledger.uncertain_rows()

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return EXIT_OK

    if not rows:
        print("No adjustments are in an uncertain state.")
        return EXIT_OK

    print(f"{len(rows)} adjustment(s) in an unknown state -- these BLOCK future")
    print("reconciliation of the same request-day until resolved.\n")
    for row in rows:
        print(f"  key:      {row['ledger_key']}")
        print(f"  employee: {row['employee_name']} (bamboo id {row['bamboo_employee_id']})")
        print(f"  date:     {row['date']}   delta: {row['delta']}h")
        print(f"  run:      {row['run_id']}   status: {row['status']}")
        print(f"  error:    {(row['error'] or '')[:200]}")
        print()
    print("Check the balance in BambooHR, then record what you found:")
    print("  pto-reconcile clear-uncertain <key> --resolution applied|failed "
          "--operator <you>")
    return EXIT_UNCERTAIN


def _cmd_clear_uncertain(args: argparse.Namespace, config) -> int:
    with Ledger(config.ledger_path) as ledger:
        changed = ledger.clear_uncertain(
            args.ledger_key, args.resolution, args.operator
        )
    if changed == 0:
        print(
            f"No uncertain adjustment with key {args.ledger_key!r}.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    print(f"Recorded {args.ledger_key} as {args.resolution} (by {args.operator}).")
    if args.resolution == "failed":
        print("The next run will reconcile this request-day again.")
    else:
        print("The balance is treated as already corrected; it will not be retried.")
    return EXIT_OK


def _cmd_history(args: argparse.Namespace, config) -> int:
    with Ledger(config.ledger_path) as ledger:
        rows = ledger.history_for_employee(args.bamboo_employee_id)
    if not rows:
        print(f"No adjustments recorded for employee {args.bamboo_employee_id}.")
        return EXIT_OK
    print(f"Adjustment history for BambooHR employee {args.bamboo_employee_id}\n")
    for row in rows:
        print(f"  {row['date']}  {row['delta']:>8}h  {row['status']:<10} "
              f"{row['time_off_type_name']}")
        print(f"      requested {row['requested_hours']}h, "
              f"actual {row['actual_hours']}h  (run {row['run_id']})")
        if row["note"]:
            print(f"      {row['note']}")
        print()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR\n\n{exc}\n", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "run":
        # One id for the run, the log file and every ledger row it writes, so
        # a report can always be traced back to its log.
        run_id = new_run_id()
        logger, log_path = setup_logging(config.log_dir, run_id, verbose=args.verbose)
        try:
            return _cmd_run(args, config, logger, run_id)
        finally:
            print(f"log:    {log_path}")

    if args.command == "uncertain":
        return _cmd_uncertain(args, config)
    if args.command == "clear-uncertain":
        return _cmd_clear_uncertain(args, config)
    if args.command == "history":
        return _cmd_history(args, config)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
