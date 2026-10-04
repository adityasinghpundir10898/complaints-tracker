"""The only place that changes a complaint's state.

Every status change goes through apply_transition(); no view, form, admin action or
signal may set current_state, current_department or current_stage_started_at directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from django.db import transaction
from django.utils import timezone

from complaints.models import (
    Complainant,
    Complaint,
    Document,
    Event,
    EventType,
    InquiryReport,
    PortalSubmission,
    Referral,
    State,
)
from complaints.services import documents as doc_service
from complaints.services.pendency import sla_days
from core.models import Department, Office


class TransitionError(Exception):
    pass


class InvalidTransition(TransitionError):
    """The action is not legal from the complaint's current state."""


class InvalidTransitionData(TransitionError):
    """The action is legal but its data is missing or wrong."""


class Action(StrEnum):
    ISSUE_NOTICE = "issue_notice"
    REISSUE_NOTICE = "reissue_notice"
    RECORD_REPORT = "record_report"
    REVIEW_NOT_SATISFIED = "review_not_satisfied"
    REVIEW_SATISFIED = "review_satisfied"
    SUBMIT_TO_PORTAL = "submit_to_portal"
    PORTAL_ACCEPTED = "portal_accepted"
    PORTAL_REJECTED = "portal_rejected"
    CLOSE_WITHOUT_PORTAL = "close_without_portal"
    REOPEN = "reopen"


NEXT_STEPS = {
    "resubmit": State.APPROVED,
    "review_again": State.UNDER_REVIEW,
    "new_inquiry": State.AWAITING_REPORT,
}
REISSUE_OUTCOMES = {Referral.Outcome.NO_RESPONSE, Referral.Outcome.SUPERSEDED}


@dataclass
class _Ctx:
    complaint: Complaint
    actor: Any
    data: dict[str, Any]
    at: datetime

    @property
    def today(self) -> date:
        return timezone.localdate(self.at)


@dataclass
class _Outcome:
    to_state: State
    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    department: Department | None = None
    closed_on: date | None = None


@dataclass(frozen=True)
class _Rule:
    from_states: frozenset[State]
    fields: frozenset[str]
    handler: Callable[[_Ctx], _Outcome]


# ---------------------------------------------------------------- helpers


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _require(ctx: _Ctx, key: str) -> Any:
    value = ctx.data.get(key)
    if _blank(value):
        raise InvalidTransitionData(f"'{key}' is required.")
    return value


def _document(ctx: _Ctx, key: str, *, required: bool) -> Document | None:
    doc = ctx.data.get(key)
    if doc is None:
        if required:
            raise InvalidTransitionData(f"'{key}' is required.")
        return None
    if doc.complaint_id != ctx.complaint.pk:
        raise InvalidTransitionData(f"'{key}' belongs to a different complaint.")
    if doc.is_deleted:
        raise InvalidTransitionData(f"'{key}' has been deleted.")
    return doc


def _department(ctx: _Ctx, default: Department | None = None) -> Department:
    dept = ctx.data.get("department") or default
    if dept is None:
        raise InvalidTransitionData("'department' is required.")
    if dept.office_id != ctx.complaint.office_id:
        raise InvalidTransitionData("Department belongs to a different office.")
    if not dept.is_active:
        raise InvalidTransitionData("Department is not active.")
    return dept


def _open_referral(complaint: Complaint) -> Referral:
    return Referral.objects.select_for_update().get(complaint=complaint, closed_on__isnull=True)


def _latest_report(complaint: Complaint) -> InquiryReport | None:
    return (
        InquiryReport.objects.select_for_update()
        .filter(referral__complaint=complaint)
        .order_by("-referral__round_no")
        .first()
    )


def _latest_referral(complaint: Complaint) -> Referral | None:
    return Referral.objects.filter(complaint=complaint).order_by("-round_no").first()


