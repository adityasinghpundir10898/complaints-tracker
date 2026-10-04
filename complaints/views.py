from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from complaints import permissions as perms
from complaints.forms import (
    ACTION_FORMS,
    AttachDocumentForm,
    ComplaintFilterForm,
    EditDetailsForm,
    IntakeForm,
    IntakeUploadForm,
    NoteForm,
)
from complaints.models import (
    Complainant,
    Complaint,
    Document,
    PortalSubmission,
    Referral,
    State,
)
from complaints.services import intake
from complaints.services.documents import DuplicateDocument, extract, store_document
from complaints.services.pendency import (
    ageing_bucket,
    bucket_labels,
    current_location,
    days_at_current_office,
    total_age_days,
)
from complaints.services.search import filter_complaints, kpi_counts, paginate
from complaints.services.summary import build_summary
from complaints.workflow import (
    Action,
    TransitionError,
    add_note,
    apply_transition,
    attach_document,
    edit_details,
    register_complaint,
)

ACTION_LABELS = {
    Action.ISSUE_NOTICE: "Issue notice",
    Action.REISSUE_NOTICE: "Re-issue notice",
    Action.RECORD_REPORT: "Record report received",
    Action.REVIEW_SATISFIED: "Mark satisfied",
    Action.REVIEW_NOT_SATISFIED: "Mark not satisfied",
    Action.SUBMIT_TO_PORTAL: "Record portal submission",
    Action.PORTAL_ACCEPTED: "Portal accepted",
    Action.PORTAL_REJECTED: "Portal rejected",
    Action.CLOSE_WITHOUT_PORTAL: "Close without portal",
    Action.REOPEN: "Reopen",
    perms.ATTACH_DOCUMENT: "Attach document",
    perms.ADD_NOTE: "Add note",
    perms.EDIT_DETAILS: "Edit details",
}
NON_STATE_ACTIONS = (perms.ATTACH_DOCUMENT, perms.ADD_NOTE, perms.EDIT_DETAILS)


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _visible_complaint(request: HttpRequest, pk: int) -> Complaint:
    qs = perms.visible_complaints(request.user).select_related(
        "complainant", "village", "category", "current_department", "office"
    )
    return get_object_or_404(qs, pk=pk)


def _decorate(complaint: Complaint) -> Complaint:
    """Attach display values so templates need no logic."""
    complaint.days_pending = days_at_current_office(complaint)
    complaint.bucket = ageing_bucket(complaint)
    complaint.location = current_location(complaint)
    return complaint


# --------------------------------------------------------------------- list


@login_required
def complaint_list(request: HttpRequest) -> HttpResponse:
    visible = perms.visible_complaints(request.user)
    form = ComplaintFilterForm(request.GET or None, office=request.user.office)
    filters = form.filters()
    qs = filter_complaints(
        visible.select_related("complainant", "village", "current_department"), filters
    )
    page = paginate(qs, request.GET.get("page"))
    for complaint in page:
        _decorate(complaint)

    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "form": form,
        "page": page,
        "kpi": kpi_counts(visible),
        "querystring": params.urlencode(),
        "buckets": [("", "All"), *bucket_labels()],
        "active_bucket": filters.get("bucket", ""),
        "can_register": perms.can(request.user, perms.REGISTER),
    }
    template = "complaints/_list.html" if _is_htmx(request) else "complaints/list.html"
    return render(request, template, context)


# ------------------------------------------------------------------- detail


def _grouped_documents(complaint: Complaint) -> list[tuple[str, list[Document]]]:
    groups: dict[str, list[Document]] = defaultdict(list)
    for doc in (
        complaint.documents.filter(is_deleted=False)
        .select_related("supersedes")
        .prefetch_related("superseded_by")
    ):
        groups[doc.get_kind_display()].append(doc)
    return list(groups.items())


def _detail_context(request: HttpRequest, complaint: Complaint, **extra: Any) -> dict[str, Any]:
    _decorate(complaint)
    user = request.user
    state_actions = perms.allowed_actions(user, complaint)
    actions = [(a, ACTION_LABELS[a]) for a in state_actions]
    actions += [(a, ACTION_LABELS[a]) for a in NON_STATE_ACTIONS if perms.can(user, a, complaint)]
    open_referral = complaint.referrals.filter(closed_on__isnull=True).first()
    return {
        "complaint": complaint,
        "summary": build_summary(complaint),
        "documents": _grouped_documents(complaint),
        "events": complaint.events.select_related("actor"),
        "actions": actions,
        "total_age": total_age_days(complaint),
        "open_referral": open_referral,
        "referral_overdue": bool(open_referral and open_referral.due_on < timezone.localdate()),
        **extra,
    }


