"""Turn a Phase 1 JSON report into a readable briefing for HR.

    pto-advise data/reports/report-<run-id>-dryrun.json -o briefing.md

Produces three sections: suggested matches for unmatched employees, a
plain-English summary of the run, and context on each flagged large delta.
Every one of them is a suggestion for a person to act on. Nothing here
writes to BambooHR, and nothing here can reach the code that does.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

MODEL = "claude-opus-5"

# Fields allowed out of the report and into the model prompt. Anything not
# listed is dropped. The report should not contain employee-written text in
# the first place; this is the belt to that pair of braces.
DECISION_FIELDS = (
    "action",
    "reason",
    "date",
    "time_off_type",
    "requested_hours",
    "actual_hours",
    "delta",
)
EMPLOYEE_FIELDS = ("name", "email", "bamboo_id", "harvest_id")


class AdvisoryError(RuntimeError):
    pass


def _assert_isolated() -> None:
    """Fail if the write client is reachable in this process.

    Phase 2 must not share a process with the BambooHR client. If someone
    later wires them together, this stops the run rather than letting an
    advisory process quietly gain write access.
    """
    if "pto_recon.bamboo" in sys.modules:
        raise AdvisoryError(
            "pto_recon.bamboo is loaded in this process. The advisory tool "
            "must run as a separate process with no access to the BambooHR "
            "write client. Refusing to continue."
        )


def sanitise(report: dict) -> dict:
    """Whitelist the fields that may be shown to the model."""

    def employee(raw: dict | None) -> dict:
        raw = raw or {}
        return {k: raw.get(k) for k in EMPLOYEE_FIELDS}

    def decision(raw: dict) -> dict:
        out = {k: raw.get(k) for k in DECISION_FIELDS}
        out["employee"] = employee(raw.get("employee"))
        return out

    decisions = [decision(d) for d in report.get("decisions", [])]
    return {
        "run_id": report.get("run_id"),
        "period": report.get("period"),
        "dry_run": report.get("dry_run"),
        "counts": report.get("counts"),
        "needs_review": [
            d for d in decisions if str(d["action"]).startswith("review_")
        ],
        "applied_or_intended": [
            d for d in decisions if str(d["action"]).startswith("apply_")
        ],
        "unmatched_bamboo": [
            {k: e.get(k) for k in ("bamboo_id", "name", "email")}
            for e in report.get("unmatched_bamboo", [])
        ],
        "unmatched_harvest": report.get("unmatched_harvest", []),
    }


def candidate_matches(payload: dict) -> list[dict]:
    """Deterministic name-similarity candidates, computed locally.

    The model is asked to assess these, not to invent pairings from scratch,
    so its output stays anchored to names that actually appear in the data.
    """
    candidates = []
    harvest = payload.get("unmatched_harvest", [])
    for employee in payload.get("unmatched_bamboo", []):
        name = (employee.get("name") or "").lower()
        scored = sorted(
            (
                {
                    "harvest_id": h.get("harvest_id"),
                    "harvest_label": h.get("label"),
                    "similarity": round(
                        difflib.SequenceMatcher(
                            None, name, str(h.get("label", "")).lower()
                        ).ratio(),
                        3,
                    ),
                }
                for h in harvest
            ),
            key=lambda c: c["similarity"],
            reverse=True,
        )
        candidates.append(
            {
                "bamboo_id": employee.get("bamboo_id"),
                "bamboo_name": employee.get("name"),
                "bamboo_email": employee.get("email"),
                "candidates": scored[:3],
            }
        )
    return candidates


PROMPT = """\
You are helping an HR administrator read the output of a PTO reconciliation \
run. The run compared approved time-off requests in BambooHR against hours \
actually logged in Harvest, and corrected balances where they disagreed.

You are advisory only. You cannot change any balance, and nothing you write \
will be applied automatically. A person reads your output and decides.

Write three sections in Markdown.

## Summary for HR
Three to six sentences, plain English, no jargon. What period was checked, \
how many corrections were made or proposed, the net effect on balances, and \
anything that needs attention. Write for someone who does not know what an \
API is.

## Suggested matches
For each unmatched BambooHR employee, assess the candidate Harvest users \
supplied. Give a confidence of high, medium or low and say what a person \
should check to confirm it. Say plainly when none of the candidates look \
right -- a wrong match writes an adjustment against the wrong person, so \
"no confident suggestion" is a useful answer. If there are no unmatched \
employees, write "None -- every employee matched."

