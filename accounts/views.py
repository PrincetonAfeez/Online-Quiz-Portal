"""Authentication views."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordResetConfirmView, PasswordResetView
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.mfa import (
    clear_mfa_satisfied,
    mark_mfa_satisfied,
    new_totp_secret,
    provisioning_uri,
    session_mfa_satisfied,
    user_requires_mfa,
    verify_totp,
)
from accounts.privacy import anonymize_user, export_user_data
from assessments.models import Attempt
from assessments.ratelimit import client_identity, hit, rate_limit, refund
from assessments.tenancy import memberships_for

logger = logging.getLogger(__name__)


def _mfa_throttle(request):
    """Bound TOTP guessing after password auth (audit R2-03)."""
    identity = f"{client_identity(request)}|uid:{request.user.pk}"
    if hit(
        "mfa",
        identity,
        settings.MFA_RATE_LIMIT,
        settings.MFA_RATE_WINDOW_SECONDS,
    ):
        return None
    logger.warning(
        "mfa blocked by throttle",
        extra={"request_id": getattr(request, "request_id", "-"), "user_id": request.user.pk},
    )
    response = render(
        request,
        "registration/mfa_throttled.html",
        {"retry_after_seconds": settings.MFA_RATE_WINDOW_SECONDS},
        status=429,
    )
    response["Retry-After"] = str(settings.MFA_RATE_WINDOW_SECONDS)
    return response


def _auth_throttle_response(request, scope: str, identity: str):
    limit = settings.LOGIN_RATE_LIMIT
    window = settings.LOGIN_RATE_WINDOW_SECONDS
    if hit(scope, identity, limit, window):
        return None
    logger.warning(
        "%s blocked by throttle",
        scope,
        extra={"request_id": getattr(request, "request_id", "-")},
    )
    response = render(
        request,
        "registration/login_throttled.html",
        {"retry_after_seconds": window},
        status=429,
    )
    response["Retry-After"] = str(window)
    return response


class ThrottledLoginView(LoginView):
    """Rate-limited login (spec 12).

    Budget is consumed *before* authentication so concurrent guesses cannot
    all observe an empty window (audit F07). A successful login refunds one
    unit so legitimate re-auth is not starved (audit R2-11).
    """

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def _identity(self):
        username = (self.request.POST.get("username") or "")[:150]
        return f"{client_identity(self.request)}|{username.lower()}"

    def post(self, request, *args, **kwargs):
        blocked = _auth_throttle_response(request, "login", self._identity())
        if blocked is not None:
            return blocked
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        refund("login", self._identity(), settings.LOGIN_RATE_WINDOW_SECONDS)
        # Rotate the session id on privilege change.
        self.request.session.cycle_key()
        clear_mfa_satisfied(self.request)
        response = super().form_valid(form)
        user = self.request.user
        if user_requires_mfa(user):
            if user.mfa_enabled:
                return redirect("mfa_verify")
            return redirect("mfa_setup")
        # Never stamp mfa_ok without a successful TOTP — enabling MFA_REQUIRED
        # later must force a challenge (audit R4-03).
        return response


class ThrottledPasswordResetView(PasswordResetView):
    """Rate-limited password reset request (audit R3-06)."""

    template_name = "registration/password_reset_form.html"

    def post(self, request, *args, **kwargs):
        identity = client_identity(request)
        blocked = _auth_throttle_response(request, "password_reset", identity)
        if blocked is not None:
            return blocked
        return super().post(request, *args, **kwargs)


class ThrottledPasswordResetConfirmView(PasswordResetConfirmView):
    """Rate-limited password reset confirmation (audit R3-06)."""

    template_name = "registration/password_reset_confirm.html"

    def post(self, request, *args, **kwargs):
        identity = client_identity(request)
        blocked = _auth_throttle_response(request, "password_reset_confirm", identity)
        if blocked is not None:
            return blocked
        return super().post(request, *args, **kwargs)


@login_required
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    if not user_requires_mfa(request.user):
        return redirect(settings.LOGIN_REDIRECT_URL)

    # Enrolled users may rotate only after a satisfied MFA session, and must
    # confirm the current factor first (audit R2-01).
    if request.user.mfa_enabled and not session_mfa_satisfied(request):
        return redirect("mfa_verify")

    throttled = _mfa_throttle(request) if request.method == "POST" else None
    if throttled is not None:
        return throttled

    secret = request.session.get("mfa_pending_secret")
    if not secret:
        secret = new_totp_secret()
        request.session["mfa_pending_secret"] = secret

    if request.method == "POST":
        code = request.POST.get("code", "")
        if request.user.mfa_enabled:
            current = request.POST.get("current_code", "")
            if not verify_totp(request.user.mfa_secret, current):
                messages.error(
                    request,
                    "Enter a code from your current authenticator before rotating.",
                )
                return render(
                    request,
                    "registration/mfa_setup.html",
                    {
                        "secret": secret,
                        "provisioning_uri": provisioning_uri(request.user, secret),
                        "rotating": True,
                    },
                )
        if verify_totp(secret, code):
            user = request.user
            user.mfa_secret = secret
            user.mfa_enabled = True
            user.save(update_fields=["mfa_secret", "mfa_enabled"])
            request.session.pop("mfa_pending_secret", None)
            mark_mfa_satisfied(request)
            messages.success(request, "Two-factor authentication is now enabled.")
            return redirect(settings.LOGIN_REDIRECT_URL)
        messages.error(request, "That code was not accepted. Try again.")

    return render(
        request,
        "registration/mfa_setup.html",
        {
            "secret": secret,
            "provisioning_uri": provisioning_uri(request.user, secret),
            "rotating": bool(request.user.mfa_enabled),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def mfa_verify(request):
    if not user_requires_mfa(request.user):
        return redirect(settings.LOGIN_REDIRECT_URL)
    if not request.user.mfa_enabled:
        return redirect("mfa_setup")

    throttled = _mfa_throttle(request) if request.method == "POST" else None
    if throttled is not None:
        return throttled

    if request.method == "POST":
        code = request.POST.get("code", "")
        if verify_totp(request.user.mfa_secret, code):
            mark_mfa_satisfied(request)
            return redirect(settings.LOGIN_REDIRECT_URL)
        messages.error(request, "That code was not accepted. Try again.")

    return render(request, "registration/mfa_verify.html")


@login_required
def account(request):
    """Self-service account page: data export and erasure (LAUNCH control)."""
    return render(
        request,
        "accounts/account.html",
        {
            "memberships": memberships_for(request.user),
            "mfa_required": user_requires_mfa(request.user),
            "mfa_enabled": request.user.mfa_enabled,
        },
    )


@login_required
@require_POST
@rate_limit("data_export", limit=3, window_seconds=3600, message="Export already requested.")
def export_my_data(request):
    payload = json.dumps(export_user_data(request.user), indent=2)
    response = HttpResponse(payload, content_type="application/json")
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    response["Content-Disposition"] = f'attachment; filename="quizops-data-{stamp}.json"'
    logger.info("user exported their data", extra={"user_id": request.user.pk})
    return response


@login_required
@require_POST
def delete_my_account(request):
    """Erasure is destructive and unauthenticated afterwards, so re-auth first."""
    password = request.POST.get("password", "")
    confirm = request.POST.get("confirm", "")
    if confirm != "DELETE":
        messages.error(request, "Type DELETE to confirm account deletion.")
        return redirect("account")
    if not request.user.check_password(password):
        messages.error(request, "That password is not correct.")
        return redirect("account")

    open_attempts = Attempt.objects.filter(
        student=request.user, status=Attempt.Status.IN_PROGRESS
    ).exists()
    if open_attempts:
        messages.error(
            request,
            "You have an assessment in progress. Submit it before deleting your account.",
        )
        return redirect("account")

    anonymize_user(request.user, actor=request.user, reason="self-service deletion")
    logout(request)
    return render(request, "accounts/account_deleted.html")
