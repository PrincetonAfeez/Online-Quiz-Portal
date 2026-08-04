"""Django admin for users, organizations, and memberships."""

import logging

from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied

from accounts.mfa import reset_mfa
from accounts.models import Organization, OrganizationMembership, User, timezone_choices
from assessments.models import AdminActionLog

logger = logging.getLogger(__name__)


def _is_identity_admin(user) -> bool:
    """Only platform operators (or superusers) may mutate identity grants."""
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "is_platform_operator", False)
    )


class PlatformOperatorRequiredMixin:
    """Gate User / OrganizationMembership admin to platform operators (audit)."""

    def has_module_permission(self, request):
        return _is_identity_admin(request.user) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return _is_identity_admin(request.user) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return _is_identity_admin(request.user) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return _is_identity_admin(request.user) and super().has_change_permission(
            request, obj
        )

    def has_delete_permission(self, request, obj=None):
        return _is_identity_admin(request.user) and super().has_delete_permission(
            request, obj
        )


@admin.action(description="Reset MFA enrollment (forces re-setup)")
def reset_user_mfa(modeladmin, request, queryset):
    """Break-glass when an authenticator is lost (audit R2-12 / R3-07)."""
    if not _is_identity_admin(request.user):
        raise PermissionDenied("Platform operator access is required to reset MFA.")
    count = 0
    for user in queryset:
        if not user.mfa_enabled and not user.mfa_secret:
            continue
        reset_mfa(user)
        count += 1
        # Always write a platform audit row — membership-less operators otherwise
        # leave no AdminActionLog trail (audit R3-07).
        LogEntry.objects.log_actions(
            user_id=request.user.pk,
            queryset=User.objects.filter(pk=user.pk),
            action_flag=CHANGE,
            change_message="Reset MFA enrollment",
            single_object=True,
        )
        logger.info(
            "mfa reset by admin",
            extra={"actor_id": request.user.pk, "target_user_id": user.pk},
        )
        for membership in user.memberships.select_related("organization"):
            AdminActionLog.objects.create(
                organization=membership.organization,
                actor=request.user,
                action="user.mfa_reset",
                target_type="User",
                target_id=str(user.pk),
                detail={"username": user.get_username()},
            )
    modeladmin.message_user(
        request,
        f"Reset MFA for {count} user(s). They must re-enroll at next privileged login.",
        messages.SUCCESS,
    )


@admin.register(User)
class CustomUserAdmin(PlatformOperatorRequiredMixin, UserAdmin):
    list_display = [
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "is_platform_operator",
        "mfa_enabled",
    ]
    list_filter = ["is_staff", "is_superuser", "is_active", "is_platform_operator", "mfa_enabled"]
    search_fields = ["username", "email", "first_name", "last_name"]
    actions = [reset_user_mfa]
    readonly_fields = ["mfa_enabled"]
    fieldsets = UserAdmin.fieldsets + (
        (
            "Platform access",
            {
                "fields": ("is_platform_operator", "mfa_enabled"),
                "description": (
                    "Grants cross-tenant visibility. This is deliberately separate from "
                    "'staff status', which only grants admin-site access. Use the "
                    "'Reset MFA enrollment' action to clear a lost authenticator. "
                    "Only platform operators and superusers may edit these fields."
                ),
            },
        ),
    )


@admin.register(Organization)
class OrganizationAdmin(PlatformOperatorRequiredMixin, admin.ModelAdmin):
    list_display = ["name", "slug", "timezone", "ai_monthly_budget"]
    prepopulated_fields = {"slug": ["name"]}

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # A free-text timezone that zoneinfo cannot resolve used to 500 the
        # dashboard for the whole organization (finding 44).
        if db_field.name == "timezone":
            kwargs["choices"] = timezone_choices()
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(PlatformOperatorRequiredMixin, admin.ModelAdmin):
    list_display = ["user", "organization", "role", "is_active"]
    list_filter = ["role", "is_active", "organization"]
    search_fields = ["user__username", "user__email", "organization__name"]
