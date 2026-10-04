from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from complaints import permissions as perms
from complaints.models import Complainant, Complaint, Document, Platform, State, Tag
from complaints.services.documents import Attachment, store_document
from complaints.services.search import (
    apply_bucket,
    filter_complaints,
    kpi_counts,
    paginate,
)
from complaints.workflow import Action, apply_transition


def refs(qs) -> set[str]:
    return set(qs.values_list("ref_no", flat=True))


@pytest.fixture
def person(office):
    return lambda name, phone="": Complainant.objects.create(office=office, name=name, phone=phone)


def search(user, **filters):
    return filter_complaints(perms.visible_complaints(user), filters)


def test_search_by_reference_number_and_external_ref(clerk, make_complaint):
    a = make_complaint(external_ref="CMW-778899")
    b = make_complaint()
    assert refs(search(clerk, q=a.ref_no)) == {a.ref_no}
    assert refs(search(clerk, q="778899")) == {a.ref_no}
    assert b.ref_no not in refs(search(clerk, q="CMW-778899"))


def test_search_by_phone_fragment(clerk, make_complaint, person):
    a = make_complaint(complainant=person("Anyone", "9876543210"))
    make_complaint(complainant=person("Someone", "9123456789"))
    assert refs(search(clerk, q="98765432")) == {a.ref_no}


def test_search_by_name_including_a_misspelling(clerk, make_complaint, person):
    a = make_complaint(complainant=person("Sunita Devi"))
    make_complaint(complainant=person("Ramesh Kumar"))
    assert refs(search(clerk, q="Sunita")) == {a.ref_no}
    assert refs(search(clerk, q="Suneeta")) == {a.ref_no}


def test_search_by_village_in_english_and_hindi(clerk, make_complaint, office, village):
    from core.models import Village

    other = Village.objects.create(office=office, name="Tohana")
    a = make_complaint(village=village)
    make_complaint(village=other)
    assert refs(search(clerk, q="Bhattu")) == {a.ref_no}
    assert refs(search(clerk, q="भट्टू")) == {a.ref_no}


def test_search_by_subject_and_description_text(clerk, make_complaint):
    a = make_complaint(subject="Pension not received", description="Widow pension stopped in March")
    b = make_complaint(subject="Street light broken", description="Near the school gate")
    assert refs(search(clerk, q="pension")) == {a.ref_no}
    assert refs(search(clerk, q="school")) == {b.ref_no}


def test_search_inside_extracted_document_text(clerk, make_complaint):
    a = make_complaint()
    make_complaint()
    store_document(
        a,
        Attachment(
            content=b"%PDF-1.4\nscan",
            filename="scan.pdf",
            mime_type="application/pdf",
            extracted_text="Khasra number 4521 mutation pending",
            text_status=Document.TextStatus.DONE,
        ),
        Document.Kind.COMPLAINT,
        clerk,
    )
    assert refs(search(clerk, q="khasra")) == {a.ref_no}


def test_deleted_documents_are_not_searched(clerk, make_complaint):
    a = make_complaint()
    doc = store_document(
        a,
        Attachment(
            content=b"%PDF-1.4\nx",
            filename="x.pdf",
            mime_type="application/pdf",
            extracted_text="unique-phrase-zebra",
            text_status=Document.TextStatus.DONE,
        ),
        Document.Kind.OTHER,
        clerk,
    )
    doc.is_deleted = True
    doc.save()
    assert refs(search(clerk, q="zebra")) == set()


def test_search_never_escapes_visibility(officer, make_complaint):
    make_complaint(subject="findable")
    assert refs(search(officer, q="findable")) == set()


# ----------------------------------------------------------------- filters


def test_filters_combine(clerk, make_complaint, tehsildar, bdpo, village, category):
    a = make_complaint(source_platform=Platform.JAN_SAMVAD)
    b = make_complaint(source_platform=Platform.CM_WINDOW)
    apply_transition(a, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(b, Action.ISSUE_NOTICE, clerk, department=bdpo)

    assert refs(search(clerk, department=tehsildar)) == {a.ref_no}
    assert refs(search(clerk, platform=Platform.CM_WINDOW)) == {b.ref_no}
    assert refs(search(clerk, status=State.AWAITING_REPORT)) == {a.ref_no, b.ref_no}
    assert refs(search(clerk, status=State.CLOSED)) == set()
    assert refs(search(clerk, village=village, category=category)) == {a.ref_no, b.ref_no}
    assert refs(search(clerk, department=tehsildar, platform=Platform.CM_WINDOW)) == set()


def test_filter_by_tag_and_received_date_range(clerk, make_complaint, office):
    tag = Tag.objects.create(office=office, name="Priority")
    a = make_complaint(received_on=date(2026, 9, 1), tags=[tag])
    b = make_complaint(received_on=date(2026, 9, 20))
    assert refs(search(clerk, tag=tag)) == {a.ref_no}
    assert refs(search(clerk, date_from=date(2026, 9, 10))) == {b.ref_no}
    assert refs(search(clerk, date_to=date(2026, 9, 10))) == {a.ref_no}
    assert refs(search(clerk, date_from=date(2026, 9, 1), date_to=date(2026, 9, 20))) == {
        a.ref_no,
        b.ref_no,
    }


def at_days_ago(today: date, days: int) -> datetime:
    day = today - timedelta(days=days)
    return datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)


@pytest.fixture
def aged(make_complaint, clerk, tehsildar):
    today = date(2026, 10, 1)
    made = {}
    for days in (0, 2, 3, 6, 7, 9, 10, 40):
        made[days] = make_complaint(at=at_days_ago(today, days))
    awaiting = make_complaint(at=at_days_ago(today, 50))
    apply_transition(
        awaiting, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=at_days_ago(today, 49)
    )
    return today, made, awaiting


@pytest.mark.parametrize(
    ("bucket", "days"),
    [
        ("safe", {0, 2}),
        ("warning", {3, 6}),
        ("overdue", {7, 9}),
        ("critical", {10, 40, 49}),
    ],
)
def test_ageing_bucket_filter_matches_boundaries(clerk, aged, bucket, days):
    today, made, awaiting = aged
    by_days = {**{d: c.ref_no for d, c in made.items()}, 49: awaiting.ref_no}
    qs = apply_bucket(perms.visible_complaints(clerk), bucket, today)
    assert refs(qs) == {by_days[d] for d in days}


def test_closed_bucket_and_kpis(clerk, aged, make_document):
    today, made, awaiting = aged
    c = made[0]
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=awaiting.current_department)
    apply_transition(c, Action.RECORD_REPORT, clerk, document=make_document(c))
    apply_transition(c, Action.REVIEW_SATISFIED, clerk)
    apply_transition(c, Action.CLOSE_WITHOUT_PORTAL, clerk, reason="done")

    assert refs(apply_bucket(perms.visible_complaints(clerk), "closed", today)) == {c.ref_no}
    counts = kpi_counts(perms.visible_complaints(clerk), today)
    assert counts["total"] == 9
    assert counts["closed"] == 1
    assert counts["pending"] == 8
    assert counts["safe"] + counts["warning"] + counts["overdue"] + counts["critical"] == 8


def test_pagination_is_25_per_page(clerk, make_complaint):
    for _ in range(27):
        make_complaint()
    qs = perms.visible_complaints(clerk)
    assert len(paginate(qs, 1)) == 25
    assert len(paginate(qs, 2)) == 2
    assert paginate(qs, "garbage").number == 1
    assert paginate(qs, 99).number == 2
    assert Complaint.objects.count() == 27