## Flagged deltas
For each item in needs_review, give a short read on whether it looks like a \
legitimate correction or a data problem worth investigating, and what to \
check. Common benign causes: someone logged time off to the wrong task, a \
timesheet was submitted late or not at all, a request was filed after the \
fact. Be concrete and brief. If nothing is flagged, write "None flagged."

Do not invent employees, dates or numbers that are not in the data. If \
something is ambiguous, say so rather than guessing.

Run data:
```json
{payload}
```

Name-similarity candidates (computed locally, not by you):
```json
{candidates}
```
"""


def build_briefing(payload: dict, candidates: list[dict]) -> str:
    """Ask Claude for the briefing. Read-only: input is JSON, output is text."""
    try:
        import anthropic
    except ImportError as exc:
        raise AdvisoryError(
            "The advisory tool needs the anthropic package: "
            "pip install -e '.[advisory]'"
        ) from exc

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        messages=[
            {
                "role": "user",
                "content": PROMPT.format(
                    payload=json.dumps(payload, indent=2),
                    candidates=json.dumps(candidates, indent=2),
                ),
            }
        ],
    )
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def overrides_snippet(candidates: list[dict]) -> str:
    """A starting point for overrides.json -- deliberately incomplete.

    ``confirmed_by`` is omitted on purpose. Phase 1 rejects any override
    without it, so pasting this file unedited fails loudly instead of
    silently letting a machine-suggested match affect a balance.
    """
    entries = [
        {
            "bamboo_employee_id": c["bamboo_id"],
            "harvest_user_id": c["candidates"][0]["harvest_id"],
            "note": (
                f"SUGGESTED, NOT CONFIRMED: {c['bamboo_name']} (BambooHR) "
                f"vs {c['candidates'][0]['harvest_label']} (Harvest), "
                f"similarity {c['candidates'][0]['similarity']}"
            ),
        }
        for c in candidates
        if c.get("candidates")
    ]
    return json.dumps({"manual_matches": entries}, indent=2)


def render(
    payload: dict, candidates: list[dict], briefing: str, used_llm: bool = True
) -> str:
    counts = payload.get("counts") or {}
    period = payload.get("period") or {}
    dry_run = payload.get("dry_run")

    if used_llm:
        banner = (
            "> Advisory only. Every suggestion below was produced by a "
            "language model reading the run report, and none of it has been "
            "applied. The balance corrections themselves were plain "
            "arithmetic and are recorded in the ledger."
        )
    else:
        banner = (
            "> Advisory only, generated without a language model "
            "(`--no-llm`): the suggested matches below are local name "
            "similarity scores. Nothing here has been applied."
        )

    # On a dry run nothing was applied yet, so the meaningful figure is what
    # the run intended to write.
    adjustments = (
        counts.get("writes_intended", 0) if dry_run else counts.get("applied", 0)
    )

    header = [
        "# PTO reconciliation briefing",
        "",
        f"Run `{payload.get('run_id')}` covering "
        f"{period.get('start')} to {period.get('end')} "
        f"({'dry run' if dry_run else 'live'}).",
        "",
        banner,
        "",
        f"- request-days examined: {counts.get('examined', 0)}",
        f"- adjustments {'proposed' if dry_run else 'applied'}: {adjustments}",
        f"- needs review: {counts.get('needs_review', 0)}",
        "",
        "---",
        "",
    ]
    footer = []
    if candidates:
        footer = [
            "",
            "---",
            "",
            "## Draft overrides.json",
            "",
            "Confirm each pairing yourself first. This snippet has no "
            "`confirmed_by` field, so the reconciliation tool will **reject "
            "it** until you add your name to each entry you have verified.",
            "",
            "```json",
            overrides_snippet(candidates),
            "```",
        ]
    return "\n".join(header + [briefing] + footer)


def main(argv: list[str] | None = None) -> int:
    _assert_isolated()

    parser = argparse.ArgumentParser(
        prog="pto-advise",
        description=(
            "Read-only advisory briefing from a reconciliation report. "
            "Cannot change any balance."
        ),
    )
    parser.add_argument("report", type=Path, help="report-<run-id>-*.json")
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="emit the local candidate analysis only, without calling Claude",
    )
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"No such report: {args.report}", file=sys.stderr)
        return 1

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{args.report} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    payload = sanitise(report)
    candidates = candidate_matches(payload)

    if args.no_llm:
        briefing = (
            "## Suggested matches\n\n```json\n"
            + json.dumps(candidates, indent=2)
            + "\n```\n"
        )
    else:
        try:
            briefing = build_briefing(payload, candidates)
        except AdvisoryError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    document = render(payload, candidates, briefing, used_llm=not args.no_llm)
    if args.output:
        args.output.write_text(document, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
