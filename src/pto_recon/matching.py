"""Employee matching between BambooHR and Harvest.

Matching stays **exact** on normalised work email, exactly as the PoC did.
Fuzzy name matching is deliberately NOT in this path: "Jon Smith" resolving to
the wrong Jonathan writes a balance adjustment against the wrong person, and
that is not a mistake worth risking for convenience.

The one escape hatch is ``overrides.json`` -- a file a human edits to pin a
BambooHR employee to a Harvest user. That is also the only channel by which
the Phase 2 advisory tool's suggestions can ever influence a write: it
proposes, a person reviews and writes the file, and this module reads it as
plain configuration. No suggestion reaches the write path unread.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .models import Employee

log = logging.getLogger("pto_recon.matching")


class OverrideError(RuntimeError):
    """The overrides file is malformed or contradicts itself."""


def normalise_email(email: str | None) -> str | None:
    """Lowercase and strip. Nothing cleverer -- no plus-address stripping, no
    dot-folding: those are provider-specific and two different mailboxes can
    legitimately differ only in a dot."""
    if not email:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


@dataclass
class MatchResult:
    matched: list[Employee] = field(default_factory=list)
    unmatched_bamboo: list[Employee] = field(default_factory=list)
    unmatched_harvest: dict[int, str] = field(default_factory=dict)

    @property
    def by_bamboo_id(self) -> dict[str, Employee]:
        return {e.bamboo_id: e for e in self.matched + self.unmatched_bamboo}

    @property
    def by_harvest_id(self) -> dict[int, Employee]:
        return {e.harvest_id: e for e in self.matched if e.harvest_id is not None}


def load_overrides(path: Path) -> dict[str, int]:
    """Load {bamboo_employee_id: harvest_user_id} from a human-edited file.

    Format::

        {
          "manual_matches": [
            {"bamboo_employee_id": "142", "harvest_user_id": 987654,
             "confirmed_by": "cschrader", "confirmed_on": "2026-09-04",
             "note": "Jon Smith in Harvest = Jonathan Smith in BambooHR"}
          ]
        }
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OverrideError(f"{path} is not valid JSON: {exc}") from exc

    entries = payload.get("manual_matches", []) if isinstance(payload, dict) else None
    if entries is None:
        raise OverrideError(f"{path} must be an object with a 'manual_matches' list")

    overrides: dict[str, int] = {}
    seen_harvest: dict[int, str] = {}
    for entry in entries:
        try:
            bamboo_id = str(entry["bamboo_employee_id"]).strip()
            harvest_id = int(entry["harvest_user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OverrideError(
                f"{path}: each manual_matches entry needs "
                f"'bamboo_employee_id' and 'harvest_user_id'; got {entry!r}"
            ) from exc
        if not entry.get("confirmed_by"):
            raise OverrideError(
                f"{path}: entry for BambooHR employee {bamboo_id} is missing "
                "'confirmed_by'. Every manual match must name the person who "
                "verified it -- that name is the audit trail for this override."
            )
        if bamboo_id in overrides:
            raise OverrideError(
                f"{path}: BambooHR employee {bamboo_id} is mapped twice"
            )
        if harvest_id in seen_harvest:
            raise OverrideError(
                f"{path}: Harvest user {harvest_id} is mapped to both BambooHR "
                f"employees {seen_harvest[harvest_id]} and {bamboo_id}"
            )
        overrides[bamboo_id] = harvest_id
        seen_harvest[harvest_id] = bamboo_id
    return overrides


def match_employees(
    bamboo_employees: list[Employee],
    harvest_users: dict[int, str],
    harvest_names: dict[int, str] | None = None,
    overrides: dict[str, int] | None = None,
) -> MatchResult:
    """Pair BambooHR employees with Harvest users. Pure function.

    Precedence: a manual override wins over email, so a confirmed pin is not
    silently overturned when someone's email later changes.
    """
    overrides = overrides or {}
    harvest_names = harvest_names or {}

    email_to_harvest: dict[str, int] = {}
    ambiguous_emails: set[str] = set()
    for harvest_id, email in harvest_users.items():
        key = normalise_email(email)
        if key is None:
            continue
        if key in email_to_harvest:
            # Two Harvest users share an address: refuse both rather than
            # pick one arbitrarily.
            ambiguous_emails.add(key)
        else:
            email_to_harvest[key] = harvest_id

    for email in ambiguous_emails:
        log.warning(
            "Harvest has more than one user with email %s; treating as unmatched",
            email,
        )
        email_to_harvest.pop(email, None)

    result = MatchResult()
    claimed: set[int] = set()

    for employee in bamboo_employees:
        harvest_id: int | None = None

        if employee.bamboo_id in overrides:
            harvest_id = overrides[employee.bamboo_id]
            if harvest_id not in harvest_users:
                log.warning(
                    "Override maps BambooHR employee %s to Harvest user %s, "
                    "which does not exist; treating as unmatched",
                    employee.bamboo_id,
                    harvest_id,
                )
                harvest_id = None
        else:
            key = normalise_email(employee.email)
            if key is not None:
                harvest_id = email_to_harvest.get(key)

        if harvest_id is None:
            result.unmatched_bamboo.append(employee)
            continue

        claimed.add(harvest_id)
        result.matched.append(
            Employee(
                bamboo_id=employee.bamboo_id,
                email=employee.email,
                display_name=employee.display_name,
                harvest_id=harvest_id,
            )
        )

    result.unmatched_harvest = {
        harvest_id: harvest_names.get(harvest_id, email)
        for harvest_id, email in sorted(harvest_users.items())
        if harvest_id not in claimed
    }
    return result
