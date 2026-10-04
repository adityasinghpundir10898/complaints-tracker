from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from complaints.models import (
    Document,
    Event,
    EventType,
    InquiryReport,
    PortalSubmission,
    Referral,
    State,
)
from complaints.workflow import (
    Action,
    InvalidTransition,
    InvalidTransitionData,
    apply_transition,
    available_actions,
)

# Written out independently of workflow.RULES so a change to the rules fails a test.
LEGAL = {
    Action.ISSUE_NOTICE: (State.RECEIVED, State.AWAITING_REPORT, EventType.NOTICE_ISSUED),
    Action.REISSUE_NOTICE: (State.AWAITING_REPORT, State.AWAITING_REPORT, EventType.NOTICE_ISSUED),
    Action.RECORD_REPORT: (State.AWAITING_REPORT, State.UNDER_REVIEW, EventType.REPORT_RECEIVED),
    Action.REVIEW_NOT_SATISFIED: (
        State.UNDER_REVIEW,
        State.AWAITING_REPORT,
        EventType.REVIEW_NOT_SATISFIED,
    ),
    Action.REVIEW_SATISFIED: (State.UNDER_REVIEW, State.APPROVED, EventType.REVIEW_SATISFIED),
    Action.SUBMIT_TO_PORTAL: (State.APPROVED, State.PORTAL_SUBMITTED, EventType.PORTAL_SUBMITTED),
    Action.PORTAL_ACCEPTED: (State.PORTAL_SUBMITTED, State.CLOSED, EventType.PORTAL_ACCEPTED),
    Action.PORTAL_REJECTED: (State.PORTAL_SUBMITTED, State.APPROVED, EventType.PORTAL_REJECTED),
    Action.CLOSE_WITHOUT_PORTAL: (State.APPROVED, State.CLOSED, EventType.CLOSED),
    Action.REOPEN: (State.CLOSED, State.UNDER_REVIEW, EventType.REOPENED),
}
ILLEGAL = [
    (state, action)
    for action, (from_state, _, _) in LEGAL.items()
    for state in State
    if state != from_state
]


@pytest.fixture
def t(complaint, clerk, reviewer, tehsildar, make_document):
    """Helpers to drive one complaint through the loop."""

    class Driver:
        def __init__(self):
            self.complaint = complaint
            self.n = 0

        def go(self, action, actor=None, **data):
            return apply_transition(self.complaint, action, actor or clerk, **data)

        def data(self, action, **over):
            c = self.complaint
            d = {
                Action.ISSUE_NOTICE: {"department": tehsildar},
                Action.REISSUE_NOTICE: {"outcome": "no_response"},
                Action.RECORD_REPORT: {"document": make_document(c, Document.Kind.INQUIRY_REPORT)},
                Action.REVIEW_NOT_SATISFIED: {"note": "Incomplete"},
                Action.REVIEW_SATISFIED: {"note": "Fine"},
                Action.SUBMIT_TO_PORTAL: {"document": make_document(c, Document.Kind.ATR)},
                Action.PORTAL_ACCEPTED: {},
                Action.PORTAL_REJECTED: {"reason": "Wrong format", "next_step": "resubmit"},
                Action.CLOSE_WITHOUT_PORTAL: {"reason": "No portal upload needed"},
                Action.REOPEN: {"reason": "New facts"},
            }[action]
            return d | over

        def do(self, action, **over):
            return self.go(action, **self.data(action, **over))

        def to_state(self, state):
            path = {
                State.RECEIVED: [],
                State.AWAITING_REPORT: [Action.ISSUE_NOTICE],
                State.UNDER_REVIEW: [Action.ISSUE_NOTICE, Action.RECORD_REPORT],
                State.APPROVED: [
                    Action.ISSUE_NOTICE,
                    Action.RECORD_REPORT,
                    Action.REVIEW_SATISFIED,
                ],
                State.PORTAL_SUBMITTED: [
                    Action.ISSUE_NOTICE,
                    Action.RECORD_REPORT,
                    Action.REVIEW_SATISFIED,
                    Action.SUBMIT_TO_PORTAL,
                ],
                State.CLOSED: [
                    Action.ISSUE_NOTICE,
                    Action.RECORD_REPORT,
                    Action.REVIEW_SATISFIED,
                    Action.SUBMIT_TO_PORTAL,
                    Action.PORTAL_ACCEPTED,
                ],
            }[state]
            for action in path:
                self.do(action)
            assert self.complaint.current_state == state

    return Driver()


