from django.contrib import admin

from .models import (
    Complainant,
    Complaint,
    Document,
    Event,
    InquiryReport,
    PortalSubmission,
    Referral,
    Tag,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Case data changes only through the app, where apply_transition() logs it."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Event)
class EventAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "complaint", "type", "actor")
    list_filter = ("type", "office")
    search_fields = ("complaint__ref_no",)
    date_hierarchy = "occurred_at"


@admin.register(Complaint)
class ComplaintAdmin(ReadOnlyAdmin):
    list_display = ("ref_no", "subject", "village", "source_platform", "current_state")
    list_filter = ("office", "current_state", "source_platform", "current_department", "village")
    search_fields = ("ref_no", "external_ref", "subject", "complainant__name", "complainant__phone")


@admin.register(Complainant)
class ComplainantAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "village")
    list_filter = ("office", "village")
    search_fields = ("name", "phone")


@admin.register(Referral)
class ReferralAdmin(ReadOnlyAdmin):
    list_display = ("complaint", "round_no", "to_department", "issued_on", "due_on", "outcome")
    list_filter = ("office", "to_department", "outcome")
    search_fields = ("complaint__ref_no",)


@admin.register(InquiryReport)
class InquiryReportAdmin(ReadOnlyAdmin):
    list_display = ("referral", "received_on", "review_outcome", "reviewed_by")
    list_filter = ("office", "review_outcome")
    search_fields = ("referral__complaint__ref_no",)


@admin.register(PortalSubmission)
class PortalSubmissionAdmin(ReadOnlyAdmin):
    list_display = ("complaint", "attempt_no", "submitted_on", "outcome", "portal_ack_no")
    list_filter = ("office", "outcome")
    search_fields = ("complaint__ref_no", "portal_ack_no")


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ("original_filename", "complaint", "kind", "uploaded_at", "is_deleted")
    list_filter = ("office", "kind", "is_deleted")
    search_fields = ("original_filename", "complaint__ref_no", "sha256")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "office")
    list_filter = ("office",)
    search_fields = ("name",)
