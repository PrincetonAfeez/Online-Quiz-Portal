"""Force TOTP completion for privileged sessions (audit F17 / R2-01)."""

from __future__ import annotations

from django.shortcuts import redirect

from accounts.mfa import session_mfa_satisfied, user_requires_mfa

_ALWAYS_ALLOWED_PREFIXES = (
    "/accounts/logout/",
    "/healthz/",
    "/readyz/",
    "/legal/",
    "/static/",
)


class MFAEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is None
            or not user.is_authenticated
            or not user_requires_mfa(user)
            or session_mfa_satisfied(request)
        ):
            return self.get_response(request)

        path = request.path
        if any(path.startswith(prefix) for prefix in _ALWAYS_ALLOWED_PREFIXES):
            return self.get_response(request)

        # Enrolled users may only complete verify — never open setup and
        # replace the factor with a password-only session (audit R2-01).
        if user.mfa_enabled:
            if path.startswith("/accounts/mfa/verify/"):
                return self.get_response(request)
            return redirect("mfa_verify")

        if path.startswith("/accounts/mfa/setup/"):
            return self.get_response(request)
        return redirect("mfa_setup")
