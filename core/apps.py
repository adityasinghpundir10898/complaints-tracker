from django.apps import AppConfig
from django.db.models.signals import post_migrate


def grant_admin_permissions(sender, **kwargs):
    """Give the Admin role every Django-admin permission on our apps; case tables stay read-only."""
    # Runs when the last of our apps is migrated, so every permission row already exists.
    if sender.name != "complaints":
        return
    from django.contrib.auth.models import Group, Permission
    from django.db.models import Q

    from core.roles import Role

    group = Group.objects.filter(name=Role.ADMIN).first()
    if group is None:
        return
    perms = Permission.objects.filter(
        Q(content_type__app_label__in=["core", "complaints"])
        | Q(content_type__app_label="auth", content_type__model="group")
    )
    group.permissions.set(perms)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        post_migrate.connect(grant_admin_permissions, dispatch_uid="core_grant_admin_permissions")
