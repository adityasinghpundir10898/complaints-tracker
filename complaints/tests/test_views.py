from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from complaints.models import Complaint, Document, Event, EventType, State
from complaints.services.documents import Extraction
from complaints.workflow import Action, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}


def pdf(name: str = "r.pdf", body: bytes = b"unique") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"%PDF-1.4\n" + body, content_type="application/pdf")


@pytest.fixture
def login(client):
    def _login(user):
        client.force_login(user)
        return client

    return _login


@pytest.fixture
def awaiting(complaint, clerk, tehsildar) -> Complaint:
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    return complaint


# ----------------------------------------------------------------- access


@pytest.mark.parametrize(
    "name",
    [
        "complaints:list",
        "complaints:overdue",
        "complaints:intake_upload",
        "complaints:intake_review",
    ],
)
def test_anonymous_users_are_sent_to_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


def test_anonymous_cannot_see_detail_or_documents(client, complaint, make_document):
    doc = make_document(complaint)
    for url in (
        reverse("complaints:detail", args=[complaint.pk]),
        reverse("complaints:document", args=[doc.pk]),
        reverse("complaints:action", args=[complaint.pk, "add_note"]),
    ):
        assert client.get(url).status_code == 302


def test_login_page_renders(client):
    response = client.get(reverse("login"))
    assert response.status_code == 200
    assert b"Log in" in response.content


# ------------------------------------------------------------------- list


def test_list_shows_complaints_kpis_and_filters(login, clerk, complaint):
    response = login(clerk).get(reverse("complaints:list"))
    assert response.status_code == 200
    html = response.content.decode()
    assert complaint.ref_no in html
    assert "Pending at SDM office" in html
    assert 'id="filters"' in html


def test_list_htmx_returns_only_the_partial(login, clerk, complaint):
    response = login(clerk).get(reverse("complaints:list"), **HX)
    html = response.content.decode()
    assert complaint.ref_no in html
    assert "<html" not in html


def test_list_search_and_filter_params(login, clerk, make_complaint):
    a = make_complaint(subject="Pension stopped")
    b = make_complaint(subject="Drain overflow")
    html = login(clerk).get(reverse("complaints:list"), {"q": "pension"}).content.decode()
    assert a.ref_no in html and b.ref_no not in html
    html = login(clerk).get(reverse("complaints:list"), {"bucket": "closed"}).content.decode()
    assert a.ref_no not in html and "No complaints match" in html


def test_list_ignores_invalid_filter_values(login, clerk, complaint):
    response = login(clerk).get(reverse("complaints:list"), {"department": "abc", "bucket": "zzz"})
    assert response.status_code == 200
    assert complaint.ref_no in response.content.decode()


def test_officer_list_only_has_their_cases(login, officer, awaiting, make_complaint):
    other = make_complaint()
    html = login(officer).get(reverse("complaints:list")).content.decode()
    assert awaiting.ref_no in html
    assert other.ref_no not in html


# ----------------------------------------------------------------- detail


def test_detail_full_page_and_htmx_pane(login, clerk, awaiting):
    url = reverse("complaints:detail", args=[awaiting.pk])
    page = login(clerk).get(url).content.decode()
    pane = login(clerk).get(url, **HX).content.decode()
    assert "<html" in page and "<html" not in pane
    for html in (page, pane):
        assert awaiting.ref_no in html
        assert "Notice to Tehsildar" in html
        assert "Awaiting report" in html
        assert "Notice Issued" in html


def test_detail_only_offers_actions_valid_for_the_state_and_role(login, clerk, reviewer, awaiting):
    url = reverse("complaints:detail", args=[awaiting.pk])
    as_clerk = login(clerk).get(url).content.decode()
    assert "Record report received" in as_clerk and "Re-issue notice" in as_clerk
    assert "Issue notice<" not in as_clerk and "Mark satisfied" not in as_clerk
    assert "Reopen" not in as_clerk