@pytest.mark.parametrize("action", list(LEGAL))
def test_legal_transition(t, action):
    from_state, to_state, event_type = LEGAL[action]
    t.to_state(from_state)
    events_before = Event.objects.filter(complaint=t.complaint).count()

    event = t.do(action)

    t.complaint.refresh_from_db()
    assert t.complaint.current_state == to_state
    assert event.type == event_type
    assert event.payload["from_state"] == from_state
    assert event.payload["to_state"] == to_state
    assert Event.objects.filter(complaint=t.complaint).count() == events_before + 1


@pytest.mark.parametrize(("state", "action"), ILLEGAL)
def test_illegal_transition(t, state, action):
    t.to_state(state)
    events_before = Event.objects.filter(complaint=t.complaint).count()

    with pytest.raises(InvalidTransition):
        t.do(action)

    t.complaint.refresh_from_db()
    assert t.complaint.current_state == state
    assert Event.objects.filter(complaint=t.complaint).count() == events_before


def test_unknown_action(t):
    with pytest.raises(InvalidTransition):
        t.go("teleport")


@pytest.mark.parametrize("state", list(State))
def test_available_actions_match_legal_table(t, state):
    t.to_state(state)
    expected = {a for a, (from_state, _, _) in LEGAL.items() if from_state == state}
    assert set(available_actions(t.complaint)) == expected


@pytest.mark.parametrize(
    ("state", "action", "bad"),
    [
        (State.RECEIVED, Action.ISSUE_NOTICE, {"department": None}),
        (State.AWAITING_REPORT, Action.REISSUE_NOTICE, {"outcome": None}),
        (State.AWAITING_REPORT, Action.REISSUE_NOTICE, {"outcome": "report_received"}),
        (State.UNDER_REVIEW, Action.REVIEW_NOT_SATISFIED, {"note": ""}),
        (State.PORTAL_SUBMITTED, Action.PORTAL_REJECTED, {"reason": ""}),
        (State.PORTAL_SUBMITTED, Action.PORTAL_REJECTED, {"next_step": None}),
        (State.PORTAL_SUBMITTED, Action.PORTAL_REJECTED, {"next_step": "give_up"}),
        (State.APPROVED, Action.CLOSE_WITHOUT_PORTAL, {"reason": "  "}),
        (State.CLOSED, Action.REOPEN, {"reason": ""}),
    ],
)
def test_missing_or_bad_data_changes_nothing(t, state, action, bad):
    t.to_state(state)
    data = t.data(action, **bad)
    events_before = Event.objects.filter(complaint=t.complaint).count()
    referrals_before = list(Referral.objects.filter(complaint=t.complaint).values())

    with pytest.raises(InvalidTransitionData):
        t.go(action, **data)

    t.complaint.refresh_from_db()
    assert t.complaint.current_state == state
    assert Event.objects.filter(complaint=t.complaint).count() == events_before
    assert list(Referral.objects.filter(complaint=t.complaint).values()) == referrals_before


def test_unexpected_data_is_rejected(t):
    with pytest.raises(InvalidTransitionData):
        t.go(
            Action.ISSUE_NOTICE, department=t.data(Action.ISSUE_NOTICE)["department"], colour="red"
        )


def test_document_of_another_complaint_is_rejected(t, make_complaint, make_document):
    other = make_complaint()
    foreign = make_document(other, Document.Kind.INQUIRY_REPORT)
    t.to_state(State.AWAITING_REPORT)
    with pytest.raises(InvalidTransitionData):
        t.go(Action.RECORD_REPORT, document=foreign)


