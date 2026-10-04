from django.urls import reverse

from complaints import permissions


def nav(request):
    user = request.user
    if not user.is_authenticated:
        return {}
    return {
        "can_register": permissions.can(user, permissions.REGISTER),
        "admin_index_url": reverse("admin:index") if user.is_staff else "",
    }
