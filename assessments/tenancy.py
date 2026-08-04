"""Active-organization resolution.

Every request that touches tenant data must resolve to exactly one
organization. Deriving it implicitly from "the first membership" while querying
across *all* memberships is what let an instructor at one organization read
another organization's gradebook (finding 2).
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

from accounts.models import OrganizationMembership

SESSION_KEY = "active_organization_id"


def memberships_for(user):
    if not user.is_authenticated:
        return OrganizationMembership.objects.none()
    return (
        user.memberships.select_related("organization")
        .filter(is_active=True)
        .order_by("organization__name", "role")
    )


def primary_membership(user):
    """Highest-privilege membership by explicit precedence, not alphabetically."""
    memberships = list(memberships_for(user))
    if not memberships:
        return None
    return min(memberships, key=lambda m: (m.precedence, m.organization.name))


def get_active_membership(request):
    """Membership for the organization this request is acting within."""
    # The middleware resolves this once per request; views reuse that result
    # instead of repeating the query.
    cached = getattr(request, "membership", None)
    if cached is not None:
        return cached
    memberships = list(memberships_for(request.user))
    if not memberships:
        return None
    selected_id = request.session.get(SESSION_KEY)
    if selected_id is not None:
        for membership in memberships:
            if membership.organization_id == selected_id:
                return membership
        request.session.pop(SESSION_KEY, None)
    return min(memberships, key=lambda m: (m.precedence, m.organization.name))


def get_active_organization(request):
    membership = get_active_membership(request)
    return membership.organization if membership else None


def require_active_organization(request):
    organization = get_active_organization(request)
    if organization is None:
        raise PermissionDenied("You do not belong to an organization.")
    return organization


def set_active_organization(request, organization_id: int) -> bool:
    """Switch organizations, but only to one the user actually belongs to."""
    for membership in memberships_for(request.user):
        if membership.organization_id == organization_id:
            request.session[SESSION_KEY] = organization_id
            return True
    return False