def test_inactive_or_foreign_department_is_rejected(t, tehsildar):
    tehsildar.is_active = False
    tehsildar.save()
    with pytest.raises(InvalidTransitionData):
        t.go(Action.ISSUE_NOTICE, department=tehsildar)


# --------------------------------------------------------- numbering and rules


def test_round_numbers_increase_across_all_ways_of_issuing_a_notice(t):
    t.do(Action.ISSUE_NOTICE)
    t.do(Action.REISSUE_NOTICE)
    t.do(Action.RECORD_REPORT)
    t.do(Action.REVIEW_NOT_SATISFIED)
    rounds = list(Referral.objects.filter(complaint=t.complaint).values_list("round_no", flat=True))
    assert rounds == [1, 2, 3]


def test_only_the_latest_referral_stays_open(t):
    t.do(Action.ISSUE_NOTICE)
    t.do(Action.REISSUE_NOTICE, outcome="no_response")
    t.do(Action.REISSUE_NOTICE, outcome="superseded")
    referrals = list(Referral.objects.filter(complaint=t.complaint))
    assert [r.outcome for r in referrals] == ["no_response", "superseded", ""]
    assert [r.closed_on is None for r in referrals] == [False, False, True]


def test_second_open_referral_is_blocked_by_the_database(t, tehsildar, clerk):
    from django.db import IntegrityError, transaction

    t.do(Action.ISSUE_NOTICE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Referral.objects.create(
            office=t.complaint.office,
            complaint=t.complaint,
            round_no=2,
            to_department=tehsildar,
            issued_on=date.today(),
            due_on=date.today(),
            created_by=clerk,
        )


def test_duplicate_round_number_is_blocked_by_the_database(t, tehsildar, clerk):
    from django.db import IntegrityError, transaction

    t.do(Action.ISSUE_NOTICE)
    t.do(Action.REISSUE_NOTICE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Referral.objects.create(
            office=t.complaint.office,
            complaint=t.complaint,
            round_no=1,
            to_department=tehsildar,
            issued_on=date.today(),
            due_on=date.today(),
            closed_on=date.today(),
            created_by=clerk,
        )


def test_attempt_numbers_increase_and_one_pending_at_a_time(t, make_document, clerk):
    from django.db import IntegrityError, transaction

    t.to_state(State.PORTAL_SUBMITTED)
    t.do(Action.PORTAL_REJECTED, next_step="resubmit")
    t.do(Action.SUBMIT_TO_PORTAL)
    attempts = list(
        PortalSubmission.objects.filter(complaint=t.complaint).values_list("attempt_no", "outcome")
    )
    assert attempts == [(1, "rejected"), (2, "pending")]

    with pytest.raises(IntegrityError), transaction.atomic():
        PortalSubmission.objects.create(
            office=t.complaint.office,
            complaint=t.complaint,
            attempt_no=3,
            submitted_on=date.today(),
            document=make_document(t.complaint),
            created_by=clerk,
        )


# ------------------------------------------------------------ derived fields


def test_derived_fields_follow_the_state(t, tehsildar, bdpo):
    at = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    t.go(Action.ISSUE_NOTICE, department=tehsildar, at=at)
    c = t.complaint
    assert (c.current_department, c.current_stage_started_at) == (tehsildar, at)

    at2 = at + timedelta(days=7)
    t.go(Action.RECORD_REPORT, **t.data(Action.RECORD_REPORT), at=at2)
    assert c.current_department is None
    assert c.current_stage_started_at == at2

    t.go(Action.REVIEW_NOT_SATISFIED, note="No", department=bdpo)
    assert c.current_department == bdpo
    assert c.closed_on is None


def test_due_date_comes_from_department_sla_or_category_override(
    make_complaint, clerk, tehsildar, bdpo, category
):
    plain = make_complaint(category=None)
    apply_transition(plain, Action.ISSUE_NOTICE, clerk, department=bdpo)
    issued = Referral.objects.get(complaint=plain)
    assert issued.due_on == issued.issued_on + timedelta(days=15)

    category.sla_days = 3
    category.save()
    with_category = make_complaint(category=category)
    apply_transition(with_category, Action.ISSUE_NOTICE, clerk, department=bdpo)
    assert Referral.objects.get(complaint=with_category).due_on == issued.issued_on + timedelta(
        days=3
    )

    explicit = make_complaint()
    due = date(2030, 1, 1)
    apply_transition(explicit, Action.ISSUE_NOTICE, clerk, department=tehsildar, due_on=due)
    assert Referral.objects.get(complaint=explicit).due_on == due


def test_closed_on_is_set_when_closed_and_cleared_on_reopen(t):
    t.to_state(State.CLOSED)
    assert t.complaint.closed_on is not None
    t.do(Action.REOPEN)
    assert t.complaint.closed_on is None


# --------------------------------------------------------------- specific rules


def test_reissue_may_change_the_department(t, bdpo):
    t.do(Action.ISSUE_NOTICE)
    t.do(Action.REISSUE_NOTICE, department=bdpo, outcome="superseded")
    assert t.complaint.current_department == bdpo


def test_record_report_closes_referral_and_creates_pending_report(t):
    t.to_state(State.UNDER_REVIEW)
    referral = Referral.objects.get(complaint=t.complaint)
    report = InquiryReport.objects.get(referral=referral)
    assert referral.outcome == Referral.Outcome.REPORT_RECEIVED
    assert referral.closed_on is not None
    assert report.review_outcome == InquiryReport.ReviewOutcome.PENDING


def test_review_not_satisfied_records_verdict_and_opens_next_round(t):
    t.to_state(State.UNDER_REVIEW)
    t.go(Action.REVIEW_NOT_SATISFIED, note="Needs site visit")
    report = InquiryReport.objects.get(referral__complaint=t.complaint)
    assert report.review_outcome == InquiryReport.ReviewOutcome.NOT_SATISFIED
    assert report.review_note == "Needs site visit"
    assert report.reviewed_by is not None
    assert (
        Referral.objects.filter(complaint=t.complaint, closed_on__isnull=True).get().round_no == 2
    )


@pytest.mark.parametrize(
    ("next_step", "state", "department_set", "new_referral"),
    [
        ("resubmit", State.APPROVED, False, False),
        ("review_again", State.UNDER_REVIEW, False, False),
        ("new_inquiry", State.AWAITING_REPORT, True, True),
    ],
)
def test_portal_rejection_next_steps(t, next_step, state, department_set, new_referral):
    t.to_state(State.PORTAL_SUBMITTED)
    referrals_before = Referral.objects.filter(complaint=t.complaint).count()

    t.do(Action.PORTAL_REJECTED, next_step=next_step)

    assert t.complaint.current_state == state
    assert (t.complaint.current_department is not None) == department_set
    assert Referral.objects.filter(complaint=t.complaint).count() == referrals_before + int(
        new_referral
    )
    submission = PortalSubmission.objects.get(complaint=t.complaint)
    assert submission.outcome == PortalSubmission.Outcome.REJECTED
    assert submission.rejection_reason == "Wrong format"


def test_review_again_and_reopen_send_the_report_back_to_pending(t):
    t.to_state(State.PORTAL_SUBMITTED)
    report = InquiryReport.objects.get(referral__complaint=t.complaint)
    assert report.review_outcome == InquiryReport.ReviewOutcome.SATISFIED

    event = t.do(Action.PORTAL_REJECTED, next_step="review_again")
    report.refresh_from_db()
    assert report.review_outcome == InquiryReport.ReviewOutcome.PENDING
    assert event.payload["previous_review"]["outcome"] == "satisfied"

    t.do(Action.REVIEW_SATISFIED)
    t.do(Action.CLOSE_WITHOUT_PORTAL)
    t.do(Action.REOPEN)
    report.refresh_from_db()
    assert report.review_outcome == InquiryReport.ReviewOutcome.PENDING


def test_report_can_be_recorded_without_a_file(t):
    t.to_state(State.AWAITING_REPORT)
    event = t.go(Action.RECORD_REPORT)
    report = InquiryReport.objects.get(referral__complaint=t.complaint)
    assert t.complaint.current_state == State.UNDER_REVIEW
    assert report.document is None
    assert event.payload["document_id"] is None


def test_portal_submission_can_be_recorded_without_a_file(t):
    t.to_state(State.APPROVED)
    event = t.go(Action.SUBMIT_TO_PORTAL, portal_ack_no="A-1")
    submission = PortalSubmission.objects.get(complaint=t.complaint)
    assert t.complaint.current_state == State.PORTAL_SUBMITTED
    assert submission.document is None
    assert event.payload["document_id"] is None


def test_portal_acceptance_can_store_the_ack_number(t):
    t.to_state(State.PORTAL_SUBMITTED)
    t.do(Action.PORTAL_ACCEPTED, portal_ack_no="ACK-77")
    assert PortalSubmission.objects.get(complaint=t.complaint).portal_ack_no == "ACK-77"


# ------------------------------------------------------------------ scenario


def test_full_scenario_three_notice_rounds_two_portal_rejections_then_closed(t, bdpo):
    t.do(Action.ISSUE_NOTICE)  # round 1
    t.do(Action.RECORD_REPORT)
    t.do(Action.REVIEW_NOT_SATISFIED)  # round 2
    t.do(Action.RECORD_REPORT)
    t.do(Action.REVIEW_NOT_SATISFIED, department=bdpo)  # round 3
    t.do(Action.RECORD_REPORT)
    t.do(Action.REVIEW_SATISFIED)
    t.do(Action.SUBMIT_TO_PORTAL)  # attempt 1
    t.do(Action.PORTAL_REJECTED, next_step="resubmit")
    t.do(Action.SUBMIT_TO_PORTAL)  # attempt 2
    t.do(Action.PORTAL_REJECTED, next_step="resubmit")
    t.do(Action.SUBMIT_TO_PORTAL)  # attempt 3
    t.do(Action.PORTAL_ACCEPTED)

    c = t.complaint
    assert c.current_state == State.CLOSED
    assert c.closed_on is not None
    assert Referral.objects.filter(complaint=c).count() == 3
    assert InquiryReport.objects.filter(referral__complaint=c).count() == 3
    assert list(
        PortalSubmission.objects.filter(complaint=c).values_list("attempt_no", "outcome")
    ) == [(1, "rejected"), (2, "rejected"), (3, "accepted")]
    types = list(Event.objects.filter(complaint=c).values_list("type", flat=True))
    assert types.count(EventType.NOTICE_ISSUED) == 1
    assert types.count(EventType.REVIEW_NOT_SATISFIED) == 2
    assert types.count(EventType.PORTAL_REJECTED) == 2
    assert types[-1] == EventType.PORTAL_ACCEPTED


def test_reopen_then_resubmit_after_acceptance(t):
    t.to_state(State.CLOSED)
    t.do(Action.REOPEN)
    t.do(Action.REVIEW_SATISFIED)
    t.do(Action.SUBMIT_TO_PORTAL)
    assert PortalSubmission.objects.filter(complaint=t.complaint).count() == 2


def test_state_cannot_be_set_directly_by_the_workflow_callers():
    """Guards the hard rule: only workflow.py assigns the derived fields."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    pattern = re.compile(r"\.(current_state|current_department|current_stage_started_at)\s*=[^=]")
    offenders = []
    for path in list(root.glob("complaints/**/*.py")) + list(root.glob("core/**/*.py")):
        if path.name == "workflow.py" or "tests" in path.parts or "migrations" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == []
