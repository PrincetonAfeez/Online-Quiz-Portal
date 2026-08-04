"""Unit tests for accounts.mfa."""

from __future__ import annotations

import pyotp
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings

from accounts.mfa import (
    SESSION_MFA_EPOCH,
    SESSION_MFA_USER,
    clear_mfa_satisfied,
    mark_mfa_satisfied,
    mfa_enforcement_enabled,
    mfa_privilege_epoch,
    new_totp_secret,
    provisioning_uri,
    reset_mfa,
    session_mfa_satisfied,
    user_requires_mfa,
    verify_totp,
)
from accounts.models import Organization, OrganizationMembership, User


class MFAFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="MFA Org", slug="mfa-org")
        self.student = User.objects.create_user("mfstu", password="pass-phrase-12")
        self.instructor = User.objects.create_user("mfinst", password="pass-phrase-12")
        self.org_admin = User.objects.create_user("mfadmin", password="pass-phrase-12")
        self.operator = User.objects.create_user(
            "mfop", password="pass-phrase-12", is_platform_operator=True
        )
        self.staff = User.objects.create_user(
            "mfstaff", password="pass-phrase-12", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            "mfsuper", "super@test.dev", "pass-phrase-12"
        )
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        OrganizationMembership.objects.create(
            user=self.org_admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.factory = RequestFactory()

    def _request_for(self, user):
        request = self.factory.get("/")
        request.user = user
        request.session = self.client.session
        return request


@override_settings(MFA_REQUIRED=False)
class MFAEnforcementFlagTests(TestCase):
    def test_mfa_enforcement_disabled(self):
        self.assertFalse(mfa_enforcement_enabled())

    @override_settings(MFA_REQUIRED=True)
    def test_mfa_enforcement_enabled(self):
        self.assertTrue(mfa_enforcement_enabled())


@override_settings(MFA_REQUIRED=True)
class UserRequiresMFATests(MFAFixture):
    def test_staff_requires_mfa(self):
        self.assertTrue(user_requires_mfa(self.staff))

    def test_superuser_requires_mfa(self):
        self.assertTrue(user_requires_mfa(self.superuser))

    def test_platform_operator_requires_mfa(self):
        self.assertTrue(user_requires_mfa(self.operator))

    def test_org_admin_requires_mfa(self):
        self.assertTrue(user_requires_mfa(self.org_admin))

    def test_instructor_does_not_require_mfa(self):
        self.assertFalse(user_requires_mfa(self.instructor))

    def test_student_does_not_require_mfa(self):
        self.assertFalse(user_requires_mfa(self.student))

    def test_inactive_user_does_not_require_mfa(self):
        self.org_admin.is_active = False
        self.org_admin.save(update_fields=["is_active"])
        self.assertFalse(user_requires_mfa(self.org_admin))

    def test_anonymous_user_does_not_require_mfa(self):
        self.assertFalse(user_requires_mfa(AnonymousUser()))

    @override_settings(MFA_REQUIRED=False)
    def test_nobody_requires_mfa_when_enforcement_off(self):
        self.assertFalse(user_requires_mfa(self.org_admin))
        self.assertFalse(user_requires_mfa(self.staff))


@override_settings(MFA_REQUIRED=True)
class MFAPrivilegeEpochTests(MFAFixture):
    def test_epoch_changes_when_staff_flag_flips(self):
        before = mfa_privilege_epoch(self.student)
        self.student.is_staff = True
        self.student.save(update_fields=["is_staff"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.student))

    def test_epoch_changes_when_superuser_flag_flips(self):
        before = mfa_privilege_epoch(self.student)
        self.student.is_superuser = True
        self.student.save(update_fields=["is_superuser"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.student))

    def test_epoch_changes_when_operator_flag_flips(self):
        before = mfa_privilege_epoch(self.student)
        self.student.is_platform_operator = True
        self.student.save(update_fields=["is_platform_operator"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.student))

    def test_epoch_changes_when_org_admin_membership_added(self):
        before = mfa_privilege_epoch(self.student)
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.assertNotEqual(before, mfa_privilege_epoch(self.student))

    def test_epoch_changes_when_mfa_enabled_flips(self):
        before = mfa_privilege_epoch(self.org_admin)
        self.org_admin.mfa_enabled = True
        self.org_admin.save(update_fields=["mfa_enabled"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.org_admin))

    def test_epoch_changes_when_secret_rotates(self):
        self.org_admin.mfa_secret = new_totp_secret()
        self.org_admin.mfa_enabled = True
        self.org_admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        before = mfa_privilege_epoch(self.org_admin)
        self.org_admin.mfa_secret = new_totp_secret()
        self.org_admin.save(update_fields=["mfa_secret"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.org_admin))

    @override_settings(MFA_REQUIRED=False)
    def test_epoch_changes_when_enforcement_bit_flips(self):
        with override_settings(MFA_REQUIRED=False):
            off = mfa_privilege_epoch(self.org_admin)
        with override_settings(MFA_REQUIRED=True):
            on = mfa_privilege_epoch(self.org_admin)
        self.assertNotEqual(off, on)


@override_settings(MFA_REQUIRED=True)
class SessionMFASatisfiedTests(MFAFixture):
    def test_mark_and_clear_round_trip(self):
        client = self.client
        client.force_login(self.org_admin)
        request = self._request_for(self.org_admin)
        mark_mfa_satisfied(request)
        request.session.save()
        self.assertTrue(session_mfa_satisfied(request))
        clear_mfa_satisfied(request)
        request.session.save()
        self.assertFalse(session_mfa_satisfied(request))

    def test_wrong_user_id_is_not_satisfied(self):
        client = self.client
        client.force_login(self.org_admin)
        request = self._request_for(self.org_admin)
        request.session[SESSION_MFA_USER] = self.student.pk
        request.session[SESSION_MFA_EPOCH] = mfa_privilege_epoch(self.org_admin)
        request.session.save()
        self.assertFalse(session_mfa_satisfied(request))

    def test_stale_epoch_is_not_satisfied(self):
        client = self.client
        client.force_login(self.org_admin)
        request = self._request_for(self.org_admin)
        request.session[SESSION_MFA_USER] = self.org_admin.pk
        request.session[SESSION_MFA_EPOCH] = "stale-epoch"
        request.session.save()
        self.assertFalse(session_mfa_satisfied(request))

    def test_unauthenticated_request_is_not_satisfied(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()
        request.session = self.client.session
        self.assertFalse(session_mfa_satisfied(request))


class TOTPUtilityTests(TestCase):
    def test_new_totp_secret_is_non_empty(self):
        secret = new_totp_secret()
        self.assertTrue(secret)
        self.assertNotEqual(secret, new_totp_secret())

    def test_verify_totp_rejects_empty_inputs(self):
        self.assertFalse(verify_totp("", "123456"))
        self.assertFalse(verify_totp("ABCDEF", ""))
        self.assertFalse(verify_totp("", ""))

    def test_verify_totp_accepts_valid_code(self):
        secret = new_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(secret, code))

    def test_verify_totp_rejects_wrong_code(self):
        secret = new_totp_secret()
        self.assertFalse(verify_totp(secret, "000000"))

    def test_verify_totp_strips_whitespace(self):
        secret = new_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(secret, f"  {code}  "))

    @override_settings(MFA_ISSUER_NAME="TestIssuer")
    def test_provisioning_uri_includes_issuer_and_username(self):
        user = User.objects.create_user("totpuser", password="pass-phrase-12")
        secret = new_totp_secret()
        uri = provisioning_uri(user, secret)
        self.assertIn("otpauth://", uri)
        self.assertIn("TestIssuer", uri)
        self.assertIn("totpuser", uri)

    def test_reset_mfa_clears_enrollment(self):
        user = User.objects.create_user("resetme", password="pass-phrase-12")
        user.mfa_secret = new_totp_secret()
        user.mfa_enabled = True
        user.save(update_fields=["mfa_secret", "mfa_enabled"])
        reset_mfa(user)
        user.refresh_from_db()
        self.assertFalse(user.mfa_enabled)
        self.assertEqual(user.mfa_secret, "")
