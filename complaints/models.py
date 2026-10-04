from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models import Q

from core.models import Category, Department, Office, TimeStamped, Village

SEARCH_CONFIG = "simple"


class State(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    AWAITING_REPORT = "AWAITING_REPORT", "Awaiting report"
    UNDER_REVIEW = "UNDER_REVIEW", "Under review"
    APPROVED = "APPROVED", "Approved"
    PORTAL_SUBMITTED = "PORTAL_SUBMITTED", "Portal submitted"
    CLOSED = "CLOSED", "Closed"


class Platform(models.TextChoices):
    CM_WINDOW = "cm_window", "CM Window"
    JAN_SAMVAD = "jan_samvad", "Jan Samvad"
    SAMADHAN_SHIVIR = "samadhan_shivir", "Samadhan Shivir"
    SOCIAL_MEDIA = "social_media", "Social Media"
    INTERNAL = "internal", "Internal"


class EventType(models.TextChoices):
    COMPLAINT_REGISTERED = "COMPLAINT_REGISTERED"
    NOTICE_ISSUED = "NOTICE_ISSUED"
    REPORT_RECEIVED = "REPORT_RECEIVED"
    REVIEW_SATISFIED = "REVIEW_SATISFIED"
    REVIEW_NOT_SATISFIED = "REVIEW_NOT_SATISFIED"
    PORTAL_SUBMITTED = "PORTAL_SUBMITTED"
    PORTAL_ACCEPTED = "PORTAL_ACCEPTED"
    PORTAL_REJECTED = "PORTAL_REJECTED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"
    DOCUMENT_ATTACHED = "DOCUMENT_ATTACHED"
    DETAILS_EDITED = "DETAILS_EDITED"
    NOTE_ADDED = "NOTE_ADDED"


class Tag(TimeStamped):
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["office", "name"], name="uniq_tag_office_name")
        ]

    def __str__(self) -> str:
        return self.name


class Complainant(TimeStamped):
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, db_index=True, blank=True)
    address = models.TextField(blank=True)
    village = models.ForeignKey(Village, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        indexes = [
            GinIndex(fields=["name"], opclasses=["gin_trgm_ops"], name="complainant_name_trgm"),
        ]

    def __str__(self) -> str:
        return self.name


class Complaint(TimeStamped):
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    ref_no = models.CharField(max_length=32)
    source_platform = models.CharField(max_length=30, choices=Platform.choices)
    external_ref = models.CharField(max_length=100, blank=True)
    received_on = models.DateField()
    complainant = models.ForeignKey(
        Complainant, on_delete=models.PROTECT, related_name="complaints"
    )
    village = models.ForeignKey(Village, null=True, blank=True, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT)
    subject = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="complaints")
    related_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="related_complaints"
    )

    # Derived cache: written only by complaints.workflow.
    current_state = models.CharField(max_length=20, choices=State.choices, default=State.RECEIVED)
    current_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    current_stage_started_at = models.DateTimeField()
    closed_on = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-received_on", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["office", "ref_no"], name="uniq_complaint_office_ref"),
        ]
        indexes = [
            models.Index(fields=["office", "current_state"], name="cmp_office_state_idx"),
            models.Index(fields=["office", "current_department"], name="cmp_office_dept_idx"),
            models.Index(fields=["office", "received_on"], name="cmp_office_received_idx"),
            models.Index(fields=["village"], name="cmp_village_idx"),
            models.Index(fields=["source_platform"], name="cmp_platform_idx"),
            GinIndex(
                SearchVector(
                    "ref_no", "external_ref", "subject", "description", config=SEARCH_CONFIG
                ),
                name="cmp_search_vector_gin",
            ),
            GinIndex(fields=["subject"], opclasses=["gin_trgm_ops"], name="cmp_subject_trgm"),
        ]

    def __str__(self) -> str:
        return self.ref_no

    @property
    def is_open(self) -> bool:
        return self.current_state != State.CLOSED


