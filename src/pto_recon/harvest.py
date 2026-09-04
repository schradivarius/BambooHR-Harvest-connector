"""Harvest API client.

  GET /v2/users
  GET /v2/tasks
  GET /v2/projects
  GET /v2/time_entries?task_id=&from=&to=

Unlike the proof-of-concept this supports several time-off tasks and an
optional project filter, and it keeps the Harvest entry ids behind every
total so an adjustment can be traced back to the exact timesheet rows that
justified it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

import requests

from .config import Config
from .http import ApiError, get_json
from .models import HarvestHours

log = logging.getLogger("pto_recon.harvest")

PER_PAGE = 100
# A run covering one pay period should need a handful of pages. This only
# exists so a pagination bug cannot spin forever against a live API.
MAX_PAGES = 500


class HarvestClient:
    def __init__(self, config: Config, read_session: requests.Session):
        self.config = config
        self.read = read_session
        self.headers = {
            "Authorization": f"Bearer {config.harvest_token}",
            "Harvest-Account-Id": config.harvest_account_id,
            "Accept": "application/json",
        }

    def _paginate(self, path: str, key: str, params: dict) -> list[dict]:
        """Walk Harvest's paginated list endpoints."""
        results: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            payload = get_json(
                self.read,
                f"{self.config.harvest_base}{path}",
                timeout=self.config.http_timeout_seconds,
                context=f"fetching Harvest {key}",
                headers=self.headers,
                params={**params, "page": page, "per_page": PER_PAGE},
            )
            if not isinstance(payload, dict) or key not in payload:
                raise ApiError(
                    f"Harvest {path} returned an unexpected shape; expected an "
                    f"object with '{key}'"
                )
            results.extend(payload[key])
            if not (payload.get("links") or {}).get("next"):
                return results
            page += 1
        raise ApiError(
            f"Harvest {path} exceeded {MAX_PAGES} pages; refusing to continue"
        )

    def users(self) -> dict[int, str]:
        """Returns {harvest_user_id: lowercased email}."""
        users: dict[int, str] = {}
        for record in self._paginate("/users", "users", {}):
            email = (record.get("email") or "").strip().lower()
            if email:
                users[int(record["id"])] = email
        return users

    def user_names(self) -> dict[int, str]:
        """Returns {harvest_user_id: display name}, for the unmatched report."""
        names: dict[int, str] = {}
        for record in self._paginate("/users", "users", {}):
            name = " ".join(
                filter(None, [record.get("first_name"), record.get("last_name")])
            ).strip()
            names[int(record["id"])] = name or f"harvest user {record['id']}"
        return names

    def time_off_task_ids(self) -> dict[int, str]:
        """Returns {task_id: task name} for the configured time-off tasks."""
        tasks = self._paginate("/tasks", "tasks", {})
        by_name = {
            str(t["name"]).strip().lower(): (int(t["id"]), str(t["name"]).strip())
            for t in tasks
            if t.get("name")
        }
        resolved: dict[int, str] = {}
        missing: list[str] = []
        for name in self.config.harvest_task_names:
            hit = by_name.get(name.strip().lower())
            if hit is None:
                missing.append(name)
            else:
                resolved[hit[0]] = hit[1]
        if missing:
            raise ApiError(
                "These HARVEST_TASK_NAMES do not exist in Harvest: "
                f"{', '.join(missing)}. If your firm tracks time off as a "
                "PROJECT rather than a task, set HARVEST_PROJECT_NAMES and "
                "list the task names used within it."
            )
        return resolved

    def project_ids(self) -> set[int] | None:
        """Project ids matching HARVEST_PROJECT_NAMES, or None if unfiltered."""
        if not self.config.harvest_project_names:
            return None
        projects = self._paginate("/projects", "projects", {})
        by_name = {
            str(p["name"]).strip().lower(): int(p["id"])
            for p in projects
            if p.get("name")
        }
        resolved: set[int] = set()
        missing: list[str] = []
        for name in self.config.harvest_project_names:
            hit = by_name.get(name.strip().lower())
            if hit is None:
                missing.append(name)
            else:
                resolved.add(hit)
        if missing:
            raise ApiError(
                "These HARVEST_PROJECT_NAMES do not exist in Harvest: "
                f"{', '.join(missing)}"
            )
        return resolved

    def time_off_hours(
        self, start: str, end: str, task_ids: dict[int, str], project_ids: set[int] | None
    ) -> list[HarvestHours]:
        """Actual logged time-off hours, one record per user/date/BambooHR type.

        Hours are summed per BambooHR *type*, not per Harvest task, because
        several Harvest tasks may feed one balance (TASK_TYPE_MAP).
        """
        totals: dict[tuple[int, str, str], Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        entry_ids: dict[tuple[int, str, str], list[int]] = defaultdict(list)

        for task_id, task_name in sorted(task_ids.items()):
            bamboo_type = self.config.bamboo_type_for_harvest_task(task_name)
            if bamboo_type is None:
                # load_config guarantees a mapping exists; this guards against
                # a task whose name differs in case/whitespace from config.
                raise ApiError(
                    f"Harvest task {task_name!r} has no TASK_TYPE_MAP entry"
                )

            entries = self._paginate(
                "/time_entries",
                "time_entries",
                {"task_id": task_id, "from": start, "to": end},
            )
            for entry in entries:
                if project_ids is not None:
                    project = entry.get("project") or {}
                    if int(project.get("id", -1)) not in project_ids:
                        continue
                user_id = int((entry.get("user") or {})["id"])
                date = str(entry["spent_date"])
                key = (user_id, date, bamboo_type)
                totals[key] += Decimal(str(entry.get("hours", 0)))
                entry_ids[key].append(int(entry["id"]))

        return [
            HarvestHours(
                harvest_user_id=user_id,
                date=date,
                time_off_type_name=type_name,
                hours=hours,
                entry_ids=tuple(sorted(entry_ids[(user_id, date, type_name)])),
            )
            for (user_id, date, type_name), hours in sorted(totals.items())
        ]