def test_detail_of_hidden_complaint_is_404(login, officer, complaint):
    response = login(officer).get(reverse("complaints:detail", args=[complaint.pk]))
    assert response.status_code == 404


def test_auditor_sees_history_but_no_action_buttons(login, auditor, awaiting):
    html = login(auditor).get(reverse("complaints:detail", args=[awaiting.pk])).content.decode()
    assert "History" in html
    assert 'hx-get="/c/' not in html


# ---------------------------------------------------------------- actions


def action_url(complaint, action):
    return reverse("complaints:action", args=[complaint.pk, action])


def test_action_form_renders(login, clerk, complaint):
    response = login(clerk).get(action_url(complaint, "issue_notice"), **HX)
    assert response.status_code == 200
    assert "Send notice to" in response.content.decode()


def test_issue_notice_through_the_ui_changes_state_and_refreshes_pane(
    login, clerk, complaint, tehsildar
):
    response = login(clerk).post(
        action_url(complaint, "issue_notice"), {"department": tehsildar.pk}, **HX
    )
    complaint.refresh_from_db()
    assert response.status_code == 200
    assert complaint.current_state == State.AWAITING_REPORT
    assert response["HX-Trigger"] == "complaintChanged"
    assert response["HX-Retarget"] == "#detail-pane"
    assert Event.objects.filter(complaint=complaint, type=EventType.NOTICE_ISSUED).exists()


def test_issue_notice_with_attached_notice_creates_a_document(login, clerk, complaint, tehsildar):
    login(clerk).post(
        action_url(complaint, "issue_notice"),
        {"department": tehsildar.pk, "file": pdf("notice.pdf")},
        **HX,
    )
    doc = Document.objects.get(complaint=complaint)
    assert doc.kind == Document.Kind.NOTICE
    assert complaint.referrals.get().notice_document == doc


def test_report_can_be_recorded_without_a_file(login, clerk, awaiting):
    response = login(clerk).post(action_url(awaiting, "record_report"), {}, **HX)
    awaiting.refresh_from_db()
    assert response.status_code == 200
    assert awaiting.current_state == State.UNDER_REVIEW
    assert Document.objects.count() == 0


def test_portal_submission_can_be_recorded_without_a_file(
    login, clerk, reviewer, awaiting, make_document
):
    apply_transition(awaiting, Action.RECORD_REPORT, clerk, document=make_document(awaiting))
    apply_transition(awaiting, Action.REVIEW_SATISFIED, reviewer)
    login(clerk).post(action_url(awaiting, "submit_to_portal"), {"portal_ack_no": "A1"}, **HX)
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.PORTAL_SUBMITTED


def test_record_report_with_file_moves_to_review(login, clerk, awaiting):
    login(clerk).post(action_url(awaiting, "record_report"), {"file": pdf()}, **HX)
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.UNDER_REVIEW
    assert Document.objects.get(complaint=awaiting).kind == Document.Kind.INQUIRY_REPORT


def test_wrong_file_type_is_rejected_and_nothing_changes(login, clerk, awaiting):
    exe = SimpleUploadedFile("r.pdf", b"MZ\x90\x00", content_type="application/pdf")
    response = login(clerk).post(action_url(awaiting, "record_report"), {"file": exe}, **HX)
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.AWAITING_REPORT
    assert "Only PDF, JPG and PNG" in response.content.decode()
    assert Document.objects.count() == 0


def test_same_file_twice_shows_an_error_and_rolls_back(login, clerk, awaiting):
    login(clerk).post(action_url(awaiting, "add_note"), {"text": "hello"}, **HX)
    client = login(clerk)
    client.post(
        action_url(awaiting, "attach_document"), {"kind": "other", "file": pdf(body=b"same")}, **HX
    )
    response = client.post(
        action_url(awaiting, "attach_document"), {"kind": "other", "file": pdf(body=b"same")}, **HX
    )
    assert "already attached" in response.content.decode()
    assert Document.objects.filter(complaint=awaiting).count() == 1


