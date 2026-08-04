"""Request-scoped organization and timezone activation."""

from __future__ import annotations

import uuid

from django.utils import timezone

from assessments.tenancy import get_active_membership


class OrganizationTimezoneMiddleware:
    """Render every timestamp in the organization's own calendar (finding 18).

    Deadlines were previously rendered in UTC to organizations configured for
    other zones, silently shifting exam windows by hours.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.membership = None
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            membership = get_active_membership(request)
            if membership:
                request.membership = membership
                request.organization = membership.organization
                timezone.activate(request.organization.zoneinfo)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()


class RequestIDMiddleware:
    """Attach a request id so logs can be correlated (spec 15)."""

    header = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.META.get(self.header, "")
        # Never echo unbounded client input back into logs or headers.
        request.request_id = (
            supplied[:64] if supplied and supplied.isascii() and supplied.isprintable()
            else uuid.uuid4().hex
        )
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response


class ContentSecurityPolicyMiddleware:
    """Defence in depth against XSS on a page that renders instructor content.

    The app ships no inline scripts and loads no third-party origins, so a
    strict policy costs nothing. Styles allow 'unsafe-inline' only for the
    small number of Django admin inline styles.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data:",
                "font-src 'self'",
                "connect-src 'self'",
                "frame-ancestors 'none'",
                "form-action 'self'",
                "base-uri 'self'",
                "object-src 'none'",
            ]
        )

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.policy)
        response.setdefault("Referrer-Policy", "same-origin")
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        return response
