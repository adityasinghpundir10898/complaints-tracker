"""Fill a development database with fake master data, users and complaints."""

from __future__ import annotations

import random
from datetime import UTC, datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_timezone

from complaints.models import Complainant, Complaint, Document, Platform, Tag
from complaints.services.documents import Attachment, store_document
from complaints.workflow import Action, apply_transition, register_complaint
from core.models import Category, Department, Office, User, Village
from core.roles import Role

DEPARTMENTS = [
    # name, short_name, kind, SLA days (placeholders until the office supplies real data)
    ("Tehsildar", "Tehsildar", Department.Kind.EXTERNAL_DEPARTMENT, 7),
    ("Block Development and Panchayat Officer", "BDPO", Department.Kind.EXTERNAL_DEPARTMENT, 15),
    ("District Revenue Officer", "DRO", Department.Kind.EXTERNAL_DEPARTMENT, 15),
    ("Revenue Branch", "Revenue Branch", Department.Kind.INTERNAL_BRANCH, 5),
    ("Accounts Branch", "Accounts Branch", Department.Kind.INTERNAL_BRANCH, 5),
    ("Establishment Branch", "Establishment", Department.Kind.INTERNAL_BRANCH, 5),
    ("Grievance Cell", "Grievance Cell", Department.Kind.INTERNAL_BRANCH, 5),
    ("Police Station", "Police Station", Department.Kind.EXTERNAL_DEPARTMENT, 10),
    ("Municipal Council", "Municipal Council", Department.Kind.EXTERNAL_DEPARTMENT, 10),
]
VILLAGES = [
    ("Bhattu Kalan", "भट्टू कलां"),
    ("Tohana", "तोहाना"),
    ("Bhuna", "भूना"),
    ("Ratia", "रतिया"),
    ("Jakhal", "जाखल"),
    ("Bhirdana", "भिरडाना"),
    ("Mandi Dabwali", "मंडी डबवाली"),
]
CATEGORIES = [("Land", 15), ("Pension", 10), ("Civic", 7), ("Police", 10), ("Other", None)]
USERS = [
    ("clerk", Role.CLERK, "Demo Clerk"),
    ("officer", Role.DEPARTMENT_OFFICER, "Demo Officer"),
    ("reviewer", Role.REVIEWER, "Demo SDM"),
    ("admin", Role.ADMIN, "Demo Admin"),
    ("auditor", Role.AUDITOR, "Demo Auditor"),
]
NAMES = [
    "Sunita Devi",
    "Ramesh Kumar",
    "Baljeet Singh",
    "Kamla Rani",
    "Mohan Lal",
    "Geeta Bai",
    "Harpal Singh",
    "Sarita",
    "Jagdish Prasad",
    "Anita Kumari",
    "Rajinder Singh",
    "Pushpa Devi",
]
SUBJECTS = [
    "Encroachment on village common land",
    "Old age pension not received for six months",
    "Street lights not working",
    "Delay in registration of FIR",
    "Boundary dispute with neighbour",
    "Drain overflowing near the school",
    "Mutation of land pending",
    "Ration card not issued",
]

# Step names used in the plans below.
ISSUE, REISSUE, REPORT, NOT_OK, OK = "issue", "reissue", "report", "not_satisfied", "satisfied"
SUBMIT, REJ_RESUBMIT, REJ_REVIEW, REJ_INQUIRY = (
    "submit",
    "reject_resubmit",
    "reject_review",
    "reject_inquiry",
)
ACCEPT, CLOSE, REOPEN = "accept", "close", "reopen"

APPROVED = [ISSUE, REPORT, OK]
SUBMITTED = [*APPROVED, SUBMIT]

# (days since received, days at the current stage, steps taken so far): 30 complaints.
PLANS: list[tuple[int, int, list[str]]] = [
    # RECEIVED
    (1, 1, []),
    (4, 4, []),
    (8, 8, []),
    (12, 12, []),
    # AWAITING_REPORT: safe, warning, overdue, critical, and later rounds
    (3, 1, [ISSUE]),
    (5, 2, [ISSUE]),
    (9, 4, [ISSUE]),
    (12, 8, [ISSUE]),
    (16, 11, [ISSUE]),
    (14, 2, [ISSUE, REPORT, NOT_OK]),
    (25, 6, [ISSUE, REPORT, NOT_OK, REPORT, NOT_OK]),
    (30, 12, [ISSUE, REISSUE, REISSUE]),
    (33, 5, [*SUBMITTED, REJ_INQUIRY]),
    # UNDER_REVIEW
    (8, 1, [ISSUE, REPORT]),
    (11, 3, [ISSUE, REPORT]),
    (15, 8, [ISSUE, REPORT]),
    (22, 12, [ISSUE, REPORT]),
    (28, 2, [*SUBMITTED, REJ_REVIEW]),
    # APPROVED
    (10, 2, APPROVED),
    (18, 5, APPROVED),
    (24, 3, [*SUBMITTED, REJ_RESUBMIT]),
    # PORTAL_SUBMITTED
    (12, 1, SUBMITTED),
    (17, 4, SUBMITTED),
    (26, 3, [*SUBMITTED, REJ_RESUBMIT, SUBMIT]),
    (35, 9, [*SUBMITTED, REJ_RESUBMIT, SUBMIT, REJ_RESUBMIT, SUBMIT]),
    # CLOSED
    (20, 4, [*SUBMITTED, ACCEPT]),
    (30, 10, [*SUBMITTED, REJ_RESUBMIT, SUBMIT, ACCEPT]),
    (40, 15, [*APPROVED, CLOSE]),
    (45, 20, [*SUBMITTED, ACCEPT]),
    (50, 3, [*SUBMITTED, ACCEPT, REOPEN, OK, CLOSE]),
]