def test_a_state_valid_for_role_but_not_for_the_case_shows_an_error_not_a_crash(
    login, reviewer, complaint
):
    response = login(reviewer).post(action_url(complaint, "reopen"), {"reason": "x"}, **HX)
    assert response.status_code == 200
    assert "not allowed" in response.content.decode()
    complaint.refresh_from_db()
    assert complaint.current_state == State.RECEIVED


def test_auditor_cannot_act(login, auditor, complaint, tehsildar):
    response = login(auditor).post(
        action_url(complaint, "issue_notice"), {"department": tehsildar.pk}, **HX
    )
    assert response.status_code == 403
    complaint.refresh_from_db()
    assert complaint.current_state == State.RECEIVED


def test_admin_role_can_do_everything_a_reviewer_can(
    login, admin_role, complaint, tehsildar, make_document
):
    client = login(admin_role)
    client.post(action_url(complaint, "issue_notice"), {"department": tehsildar.pk}, **HX)
    client.post(action_url(complaint, "record_report"), {}, **HX)
    client.post(action_url(complaint, "review_satisfied"), {}, **HX)
    client.post(action_url(complaint, "close_without_portal"), {"reason": "done"}, **HX)
    complaint.refresh_from_db()
    assert complaint.current_state == State.CLOSED
    assert (
        client.post(action_url(complaint, "reopen"), {"reason": "again"}, **HX).status_code == 200
    )


def test_admin_role_can_open_master_data_in_django_admin_but_not_edit_cases(
    login, admin_role, complaint
):
    client = login(admin_role)
    assert client.get("/admin/core/department/").status_code == 200
    assert client.get("/admin/core/user/").status_code == 200
    assert client.get("/admin/complaints/complaint/add/").status_code == 403
    change_url = f"/admin/complaints/complaint/{complaint.pk}/change/"
    assert client.post(change_url, {"subject": "hacked"}).status_code == 403


def test_other_roles_cannot_use_django_admin(login, clerk, auditor):
    for user in (clerk, auditor):
        response = login(user).get("/admin/core/department/")
        assert response.status_code == 302 and "/admin/login/" in response["Location"]


def test_clerk_cannot_review_but_reviewer_can(login, clerk, reviewer, awaiting, make_document):
    apply_transition(awaiting, Action.RECORD_REPORT, clerk, document=make_document(awaiting))
    assert login(clerk).post(action_url(awaiting, "review_satisfied"), {}, **HX).status_code == 403
    assert (
        login(reviewer).post(action_url(awaiting, "review_satisfied"), {}, **HX).status_code == 200
    )
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.APPROVED


def test_department_officer_can_note_on_own_case_only(login, officer, awaiting, make_complaint):
    other = make_complaint()
    ok = login(officer).post(action_url(awaiting, "add_note"), {"text": "Inquiry started"}, **HX)
    assert ok.status_code == 200
    assert Event.objects.filter(complaint=awaiting, type=EventType.NOTE_ADDED).count() == 1
    assert (
        login(officer).post(action_url(other, "add_note"), {"text": "x"}, **HX).status_code == 404
    )
    assert (
        login(officer)
        .post(action_url(awaiting, "issue_notice"), {"department": 1}, **HX)
        .status_code
        == 403
    )


def test_unknown_action_is_404(login, clerk, complaint):
    assert login(clerk).get(action_url(complaint, "make_coffee")).status_code == 404


def test_edit_details_through_the_ui(login, clerk, complaint):
    client = login(clerk)
    form = client.get(action_url(complaint, "edit_details"), **HX)
    assert "Complainant name" in form.content.decode()
    data = {
        "complainant_name": complaint.complainant.name,
        "complainant_phone": "9999912345",
        "complainant_address": "",
        "source_platform": complaint.source_platform,
        "external_ref": "",
        "received_on": complaint.received_on.isoformat(),
        "village": complaint.village_id,
        "category": complaint.category_id,
        "subject": "Edited subject",
        "description": "",
    }
    client.post(action_url(complaint, "edit_details"), data, **HX)
    complaint.refresh_from_db()
    assert complaint.subject == "Edited subject"
    assert complaint.complainant.phone == "9999912345"
    assert Event.objects.filter(complaint=complaint, type=EventType.DETAILS_EDITED).exists()


