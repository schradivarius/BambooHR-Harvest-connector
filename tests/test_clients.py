"""API client tests against mocked responses. No live calls."""

from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from pto_recon.bamboo import BambooClient
from pto_recon.config import Config
from pto_recon.harvest import HarvestClient
from pto_recon.http import (
    AmbiguousWriteError,
    ApiError,
    build_read_session,
    build_write_session,
)

BAMBOO = "https://acme.bamboohr.com/api/v1"
HARVEST = "https://api.harvestapp.com/v2"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        bamboo_subdomain="acme",
        bamboo_api_key="key",
        harvest_account_id="12345",
        harvest_token="token",
        timeoff_types=("PTO", "Vacation"),
        harvest_task_names=("PTO", "Vacation"),
        harvest_project_names=(),
        task_type_map={"pto": "PTO", "vacation": "Vacation"},
        tolerance_hours=Decimal("0.01"),
        max_auto_credit_hours=Decimal("4.0"),
        max_auto_debit_hours=Decimal("2.0"),
        ledger_path=tmp_path / "ledger.db",
        log_dir=tmp_path / "logs",
        report_dir=tmp_path / "reports",
        http_timeout_seconds=5,
        http_max_retries=0,
        http_backoff_seconds=0,
        http_user_agent="test",
    )


@pytest.fixture
def bamboo_client(config) -> BambooClient:
    return BambooClient(
        config,
        build_read_session(max_retries=0, backoff_seconds=0, user_agent="test"),
        build_write_session(backoff_seconds=0, user_agent="test"),
    )


@pytest.fixture
def harvest_client(config) -> HarvestClient:
    return HarvestClient(
        config,
        build_read_session(max_retries=0, backoff_seconds=0, user_agent="test"),
    )


# -- BambooHR ----------------------------------------------------------------


@responses.activate
def test_resolves_configured_time_off_types(bamboo_client):
    responses.get(
        f"{BAMBOO}/meta/time_off/types",
        json=[
            {"id": 78, "name": "PTO"},
            {"id": 79, "name": "Vacation"},
            {"id": 80, "name": "Sick"},
        ],
    )
    assert bamboo_client.time_off_type_ids() == {"pto": "78", "vacation": "79"}


@responses.activate
def test_unknown_time_off_type_fails_loudly(bamboo_client):
    responses.get(
        f"{BAMBOO}/meta/time_off/types", json=[{"id": 80, "name": "Sick"}]
    )
    with pytest.raises(ApiError, match="do not exist in BambooHR"):
        bamboo_client.time_off_type_ids()


@responses.activate
def test_request_days_flatten_the_dates_field(bamboo_client):
    responses.get(
        f"{BAMBOO}/time_off/requests",
        json=[
            {
                "id": 5001,
                "employeeId": 142,
                "type": {"id": 78, "name": "PTO"},
                "status": {"status": "approved"},
                "dates": {"2026-08-14": "8", "2026-08-15": "4"},
            }
        ],
    )
    days = bamboo_client.approved_request_days(
        "2026-08-01", "2026-08-31", {"pto": "78"}
    )
    assert [(d.date, d.requested_hours) for d in days] == [
        ("2026-08-14", Decimal("8")),
        ("2026-08-15", Decimal("4")),
    ]


@responses.activate
def test_unconfigured_types_are_ignored(bamboo_client):
    responses.get(
        f"{BAMBOO}/time_off/requests",
        json=[
            {
                "id": 6001,
                "employeeId": 142,
                "type": {"id": 80, "name": "Sick"},
                "status": {"status": "approved"},
                "dates": {"2026-08-14": "8"},
            }
        ],
    )
    assert bamboo_client.approved_request_days(
        "2026-08-01", "2026-08-31", {"pto": "78"}
    ) == []


@responses.activate
def test_non_approved_requests_are_skipped(bamboo_client):
    responses.get(
        f"{BAMBOO}/time_off/requests",
        json=[
            {
                "id": 5001,
                "employeeId": 142,
                "type": {"id": 78, "name": "PTO"},
                "status": {"status": "cancelled"},
                "dates": {"2026-08-14": "8"},
            }
        ],
    )
    assert bamboo_client.approved_request_days(
        "2026-08-01", "2026-08-31", {"pto": "78"}
    ) == []


@responses.activate
def test_zero_hour_days_are_dropped(bamboo_client):
    responses.get(
        f"{BAMBOO}/time_off/requests",
        json=[
            {
                "id": 5001,
                "employeeId": 142,
                "type": {"id": 78, "name": "PTO"},
                "status": {"status": "approved"},
                "dates": {"2026-08-15": "0", "2026-08-16": "0", "2026-08-17": "8"},
            }
        ],
    )
    days = bamboo_client.approved_request_days(
        "2026-08-01", "2026-08-31", {"pto": "78"}
    )
    assert [d.date for d in days] == ["2026-08-17"]


