from django import template
from django.utils import timezone

from complaints.models import EventType

register = template.Library()


@register.filter
def event_label(event) -> str:
    return event.get_type_display()


@register.filter
def event_detail(event) -> str:
    """One short line of context for a timeline row, built from the event payload."""
    p = event.payload
    match event.type:
        case EventType.COMPLAINT_REGISTERED:
            return f"related to {p['related_to']}" if p.get("related_to") else ""
        case EventType.NOTICE_ISSUED:
            text = f"Round {p.get('round_no')} to {p.get('department')}, due {p.get('due_on')}"
            if p.get("closed_outcome"):
                text += f" (previous notice: {p['closed_outcome'].replace('_', ' ')})"
            return text
        case EventType.REPORT_RECEIVED:
            return f"From {p.get('department')} (round {p.get('round_no')})"
        case EventType.REVIEW_SATISFIED | EventType.REVIEW_NOT_SATISFIED:
            return p.get("note", "")
        case EventType.PORTAL_SUBMITTED:
            ack = f", ack {p['portal_ack_no']}" if p.get("portal_ack_no") else ""
            return f"Attempt {p.get('attempt_no')}{ack}"
        case EventType.PORTAL_ACCEPTED:
            return f"Attempt {p.get('attempt_no')}"
        case EventType.PORTAL_REJECTED:
            step = p.get("next_step", "").replace("_", " ")
            return f"Attempt {p.get('attempt_no')}: {p.get('reason', '')} (next: {step})"
        case EventType.CLOSED | EventType.REOPENED:
            return p.get("reason", "")
        case EventType.NOTE_ADDED:
            return p.get("note", "")
        case EventType.DOCUMENT_ATTACHED:
            return p.get("kind", "").replace("_", " ")
        case EventType.DETAILS_EDITED:
            return "; ".join(
                f"{name}: {v['old'] or '-'} -> {v['new'] or '-'}"
                for name, v in p.get("changes", {}).items()
            )
    return ""


@register.filter
def local_dt(value) -> str:
    local = timezone.localtime(value)
    return f"{local.day} {local:%b %Y, %H:%M}"