class Document(models.Model):
    """Immutable: never overwritten, soft delete only."""

    class Kind(models.TextChoices):
        COMPLAINT = "complaint", "Complaint"
        NOTICE = "notice", "Notice"
        INQUIRY_REPORT = "inquiry_report", "Inquiry report"
        ATR = "atr", "ATR"
        PORTAL_ACK = "portal_ack", "Portal acknowledgement"
        PORTAL_REJECTION = "portal_rejection", "Portal rejection"
        OTHER = "other", "Other"

    class TextStatus(models.TextChoices):
        NONE = "none", "Not extracted"
        DONE = "done", "Extracted"
        FAILED = "failed", "Failed"

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    complaint = models.ForeignKey(Complaint, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    file = models.FileField(upload_to="documents/", max_length=255)
    sha256 = models.CharField(max_length=64)
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extracted_text = models.TextField(blank=True)
    text_status = models.CharField(
        max_length=10, choices=TextStatus.choices, default=TextStatus.NONE
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["uploaded_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["complaint", "sha256"], name="uniq_document_complaint_sha"
            ),
        ]
        indexes = [
            GinIndex(
                SearchVector("extracted_text", config=SEARCH_CONFIG), name="doc_text_search_gin"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.original_filename}"

    def delete(self, *args, **kwargs):
        raise NotImplementedError("Documents are soft-deleted via is_deleted.")


class Referral(TimeStamped):
    """One notice sent to a department."""

    class Outcome(models.TextChoices):
        REPORT_RECEIVED = "report_received", "Report received"
        NO_RESPONSE = "no_response", "No response"
        SUPERSEDED = "superseded", "Superseded"

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    complaint = models.ForeignKey(Complaint, on_delete=models.PROTECT, related_name="referrals")
    round_no = models.PositiveSmallIntegerField()
    to_department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="+")
    issued_on = models.DateField()
    due_on = models.DateField()
    notice_document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_on = models.DateField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["complaint_id", "round_no"]
        constraints = [
            models.UniqueConstraint(fields=["complaint", "round_no"], name="uniq_referral_round"),
            models.UniqueConstraint(
                fields=["complaint"],
                condition=Q(closed_on__isnull=True),
                name="one_open_referral_per_complaint",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.complaint.ref_no} round {self.round_no}"


class InquiryReport(TimeStamped):
    """A department's reply to one referral, plus the SDM's verdict on it."""

    class ReviewOutcome(models.TextChoices):
        PENDING = "pending", "Pending"
        SATISFIED = "satisfied", "Satisfied"
        NOT_SATISFIED = "not_satisfied", "Not satisfied"

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    referral = models.OneToOneField(Referral, on_delete=models.PROTECT, related_name="report")
    received_on = models.DateField()
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    review_outcome = models.CharField(
        max_length=20, choices=ReviewOutcome.choices, default=ReviewOutcome.PENDING
    )
    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_on = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Report for {self.referral}"


class PortalSubmission(TimeStamped):
    """One upload to the CM portal."""

    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    complaint = models.ForeignKey(Complaint, on_delete=models.PROTECT, related_name="submissions")
    attempt_no = models.PositiveSmallIntegerField()
    submitted_on = models.DateField()
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    portal_ack_no = models.CharField(max_length=100, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.PENDING)
    outcome_on = models.DateField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["complaint_id", "attempt_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["complaint", "attempt_no"], name="uniq_submission_attempt"
            ),
            models.UniqueConstraint(
                fields=["complaint"],
                condition=Q(outcome="pending"),
                name="one_pending_submission_per_complaint",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.complaint.ref_no} attempt {self.attempt_no}"


class AppendOnlyError(Exception):
    pass


class EventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("Events are append-only.")

    def delete(self):
        raise AppendOnlyError("Events are append-only.")


class Event(models.Model):
    """The timeline. Append-only: no update or delete in code."""

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    complaint = models.ForeignKey(Complaint, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=30, choices=EventType.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    payload = models.JSONField(default=dict, blank=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(fields=["complaint", "occurred_at"], name="event_complaint_time_idx")
        ]

    def __str__(self) -> str:
        return f"{self.complaint.ref_no} {self.type}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("Events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Events are append-only.")