def _fake_pdf(text: str) -> bytes:
    """A tiny valid one-page PDF so the document viewer has something to show."""
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


class Command(BaseCommand):
    help = "Create fake master data, one user per role and 30 fake complaints (development only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", default="demo-pass-123", help="Password for the demo users."
        )
        parser.add_argument(
            "--allow-production", action="store_true", help="Run even when DEBUG is False."
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError("seed creates fake data; refusing to run with DEBUG=False.")
        random.seed(2026)
        with transaction.atomic():
            office = self._office()
            departments = self._departments(office)
            villages = self._villages(office)
            categories = self._categories(office)
            users = self._users(office, departments["Tehsildar"], options["password"])
            created = self._complaints(
                office, departments, villages, categories, users["clerk"], users["reviewer"]
            )

        self.stdout.write(self.style.SUCCESS(f"Seeded {office.name}: {created} new complaints."))
        self.stdout.write(
            f"Users: {', '.join(u for u, _, _ in USERS)} / password: {options['password']}"
        )

    # ---------------------------------------------------------- master data

    def _office(self) -> Office:
        office, _ = Office.objects.get_or_create(
            code="MDB",
            defaults={"name": "SDM Office Mandi Dabwali", "district": "Sirsa", "state": "Haryana"},
        )
        return office

    def _departments(self, office: Office) -> dict[str, Department]:
        result = {}
        for name, short, kind, sla in DEPARTMENTS:
            dept, _ = Department.objects.get_or_create(
                office=office,
                name=name,
                defaults={"short_name": short, "kind": kind, "default_sla_days": sla},
            )
            result[short] = dept
        return result

    def _villages(self, office: Office) -> list[Village]:
        villages = []
        for name, name_hi in VILLAGES:
            village, created = Village.objects.get_or_create(
                office=office, name=name, defaults={"name_hi": name_hi}
            )
            if not created and not village.name_hi:
                village.name_hi = name_hi  # databases seeded before Hindi names existed
                village.save(update_fields=["name_hi", "updated_at"])
            villages.append(village)
        return villages

    def _categories(self, office: Office) -> list[Category]:
        return [
            Category.objects.get_or_create(office=office, name=n, defaults={"sla_days": sla})[0]
            for n, sla in CATEGORIES
        ]

    def _users(self, office: Office, tehsildar: Department, password: str) -> dict[str, User]:
        users = {}
        for username, role, full_name in USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "office": office,
                    "first_name": full_name.split()[0],
                    "last_name": " ".join(full_name.split()[1:]),
                    "department": tehsildar if role == Role.DEPARTMENT_OFFICER else None,
                    "is_staff": role == Role.ADMIN,
                },
            )
            if created:
                user.set_password(password)
                user.save()
                user.groups.add(Group.objects.get(name=role.value))
            users[username] = user
        return users

    # ------------------------------------------------------------ complaints

    def _complaints(self, office, departments, villages, categories, clerk, reviewer) -> int:
        if Complaint.objects.filter(office=office).exists():
            self.stdout.write("Complaints already exist; skipping fake complaints.")
            return 0
        tags = [
            Tag.objects.get_or_create(office=office, name=n)[0]
            for n in ("Repeat filer", "Priority")
        ]
        today = dj_timezone.localdate()
        notice_targets = list(departments.values())
        previous = None
        for index, (age, stage_days, steps) in enumerate(PLANS):
            received = today - timedelta(days=age)
            complainant = Complainant.objects.create(
                office=office,
                name=NAMES[index % len(NAMES)],
                phone=f"9000000{index:03d}",
                village=villages[index % len(villages)],
            )
            complaint = register_complaint(
                office=office,
                complainant=complainant,
                actor=clerk,
                source_platform=list(Platform)[index % len(Platform)],
                received_on=received,
                subject=SUBJECTS[index % len(SUBJECTS)],
                description="Fake demo complaint. Not real data.",
                external_ref=f"DEMO-{2026000 + index}" if index % 5 != 4 else "",
                village=villages[index % len(villages)],
                category=categories[index % len(categories)],
                tags=[tags[0]] if index % 7 == 0 else ([tags[1]] if index % 11 == 0 else []),
                related_to=previous if index == 13 else None,
                attachment=Attachment(
                    content=_fake_pdf(f"Demo complaint {index + 1}"),
                    filename=f"complaint-{index + 1}.pdf",
                    mime_type="application/pdf",
                ),
                at=self._at(received),
            )
            self._run_steps(
                complaint,
                steps,
                received,
                today - timedelta(days=stage_days),
                notice_targets,
                clerk,
                reviewer,
            )
            previous = complaint
        return len(PLANS)

    @staticmethod
    def _at(day) -> datetime:
        return datetime.combine(day, time(hour=5), tzinfo=UTC)

    def _run_steps(self, complaint, steps, received, stage_start, targets, clerk, reviewer) -> None:
        count = len(steps)
        span = (stage_start - received).days
        for i, step in enumerate(steps):
            day = received + timedelta(days=round(span * (i + 1) / count)) if count else received
            at = self._at(day)
            self._apply(complaint, step, at, random.choice(targets), clerk, reviewer)

    def _document(self, complaint, kind, label, actor) -> Document:
        text = f"{complaint.ref_no} {label}"
        return store_document(
            complaint,
            Attachment(
                content=_fake_pdf(text), filename=f"{label}.pdf", mime_type="application/pdf"
            ),
            kind,
            actor,
        )

    def _apply(self, c: Complaint, step: str, at, dept: Department, clerk, reviewer) -> None:
        label = f"{step}-{at:%d%m}-{random.randint(1000, 9999)}"
        doc = self._document
        match step:
            case "issue":
                apply_transition(
                    c,
                    Action.ISSUE_NOTICE,
                    clerk,
                    department=dept,
                    at=at,
                    notice_document=doc(c, Document.Kind.NOTICE, f"notice-{label}", clerk),
                )
            case "reissue":
                apply_transition(
                    c,
                    Action.REISSUE_NOTICE,
                    clerk,
                    outcome="no_response",
                    at=at,
                    notice_document=doc(c, Document.Kind.NOTICE, f"notice-{label}", clerk),
                )
            case "report":
                apply_transition(
                    c,
                    Action.RECORD_REPORT,
                    clerk,
                    at=at,
                    document=doc(c, Document.Kind.INQUIRY_REPORT, f"report-{label}", clerk),
                )
            case "not_satisfied":
                apply_transition(
                    c,
                    Action.REVIEW_NOT_SATISFIED,
                    reviewer,
                    note="Report is incomplete.",
                    at=at,
                    notice_document=doc(c, Document.Kind.NOTICE, f"notice-{label}", clerk),
                )
            case "satisfied":
                apply_transition(
                    c, Action.REVIEW_SATISFIED, reviewer, note="Report accepted.", at=at
                )
            case "submit":
                apply_transition(
                    c,
                    Action.SUBMIT_TO_PORTAL,
                    clerk,
                    at=at,
                    document=doc(c, Document.Kind.ATR, f"atr-{label}", clerk),
                    portal_ack_no=f"ACK-{random.randint(100000, 999999)}",
                )
            case "reject_resubmit":
                apply_transition(
                    c,
                    Action.PORTAL_REJECTED,
                    clerk,
                    reason="ATR not signed.",
                    next_step="resubmit",
                    at=at,
                )
            case "reject_review":
                apply_transition(
                    c,
                    Action.PORTAL_REJECTED,
                    clerk,
                    reason="Action taken is unclear.",
                    next_step="review_again",
                    at=at,
                )
            case "reject_inquiry":
                apply_transition(
                    c,
                    Action.PORTAL_REJECTED,
                    clerk,
                    reason="Needs a fresh inquiry.",
                    next_step="new_inquiry",
                    department=dept,
                    at=at,
                    notice_document=doc(c, Document.Kind.NOTICE, f"notice-{label}", clerk),
                )
            case "accept":
                apply_transition(c, Action.PORTAL_ACCEPTED, clerk, at=at)
            case "close":
                apply_transition(
                    c,
                    Action.CLOSE_WITHOUT_PORTAL,
                    reviewer,
                    reason="No portal upload needed.",
                    at=at,
                )
            case "reopen":
                apply_transition(c, Action.REOPEN, reviewer, reason="New facts came in.", at=at)
            case _:
                raise CommandError(f"Unknown seed step {step!r}")
