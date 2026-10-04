from django.db import models


class Role(models.TextChoices):
    """Role names double as Django group names."""

    CLERK = "Clerk"
    DEPARTMENT_OFFICER = "DepartmentOfficer"
    REVIEWER = "Reviewer"
    ADMIN = "Admin"
    AUDITOR = "Auditor"
