"""The one shared search / filter / pagination helper used by every list screen."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.contrib.postgres.search import (
    SearchQuery,
    SearchVector,
    TrigramWordSimilarity,
)
from django.core.paginator import Page, Paginator
from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from complaints.models import SEARCH_CONFIG, Complaint, Document, State
from complaints.services.pendency import BUCKET_ORDER, CLOSED_BUCKET, bucket_day_range

# word_similarity is a Jaccard-style score, so a one-letter spelling change scores ~0.35.
TRIGRAM_THRESHOLD = 0.3
BUCKET_CHOICES = [*BUCKET_ORDER, CLOSED_BUCKET]


def apply_text_search(qs: QuerySet[Complaint], text: str) -> QuerySet[Complaint]:
    text = text.strip()
    if not text:
        return qs
    query = SearchQuery(text, config=SEARCH_CONFIG, search_type="websearch")
    document_hit = Document.objects.filter(complaint=OuterRef("pk"), is_deleted=False).annotate(
        vector=SearchVector("extracted_text", config=SEARCH_CONFIG)
    )
    qs = qs.annotate(
        vector=SearchVector(
            "ref_no", "external_ref", "subject", "description", config=SEARCH_CONFIG
        ),
        name_similarity=TrigramWordSimilarity(text, "complainant__name"),
        subject_similarity=TrigramWordSimilarity(text, "subject"),
    )
    return qs.filter(
        Q(vector=query)
        | Q(ref_no__icontains=text)
        | Q(external_ref__icontains=text)
        | Q(complainant__phone__contains=text)
        | Q(complainant__name__icontains=text)
        | Q(village__name__icontains=text)
        | Q(village__name_hi__icontains=text)
        | Q(name_similarity__gte=TRIGRAM_THRESHOLD)
        | Q(subject_similarity__gte=TRIGRAM_THRESHOLD)
        | Exists(document_hit.filter(vector=query))
    )


def apply_bucket(
    qs: QuerySet[Complaint], bucket: str, today: date | None = None
) -> QuerySet[Complaint]:
    today = today or timezone.localdate()
    if bucket == CLOSED_BUCKET:
        return qs.filter(current_state=State.CLOSED)
    low, high = bucket_day_range(bucket)
    qs = qs.exclude(current_state=State.CLOSED).filter(
        current_stage_started_at__date__lte=today - timedelta(days=low)
    )
    if high is not None:
        qs = qs.filter(current_stage_started_at__date__gte=today - timedelta(days=high))
    return qs


def filter_complaints(
    qs: QuerySet[Complaint], filters: dict[str, Any], today: date | None = None
) -> QuerySet[Complaint]:
    """Apply already-validated filter values (see forms.ComplaintFilterForm)."""
    qs = apply_text_search(qs, filters.get("q") or "")
    exact = {
        "department": "current_department",
        "village": "village",
        "platform": "source_platform",
        "status": "current_state",
        "category": "category",
    }
    for key, field in exact.items():
        value = filters.get(key)
        if value:
            qs = qs.filter(**{field: value})
    if filters.get("tag"):
        qs = qs.filter(tags=filters["tag"])
    if filters.get("date_from"):
        qs = qs.filter(received_on__gte=filters["date_from"])
    if filters.get("date_to"):
        qs = qs.filter(received_on__lte=filters["date_to"])
    if filters.get("bucket"):
        qs = apply_bucket(qs, filters["bucket"], today)
    return qs


def kpi_counts(qs: QuerySet[Complaint], today: date | None = None) -> dict[str, int]:
    """Totals for the KPI strip, over everything the user may see."""
    today = today or timezone.localdate()
    aggregates: dict[str, Count] = {
        "total": Count("pk"),
        "closed": Count("pk", filter=Q(current_state=State.CLOSED)),
        "pending": Count("pk", filter=~Q(current_state=State.CLOSED)),
    }
    for bucket in BUCKET_ORDER:
        low, high = bucket_day_range(bucket)
        cond = ~Q(current_state=State.CLOSED) & Q(
            current_stage_started_at__date__lte=today - timedelta(days=low)
        )
        if high is not None:
            cond &= Q(current_stage_started_at__date__gte=today - timedelta(days=high))
        aggregates[bucket] = Count("pk", filter=cond)
    return qs.order_by().aggregate(**aggregates)


def paginate(qs: QuerySet, page_number: Any, per_page: int | None = None) -> Page:
    paginator = Paginator(qs, per_page or settings.PAGE_SIZE)
    return paginator.get_page(page_number)
