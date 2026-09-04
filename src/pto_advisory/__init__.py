"""Read-only advisory layer. Strictly separate from the reconciliation tool.

This package exists to make a Phase 1 run easier for a human to act on. It is
architecturally prevented from changing anything:

1. It imports nothing from ``pto_recon``. Its only input is the JSON report
   file that a Phase 1 run already wrote to disk. There is no import path from
   here to ``BambooClient.apply_adjustment``, so no output of this package can
   be passed to the write function without a person retyping it.
2. It never receives BambooHR or Harvest credentials. Deploy it without them
   (see the README) and it could not call either API even if it tried.
3. It never sees employee-writable free text. The report contains dates,
   hours, names, emails and this tool's own generated reason strings --
   never request notes or timesheet comments. ``sanitise`` re-enforces that
   by whitelisting fields before anything reaches the model, so an employee
   cannot write text that the model reads.
4. Its suggestions land in a Markdown file for a person to read. The one way
   a suggestion can affect a future run is a human editing ``overrides.json``
   themselves -- and the snippet this tool emits is deliberately incomplete,
   so pasting it without editing fails loudly rather than taking effect.

The reconciliation arithmetic and the balance write stay entirely in
``pto_recon`` and involve no model at any point.
"""

__version__ = "1.0.0"
