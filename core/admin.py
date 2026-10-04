from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Category, Department, Office, User, Village


@admin.register(Office)
class OfficeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "district", "state")
    search_fields = ("name", "code")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "kind", "default_sla_days", "is_active")
    list_filter = ("office", "kind", "is_active")
    search_fields = ("name", "short_name", "head_name")


@admin.register(Village)
class VillageAdmin(admin.ModelAdmin):
    list_display = ("name", "name_hi", "block", "tehsil", "is_active")
    list_filter = ("office", "tehsil", "is_active")
    search_fields = ("name", "name_hi")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "sla_days")
    list_filter = ("office",)
    search_fields = ("name",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Office", {"fields": ("office", "department", "phone")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Office", {"fields": ("office", "department", "phone")}),
    )
    list_display = ("username", "get_full_name", "office", "department", "is_active")
    list_filter = ("office", "department", "groups", "is_active")
    search_fields = ("username", "first_name", "last_name", "phone")