def _new_referral(ctx: _Ctx, department: Department) -> Referral:
    last = _latest_referral(ctx.complaint)
    due_on = ctx.data.get("due_on") or ctx.today + timedelta(
        days=sla_days(ctx.complaint.category, department)
    )
    return Referral.objects.create(
        office_id=ctx.complaint.office_id,
        complaint=ctx.complaint,
        round_no=(last.round_no if last else 0) + 1,
        to_department=department,
        issued_on=ctx.today,
        due_on=due_on,
        notice_document=_document(ctx, "notice_document", required=False),
        remarks=ctx.data.get("remarks", ""),
        created_by=ctx.actor,
    )


def _referral_payload(referral: Referral) -> dict[str, Any]:
    return {
        "referral_id": referral.pk,
        "round_no": referral.round_no,
        "department_id": referral.to_department_id,
        "department": str(referral.to_department),
        "due_on": referral.due_on.isoformat(),
        "notice_document_id": referral.notice_document_id,
    }


def _reset_review(report: InquiryReport | None) -> dict[str, Any]:
    """Send a report back to pending; the previous verdict stays in the event payload."""
    if report is None or report.review_outcome == InquiryReport.ReviewOutcome.PENDING:
        return {}
    previous = {"outcome": report.review_outcome, "note": report.review_note}
    report.review_outcome = InquiryReport.ReviewOutcome.PENDING
    report.review_note = ""
    report.reviewed_by = None
    report.reviewed_on = None
    report.save()
    return {"previous_review": previous}


def _log(
    complaint: Complaint, type_: EventType, actor: Any, at: datetime, payload: dict[str, Any]
) -> Event:
    return Event.objects.create(
        office_id=complaint.office_id,
        complaint=complaint,
        type=type_,
        actor=actor,
        occurred_at=at,
        payload=payload,
    )


# --------------------------------------------------------------- handlers


def _issue_notice(ctx: _Ctx) -> _Outcome:
    department = _department(ctx)
    referral = _new_referral(ctx, department)
    return _Outcome(
        State.AWAITING_REPORT, EventType.NOTICE_ISSUED, _referral_payload(referral), department
    )


def _reissue_notice(ctx: _Ctx) -> _Outcome:
    outcome = _require(ctx, "outcome")
    if outcome not in REISSUE_OUTCOMES:
        raise InvalidTransitionData("'outcome' must be no_response or superseded.")
    current = _open_referral(ctx.complaint)
    department = _department(ctx, default=current.to_department)
    current.closed_on = ctx.today
    current.outcome = outcome
    current.save()
    referral = _new_referral(ctx, department)
    payload = _referral_payload(referral) | {
        "closed_referral_id": current.pk,
        "closed_outcome": outcome,
    }
    return _Outcome(State.AWAITING_REPORT, EventType.NOTICE_ISSUED, payload, department)


def _record_report(ctx: _Ctx) -> _Outcome:
    document = _document(ctx, "document", required=False)
    received_on = ctx.data.get("received_on") or ctx.today
    referral = _open_referral(ctx.complaint)
    referral.closed_on = received_on
    referral.outcome = Referral.Outcome.REPORT_RECEIVED
    referral.save()
    report = InquiryReport.objects.create(
        office_id=ctx.complaint.office_id,
        referral=referral,
        received_on=received_on,
        document=document,
    )
    payload = {
        "report_id": report.pk,
        "referral_id": referral.pk,
        "round_no": referral.round_no,
        "department": str(referral.to_department),
        "document_id": document.pk if document else None,
    }
    return _Outcome(State.UNDER_REVIEW, EventType.REPORT_RECEIVED, payload)


def _review(ctx: _Ctx, outcome: InquiryReport.ReviewOutcome) -> tuple[InquiryReport, dict]:
    report = _latest_report(ctx.complaint)
    if report is None:
        raise InvalidTransitionData("No inquiry report to review.")
    report.review_outcome = outcome
    report.review_note = ctx.data.get("note", "")
    report.reviewed_by = ctx.actor
    report.reviewed_on = ctx.today
    report.save()
    return report, {"report_id": report.pk, "note": report.review_note}


