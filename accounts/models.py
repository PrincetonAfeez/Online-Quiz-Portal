"""User, organization, and membership models."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


def validate_timezone(value: str) -> None:
    """Reject timezone names ``zoneinfo`` cannot resolve (finding 44)."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"{value!r} is not a known IANA timezone.") from exc


def timezone_choices() -> list[tuple[str, str]]:
    return [(name, name) for name in sorted(available_timezones())]


class User(AbstractUser):
    """Custom user model reserved from day one for assessment identity."""

    # ``is_staff`` only means "may open the admin site"; it must never imply
    # cross-tenant data access (finding 1).
    is_platform_operator = models.BooleanField(
        default=False,
        help_text="Deliberate, audited cross-tenant access for platform support staff.",
    )
    email = models.EmailField(blank=True)
    # TOTP MFA for org admins and platform operators (audit F17).
    mfa_secret = models.CharField(max_length=64, blank=True)
    mfa_enabled = models.BooleanField(default=False)

    class Meta(AbstractUser.Meta):
        constraints = [
            # Unique when present, so several service accounts may have no address
            # while real addresses stay unique (finding 36).
            models.UniqueConstraint(
                fields=["email"],
                condition=~Q(email=""),
                name="user_email_unique_when_present",
            )
        ]

    def __str__(self) -> str:
        return self.get_full_name() or self.username


class Organization(models.Model):
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    timezone = models.CharField(
        max_length=64, default="America/Los_Angeles", validators=[validate_timezone]
    )
    ai_monthly_budget = models.PositiveIntegerField(default=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def zoneinfo(self) -> ZoneInfo:
        """Never raise on bad stored data; fall back to UTC and let checks flag it."""
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


class OrganizationMembership(models.Model):
    class Role(models.TextChoices):
        STUDENT = "student", "Student"
        INSTRUCTOR = "instructor", "Instructor"
        ORG_ADMIN = "org_admin", "Org Admin"

    # Explicit precedence, so organization selection does not depend on the
    # alphabetical ordering of the stored role value (finding 43).
    ROLE_PRECEDENCE = {
        Role.ORG_ADMIN: 0,
        Role.INSTRUCTOR: 1,
        Role.STUDENT: 2,
    }

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=24, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "organization", "role")]
        indexes = [models.Index(fields=["user", "is_active"], name="membership_user_active_idx")]

    def __str__(self) -> str:
        return f"{self.user} as {self.get_role_display()} in {self.organization}"

    @property
    def precedence(self) -> int:
        return self.ROLE_PRECEDENCE.get(self.role, 99)

    @property
    def is_instructional(self) -> bool:
        return self.role in (self.Role.INSTRUCTOR, self.Role.ORG_ADMIN)