@login_required
def complaint_detail(request: HttpRequest, pk: int) -> HttpResponse:
    complaint = _visible_complaint(request, pk)
    context = _detail_context(request, complaint)
    if _is_htmx(request):
        return render(request, "complaints/_detail.html", context)
    return render(request, "complaints/detail.html", context)


# ------------------------------------------------------------------ actions


def _form_for(action: str, request: HttpRequest, complaint: Complaint):
    kwargs = {"complaint": complaint}
    data = (request.POST, request.FILES) if request.method == "POST" else ()
    if action in ACTION_FORMS:
        return ACTION_FORMS[action](*data, **kwargs)
    forms_by_name = {
        perms.ATTACH_DOCUMENT: AttachDocumentForm,
        perms.ADD_NOTE: NoteForm,
        perms.EDIT_DETAILS: EditDetailsForm,
    }
    return forms_by_name[action](*data, **kwargs)


def _run_action(action: str, complaint: Complaint, user, form) -> None:
    """Perform the action inside one transaction; raises TransitionError / DuplicateDocument."""
    data = form.cleaned_data
    with transaction.atomic():
        if action in ACTION_FORMS:
            payload = form.transition_data()
            if form.attachment is not None:
                payload[form.file_key] = store_document(
                    complaint, form.attachment, form.file_kind, user
                )
            apply_transition(complaint, action, user, **payload)
        elif action == perms.ATTACH_DOCUMENT:
            attach_document(
                complaint, user, data["file"], data["kind"], supersedes=data["supersedes"]
            )
        elif action == perms.ADD_NOTE:
            add_note(complaint, user, data["text"])
        elif action == perms.EDIT_DETAILS:
            edit_details(complaint, user, **data)


@login_required
@require_http_methods(["GET", "POST"])
def complaint_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    complaint = _visible_complaint(request, pk)
    if action not in ACTION_LABELS:
        raise Http404
    if not perms.can(request.user, action, complaint):
        return HttpResponseForbidden("You are not allowed to do this.")

    form = _form_for(action, request, complaint)
    error = None
    if request.method == "POST" and form.is_valid():
        try:
            _run_action(action, complaint, request.user, form)
        except DuplicateDocument as exc:
            error = f"{exc} ({exc.existing.original_filename})"
        except TransitionError as exc:
            error = str(exc)
        else:
            if not _is_htmx(request):
                return redirect("complaints:detail", pk=pk)
            complaint = _visible_complaint(request, pk)
            response = render(
                request, "complaints/_detail.html", _detail_context(request, complaint)
            )
            response["HX-Trigger"] = "complaintChanged"
            response["HX-Retarget"] = "#detail-pane"
            response["HX-Reswap"] = "innerHTML"
            return response

    return render(
        request,
        "complaints/_action_form.html",
        {
            "complaint": complaint,
            "form": form,
            "action": action,
            "label": ACTION_LABELS[action],
            "error": error,
            "multipart": any(f.widget.needs_multipart_form for f in form.fields.values()),
        },
    )


# ---------------------------------------------------------------- documents