def _review_satisfied(ctx: _Ctx) -> _Outcome:
    _, payload = _review(ctx, InquiryReport.ReviewOutcome.SATISFIED)
    return _Outcome(State.APPROVED, EventType.REVIEW_SATISFIED, payload)


def _review_not_satisfied(ctx: _Ctx) -> _Outcome:
    _require(ctx, "note")
    report, payload = _review(ctx, InquiryReport.ReviewOutcome.NOT_SATISFIED)
    department = _department(ctx, default=report.referral.to_department)
    referral = _new_referral(ctx, department)
    return _Outcome(
        State.AWAITING_REPORT,
        EventType.REVIEW_NOT_SATISFIED,
        payload | {"new_referral": _referral_payload(referral)},
        department,
    )


def _submit_to_portal(ctx: _Ctx) -> _Outcome:
    document = _document(ctx, "document", required=False)
    last = PortalSubmission.objects.filter(complaint=ctx.complaint).order_by("-attempt_no").first()
    submission = PortalSubmission.objects.create(
        office_id=ctx.complaint.office_id,
        complaint=ctx.complaint,
        attempt_no=(last.attempt_no if last else 0) + 1,
        submitted_on=ctx.data.get("submitted_on") or ctx.today,
        document=document,
        portal_ack_no=ctx.data.get("portal_ack_no", ""),
        created_by=ctx.actor,
    )
    payload = {
        "submission_id": submission.pk,
        "attempt_no": submission.attempt_no,
        "document_id": document.pk if document else None,
        "portal_ack_no": submission.portal_ack_no,
    }
    return _Outcome(State.PORTAL_SUBMITTED, EventType.PORTAL_SUBMITTED, payload)


def _pending_submission(complaint: Complaint) -> PortalSubmission:
    return PortalSubmission.objects.select_for_update().get(
        complaint=complaint, outcome=PortalSubmission.Outcome.PENDING
    )


def _portal_accepted(ctx: _Ctx) -> _Outcome:
    submission = _pending_submission(ctx.complaint)
    submission.outcome = PortalSubmission.Outcome.ACCEPTED
    submission.outcome_on = ctx.today
    if ctx.data.get("portal_ack_no"):
        submission.portal_ack_no = ctx.data["portal_ack_no"]
    submission.save()
    payload = {
        "submission_id": submission.pk,
        "attempt_no": submission.attempt_no,
        "portal_ack_no": submission.portal_ack_no,
        "closed": True,
    }
    return _Outcome(State.CLOSED, EventType.PORTAL_ACCEPTED, payload, closed_on=ctx.today)


def _portal_rejected(ctx: _Ctx) -> _Outcome:
    reason = _require(ctx, "reason")
    next_step = _require(ctx, "next_step")
    if next_step not in NEXT_STEPS:
        raise InvalidTransitionData(f"'next_step' must be one of {sorted(NEXT_STEPS)}.")
    submission = _pending_submission(ctx.complaint)
    submission.outcome = PortalSubmission.Outcome.REJECTED
    submission.outcome_on = ctx.today
    submission.rejection_reason = reason
    submission.save()
    payload: dict[str, Any] = {
        "submission_id": submission.pk,
        "attempt_no": submission.attempt_no,
        "reason": reason,
        "next_step": next_step,
    }
    department = None
    if next_step == "review_again":
        payload |= _reset_review(_latest_report(ctx.complaint))
    elif next_step == "new_inquiry":
        last = _latest_referral(ctx.complaint)
        department = _department(ctx, default=last.to_department if last else None)
        payload["new_referral"] = _referral_payload(_new_referral(ctx, department))
    return _Outcome(NEXT_STEPS[next_step], EventType.PORTAL_REJECTED, payload, department)


def _close_without_portal(ctx: _Ctx) -> _Outcome:
    reason = _require(ctx, "reason")
    return _Outcome(
        State.CLOSED, EventType.CLOSED, {"reason": reason, "portal": False}, closed_on=ctx.today
    )


def _reopen(ctx: _Ctx) -> _Outcome:
    reason = _require(ctx, "reason")
    payload = {"reason": reason} | _reset_review(_latest_report(ctx.complaint))
    return _Outcome(State.UNDER_REVIEW, EventType.REOPENED, payload)


