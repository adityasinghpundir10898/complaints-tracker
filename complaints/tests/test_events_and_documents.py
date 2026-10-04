from __future__ import annotations

from datetime import date

import pytest

from complaints.models import (
    AppendOnlyError,
    Complainant,
    Complaint,
    Document,
    Event,
    EventType,
    State,
)
from complaints.services.documents import DuplicateDocument, storage_path
from complaints.tests.helpers import fake_attachment
from complaints.workflow import (
    InvalidTransitionData,
    add_note,
    attach_document,
    edit_details,
    next_ref_no,
    register_complaint,
)

# ------------------------------------------------------------------ Event


def test_events_cannot_be_updated_or_deleted(complaint):
    event = Event.objects.get(complaint=complaint)
    event.payload = {"tampered": True}
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        Event.objects.filter(pk=event.pk).update(type=EventType.CLOSED)
    with pytest.raises(AppendOnlyError):
        Event.objects.filter(pk=event.pk).delete()
    assert Event.objects.get(pk=event.pk).payload != {"tampered": True}


# ------------------------------------------------------------ registration


def test_register_creates_received_complaint_and_event(complaint, clerk):
    assert complaint.current_state == State.RECEIVED
    assert complaint.current_department is None
    event = Event.objects.get(complaint=complaint)
    assert event.type == EventType.COMPLAINT_REGISTERED
    assert event.actor == clerk
    assert event.payload["platform"] == complaint.source_platform


def test_ref_no_sequence_per_year(office, make_complaint):
    refs = [make_complaint(received_on=date(2026, 1, 5)).ref_no for _ in range(3)]
    assert refs == ["C-2026-0001", "C-2026-0002", "C-2026-0003"]
    assert make_complaint(received_on=date(2027, 1, 2)).ref_no == "C-2027-0001"
    assert next_ref_no(office, 2026) == "C-2026-0004"


def test_ref_no_sequence_is_per_office(make_complaint, clerk):
    from core.models import Office

    other = Office.objects.create(name="Other", code="OTH", district="d", state="s")
    make_complaint(received_on=date(2026, 1, 5))
    person = Complainant.objects.create(office=other, name="X")
    c = register_complaint(
        office=other,
        complainant=person,
        actor=clerk,
        source_platform="internal",
        received_on=date(2026, 1, 5),
        subject="s",
    )
    assert c.ref_no == "C-2026-0001"


def test_register_with_attachment_stores_a_complaint_document(make_complaint):
    c = make_complaint(attachment=fake_attachment("original"))
    doc = Document.objects.get(complaint=c)
    assert doc.kind == Document.Kind.COMPLAINT
    assert Event.objects.get(complaint=c).payload["document_id"] == doc.pk


def test_register_related_complaint_is_linked(make_complaint):
    first = make_complaint()
    second = make_complaint(related_to=first)
    assert second.related_to == first
    assert Event.objects.get(complaint=second).payload["related_to"] == first.ref_no


# --------------------------------------------------------------- documents


def test_document_is_stored_once_per_hash_across_complaints(make_complaint, clerk, media_root):
    from complaints.services.documents import store_document

    a, b = make_complaint(), make_complaint()
    same = fake_attachment("shared")
    doc_a = store_document(a, same, Document.Kind.OTHER, clerk)
    doc_b = store_document(b, fake_attachment("shared"), Document.Kind.OTHER, clerk)

    assert doc_a.file.name == doc_b.file.name == storage_path(same.sha256, "application/pdf")
    assert len(list(media_root.rglob("*.pdf"))) == 1
    assert Document.objects.filter(sha256=same.sha256).count() == 2


def test_same_file_cannot_be_attached_twice_to_one_complaint(complaint, clerk):
    attach_document(complaint, clerk, fake_attachment("twice"), Document.Kind.OTHER)
    with pytest.raises(DuplicateDocument):
        attach_document(complaint, clerk, fake_attachment("twice"), Document.Kind.OTHER)
    assert Document.objects.filter(complaint=complaint).count() == 1


def test_attach_document_logs_an_event_and_supports_supersedes(complaint, clerk):
    old = attach_document(complaint, clerk, fake_attachment("v1"), Document.Kind.NOTICE)
    new = attach_document(
        complaint, clerk, fake_attachment("v2"), Document.Kind.NOTICE, supersedes=old
    )
    assert new.supersedes == old
    old.refresh_from_db()
    assert old.is_deleted is False
    events = Event.objects.filter(complaint=complaint, type=EventType.DOCUMENT_ATTACHED)
    assert events.count() == 2
    assert events.last().payload["supersedes_id"] == old.pk


def test_documents_cannot_be_hard_deleted(complaint, make_document):
    doc = make_document(complaint)
    with pytest.raises(NotImplementedError):
        doc.delete()


def test_cannot_supersede_a_document_of_another_complaint(make_complaint, make_document, clerk):
    a, b = make_complaint(), make_complaint()
    foreign = make_document(a)
    with pytest.raises(ValueError):
        attach_document(b, clerk, fake_attachment("x"), Document.Kind.OTHER, supersedes=foreign)


# ---------------------------------------------------------- notes and edits


def test_add_note_logs_event_without_touching_state(complaint, clerk):
    before = (complaint.current_state, complaint.current_stage_started_at)
    event = add_note(complaint, clerk, "  Called complainant  ")
    complaint.refresh_from_db()
    assert event.type == EventType.NOTE_ADDED
    assert event.payload["note"] == "Called complainant"
    assert (complaint.current_state, complaint.current_stage_started_at) == before


def test_blank_note_is_rejected(complaint, clerk):
    with pytest.raises(InvalidTransitionData):
        add_note(complaint, clerk, "   ")


def test_edit_details_logs_old_and_new_values(complaint, clerk):
    old_subject = complaint.subject
    event = edit_details(complaint, clerk, subject="New subject", complainant_phone="9999999999")
    complaint.refresh_from_db()
    assert complaint.subject == "New subject"
    assert complaint.complainant.phone == "9999999999"
    assert event.type == EventType.DETAILS_EDITED
    assert event.payload["changes"]["subject"] == {"old": old_subject, "new": "New subject"}
    assert event.payload["changes"]["complainant_phone"]["new"] == "9999999999"


def test_edit_details_with_no_change_logs_nothing(complaint, clerk):
    count = Event.objects.filter(complaint=complaint).count()
    assert edit_details(complaint, clerk, subject=complaint.subject) is None
    assert Event.objects.filter(complaint=complaint).count() == count


@pytest.mark.parametrize("field", ["current_state", "current_department", "closed_on", "ref_no"])
def test_edit_details_cannot_change_workflow_or_identity_fields(complaint, clerk, field):
    with pytest.raises(InvalidTransitionData):
        edit_details(complaint, clerk, **{field: "x"})


def test_stale_instance_does_not_overwrite_workflow_fields(complaint, clerk, tehsildar):
    from complaints.workflow import Action, apply_transition

    stale = Complaint.objects.get(pk=complaint.pk)
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    edit_details(stale, clerk, subject="Edited from a stale copy")
    complaint.refresh_from_db()
    assert complaint.current_state == State.AWAITING_REPORT
    assert complaint.subject == "Edited from a stale copy"