@login_required
def document_download(request: HttpRequest, pk: int) -> FileResponse:
    """Files are only ever served here, after a visibility check; there is no public media URL."""
    document = get_object_or_404(Document, pk=pk, is_deleted=False)
    if not perms.visible_complaints(request.user).filter(pk=document.complaint_id).exists():
        raise Http404
    response = FileResponse(
        document.file.open("rb"),
        content_type=document.mime_type,
        as_attachment=request.GET.get("download") == "1",
        filename=document.original_filename,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ------------------------------------------------------------------- intake

INTAKE_SESSION_KEY = "intake"


@login_required
@require_http_methods(["GET", "POST"])
def intake_upload(request: HttpRequest) -> HttpResponse:
    if not perms.can(request.user, perms.REGISTER):
        return HttpResponseForbidden("You are not allowed to register complaints.")
    form = IntakeUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        attachment = form.cleaned_data["file"]
        extraction = extract(attachment.content, attachment.mime_type)
        sha = intake.stage_upload(attachment)
        request.session[INTAKE_SESSION_KEY] = {
            "sha": sha,
            "filename": attachment.filename,
            "extracted_text": extraction.text,
            "latin_text": extraction.latin_text,
            "text_status": extraction.status,
            "page_count": extraction.pages,
        }
        return redirect("complaints:intake_review")
    return render(request, "complaints/intake_upload.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def intake_review(request: HttpRequest) -> HttpResponse:
    if not perms.can(request.user, perms.REGISTER):
        return HttpResponseForbidden("You are not allowed to register complaints.")
    meta = request.session.get(INTAKE_SESSION_KEY)
    attachment = intake.load_staged(meta["sha"], meta) if meta else None
    if attachment is None:
        return redirect("complaints:intake_upload")

    office = request.user.office
    guess = intake.guess_fields(attachment.extracted_text, office, meta.get("latin_text", ""))
    if request.method == "POST":
        form = IntakeForm(request.POST, office=office)
    else:
        form = IntakeForm(initial=guess.as_initial(), office=office)

    duplicates: list = []
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        if not cd["confirm_duplicates"]:
            duplicates = intake.find_possible_duplicates(
                office,
                external_ref=cd["external_ref"],
                phone=cd["phone"],
                subject=cd["subject"],
                village=cd["village"],
            )
        if not duplicates:
            complaint = _save_intake(request, cd, attachment, office)
            request.session.pop(INTAKE_SESSION_KEY, None)
            return redirect("complaints:detail", pk=complaint.pk)
        # Same page again, now with the warning; the clerk chooses how to continue.
        form.data = form.data.copy()
        form.data["confirm_duplicates"] = "on"

    return render(
        request,
        "complaints/intake_review.html",
        {
            "form": form,
            "duplicates": duplicates,
            "filename": attachment.filename,
            "text_status": attachment.text_status,
            "village_hint": guess.village_text if guess.village_id is None else "",
            "document_preview": attachment.extracted_text[:2000],
        },
    )


def _save_intake(request: HttpRequest, cd: dict[str, Any], attachment, office) -> Complaint:
    with transaction.atomic():
        complainant = None
        if cd["phone"]:
            complainant = Complainant.objects.filter(office=office, phone=cd["phone"]).first()
        if complainant is None:
            complainant = Complainant.objects.create(
                office=office,
                name=cd["name"],
                phone=cd["phone"],
                address=cd["address"],
                village=cd["village"],
            )
        return register_complaint(
            office=office,
            complainant=complainant,
            actor=request.user,
            source_platform=cd["source_platform"],
            received_on=cd["received_on"],
            subject=cd["subject"],
            description=cd["description"],
            external_ref=cd["external_ref"],
            village=cd["village"],
            category=cd["category"],
            tags=cd["tags"],
            related_to=cd["related_to"],
            attachment=attachment,
        )


# ------------------------------------------------------------------ overdue


@login_required
def overdue_today(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    visible = perms.visible_complaints(request.user).select_related(
        "complainant", "village", "current_department", "category"
    )

    late = (
        visible.filter(
            current_state=State.AWAITING_REPORT,
            referrals__closed_on__isnull=True,
            referrals__due_on__lt=today,
        )
        .annotate(
            due_on=Subquery(
                Referral.objects.filter(complaint=OuterRef("pk"), closed_on__isnull=True).values(
                    "due_on"
                )[:1]
            )
        )
        .order_by("current_stage_started_at")
    )
    by_department: dict[str, list[Complaint]] = defaultdict(list)
    for complaint in late:
        _decorate(complaint)
        complaint.days_overdue = (today - complaint.due_on).days
        by_department[str(complaint.current_department)].append(complaint)

    latest_outcome = Subquery(
        PortalSubmission.objects.filter(complaint=OuterRef("pk"))
        .order_by("-attempt_no")
        .values("outcome")[:1]
    )
    rejected = (
        visible.annotate(latest_outcome=latest_outcome)
        .filter(
            latest_outcome=PortalSubmission.Outcome.REJECTED,
            current_state__in=[State.APPROVED, State.UNDER_REVIEW],
        )
        .order_by("current_stage_started_at")
    )
    rejected = [_decorate(c) for c in rejected]

    # No SLA applies at the SDM office itself, so the 'overdue' ageing threshold is used.
    sdm_days = settings.AGEING_THRESHOLDS["overdue"]
    rejected_ids = {c.pk for c in rejected}
    at_office = (
        visible.filter(
            current_state__in=[State.RECEIVED, State.UNDER_REVIEW, State.APPROVED],
            current_stage_started_at__date__lte=today - timedelta(days=sdm_days),
        )
        .exclude(pk__in=rejected_ids)
        .order_by("current_stage_started_at")
    )
    at_office = [_decorate(c) for c in at_office]

    return render(
        request,
        "complaints/overdue.html",
        {
            "by_department": dict(by_department),
            "rejected": rejected,
            "at_office": at_office,
            "sdm_days": sdm_days,
            "today": today,
        },
    )
