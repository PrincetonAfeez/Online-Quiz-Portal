"""Tests for accounts Django admin actions and forms."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase

from accounts.admin import (
    CustomUserAdmin,
    OrganizationAdmin,
    OrganizationMembershipAdmin,
    reset_user_mfa,
)
from accounts.mfa import new_totp_secret
from accounts.models import Organization, OrganizationMembership, User, timezone_choices


class ResetUserMFAActionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Admin Org", slug="admin-org")
        self.actor = User.objects.create_superuser(
            "super", "super@test.dev", "pass-phrase-12"
        )
        self.enrolled = User.objects.create_user("enrolled", password="pass-phrase-12")
        self.enrolled.mfa_secret = new_totp_secret()
        self.enrolled.mfa_enabled = True
        self.enrolled.save(update_fields=["mfa_secret", "mfa_enabled"])
        OrganizationMembership.objects.create(
            user=self.enrolled,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.plain = User.objects.create_user("plain", password="pass-phrase-12")
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.actor

    def _admin_stub(self):
        class _Admin:
            def message_user(self, *args, **kwargs):
                return None

        return _Admin()

    def test_reset_skips_users_without_enrollment(self):
        before_logs = LogEntry.objects.count()
        reset_user_mfa(
            self._admin_stub(),
            self.request,
            User.objects.filter(pk=self.plain.pk),
        )
        self.plain.refresh_from_db()
        self.assertFalse(self.plain.mfa_enabled)
        self.assertEqual(self.plain.mfa_secret, "")
        self.assertEqual(LogEntry.objects.count(), before_logs)

    def test_reset_clears_enrolled_user_and_writes_logentry(self):
        before = LogEntry.objects.count()
        reset_user_mfa(
            self._admin_stub(),
            self.request,
            User.objects.filter(pk=self.enrolled.pk),
        )
        self.enrolled.refresh_from_db()
        self.assertFalse(self.enrolled.mfa_enabled)
        self.assertEqual(self.enrolled.mfa_secret, "")
        self.assertGreater(LogEntry.objects.count(), before)
        entry = LogEntry.objects.latest("action_time")
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn("Reset MFA", entry.change_message)

    def test_reset_logs_admin_action_for_memberships(self):
        from assessments.models import AdminActionLog

        reset_user_mfa(
            self._admin_stub(),
            self.request,
            User.objects.filter(pk=self.enrolled.pk),
        )
        self.assertTrue(
            AdminActionLog.objects.filter(
                organization=self.org,
                action="user.mfa_reset",
                target_id=str(self.enrolled.pk),
            ).exists()
        )

    def test_reset_logs_even_for_operator_without_memberships(self):
        operator = User.objects.create_user(
            "platop",
            password="pass-phrase-12",
            is_platform_operator=True,
            is_staff=True,
        )
        operator.mfa_enabled = True
        operator.mfa_secret = new_totp_secret()
        operator.save(update_fields=["mfa_enabled", "mfa_secret"])
        before = LogEntry.objects.count()
        reset_user_mfa(
            self._admin_stub(),
            self.request,
            User.objects.filter(pk=operator.pk),
        )
        self.assertGreater(LogEntry.objects.count(), before)


class OrganizationAdminTimezoneTests(TestCase):
    def test_timezone_field_uses_iana_choices(self):
        model_admin = OrganizationAdmin(Organization, AdminSite())
        request = RequestFactory().get("/admin/")
        field = Organization._meta.get_field("timezone")
        expected = timezone_choices()
        with patch.object(
            admin.options.ModelAdmin,
            "formfield_for_dbfield",
            return_value="formfield",
        ) as parent:
            result = model_admin.formfield_for_dbfield(field, request)
            self.assertEqual(result, "formfield")
            self.assertEqual(parent.call_args.kwargs["choices"], expected)


class IdentityAdminGateTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.staff = User.objects.create_user(
            "plainstaff", password="pass-phrase-12", is_staff=True
        )
        self.operator = User.objects.create_user(
            "platop",
            password="pass-phrase-12",
            is_staff=True,
            is_platform_operator=True,
        )
        self.factory = RequestFactory()

    def test_plain_staff_cannot_change_users_or_memberships(self):
        request = self.factory.get("/admin/")
        request.user = self.staff
        user_admin = CustomUserAdmin(User, self.site)
        membership_admin = OrganizationMembershipAdmin(OrganizationMembership, self.site)
        self.assertFalse(user_admin.has_change_permission(request))
        self.assertFalse(user_admin.has_add_permission(request))
        self.assertFalse(membership_admin.has_change_permission(request))
        self.assertFalse(membership_admin.has_module_permission(request))

    def test_platform_operator_can_change_users(self):
        # Django ModelAdmin also requires the auth.change_user codename.
        from django.contrib.auth.models import Permission

        perm = Permission.objects.get(codename="change_user")
        self.operator.user_permissions.add(perm)
        request = self.factory.get("/admin/")
        request.user = User.objects.get(pk=self.operator.pk)
        user_admin = CustomUserAdmin(User, self.site)
        self.assertTrue(user_admin.has_change_permission(request))
        self.assertTrue(user_admin.has_module_permission(request))

    def test_staff_with_change_user_perm_still_blocked(self):
        from django.contrib.auth.models import Permission

        perm = Permission.objects.get(codename="change_user")
        self.staff.user_permissions.add(perm)
        request = self.factory.get("/admin/")
        request.user = User.objects.get(pk=self.staff.pk)
        user_admin = CustomUserAdmin(User, self.site)
        self.assertFalse(user_admin.has_change_permission(request))

    def test_plain_staff_cannot_run_mfa_reset_action(self):
        request = self.factory.get("/admin/")
        request.user = self.staff
        target = User.objects.create_user("tgt", password="pass-phrase-12")
        target.mfa_enabled = True
        target.mfa_secret = new_totp_secret()
        target.save(update_fields=["mfa_enabled", "mfa_secret"])

        class _Admin:
            def message_user(self, *args, **kwargs):
                return None

        with self.assertRaises(PermissionDenied):
            reset_user_mfa(_Admin(), request, User.objects.filter(pk=target.pk))
