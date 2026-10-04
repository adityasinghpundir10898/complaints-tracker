"""All permission checks live here: visible_complaints() and can()."""

from __future__ import annotations

from django.db.models import QuerySet

from complaints.models import Complaint
from complaints.workflow import Action, available_actions
from core.roles import Role

# Case actions that are not state transitions.
REGISTER = "register"
ATTACH_DOCUMENT = "attach_document"
ADD_NOTE = "add_note"
EDIT_DETAILS = "edit_details"

_CLERK_ACTIONS = {
    REGISTER,
    ATTACH_DOCUMENT,
    ADD_NOTE,
    EDIT_DETAILS,
    Action.ISSUE_NOTICE,
    Action.REISSUE_NOTICE,
    Action.RECORD_REPORT,
    Action.SUBMIT_TO_PORTAL,
    Action.PORTAL_ACCEPTED,
    Action.PORTAL_REJECTED,
}
_REVIEWER_ACTIONS = _CLERK_ACTIONS | {
    Action.REVIEW_SATISFIED,
    Action.REVIEW_NOT_SATISFIED,
    Action.CLOSE_WITHOUT_PORTAL,
    Action.REOPEN,
}
_ADMIN_ACTIONS = _REVIEWER_ACTIONS  # Admin can do everything Reviewer can
_OFFICER_ACTIONS = {ATTACH_DOCUMENT, ADD_NOTE}


def _roles(user) -> set[str]:
    return user.role_names if user.is_authenticated else set()


def visible_complaints(user) -> QuerySet[Complaint]:
    """Department scoping is enforced here, in the query."""
    if not user.is_authenticated:
        return Complaint.objects.none()
    qs = Complaint.objects.all()
    roles = _roles(user)
    if not user.office_id:
        return qs.none()
    qs = qs.filter(office_id=user.office_id)
    if roles & {Role.CLERK, Role.REVIEWER, Role.ADMIN, Role.AUDITOR}:
        return qs
    if Role.DEPARTMENT_OFFICER in roles and user.department_id:
        return qs.filter(current_department_id=user.department_id)
    return qs.none()


def can(user, action: str, complaint: Complaint | None = None) -> bool:
    """May this user perform `action` (optionally on this complaint)?

    Only roles grant rights; Django's superuser flag has no special meaning here.
    Auditors are read-only.
    """
    if not user.is_authenticated:
        return False
    if complaint is not None and not visible_complaints(user).filter(pk=complaint.pk).exists():
        return False
    roles = _roles(user)
    allowed: set = set()
    if Role.CLERK in roles:
        allowed |= _CLERK_ACTIONS
    if Role.REVIEWER in roles:
        allowed |= _REVIEWER_ACTIONS
    if Role.ADMIN in roles:
        allowed |= _ADMIN_ACTIONS
    if Role.DEPARTMENT_OFFICER in roles and complaint is not None:
        # visible_complaints() already limited the case to the officer's own department.
        allowed |= _OFFICER_ACTIONS
    return action in allowed


def allowed_actions(user, complaint: Complaint) -> list[str]:
    """State-valid actions the user may take, for rendering buttons."""
    return [str(a) for a in available_actions(complaint) if can(user, a, complaint)]
