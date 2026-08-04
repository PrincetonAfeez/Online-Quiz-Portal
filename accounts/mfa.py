"""TOTP multi-factor authentication for privileged roles (audit F17 / R2 / R3)."""

from __future__ import annotations

import hashlib

import pyotp
from django.conf import settings

from accounts.models import OrganizationMembership

SESSION_MFA_USER = "mfa_ok_user_id"
SESSION_MFA_EPOCH = "mfa_ok_epoch"


def mfa_enforcement_enabled() -> bool:
    return bool(getattr(settings, "MFA_REQUIRED", False))


def user_requires_mfa(user) -> bool:
    """Privileged roles must complete TOTP when enforcement is on.

    Covers org admins, platform operators, and Django staff/superusers so
    ``/admin/`` is not password-only (audit R3-08).
    """
    if not mfa_enforcement_enabled():
        return False
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_staff or user.is_superuser:
        return True
    if getattr(user, "is_platform_operator", False):
        return True
    return user.memberships.filter(
        is_active=True, role=OrganizationMembership.Role.ORG_ADMIN
    ).exists()


def mfa_privilege_epoch(user) -> str:
    """Fingerprint of privilege + enrollment + factor (audit R2-02 / R3-01).

    Changing org-admin memberships, staff/operator flags, MFA enrollment, or
    the TOTP secret invalidates a previously satisfied session.
    """
    admin_orgs = ",".join(
        str(pk)
        for pk in user.memberships.filter(
            is_active=True, role=OrganizationMembership.Role.ORG_ADMIN
        )
        .order_by("organization_id")
        .values_list("organization_id", flat=True)
    )
    secret = getattr(user, "mfa_secret", "") or ""
    secret_fp = hashlib.sha256(secret.encode()).hexdigest()[:16] if secret else "none"
    material = (
        f"{user.pk}|op={int(bool(getattr(user, 'is_platform_operator', False)))}"
        f"|staff={int(bool(user.is_staff))}|super={int(bool(user.is_superuser))}"
        f"|enabled={int(bool(getattr(user, 'mfa_enabled', False)))}"
        f"|secret={secret_fp}|admins={admin_orgs}"
        # Flipping MFA_REQUIRED must invalidate pre-stamped sessions (R4-03).
        f"|enf={int(mfa_enforcement_enabled())}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def session_mfa_satisfied(request) -> bool:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if request.session.get(SESSION_MFA_USER) != user.pk:
        return False
    return request.session.get(SESSION_MFA_EPOCH) == mfa_privilege_epoch(user)


def mark_mfa_satisfied(request) -> None:
    user = request.user
    request.session[SESSION_MFA_USER] = user.pk
    request.session[SESSION_MFA_EPOCH] = mfa_privilege_epoch(user)


def clear_mfa_satisfied(request) -> None:
    request.session.pop(SESSION_MFA_USER, None)
    request.session.pop(SESSION_MFA_EPOCH, None)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(code.strip(), valid_window=1))


def provisioning_uri(user, secret: str) -> str:
    issuer = getattr(settings, "MFA_ISSUER_NAME", "QuizOps")
    return pyotp.TOTP(secret).provisioning_uri(name=user.get_username(), issuer_name=issuer)


def reset_mfa(user) -> None:
    """Clear enrollment so the next privileged login forces setup (audit R2-12)."""
    user.mfa_enabled = False
    user.mfa_secret = ""
    user.save(update_fields=["mfa_enabled", "mfa_secret"])
