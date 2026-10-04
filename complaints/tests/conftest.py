from __future__ import annotations

import itertools
from datetime import UTC, date, datetime

import pytest
from django.contrib.auth.models import Group

from complaints.models import Complainant, Complaint, Document, Platform
from complaints.services.documents import store_document
from complaints.tests.helpers import fake_attachment
from complaints.workflow import register_complaint
from core.models import Category, Department, Office, User, Village
from core.roles import Role

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


@pytest.fixture
def office(db) -> Office:
    return Office.objects.create(
        name="SDM Mandi Dabwali", code="MDB", district="Sirsa", state="Haryana"
    )


@pytest.fixture
def tehsildar(office) -> Department:
    return Department.objects.create(
        office=office,
        name="Tehsildar",
        short_name="Tehsildar",
        kind=Department.Kind.EXTERNAL_DEPARTMENT,
        default_sla_days=7,
    )


@pytest.fixture
def bdpo(office) -> Department:
    return Department.objects.create(
        office=office,
        name="BDPO",
        short_name="BDPO",
        kind=Department.Kind.EXTERNAL_DEPARTMENT,
        default_sla_days=15,
    )


@pytest.fixture
def village(office) -> Village:
    return Village.objects.create(office=office, name="Bhattu Kalan", name_hi="भट्टू कलां")


@pytest.fixture
def category(office) -> Category:
    return Category.objects.create(office=office, name="Land", sla_days=None)


def make_user(office, role: Role, department=None, **extra) -> User:
    n = next(_counter)
    user = User.objects.create_user(
        username=f"{role.value.lower()}{n}",
        password="x",
        office=office,
        department=department,
        **extra,
    )
    user.groups.add(Group.objects.get(name=role.value))
    return user


@pytest.fixture
def clerk(office) -> User:
    return make_user(office, Role.CLERK)


@pytest.fixture
def reviewer(office) -> User:
    return make_user(office, Role.REVIEWER)


@pytest.fixture
def auditor(office) -> User:
    return make_user(office, Role.AUDITOR)


@pytest.fixture
def admin_role(office) -> User:
    return make_user(office, Role.ADMIN, is_staff=True)


@pytest.fixture
def officer(office, tehsildar) -> User:
    return make_user(office, Role.DEPARTMENT_OFFICER, department=tehsildar)


@pytest.fixture
def make_document(clerk):
    def _make(complaint: Complaint, kind=Document.Kind.OTHER, label=None) -> Document:
        label = label or f"file-{next(_counter)}"
        return store_document(complaint, fake_attachment(label), kind, clerk)

    return _make


@pytest.fixture
def make_complaint(office, clerk, village, category):
    def _make(**overrides) -> Complaint:
        n = next(_counter)
        complainant = overrides.pop("complainant", None) or Complainant.objects.create(
            office=office, name=f"Test Person {n}", phone=f"98765{n:05d}"
        )
        params = {
            "office": office,
            "complainant": complainant,
            "actor": clerk,
            "source_platform": Platform.CM_WINDOW,
            "received_on": date(2026, 9, 13),
            "subject": f"Subject {n}",
            "village": village,
            "category": category,
            "at": datetime(2026, 9, 13, 5, 0, tzinfo=UTC),
        }
        params.update(overrides)
        return register_complaint(**params)

    return _make


@pytest.fixture
def complaint(make_complaint) -> Complaint:
    return make_complaint()