def test_full_loop_through_the_ui(login, clerk, reviewer, complaint, tehsildar):
    c = complaint
    steps = [
        (clerk, "issue_notice", {"department": tehsildar.pk}),
        (clerk, "record_report", {"file": pdf(body=b"report")}),
        (reviewer, "review_not_satisfied", {"note": "More detail needed"}),
        (clerk, "record_report", {"file": pdf(body=b"report2")}),
        (reviewer, "review_satisfied", {"note": "ok"}),
        (clerk, "submit_to_portal", {"file": pdf(body=b"atr"), "portal_ack_no": "A1"}),
        (clerk, "portal_rejected", {"reason": "Unsigned", "next_step": "resubmit"}),
        (clerk, "submit_to_portal", {"file": pdf(body=b"atr2")}),
        (clerk, "portal_accepted", {}),
    ]
    for user, action, data in steps:
        response = login(user).post(action_url(c, action), data, **HX)
        assert response.status_code == 200, action
        c.refresh_from_db()
    assert c.current_state == State.CLOSED
    detail = login(reviewer).get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "Accepted by CM portal" in detail and "Reopen" in detail


# -------------------------------------------------------------- documents


def test_document_download_streams_the_file_with_safe_headers(
    login, clerk, complaint, make_document
):
    doc = make_document(complaint, label="stream-me")
    response = login(clerk).get(reverse("complaints:document", args=[doc.pk]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"%PDF-1.4\nstream-me"
    assert response["Content-Type"] == "application/pdf"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Disposition"].startswith("inline")
    forced = login(clerk).get(reverse("complaints:document", args=[doc.pk]) + "?download=1")
    assert forced["Content-Disposition"].startswith("attachment")


def test_documents_are_not_reachable_without_permission(login, officer, complaint, make_document):
    doc = make_document(complaint)
    assert login(officer).get(reverse("complaints:document", args=[doc.pk])).status_code == 404


def test_deleted_documents_are_gone(login, clerk, complaint, make_document):
    doc = make_document(complaint)
    doc.is_deleted = True
    doc.save()
    assert login(clerk).get(reverse("complaints:document", args=[doc.pk])).status_code == 404


def test_media_files_have_no_public_url(client, complaint, make_document):
    doc = make_document(complaint)
    assert client.get("/media/" + doc.file.name).status_code == 404


# ----------------------------------------------------------------- overdue


def test_overdue_page_groups_late_cases_by_department(
    login, clerk, officer, awaiting, make_complaint, bdpo
):
    c2 = make_complaint()
    apply_transition(c2, Action.ISSUE_NOTICE, clerk, department=bdpo)
    awaiting.referrals.update(due_on=timezone.localdate() - timedelta(days=4))
    c2.referrals.update(due_on=timezone.localdate() + timedelta(days=4))

    html = login(clerk).get(reverse("complaints:overdue")).content.decode()
    assert awaiting.ref_no in html and "Tehsildar" in html
    assert c2.ref_no not in html
    assert "4 days" in html

    officer_html = login(officer).get(reverse("complaints:overdue")).content.decode()
    assert awaiting.ref_no in officer_html


def sdm_section(html: str) -> str:
    return html.split("Pending at the SDM office")[1].split("Portal rejections")[0]


def test_overdue_page_lists_cases_waiting_at_the_sdm_office_for_seven_days(
    login, clerk, reviewer, make_complaint, tehsildar, make_document
):
    def stage_at(days: int) -> datetime:
        day = timezone.localdate() - timedelta(days=days)
        return datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)

    fresh = make_complaint(at=stage_at(6))
    stale = make_complaint(at=stage_at(7))
    review = make_complaint(at=stage_at(20))
    apply_transition(review, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=stage_at(12))
    apply_transition(
        review, Action.RECORD_REPORT, clerk, document=make_document(review), at=stage_at(9)
    )
    approved = make_complaint(at=stage_at(20))
    apply_transition(approved, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=stage_at(15))
    apply_transition(approved, Action.RECORD_REPORT, clerk, at=stage_at(12))
    apply_transition(approved, Action.REVIEW_SATISFIED, reviewer, at=stage_at(8))
    portal = make_complaint(at=stage_at(20))
    apply_transition(portal, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=stage_at(15))
    apply_transition(portal, Action.RECORD_REPORT, clerk, at=stage_at(12))
    apply_transition(portal, Action.REVIEW_SATISFIED, reviewer, at=stage_at(11))
    apply_transition(portal, Action.SUBMIT_TO_PORTAL, clerk, at=stage_at(10))
    awaiting = make_complaint(at=stage_at(20))
    apply_transition(awaiting, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=stage_at(15))

    html = login(clerk).get(reverse("complaints:overdue")).content.decode()
    section = sdm_section(html)
    assert "7 days or more" in section
    assert stale.ref_no in section and review.ref_no in section and approved.ref_no in section
    assert fresh.ref_no not in section  # 6 days is still under the threshold
    assert portal.ref_no not in section  # waiting on the CM portal, not on the office
    assert awaiting.ref_no not in section  # waiting on a department, shown above by due date


def test_a_portal_rejection_is_listed_once_not_also_as_waiting_at_the_office(
    login, clerk, reviewer, make_complaint, tehsildar
):
    day = timezone.localdate() - timedelta(days=9)
    old = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
    c = make_complaint(at=old)
    for action, data in [
        (Action.ISSUE_NOTICE, {"department": tehsildar}),
        (Action.RECORD_REPORT, {}),
        (Action.REVIEW_SATISFIED, {}),
        (Action.SUBMIT_TO_PORTAL, {}),
        (Action.PORTAL_REJECTED, {"reason": "x", "next_step": "resubmit"}),
    ]:
        apply_transition(c, action, reviewer, at=old, **data)

    html = login(clerk).get(reverse("complaints:overdue")).content.decode()
    assert c.ref_no not in sdm_section(html)
    assert c.ref_no in html.split("Portal rejections")[1]


def test_sdm_office_threshold_comes_from_settings(login, clerk, make_complaint, settings):
    settings.AGEING_THRESHOLDS = {"safe": 0, "warning": 1, "overdue": 2, "critical": 5}
    day = timezone.localdate() - timedelta(days=2)
    c = make_complaint(at=datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC))
    section = sdm_section(login(clerk).get(reverse("complaints:overdue")).content.decode())
    assert "2 days or more" in section and c.ref_no in section


