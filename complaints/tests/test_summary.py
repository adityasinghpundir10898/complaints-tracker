from __future__ import annotations

from datetime import UTC, date, datetime

from complaints.services.summary import build_summary
from complaints.workflow import Action, apply_transition


def at(day: int, month: int = 9) -> datetime:
    return datetime(2026, month, day, 5, 0, tzinfo=UTC)


def test_summary_follows_the_event_log(make_complaint, clerk, reviewer, tehsildar, make_document):
    c = make_complaint(at=at(13), received_on=date(2026, 9, 13))
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=at(15))
    report = make_document(c)
    apply_transition(c, Action.RECORD_REPORT, clerk, document=report, at=at(22))
    apply_transition(c, Action.REVIEW_NOT_SATISFIED, reviewer, note="Incomplete", at=at(22))

    assert build_summary(c, today=date(2026, 9, 28)) == (
        "Received 13 Sep from CM Window. Notice to Tehsildar 15 Sep. "
        "Report received 22 Sep, not satisfied. Second notice to Tehsildar 22 Sep. "
        "Pending at Tehsildar for 6 days."
    )


def test_summary_for_a_closed_case_has_no_pending_sentence(
    make_complaint, clerk, reviewer, tehsildar, make_document
):
    c = make_complaint(at=at(1), received_on=date(2026, 9, 1))
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=at(2))
    apply_transition(c, Action.RECORD_REPORT, clerk, document=make_document(c), at=at(5))
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer, at=at(6))
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk, document=make_document(c), at=at(7))
    apply_transition(
        c, Action.PORTAL_REJECTED, clerk, reason="Unsigned", next_step="resubmit", at=at(8)
    )
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk, document=make_document(c), at=at(9))
    apply_transition(c, Action.PORTAL_ACCEPTED, clerk, at=at(10))

    summary = build_summary(c, today=date(2026, 9, 30))
    assert "Report received 5 Sep, satisfied." in summary
    assert "Uploaded to CM portal 7 Sep (attempt 1)." in summary
    assert "Rejected by CM portal 8 Sep: Unsigned." in summary
    assert "Uploaded to CM portal 9 Sep (attempt 2)." in summary
    assert summary.endswith("Accepted by CM portal 10 Sep; closed.")
    assert "Pending" not in summary


def test_summary_of_a_new_complaint_says_pending_at_sdm_office(make_complaint):
    c = make_complaint(at=at(13), received_on=date(2026, 9, 13))
    assert build_summary(c, today=date(2026, 9, 14)) == (
        "Received 13 Sep from CM Window. Pending at SDM office for 1 day."
    )


def test_summary_ignores_notes_and_attachments(complaint, clerk):
    from complaints.workflow import add_note

    before = build_summary(complaint, today=date(2026, 9, 14))
    add_note(complaint, clerk, "just a note")
    assert build_summary(complaint, today=date(2026, 9, 14)) == before
