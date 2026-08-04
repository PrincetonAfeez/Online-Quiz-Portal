"""Tests for accounts models."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import (
    Organization,
    OrganizationMembership,
    User,
    timezone_choices,
    validate_timezone,
)


class ValidateTimezoneTests(TestCase):
    def test_valid_timezone_passes(self):
        validate_timezone("America/Los_Angeles")

    def test_invalid_timezone_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_timezone("Not/AZone")
        self.assertIn("not a known IANA timezone", str(ctx.exception))


class TimezoneChoicesTests(TestCase):
    def test_timezone_choices_are_sorted_pairs(self):
        choices = timezone_choices()
        self.assertTrue(choices)
        self.assertEqual(choices, sorted(choices, key=lambda pair: pair[0]))
        self.assertIn(("UTC", "UTC"), choices)
        for value, label in choices[:5]:
            self.assertEqual(value, label)


class OrganizationZoneinfoTests(TestCase):
    def test_zoneinfo_resolves_valid_timezone(self):
        org = Organization.objects.create(
            name="Good TZ", slug="good-tz", timezone="Pacific/Auckland"
        )
        self.assertEqual(org.zoneinfo, ZoneInfo("Pacific/Auckland"))

    def test_zoneinfo_falls_back_to_utc_for_bad_data(self):
        org = Organization.objects.create(name="Bad TZ", slug="bad-tz")
        Organization.objects.filter(pk=org.pk).update(timezone="Not/AZone")
        org.refresh_from_db()
        self.assertEqual(org.zoneinfo, ZoneInfo("UTC"))


class UserEmailConstraintTests(TestCase):
    def test_duplicate_non_empty_email_rejected(self):
        User.objects.create_user("u1", email="dup@test.dev", password="pass-phrase-12")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    "u2", email="dup@test.dev", password="pass-phrase-12"
                )

    def test_multiple_blank_emails_allowed(self):
        User.objects.create_user("blank1", email="", password="pass-phrase-12")
        User.objects.create_user("blank2", email="", password="pass-phrase-12")
        self.assertEqual(User.objects.filter(email="").count(), 2)


class OrganizationMembershipPropertyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Role Org", slug="role-org")
        self.user = User.objects.create_user("roleu", password="pass-phrase-12")

    def test_precedence_orders_roles(self):
        admin = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        instructor = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        student = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.assertLess(admin.precedence, instructor.precedence)
        self.assertLess(instructor.precedence, student.precedence)

    def test_is_instructional_for_admin_and_instructor(self):
        admin = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        instructor = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        student = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.assertTrue(admin.is_instructional)
        self.assertTrue(instructor.is_instructional)
        self.assertFalse(student.is_instructional)

    def test_unknown_role_gets_low_precedence(self):
        membership = OrganizationMembership(
            user=self.user,
            organization=self.org,
            role="unknown",
        )
        self.assertEqual(membership.precedence, 99)