def test_department_officer_sees_no_sdm_office_cases(login, officer, make_complaint):
    day = timezone.localdate() - timedelta(days=30)
    make_complaint(at=datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC))
    section = sdm_section(login(officer).get(reverse("complaints:overdue")).content.decode())
    assert "Nothing has waited this long" in section


def test_overdue_page_lists_portal_rejections_waiting_for_action(
    login, clerk, awaiting, make_document
):
    apply_transition(awaiting, Action.RECORD_REPORT, clerk, document=make_document(awaiting))
    apply_transition(awaiting, Action.REVIEW_SATISFIED, clerk)
    apply_transition(awaiting, Action.SUBMIT_TO_PORTAL, clerk, document=make_document(awaiting))
    html = login(clerk).get(reverse("complaints:overdue")).content.decode()
    assert awaiting.ref_no not in html.split("Portal rejections")[1]

    apply_transition(awaiting, Action.PORTAL_REJECTED, clerk, reason="x", next_step="resubmit")
    html = login(clerk).get(reverse("complaints:overdue")).content.decode()
    assert awaiting.ref_no in html.split("Portal rejections")[1]


# ------------------------------------------------------------------ intake


def upload_step(client, body=b"complaint-one"):
    return client.post(reverse("complaints:intake_upload"), {"file": pdf("complaint.pdf", body)})


