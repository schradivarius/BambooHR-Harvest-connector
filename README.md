# BambooHR ↔ Harvest PTO Reconciliation

Compares approved time-off requests in BambooHR against hours actually logged
in Harvest, and corrects the BambooHR balance where they disagree.

The decision is one subtraction:

```
requested_hours - actual_hours = delta
```

Positive delta means the employee was charged for more time off than they
took, and the hours are credited back. Negative means they took more than was
approved, and the hours are debited. No language model is involved anywhere in
that path — see [Why there is no LLM in the write path](#why-there-is-no-llm-in-the-write-path).

---

## Contents

- [Install](#install)
- [Configure](#configure)
- [Run a dry run](#run-a-dry-run)
- [Reading the report](#reading-the-report)
- [Promoting to `--apply`](#promoting-to---apply)
- [When something goes wrong](#when-something-goes-wrong)
- [Answering "why did my balance change?"](#answering-why-did-my-balance-change)
- [Scheduling on Azure](#scheduling-on-azure)
- [The advisory tool (Phase 2)](#the-advisory-tool-phase-2)
- [Why there is no LLM in the write path](#why-there-is-no-llm-in-the-write-path)
- [Development](#development)

---

## Install

Python 3.10 or newer.

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Configure

```bash
cp .env.example .env
```

Fill in the four credentials. All four are required and the run aborts at
startup if any is missing — it will never start reading and then fail
halfway.

| Variable | Where to find it |
|---|---|
| `BAMBOO_SUBDOMAIN` | `aurora` for `aurora.bamboohr.com` |
| `BAMBOO_API_KEY` | BambooHR → your profile → API Keys |
| `HARVEST_ACCOUNT_ID` | Harvest → Settings → Developers |
| `HARVEST_TOKEN` | Harvest → Settings → Developers → personal access token |

Then set what to reconcile. **These must match your instances exactly** — the
tool fails loudly on a name it cannot find rather than silently reconciling
nothing:

```ini
TIMEOFF_TYPES=PTO,Vacation             # BambooHR type names
HARVEST_TASK_NAMES=PTO,Vacation        # Harvest task names
TASK_TYPE_MAP=PTO=PTO,Vacation=Vacation  # which Harvest task feeds which balance
HARVEST_PROJECT_NAMES=                 # optional: restrict to certain projects
```

`TASK_TYPE_MAP` is required whenever more than one type is in play. If two
Harvest tasks both feed the PTO balance, map both to it and their hours are
summed:

```ini
HARVEST_TASK_NAMES=PTO,Floating Holiday
TASK_TYPE_MAP=PTO=PTO,Floating Holiday=PTO
```

Any BambooHR type not listed in `TIMEOFF_TYPES` is untouched — not read, not
reported, not adjusted. Sick and bereavement stay out of scope unless you add
them.

### Thresholds

```ini
TOLERANCE_HOURS=0.01        # rounding noise, ignored
MAX_AUTO_CREDIT_HOURS=4.0   # larger credits go to review
MAX_AUTO_DEBIT_HOURS=2.0    # larger debits go to review
```

Credits and debits have **separate** limits, and the debit limit is
deliberately tighter. Returning hours to someone rarely gets disputed;
taking hours away does. With the defaults, a 3-hour discrepancy auto-applies
if it favours the employee and goes to review if it does not.

**Set `MAX_AUTO_DEBIT_HOURS=0` to require human review for every debit of any
size**, while credits still flow automatically. That is the full
human-in-the-loop setting, and it is a one-line change.

## Run a dry run

Dry run is the default. It writes nothing.

```bash
pto-reconcile run 2026-08-01 2026-08-31
```

You get a report on stdout, plus three files:

```
data/reports/report-<run-id>-dryrun.txt    the readable report
data/reports/report-<run-id>-dryrun.json   machine-readable, input to Phase 2
data/logs/run-<run-id>.jsonl               structured log, one JSON object per event
```

Dry runs are repeatable — they never consume a ledger key, so you can run the
same period as many times as you like.

## Reading the report

### Per-employee section

```
  Jonathan Smith  <jsmith@example.com>
    date        type         requested    actual     delta  outcome
    2026-08-14  PTO               8.00      4.00     +4.00  apply_credit
    2026-08-15  PTO               8.00      8.00      0.00  skip_within_tolerance
                                              net:     +4.00
```

Check that `requested` matches what BambooHR shows and `actual` matches the
Harvest timesheet. Everything else follows from those two numbers.

### `NEEDS REVIEW`

Nothing in this section was written. Each item needs a person.

| Flag | What it means | Usual cause |
|---|---|---|
| `review_credit_over_threshold` | Credit larger than `MAX_AUTO_CREDIT_HOURS` | Timesheet not submitted, or hours logged to a non-PTO task |
| `review_debit_over_threshold` | Debit larger than `MAX_AUTO_DEBIT_HOURS` | Time off taken beyond what was approved, or a duplicate entry |
| `review_orphan_harvest` | Time off logged in Harvest with **no approved BambooHR request** | Request never filed, or filed under a different type |
| `review_unmatched_employee` | No Harvest user matched this person | Email mismatch — see below |
| `review_blocked_uncertain` | A previous run wrote and could not confirm the result | See [When something goes wrong](#when-something-goes-wrong) |

Orphans are never auto-adjusted. There is no approved request to anchor an
adjustment to, and the likely causes are data-entry problems rather than
balance errors.

### `UNMATCHED EMPLOYEES`

Matching is on exact work email, lowercased. **Names are never used** — "Jon
Smith" and "Jonathan Smith" are not paired automatically, because a wrong
match writes an adjustment against the wrong person.

An unmatched employee's PTO is not reconciled at all until you resolve it.
Two ways:

1. **Fix the data.** Usually the right answer — make the Harvest email match
   the BambooHR work email.
2. **Pin it manually.** Copy `overrides.example.json` to `overrides.json` and
   add an entry. `confirmed_by` is required and records who verified it.

```json
{
  "manual_matches": [
    {
      "bamboo_employee_id": "142",
      "harvest_user_id": 987654,
      "confirmed_by": "cschrader",
      "confirmed_on": "2026-09-04",
      "note": "Harvest account predates the domain migration"
    }
  ]
}
```

## Promoting to `--apply`

1. Run the dry run and read the report end to end.
2. Confirm the `NEEDS REVIEW` and `UNMATCHED` sections are either empty or
   understood. Unresolved items stay unreconciled — they do not block the run.
3. Spot-check two or three employees against BambooHR and Harvest directly.
4. Re-run the identical command with `--apply`:

```bash
pto-reconcile run 2026-08-01 2026-08-31 --apply
```

Each adjustment is recorded in the SQLite ledger **before** the API call and
updated immediately after, so the same request-day is never adjusted twice —
even if the process is killed mid-run.

Exit codes: `0` clean, `1` config/usage error (nothing read or written),
`2` aborted with an adjustment in an unknown state, `3` completed but some
adjustments were rejected.

## When something goes wrong

### A run aborted with exit code 2

This means an adjustment was sent and the response never came back cleanly —
a timeout or a 5xx. The tool cannot tell whether BambooHR applied it, and
**retrying a balance adjustment that already applied would double it**, so it
stops, marks the key `uncertain`, and blocks it.

```bash
pto-reconcile uncertain
```

For each key listed, open the employee's balance in BambooHR and look for the
adjustment. Then record what you found:

```bash
# The adjustment IS there — leave the balance alone
pto-reconcile clear-uncertain "5001:2026-08-14:78" --resolution applied --operator cschrader

# The adjustment is NOT there — free the key so the next run retries it
pto-reconcile clear-uncertain "5001:2026-08-14:78" --resolution failed --operator cschrader
```

Adjustments applied before the abort are already recorded; re-running the
period after clearing will skip them.

### Exit code 3

BambooHR rejected some adjustments outright (a 4xx). Nothing was applied for
those, the keys are free, and the reasons are in the `FAILURES` section and
the log. Fix the cause and re-run — successful adjustments are not repeated.

### Rate limits and transient errors

Reads retry automatically with backoff on 429 and 5xx. The write retries only
on conditions that cannot have applied the adjustment (connection refused,
429). Everything else becomes an `uncertain` and stops the run.

## Answering "why did my balance change?"

```bash
pto-reconcile history 142
```

Prints every live adjustment for that BambooHR employee — date, delta, the
requested and actual hours behind it, and the run that made it. The same note
is attached to the adjustment inside BambooHR, so it is visible to HR without
this tool.

For the full picture of a run, `data/logs/run-<run-id>.jsonl` has one JSON
object per event, each carrying the run id.

## Scheduling on Azure

Packaged as an Azure Function (Python v2 model). `function_app.py` has two
triggers:

- `reconcile_timer` — scheduled. Reconciles the **previous calendar month**.
- `reconcile_manual` — HTTP, function-key auth. Dry run only, always.

```
PTO_SCHEDULE   NCRONTAB, default "0 0 6 5 * *" (06:00 UTC on the 5th)
PTO_APPLY      must be exactly "true" to write; anything else is a dry run
```

**The timer runs in dry-run mode unless `PTO_APPLY=true`.** Deploying does not
by itself let the job change balances — that takes a second, deliberate
setting change. Run it on the schedule in dry-run mode for a cycle or two,
read the reports, then flip it.

The default of the 5th assumes a monthly pay period closing at month end, with
a few days' slack for timesheets to be submitted. Adjust `PTO_SCHEDULE` to
your cadence — for semi-monthly, `"0 0 6 5,20 * *"`.

### Trade-offs worth knowing

**The SQLite ledger needs durable storage.** This is the main constraint.
Function App local disk does not survive a restart or a scale event, and
losing the ledger means losing the guarantee that an adjustment is never
applied twice. Options, best first:

1. **Mount an Azure Files share** and point `LEDGER_PATH` at it
   (`/mounted/pto/ledger.db`). Simplest change, keeps SQLite. Note that
   SQLite over SMB is fine for a single monthly writer but is not a good
   general-purpose setup — this workload suits it because runs are rare,
   short, and serialised.
2. **Move the ledger to Azure SQL.** The `Ledger` class is the only thing
   that touches storage; swapping the backend is contained. Worth doing if you
   ever want concurrent runs or a second consumer of the audit trail.
3. Blob-based persistence — workable but the weakest option, since SQLite over
   blob storage gives up the locking that makes the idempotency guarantee hold.

**Plan choice.** Use Premium or a dedicated App Service plan, not Consumption.
Consumption caps at 10 minutes (the `functionTimeout` in `host.json` asks for
30, which Consumption will not honour), and Azure Files mounting is not
supported there. For ~40 employees a run is well inside 10 minutes, but the
storage constraint alone rules Consumption out.

**Secrets.** Put the four credentials in Key Vault and reference them from App
Settings (`@Microsoft.KeyVault(...)`) rather than pasting values into the
portal.

**Alerting.** A failed run raises, which surfaces in Application Insights. Set
an alert on failures of `reconcile_timer` — the failure mode that matters is a
scheduled job that quietly stops working, and a monthly job can be broken for
a long time before anyone notices. Also worth alerting on the "needs review"
warning so flagged items do not sit unread until someone opens the report.

### Would a cron job be simpler?

For a job this size, honestly yes — a cron entry on an existing VM would be
less machinery. The Function is the better call anyway if you have no VM you
want to own, want Key Vault and App Insights wired in without extra work, or
want the run history visible to people who will not SSH anywhere. If you
already run a maintenance VM with backups and monitoring, a cron entry plus a
local SQLite file is a defensible choice and removes the storage-mount
problem entirely. The tool runs identically either way — it is a normal CLI,
and `function_app.py` is a thin wrapper over it.

## The advisory tool (Phase 2)

Separate, read-only, and structurally unable to change a balance.

```bash
pip install -e ".[advisory]"
export ANTHROPIC_API_KEY=...
pto-advise data/reports/report-<run-id>-dryrun.json -o briefing.md
```

Produces a plain-English summary for HR, suggested matches for unmatched
employees, and a second opinion on flagged deltas.

Its only input is the JSON report already on disk. It holds no BambooHR
credentials and imports nothing from `pto_recon`, so there is no code path
from its output to the adjustment endpoint. Its match suggestions can only
take effect if a person writes them into `overrides.json` — and the snippet it
emits deliberately omits `confirmed_by`, so pasting it unedited is **rejected**
by the reconciliation tool rather than silently accepted.

`--no-llm` produces the local name-similarity analysis without calling the API.

Deploy it as a separate Function App **without** the BambooHR settings, or run
it manually. Do not add it to the reconciliation Function App.

## Why there is no LLM in the write path

The reconciliation math and the BambooHR write are plain arithmetic on API
data, and they stay that way for three reasons.

**Reproducibility.** The same two numbers always produce the same adjustment.
When an employee or an auditor asks why a balance changed, the answer is a
subtraction they can check, and the exact inputs are in the ledger and the
adjustment note.

**Injection.** Request notes and timesheet comments are employee-writable. A
model that read them and could also call the balance-adjustment endpoint could
be talked into changing a balance by the person whose balance it is. The
advisory layer never receives those fields at all — `sanitise()` whitelists
what reaches the prompt, and the reconciliation report never contains them in
the first place.

**Auditability.** "The model decided" is not an answer HR can give. Every
decision this tool makes maps to a named branch in `reconcile.decide()` with a
written reason attached.

These properties are enforced by tests, not just convention — see
`tests/test_advisory_isolation.py`, which fails if anything in the advisory
package imports the reconciler or names the write function.

## Development

```bash
pytest                      # full suite, no live API calls
pytest tests/test_reconcile.py -v
```

Layout:

```
src/pto_recon/
  reconcile.py   the deterministic core — pure functions, no I/O, no model
  models.py      frozen data structures; hours are Decimal, never float
  config.py      env loading + startup validation
  bamboo.py      BambooHR client (the only module that can write)
  harvest.py     Harvest client (read-only)
  matching.py    exact-email matching + human-confirmed overrides
  ledger.py      SQLite idempotency ledger and audit trail
  http.py        retry policy — separate sessions for reads and writes
  runner.py      orchestration: read everything, decide everything, then write
  report.py      text and JSON reports
  cli.py         entry point
src/pto_advisory/
  advise.py      read-only briefing generator (no import path to pto_recon)
```

`reconcile.py` is the module to read first and the one to be most careful
changing. It contains no I/O by design — keep it that way, and the decisions
stay testable and reproducible.