def _states(*states: State) -> frozenset[State]:
    return frozenset(states)


def _f(*names: str) -> frozenset[str]:
    return frozenset(names) | {"at"}


_NOTICE_FIELDS = ("department", "due_on", "notice_document", "remarks")

RULES: dict[Action, _Rule] = {
    Action.ISSUE_NOTICE: _Rule(_states(State.RECEIVED), _f(*_NOTICE_FIELDS), _issue_notice),
    Action.REISSUE_NOTICE: _Rule(
        _states(State.AWAITING_REPORT), _f("outcome", *_NOTICE_FIELDS), _reissue_notice
    ),
    Action.RECORD_REPORT: _Rule(
        _states(State.AWAITING_REPORT), _f("document", "received_on"), _record_report
    ),
    Action.REVIEW_NOT_SATISFIED: _Rule(
        _states(State.UNDER_REVIEW), _f("note", *_NOTICE_FIELDS), _review_not_satisfied
    ),
    Action.REVIEW_SATISFIED: _Rule(_states(State.UNDER_REVIEW), _f("note"), _review_satisfied),
    Action.SUBMIT_TO_PORTAL: _Rule(
        _states(State.APPROVED), _f("document", "submitted_on", "portal_ack_no"), _submit_to_portal
    ),
    Action.PORTAL_ACCEPTED: _Rule(
        _states(State.PORTAL_SUBMITTED), _f("portal_ack_no"), _portal_accepted
    ),
    Action.PORTAL_REJECTED: _Rule(
        _states(State.PORTAL_SUBMITTED),
        _f("reason", "next_step", *_NOTICE_FIELDS),
        _portal_rejected,
    ),
    Action.CLOSE_WITHOUT_PORTAL: _Rule(
        _states(State.APPROVED), _f("reason"), _close_without_portal
    ),
    Action.REOPEN: _Rule(_states(State.CLOSED), _f("reason"), _reopen),
}


def available_actions(complaint: Complaint) -> list[Action]:
    """Actions that are legal for the complaint's current state."""
    return [a for a, rule in RULES.items() if complaint.current_state in rule.from_states]


def apply_transition(complaint: Complaint, action: str, actor: Any, **data: Any) -> Event:
    """Validate, apply and log one state change in a single transaction."""
    try:
        action = Action(action)
    except ValueError:
        raise InvalidTransition(f"Unknown action '{action}'.") from None
    rule = RULES[action]

    with transaction.atomic():
        locked = Complaint.objects.select_for_update().get(pk=complaint.pk)
        if locked.current_state not in rule.from_states:
            raise InvalidTransition(
                f"'{action}' is not allowed when the complaint is {locked.current_state}."
            )
        unexpected = set(data) - rule.fields
        if unexpected:
            raise InvalidTransitionData(f"Unexpected data for '{action}': {sorted(unexpected)}.")

        at = data.pop("at", None) or timezone.now()
        outcome = rule.handler(_Ctx(locked, actor, data, at))

        from_state = locked.current_state
        locked.current_state = outcome.to_state
        locked.current_department = outcome.department
        locked.current_stage_started_at = at
        locked.closed_on = outcome.closed_on
        locked.save()

        payload = {
            "action": str(action),
            "from_state": from_state,
            "to_state": outcome.to_state,
        } | outcome.payload
        event = _log(locked, outcome.event_type, actor, at, payload)

    complaint.refresh_from_db()
    return event


# ------------------------------------------------- changes that keep the state


