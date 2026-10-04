from __future__ import annotations

from datetime import date

from django.conf import settings
from django.utils import timezone

from complaints.models import Complaint, State
from core.models import Category, Department

CLOSED_BUCKET = "closed"
# Display order, lowest to highest severity.
BUCKET_ORDER = ["safe", "warning", "overdue", "critical"]


def sla_days(category: Category | None, department: Department) -> int:
    """Category SLA overrides the department default when set."""
    if category is not None and category.sla_days is not None:
        return category.sla_days
    return department.default_sla_days


def days_at_current_office(complaint: Complaint, today: date | None = None) -> int:
    today = today or timezone.localdate()
    started = timezone.localtime(complaint.current_stage_started_at).date()
    return (today - started).days


def total_age_days(complaint: Complaint, today: date | None = None) -> int:
    end = complaint.closed_on or today or timezone.localdate()
    return (end - complaint.received_on).days


def bucket_for_days(days: int) -> str:
    thresholds = settings.AGEING_THRESHOLDS
    bucket = BUCKET_ORDER[0]
    for name in BUCKET_ORDER:
        if days >= thresholds[name]:
            bucket = name
    return bucket


def ageing_bucket(complaint: Complaint, today: date | None = None) -> str:
    if complaint.current_state == State.CLOSED:
        return CLOSED_BUCKET
    return bucket_for_days(days_at_current_office(complaint, today))


def bucket_day_range(bucket: str) -> tuple[int, int | None]:
    """Inclusive (min_days, max_days) for a bucket; max is None for the top bucket."""
    thresholds = settings.AGEING_THRESHOLDS
    idx = BUCKET_ORDER.index(bucket)
    low = thresholds[bucket]
    high = thresholds[BUCKET_ORDER[idx + 1]] - 1 if idx + 1 < len(BUCKET_ORDER) else None
    return low, high


def bucket_labels() -> list[tuple[str, str]]:
    """(bucket, label) pairs for the ageing chips, built from the configured thresholds."""
    names = {"safe": "Safe", "warning": "Warning", "overdue": "Overdue", "critical": "Critical"}
    labels = []
    for bucket in BUCKET_ORDER:
        low, high = bucket_day_range(bucket)
        span = f"{low}+" if high is None else f"{low}-{high}"
        labels.append((bucket, f"{names[bucket]} ({span} d)"))
    labels.append((CLOSED_BUCKET, "Closed"))
    return labels


def current_location(complaint: Complaint) -> str:
    if complaint.current_state == State.CLOSED:
        return "Closed"
    if complaint.current_state == State.PORTAL_SUBMITTED:
        return "CM portal"
    if complaint.current_department_id:
        return str(complaint.current_department)
    return "SDM office"
