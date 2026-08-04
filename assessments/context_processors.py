"""Template context shared by the base layout."""

from __future__ import annotations

from assessments.models import Notification
from assessments.services import is_instructor, is_org_admin, is_platform_operator
from assessments.tenancy import memberships_for


def navigation(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    organization = getattr(request, "organization", None)
    memberships = list(memberships_for(user))
    return {
        "active_organization": organization,
        "user_memberships": memberships,
        "can_switch_organization": len(memberships) > 1,
        # Gate the instructor link so students are not handed a guaranteed 403
        # (finding 41).
        "is_instructor": bool(organization) and is_instructor(user, organization),
        "is_org_admin": bool(organization) and is_org_admin(user, organization),
        "is_platform_operator": is_platform_operator(user),
        "unread_notifications": Notification.objects.filter(
            user=user, read_at__isnull=True
        ).count(),
    }
