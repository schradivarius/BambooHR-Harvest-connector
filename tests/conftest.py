from __future__ import annotations

from decimal import Decimal

import pytest

from pto_recon.models import Employee, RequestDay

TOLERANCE = Decimal("0.01")
MAX_CREDIT = Decimal("4.0")
MAX_DEBIT = Decimal("2.0")


@pytest.fixture
def thresholds() -> dict:
    return {
        "tolerance_hours": TOLERANCE,
        "max_auto_credit_hours": MAX_CREDIT,
        "max_auto_debit_hours": MAX_DEBIT,
    }


@pytest.fixture
def matched_employee() -> Employee:
    return Employee(
        bamboo_id="142",
        email="jsmith@example.com",
        display_name="Jonathan Smith",
        harvest_id=987654,
    )


@pytest.fixture
def unmatched_employee() -> Employee:
    return Employee(
        bamboo_id="207",
        email="nobody@example.com",
        display_name="Dana Wu",
        harvest_id=None,
    )


@pytest.fixture
def request_day() -> RequestDay:
    return RequestDay(
        request_id="5001",
        bamboo_employee_id="142",
        time_off_type_id="78",
        time_off_type_name="PTO",
        date="2026-08-14",
        requested_hours=Decimal("8.00"),
    )