@responses.activate
def test_employees_without_email_are_kept_for_the_unmatched_report(bamboo_client):
    responses.get(
        f"{BAMBOO}/employees/directory",
        json={
            "employees": [
                {"id": 142, "displayName": "Jonathan Smith",
                 "workEmail": "JSmith@Example.com"},
                {"id": 207, "displayName": "Dana Wu", "workEmail": None},
            ]
        },
    )
    employees = bamboo_client.employees()
    assert employees[0].email == "jsmith@example.com"
    assert employees[1].email is None


# -- the write ---------------------------------------------------------------


@responses.activate
def test_successful_adjustment(bamboo_client):
    responses.put(
        f"{BAMBOO}/employees/142/time_off/balance_adjustment", status=201
    )
    bamboo_client.apply_adjustment("142", "2026-08-14", "78", Decimal("4.00"), "n")
    assert len(responses.calls) == 1


@responses.activate
def test_4xx_is_a_definite_failure_not_an_ambiguous_one(bamboo_client):
    responses.put(
        f"{BAMBOO}/employees/142/time_off/balance_adjustment",
        status=400,
        body="bad date",
    )
    with pytest.raises(ApiError):
        bamboo_client.apply_adjustment(
            "142", "2026-08-14", "78", Decimal("4.00"), "n"
        )


@responses.activate
def test_5xx_write_is_ambiguous_and_not_retried(bamboo_client):
    """The critical safety property: a 500 on a non-idempotent write must
    NOT be retried, because it may already have moved the balance."""
    responses.put(
        f"{BAMBOO}/employees/142/time_off/balance_adjustment",
        status=500,
        body="upstream error",
    )
    with pytest.raises(AmbiguousWriteError):
        bamboo_client.apply_adjustment(
            "142", "2026-08-14", "78", Decimal("4.00"), "n"
        )
    assert len(responses.calls) == 1


@responses.activate
def test_adjustment_amount_is_sent_as_an_exact_decimal_string(bamboo_client):
    responses.put(
        f"{BAMBOO}/employees/142/time_off/balance_adjustment", status=200
    )
    bamboo_client.apply_adjustment("142", "2026-08-14", "78", Decimal("-1.50"), "n")
    import json as _json

    body = _json.loads(responses.calls[0].request.body)
    assert body["amount"] == "-1.50"
    assert body["timeOffTypeId"] == "78"


# -- Harvest -----------------------------------------------------------------


@responses.activate
def test_harvest_pagination(harvest_client):
    responses.get(
        f"{HARVEST}/users",
        json={
            "users": [{"id": 1, "email": "a@x.com", "first_name": "A", "last_name": "A"}],
            "links": {"next": "page2"},
        },
    )
    responses.get(
        f"{HARVEST}/users",
        json={
            "users": [{"id": 2, "email": "B@x.com", "first_name": "B", "last_name": "B"}],
            "links": {"next": None},
        },
    )
    assert harvest_client.users() == {1: "a@x.com", 2: "b@x.com"}


@responses.activate
def test_hours_from_several_tasks_sum_into_one_bamboo_type(config):
    """Two Harvest tasks both mapped to PTO must add up, not overwrite."""
    config = Config(**{**config.__dict__, "task_type_map": {"pto": "PTO", "vacation": "PTO"}})
    client = HarvestClient(
        config, build_read_session(max_retries=0, backoff_seconds=0, user_agent="t")
    )
    for task_id in (10, 20):
        responses.get(
            f"{HARVEST}/time_entries",
            json={
                "time_entries": [
                    {
                        "id": 900 + task_id,
                        "user": {"id": 987654},
                        "spent_date": "2026-08-14",
                        "hours": 4.0,
                        "project": {"id": 1},
                    }
                ],
                "links": {"next": None},
            },
        )
    totals = client.time_off_hours(
        "2026-08-01", "2026-08-31", {10: "PTO", 20: "Vacation"}, None
    )
    assert len(totals) == 1
    assert totals[0].hours == Decimal("8.0")
    assert totals[0].entry_ids == (910, 920)


@responses.activate
def test_project_filter_excludes_other_projects(harvest_client):
    responses.get(
        f"{HARVEST}/time_entries",
        json={
            "time_entries": [
                {"id": 1, "user": {"id": 5}, "spent_date": "2026-08-14",
                 "hours": 8.0, "project": {"id": 99}},
                {"id": 2, "user": {"id": 5}, "spent_date": "2026-08-15",
                 "hours": 8.0, "project": {"id": 1}},
            ],
            "links": {"next": None},
        },
    )
    totals = harvest_client.time_off_hours(
        "2026-08-01", "2026-08-31", {10: "PTO"}, {1}
    )
    assert [t.date for t in totals] == ["2026-08-15"]


@responses.activate
def test_missing_harvest_task_fails_loudly(harvest_client):
    responses.get(
        f"{HARVEST}/tasks",
        json={"tasks": [{"id": 1, "name": "Development"}], "links": {"next": None}},
    )
    with pytest.raises(ApiError, match="do not exist in Harvest"):
        harvest_client.time_off_task_ids()
