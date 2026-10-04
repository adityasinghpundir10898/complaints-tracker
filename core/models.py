from django.contrib.auth.models import AbstractUser
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Office(TimeStamped):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)
    district = models.CharField(max_length=100)
    state = models.CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Department(TimeStamped):
    class Kind(models.TextChoices):
        INTERNAL_BRANCH = "internal_branch", "Internal branch"
        EXTERNAL_DEPARTMENT = "external_department", "External department"

    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    short_name = models.CharField(max_length=50)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    head_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    default_sla_days = models.PositiveSmallIntegerField(default=7)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["office", "name"], name="uniq_department_office_name"),
        ]

    def __str__(self) -> str:
        return self.short_name or self.name


class Village(TimeStamped):
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    name_hi = models.CharField("name (Hindi)", max_length=200, blank=True)
    block = models.CharField(max_length=100, blank=True)
    tehsil = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["office", "name"], name="uniq_village_office_name"),
        ]

    def __str__(self) -> str:
        return self.name


class Category(TimeStamped):
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    sla_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Overrides the department's default SLA when set."
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["office", "name"], name="uniq_category_office_name"),
        ]

    def __str__(self) -> str:
        return self.name


class User(AbstractUser):
    office = models.ForeignKey(Office, null=True, blank=True, on_delete=models.PROTECT)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="officers"
    )
    phone = models.CharField(max_length=20, blank=True)

    def has_role(self, role: str) -> bool:
        return self.groups.filter(name=role).exists()

    @property
    def role_names(self) -> set[str]:
        # Cached per instance so permission checks in one request hit the DB once.
        if not hasattr(self, "_role_names"):
            self._role_names = set(self.groups.values_list("name", flat=True))
        return self._role_names
