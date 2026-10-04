from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser

from complaints import permissions as perms
from complaints.models import Complaint, State
from complaints.workflow import Action, apply_transition
from core.models import Office, User

STATE_ACTIONS = [str(a) for a in Action]
NON_STATE = [perms.REGISTER, perms.ATTACH_DOCUMENT, perms.ADD_NOTE, perms.EDIT_DETAILS]

ALL_CLERK = {
    perms.REGISTER,
    perms.ATTACH_DOCUMENT,
    perms.ADD_NOTE,
    perms.EDIT_DETAILS,
    Action.ISSUE_NOTICE,
    Action.REISSUE_NOTICE,
    Action.RECORD_REPORT,
    Action.SUBMIT_TO_PORTAL,
    Action.PORTAL_ACCEPTED,
    Action.PORTAL_REJECTED,
}
REVIEW_EXTRAS = {
    Action.REVIEW_SATISFIED,
    Action.REVIEW_NOT_SATISFIED,
    Action.CLOSE_WITHOUT_PORTAL,
    Action.REOPEN,
}


@pytest.fixture
def with_tehsildar(make_complaint, clerk, tehsildar) -> Complaint:
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    return c


@pytest.fixture
def with_bdpo(make_complaint, clerk, bdpo) -> Complaint:
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=bdpo)
    return c


@pytest.mark.parametrize("role", ["clerk", "reviewer", "auditor", "admin_role"])
def test_office_wide_roles_see_every_complaint_in_their_office(
    request, role, with_tehsildar, with_bdpo, complaint
):
    user = request.getfixturevalue(role)
    assert set(perms.visible_complaints(user)) == {with_tehsildar, with_bdpo, complaint}


def test_complaints_of_another_office_are_never_visible(clerk, complaint, make_complaint):
    other = Office.objects.create(name="Other", code="OTH", district="d", state="s")
    outsider = User.objects.create_user("outsider", password="x", office=other)
    assert list(perms.visible_complaints(outsider)) == []
    assert set(perms.visible_complaints(clerk)) == {complaint}


def test_department_officer_sees_only_cases_currently_with_their_department(
    officer, with_tehsildar, with_bdpo, complaint
):
    assert list(perms.visible_complaints(officer)) == [with_tehsildar]


def test_officer_loses_sight_once_the_case_leaves_their_department(
    officer, with_tehsildar, clerk, make_document
):
    apply_transition(
        with_tehsildar, Action.RECORD_REPORT, clerk, document=make_document(with_tehsildar)
    )
    assert list(perms.visible_complaints(officer)) == []


def test_user_without_role_or_login_sees_nothing(office, complaint):
    plain = User.objects.create_user("plain", password="x", office=office)
    assert list(perms.visible_complaints(plain)) == []
    assert list(perms.visible_complaints(AnonymousUser())) == []
    assert not perms.can(AnonymousUser(), perms.ADD_NOTE, complaint)


def test_superuser_flag_alone_grants_nothing_in_the_app(office, complaint):
    root = User.objects.create_superuser("root", password="x", office=office)
    assert list(perms.visible_complaints(root)) == []
    assert not any(perms.can(root, a) for a in [*STATE_ACTIONS, *NON_STATE])


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("clerk", ALL_CLERK),
        ("reviewer", ALL_CLERK | REVIEW_EXTRAS),
        ("auditor", set()),
        ("admin_role", ALL_CLERK | REVIEW_EXTRAS),
    ],
)
def test_permission_matrix(request, role, allowed, complaint):
    user = request.getfixturevalue(role)
    for action in [*STATE_ACTIONS, *NON_STATE]:
        assert perms.can(user, action, complaint) == (action in allowed), f"{role}: {action}"


def test_officer_can_only_attach_and_note_on_their_own_cases(officer, with_tehsildar, with_bdpo):
    for action in [*STATE_ACTIONS, *NON_STATE]:
        expected = action in {perms.ATTACH_DOCUMENT, perms.ADD_NOTE}
        assert perms.can(officer, action, with_tehsildar) == expected, action
        assert not perms.can(officer, action, with_bdpo), action


def test_auditor_is_read_only_even_for_state_changes(auditor, complaint):
    assert perms.allowed_actions(auditor, complaint) == []


def test_allowed_actions_combines_state_and_role(clerk, reviewer, complaint, with_tehsildar):
    assert perms.allowed_actions(clerk, complaint) == ["issue_notice"]
    assert perms.allowed_actions(clerk, with_tehsildar) == ["reissue_notice", "record_report"]
    assert State.AWAITING_REPORT == with_tehsildar.current_state
    assert perms.allowed_actions(reviewer, complaint) == ["issue_notice"]
