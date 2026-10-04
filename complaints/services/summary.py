from __future__ import annotations

from datetime import date

from django.utils import timezone

from complaints.models import Complaint, EventType, Platform
from complaints.services.pendency import current_location, days_at_current_office

_ORDINALS = {1: "", 2: "Second ", 3: "Third ", 4: "Fourth ", 5: "Fifth "}


def _day(value) -> str:
    local = timezone.localtime(value).date() if hasattr(value, "hour") else value
    return f"{local.day} {local:%b}"


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"Round {n} ")


def _append_to_last(parts: list[str], suffix: str, fallback: str) -> None:
    """Merge a review verdict into the report sentence; fall back to its own sentence."""
    if parts and parts[-1].startswith("Report received"):
        parts[-1] = parts[-1].removesuffix(".") + f", {suffix}."
    else:
        parts.append(fallback)


def build_summary(complaint: Complaint, today: date | None = None) -> str:
    """One plain-English sentence per step, built only from the event log."""
    platforms = dict(Platform.choices)
    parts: list[str] = []
    for event in complaint.events.all():
        payload = event.payload
        when = _day(event.occurred_at)
        match event.type:
            case EventType.COMPLAINT_REGISTERED:
                source = platforms.get(payload.get("platform"), "")
                parts.append(f"Received {when}" + (f" from {source}." if source else "."))
            case EventType.NOTICE_ISSUED:
                label = _ordinal(payload.get("round_no", 1))
                notice = "Notice" if not label else "notice"
                parts.append(
                    f"{label}{notice} to {payload.get('department', 'department')} {when}."
                )
            case EventType.REPORT_RECEIVED:
                parts.append(f"Report received {when}.")
            case EventType.REVIEW_SATISFIED:
                _append_to_last(parts, "satisfied", f"Review satisfied {when}.")
            case EventType.REVIEW_NOT_SATISFIED:
                _append_to_last(parts, "not satisfied", f"Review not satisfied {when}.")
                new = payload.get("new_referral", {})
                if new:
                    label = _ordinal(new.get("round_no", 1))
                    parts.append(f"{label}notice to {new.get('department', 'department')} {when}.")
            case EventType.PORTAL_SUBMITTED:
                parts.append(f"Uploaded to CM portal {when} (attempt {payload.get('attempt_no')}).")
            case EventType.PORTAL_ACCEPTED:
                parts.append(f"Accepted by CM portal {when}; closed.")
            case EventType.PORTAL_REJECTED:
                parts.append(f"Rejected by CM portal {when}: {payload.get('reason', '')}.")
                new = payload.get("new_referral", {})
                if new:
                    label = _ordinal(new.get("round_no", 1))
                    parts.append(f"{label}notice to {new.get('department', 'department')} {when}.")
            case EventType.CLOSED:
                parts.append(f"Closed {when}.")
            case EventType.REOPENED:
                parts.append(f"Reopened {when}.")
            case _:
                pass

    if complaint.is_open:
        days = days_at_current_office(complaint, today)
        unit = "day" if days == 1 else "days"
        parts.append(f"Pending at {current_location(complaint)} for {days} {unit}.")
    return " ".join(parts)