def intake_data(village, **over):
    data = {
        "name": "Sunita Devi",
        "phone": "9000012345",
        "address": "",
        "village": village.pk,
        "source_platform": "cm_window",
        "external_ref": "CMW-5",
        "received_on": timezone.localdate().isoformat(),
        "category": "",
        "subject": "Pension not received",
        "description": "",
    }
    return data | over


def test_intake_end_to_end_creates_complaint_document_and_event(login, clerk, village):
    client = login(clerk)
    assert upload_step(client).status_code == 302
    review = client.get(reverse("complaints:intake_review"))
    assert review.status_code == 200 and "complaint.pdf" in review.content.decode()

    response = client.post(reverse("complaints:intake_review"), intake_data(village))
    complaint = Complaint.objects.get()
    assert response.status_code == 302 and response["Location"] == reverse(
        "complaints:detail", args=[complaint.pk]
    )
    assert complaint.ref_no.startswith("C-")
    assert complaint.current_state == State.RECEIVED
    assert complaint.complainant.phone == "9000012345"
    assert complaint.documents.get().kind == Document.Kind.COMPLAINT
    assert Event.objects.get(complaint=complaint).type == EventType.COMPLAINT_REGISTERED
    assert client.get(reverse("complaints:intake_review")).status_code == 302


def test_nothing_is_saved_until_the_clerk_clicks_save(login, clerk):
    upload_step(login(clerk))
    login(clerk).get(reverse("complaints:intake_review"))
    assert Complaint.objects.count() == 0


def test_intake_review_without_an_upload_goes_back_to_upload(login, clerk):
    response = login(clerk).get(reverse("complaints:intake_review"))
    assert response.status_code == 302
    assert response["Location"] == reverse("complaints:intake_upload")


def test_intake_reuses_the_complainant_by_phone(login, clerk, village):
    client = login(clerk)
    upload_step(client, b"one")
    client.post(reverse("complaints:intake_review"), intake_data(village, external_ref="A"))
    upload_step(client, b"two")
    client.post(
        reverse("complaints:intake_review"),
        intake_data(village, external_ref="B", subject="Something else completely")
        | {"confirm_duplicates": "on"},
    )
    assert Complaint.objects.count() == 2
    assert {c.complainant_id for c in Complaint.objects.all()} == {
        Complaint.objects.first().complainant_id
    }


def test_intake_warns_about_duplicates_then_saves_when_confirmed(login, clerk, village):
    client = login(clerk)
    upload_step(client, b"one")
    client.post(reverse("complaints:intake_review"), intake_data(village))
    first = Complaint.objects.get()

    upload_step(client, b"two")
    warned = client.post(reverse("complaints:intake_review"), intake_data(village))
    assert warned.status_code == 200
    assert first.ref_no in warned.content.decode()
    assert Complaint.objects.count() == 1

    saved = client.post(
        reverse("complaints:intake_review"),
        intake_data(village) | {"confirm_duplicates": "on", "related_to": first.pk},
    )
    assert saved.status_code == 302
    second = Complaint.objects.exclude(pk=first.pk).get()
    assert second.related_to == first


def test_intake_rejects_a_future_received_date(login, clerk, village):
    client = login(clerk)
    upload_step(client)
    future = (timezone.localdate() + timedelta(days=3)).isoformat()
    response = client.post(
        reverse("complaints:intake_review"), intake_data(village, received_on=future)
    )
    assert response.status_code == 200
    assert Complaint.objects.count() == 0


