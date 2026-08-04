"""Throttle Django admin login with the same budget as /accounts/login/ (R4-02)."""

from __future__ import annotations

import logging
import types

from django.conf import settings
from django.contrib import admin
from django.shortcuts import render

from assessments.ratelimit import client_identity, hit, refund

logger = logging.getLogger(__name__)


def patch_admin_login() -> None:
    """Wrap AdminSite.login so POST attempts share the login throttle identity."""
    site = admin.site
    if getattr(site, "_quizops_login_throttled", False):
        return
    original_login = site.login

    def login(self, request, extra_context=None):
        identity = None
        if request.method == "POST":
            username = (request.POST.get("username") or "")[:150]
            identity = f"{client_identity(request)}|{username.lower()}"
            if not hit(
                "login",
                identity,
                settings.LOGIN_RATE_LIMIT,
                settings.LOGIN_RATE_WINDOW_SECONDS,
            ):
                logger.warning(
                    "admin login blocked by throttle",
                    extra={"request_id": getattr(request, "request_id", "-")},
                )
                response = render(
                    request,
                    "registration/login_throttled.html",
                    {"retry_after_seconds": settings.LOGIN_RATE_WINDOW_SECONDS},
                    status=429,
                )
                response["Retry-After"] = str(settings.LOGIN_RATE_WINDOW_SECONDS)
                return response

        response = original_login(request, extra_context=extra_context)
        # Successful admin auth ends as a redirect with an authenticated user.
        if (
            identity is not None
            and getattr(request, "user", None) is not None
            and request.user.is_authenticated
            and getattr(response, "status_code", None) in (301, 302)
        ):
            refund("login", identity, settings.LOGIN_RATE_WINDOW_SECONDS)
        return response

    site.login = types.MethodType(login, site)
    site._quizops_login_throttled = True
