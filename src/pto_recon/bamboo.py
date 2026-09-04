"""BambooHR API client.

Endpoints, auth and response shapes follow the proof-of-concept, which was
verified against the live instance:

  GET /v1/meta/time_off/types
  GET /v1/employees/directory
  GET /v1/time_off/requests?start=&end=&status=approved
  PUT /v1/employees/{id}/time_off/balance_adjustment
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import requests

from .config import Config
from .http import ApiError, get_json, put_json
from .models import Employee, RequestDay

log = logging.getLogger("pto_recon.bamboo")

# BambooHR Basic Auth: API key as username, literal "x" as password.
_BASIC_AUTH_PASSWORD = "x"

JSON_HEADERS = {"Accept": "application/json"}


class BambooClient:
    def __init__(
        self,
        config: Config,
        read_session: requests.Session,
        write_session: requests.Session,
    ):
        self.config = config
        self.read = read_session
        self.write = write_session
        self.auth = (config.bamboo_api_key, _BASIC_AUTH_PASSWORD)

    # -- reads -------------------------------------------------------------

    def time_off_type_ids(self) -> dict[str, str]:
        """Returns {lowercased type name: type id} for the configured types.

        Raises if a configured type name does not exist -- a typo in
        TIMEOFF_TYPES would otherwise silently reconcile nothing.
        """
        payload = get_json(
            self.read,
            f"{self.config.bamboo_base}/meta/time_off/types",
            timeout=self.config.http_timeout_seconds,
            context="fetching BambooHR time-off types",
            auth=self.auth,
            headers=JSON_HEADERS,
        )
        # BambooHR returns either a bare list or {"timeOffTypes": [...]}.
        if isinstance(payload, dict):
            types = payload.get("timeOffTypes", [])
        else:
            types = payload

        available = {
            str(t["name"]).strip().lower(): str(t["id"])
            for t in types
            if t.get("name") and t.get("id") is not None
        }

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for name in self.config.timeoff_types:
            key = name.strip().lower()
            if key in available:
                resolved[key] = available[key]
            else:
                missing.append(name)

        if missing:
            raise ApiError(
                "These TIMEOFF_TYPES do not exist in BambooHR: "
                f"{', '.join(missing)}. Available types: "
                f"{', '.join(sorted(available)) or '(none returned)'}"
            )
        return resolved

    def employees(self) -> list[Employee]:
        """The employee directory, as ``Employee`` records without Harvest ids."""
        payload = get_json(
            self.read,
            f"{self.config.bamboo_base}/employees/directory",
            timeout=self.config.http_timeout_seconds,
            context="fetching BambooHR employee directory",
            auth=self.auth,
            headers=JSON_HEADERS,
        )
        if not isinstance(payload, dict) or "employees" not in payload:
            raise ApiError(
                "BambooHR employee directory returned an unexpected shape; "
                f"expected an object with 'employees', got {type(payload).__name__}"
            )

        employees: list[Employee] = []
        for record in payload["employees"]:
            email = (record.get("workEmail") or "").strip().lower() or None
            name = (
                record.get("displayName")
                or " ".join(
                    filter(None, [record.get("firstName"), record.get("lastName")])
                )
                or f"employee {record.get('id')}"
            )
            employees.append(
                Employee(
                    bamboo_id=str(record["id"]),
                    email=email,
                    display_name=str(name).strip(),
                )
            )
        return employees

    def approved_request_days(
        self, start: str, end: str, type_ids: dict[str, str]
    ) -> list[RequestDay]:
        """Approved request-days for the configured types only.

        BambooHR returns a request spanning a range with a per-day breakdown
        in ``dates``; we flatten to one ``RequestDay`` per day because Harvest
        time entries are per-day.
        """
        payload = get_json(
            self.read,
            f"{self.config.bamboo_base}/time_off/requests",
            timeout=self.config.http_timeout_seconds,
            context="fetching BambooHR time-off requests",
            auth=self.auth,
            headers=JSON_HEADERS,
            params={"start": start, "end": end, "status": "approved"},
        )
        if not isinstance(payload, list):
            raise ApiError(
                "BambooHR time-off requests returned an unexpected shape; "
                f"expected a list, got {type(payload).__name__}"
            )

        wanted_ids = {v: k for k, v in type_ids.items()}  # type id -> lower name
        canonical = {t.lower(): t for t in self.config.timeoff_types}

        days: list[RequestDay] = []
        for req in payload:
            type_id = str(req.get("type", {}).get("id", ""))
            if type_id not in wanted_ids:
                continue  # a type we were not asked to reconcile: untouched

            # Defensive: only 'approved' should come back, but the status
            # filter is server-side and a change there must not silently start
            # adjusting balances for cancelled or denied requests.
            status = str(req.get("status", {}).get("status", "")).strip().lower()
            if status and status != "approved":
                log.warning(
                    "Skipping request %s with status %r despite approved filter",
                    req.get("id"),
                    status,
                )
                continue

            type_name = canonical.get(wanted_ids[type_id], wanted_ids[type_id])
            for date, hours_raw in (req.get("dates") or {}).items():
                try:
                    hours = Decimal(str(hours_raw))
                except InvalidOperation:
                    log.warning(
                        "Request %s has unparseable hours %r on %s; skipping day",
                        req.get("id"),
                        hours_raw,
                        date,
                    )
                    continue
                if hours <= 0:
                    continue  # non-working day inside the range
                days.append(
                    RequestDay(
                        request_id=str(req["id"]),
                        bamboo_employee_id=str(req["employeeId"]),
                        time_off_type_id=type_id,
                        time_off_type_name=type_name,
                        date=str(date),
                        requested_hours=hours,
                    )
                )
        return sorted(days, key=lambda d: (d.date, d.bamboo_employee_id, d.request_id))

    # -- the one write -----------------------------------------------------

    def apply_adjustment(
        self,
        employee_id: str,
        date: str,
        time_off_type_id: str,
        amount: Decimal,
        note: str,
    ) -> None:
        """Adjust a balance. Raises ``ApiError`` (nothing applied) or
        ``AmbiguousWriteError`` (outcome unknown -- caller must not retry)."""
        put_json(
            self.write,
            f"{self.config.bamboo_base}/employees/{employee_id}/time_off/balance_adjustment",
            {
                "date": date,
                "timeOffTypeId": time_off_type_id,
                "amount": str(amount),
                "note": note,
            },
            timeout=self.config.http_timeout_seconds,
            context=f"adjusting balance for employee {employee_id} on {date}",
            auth=self.auth,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