def register_complaint(
    *,
    office: Office,
    complainant: Complainant,
    actor: Any,
    source_platform: str,
    received_on: date,
    subject: str,
    description: str = "",
    external_ref: str = "",
    village=None,
    category=None,
    tags=(),
    related_to: Complaint | None = None,
    attachment: doc_service.Attachment | None = None,
    ref_no: str | None = None,
    at: datetime | None = None,
) -> Complaint:
    """Create a complaint in RECEIVED, with its COMPLAINT_REGISTERED event."""
    at = at or timezone.now()
    with transaction.atomic():
        # Locking the office row serialises ref_no generation between clerks.
        Office.objects.select_for_update().get(pk=office.pk)
        complaint = Complaint.objects.create(
            office=office,
            ref_no=ref_no or next_ref_no(office, received_on.year),
            source_platform=source_platform,
            external_ref=external_ref,
            received_on=received_on,
            complainant=complainant,
            village=village,
            category=category,
            subject=subject,
            description=description,
            related_to=related_to,
            current_state=State.RECEIVED,
            current_stage_started_at=at,
            created_by=actor,
        )
        complaint.tags.set(tags)
        payload: dict[str, Any] = {"platform": source_platform}
        if related_to is not None:
            payload["related_to"] = related_to.ref_no
        if attachment is not None:
            document = doc_service.store_document(
                complaint, attachment, Document.Kind.COMPLAINT, actor
            )
            payload["document_id"] = document.pk
        _log(complaint, EventType.COMPLAINT_REGISTERED, actor, at, payload)
    return complaint


def next_ref_no(office: Office, year: int) -> str:
    prefix = f"C-{year}-"
    last = (
        Complaint.objects.filter(office=office, ref_no__startswith=prefix)
        .order_by("-ref_no")
        .values_list("ref_no", flat=True)
        .first()
    )
    number = int(last.removeprefix(prefix)) + 1 if last else 1
    return f"{prefix}{number:04d}"


def attach_document(
    complaint: Complaint,
    actor: Any,
    attachment: doc_service.Attachment,
    kind: str,
    supersedes: Document | None = None,
) -> Document:
    with transaction.atomic():
        document = doc_service.store_document(complaint, attachment, kind, actor, supersedes)
        payload = {"document_id": document.pk, "kind": kind}
        if supersedes is not None:
            payload["supersedes_id"] = supersedes.pk
        _log(complaint, EventType.DOCUMENT_ATTACHED, actor, timezone.now(), payload)
    return document


def add_note(complaint: Complaint, actor: Any, text: str) -> Event:
    if _blank(text):
        raise InvalidTransitionData("Note text is required.")
    return _log(complaint, EventType.NOTE_ADDED, actor, timezone.now(), {"note": text.strip()})


EDITABLE_COMPLAINT_FIELDS = (
    "source_platform",
    "external_ref",
    "received_on",
    "village",
    "category",
    "subject",
    "description",
)
EDITABLE_COMPLAINANT_FIELDS = ("name", "phone", "address")


def edit_details(complaint: Complaint, actor: Any, **changes: Any) -> Event | None:
    """Edit descriptive fields; logs old and new values. Never touches workflow fields."""
    complainant_changes = {
        k.removeprefix("complainant_"): v
        for k, v in changes.items()
        if k.startswith("complainant_")
    }
    complaint_changes = {k: v for k, v in changes.items() if not k.startswith("complainant_")}
    unknown = (set(complaint_changes) - set(EDITABLE_COMPLAINT_FIELDS)) | (
        set(complainant_changes) - set(EDITABLE_COMPLAINANT_FIELDS)
    )
    if unknown:
        raise InvalidTransitionData(f"Fields cannot be edited: {sorted(unknown)}.")

    targets = (
        (complaint, complaint_changes, ""),
        (complaint.complainant, complainant_changes, "complainant_"),
    )
    with transaction.atomic():
        diff: dict[str, dict[str, str]] = {}
        for target, values, prefix in targets:
            changed: list[str] = []
            for name, new in values.items():
                old = getattr(target, name)
                if old != new:
                    diff[prefix + name] = {"old": str(old or ""), "new": str(new or "")}
                    setattr(target, name, new)
                    changed.append(name)
            if changed:
                # update_fields keeps a stale instance from overwriting workflow fields.
                target.save(update_fields=[*changed, "updated_at"])
        if not diff:
            return None
        return _log(complaint, EventType.DETAILS_EDITED, actor, timezone.now(), {"changes": diff})