def test_intake_upload_rejects_non_documents(login, clerk):
    bad = SimpleUploadedFile("x.pdf", b"MZ not a pdf")
    response = login(clerk).post(reverse("complaints:intake_upload"), {"file": bad})
    assert response.status_code == 200
    assert "Only PDF, JPG and PNG" in response.content.decode()


@pytest.mark.parametrize("role", ["auditor", "officer"])
def test_only_registering_roles_can_use_intake(request, login, role):
    user = request.getfixturevalue(role)
    assert login(user).get(reverse("complaints:intake_upload")).status_code == 403
    assert login(user).get(reverse("complaints:intake_review")).status_code == 403


def test_admin_role_can_register_complaints(login, admin_role):
    assert login(admin_role).get(reverse("complaints:intake_upload")).status_code == 200


def test_platform_must_be_chosen_not_defaulted(login, clerk, village):
    client = login(clerk)
    upload_step(client)
    response = client.post(
        reverse("complaints:intake_review"), intake_data(village, source_platform="")
    )
    assert response.status_code == 200
    assert Complaint.objects.count() == 0


def test_phone_is_stored_as_plain_digits(login, clerk, village):
    client = login(clerk)
    upload_step(client)
    client.post(reverse("complaints:intake_review"), intake_data(village, phone="+91 90000-12345"))
    assert Complaint.objects.get().complainant.phone == "9000012345"


def test_review_form_is_prefilled_from_the_extracted_text(login, clerk, office, monkeypatch):
    from core.models import Village

    Village.objects.create(office=office, name="Tohana", name_hi="तोहाना")
    letter = (
        "विषय :- सड़क की मरम्मत बाबत।\nप्रार्थीगण\n"
        "रामलाल पुत्र मोहन सिंह निवासी गांव तोहाना\nमो. 98765 43210"
    )
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction(letter, "done", 1, latin_text="Mob 91234 56789"),
    )
    client = login(clerk)
    upload_step(client)
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert 'value="रामलाल"' in html
    assert 'value="9123456789"' in html  # the English OCR pass wins for digits
    assert "सड़क की मरम्मत बाबत।" in html
    assert "selected" in html.split('name="village"')[1].split("</select>")[0]


def test_review_page_names_a_village_that_is_missing_from_the_list(login, clerk, monkeypatch):
    letter = "विषय :- सड़क।\nप्रार्थीगण\nरामलाल पुत्र मोहन सिंह निवासी गांव नौरंग"
    monkeypatch.setattr(
        "complaints.views.extract", lambda content, mime: Extraction(letter, "done", 1)
    )
    client = login(clerk)
    upload_step(client)
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert "<strong>नौरंग</strong>" in html


# -------------------------------------------------------------------- seed


def test_seed_command_fills_every_state_and_is_idempotent(db, settings):
    call_command("seed", allow_production=True, verbosity=0)
    assert Complaint.objects.count() == 30
    assert set(Complaint.objects.values_list("current_state", flat=True)) == {
        s.value for s in State
    }
    call_command("seed", allow_production=True, verbosity=0)
    assert Complaint.objects.count() == 30


def test_seed_refuses_to_run_in_production(db, settings):
    from django.core.management.base import CommandError

    settings.DEBUG = False
    with pytest.raises(CommandError):
        call_command("seed", verbosity=0)


def test_every_seeded_complaint_renders_in_every_view(db, client):
    from core.models import User

    call_command("seed", allow_production=True, verbosity=0)
    reviewer = User.objects.get(username="reviewer")
    client.force_login(reviewer)
    assert client.get(reverse("complaints:list")).status_code == 200
    assert client.get(reverse("complaints:overdue")).status_code == 200
    for c in Complaint.objects.all():
        assert client.get(reverse("complaints:detail", args=[c.pk])).status_code == 200
        assert client.get(reverse("complaints:detail", args=[c.pk]), **HX).status_code == 200
    officer = User.objects.get(username="officer")
    client.force_login(officer)
    assert client.get(reverse("complaints:list")).status_code == 200
