from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from complaints.models import Complaint, State
from complaints.services.pendency import (
    ageing_bucket,
    bucket_day_range,
    bucket_for_days,
    current_location,
    days_at_current_office,
    sla_days,
    total_age_days,
)
from complaints.workflow import Action, apply_transition


def at_noon(day: date) -> datetime:
    # 12:00 UTC is 17:30 in Asia/Kolkata, the same calendar day.
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("days", "bucket"),
    [
        (0, "safe"),
        (2, "safe"),
        (3, "warning"),
        (6, "warning"),
        (7, "overdue"),
        (9, "overdue"),
        (10, "critical"),
        (45, "critical"),
    ],
)
def test_bucket_boundaries(days, bucket):
    assert bucket_for_days(days) == bucket


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [("safe", (0, 2)), ("warning", (3, 6)), ("overdue", (7, 9)), ("critical", (10, None))],
)
def test_bucket_ranges(bucket, expected):
    assert bucket_day_range(bucket) == expected


@override_settings(AGEING_THRESHOLDS={"safe": 0, "warning": 2, "overdue": 5, "critical": 8})
def test_thresholds_come_from_settings():
    assert bucket_for_days(2) == "warning"
    assert bucket_for_days(8) == "critical"
    assert bucket_day_range("warning") == (2, 4)


def test_days_at_current_office_counts_calendar_days(make_complaint):
    c = make_complaint(at=at_noon(date(2026, 9, 13)))
    assert days_at_current_office(c, today=date(2026, 9, 13)) == 0
    assert days_at_current_office(c, today=date(2026, 9, 16)) == 3
    assert days_at_current_office(c, today=date(2026, 10, 1)) == 18


def test_a_late_evening_stage_start_counts_as_that_local_day(make_complaint):
    # 20:00 UTC on the 13th is 01:30 on the 14th in Asia/Kolkata.
    c = make_complaint(at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC))
    assert days_at_current_office(c, today=date(2026, 9, 14)) == 0


def test_stage_clock_restarts_on_each_transition(make_complaint, clerk, tehsildar):
    c = make_complaint(received_on=date(2026, 9, 1), at=at_noon(date(2026, 9, 1)))
    apply_transition(
        c, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=at_noon(date(2026, 9, 10))
    )
    assert days_at_current_office(c, today=date(2026, 9, 12)) == 2
    assert total_age_days(c, today=date(2026, 9, 12)) == 11


def test_total_age_uses_closed_on_when_closed(make_complaint):
    c = make_complaint(received_on=date(2026, 9, 1))
    assert total_age_days(c, today=date(2026, 9, 21)) == 20
    c.closed_on = date(2026, 9, 11)
    assert total_age_days(c, today=date(2026, 9, 21)) == 10


def test_ageing_bucket_is_closed_for_closed_complaints(make_complaint):
    c = make_complaint(at=at_noon(date(2026, 1, 1)))
    assert ageing_bucket(c, today=date(2026, 9, 21)) == "critical"
    c.current_state = State.CLOSED
    assert ageing_bucket(c, today=date(2026, 9, 21)) == "closed"


def test_sla_days_category_overrides_department(category, tehsildar):
    assert sla_days(category, tehsildar) == 7
    assert sla_days(None, tehsildar) == 7
    category.sla_days = 3
    assert sla_days(category, tehsildar) == 3


def test_current_location(make_complaint, clerk, tehsildar, make_document):
    c = make_complaint()
    assert current_location(c) == "SDM office"
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    assert current_location(c) == "Tehsildar"
    apply_transition(c, Action.RECORD_REPORT, clerk, document=make_document(c))
    assert current_location(c) == "SDM office"
    apply_transition(c, Action.REVIEW_SATISFIED, clerk)
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk, document=make_document(c))
    assert current_location(c) == "CM portal"
    apply_transition(c, Action.PORTAL_ACCEPTED, clerk)
    assert current_location(c) == "Closed"


def test_complaint_ref_no_is_unique_per_office(make_complaint, office):
    first = make_complaint()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_complaint(ref_no=first.ref_no)
    assert Complaint.objects.filter(office=office, ref_no=first.ref_no).count() == 1
