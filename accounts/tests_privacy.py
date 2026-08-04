"""Tests for accounts.privacy export and erasure."""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from accounts.privacy import (
    _plain,
    anonymize_user,
    export_user_data,
    subject_tombstone,
)
from assessments.models import (
    AdminActionLog,
    Notification,
    TimingAccommodation,
)
from assessments.services import finalize_attempt, save_response, start_attempt
from assessments.tests import BaseFixture


class PlainValueTests(TestCase):
    def test_plain_datetime(self):
        dt = datetime(2026, 1, 15, 12, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(_plain(dt), dt.isoformat())

    def test_plain_date(self):
        self.assertEqual(_plain(date(2026, 1, 15)), "2026-01-15")

    def test_plain_decimal(self):
        self.assertEqual(_plain(Decimal("12.50")), "12.50")

    def test_plain_passthrough(self):
        self.assertEqual(_plain("hello"), "hello")
        self.assertEqual(_plain(42), 42)


class ExportUserDataTests(BaseFixture):
    def test_export_structure_and_keys(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        save_response(
            attempt,
            self.student,
            item.id,
            [item.choice_snapshots.first().id],
            0,
        )
        finalize_attempt(attempt, actor=self.student)

        data = export_user_data(self.student)
        self.assertIn("generated_at", data)
        self.assertEqual(data["format_version"], 1)
        self.assertEqual(data["account"]["username"], "student")
        self.assertEqual(data["account"]["email"], "student@test.dev")
        for key in (
            "organizations",
            "enrollments",
            "accommodations",
            "attempts",
            "daily_participation",
            "streaks",
            "notifications",
            "audit_trail",
        ):
            self.assertIn(key, data)
        self.assertEqual(len(data["attempts"]), 1)
        self.assertTrue(data["organizations"])
        self.assertTrue(data["enrollments"])


class SubjectTombstoneTests(TestCase):
    def test_tombstone_is_stable_for_user(self):
        user = User.objects.create_user("tomb", password="pass-phrase-12")
        first = subject_tombstone(user)
        second = subject_tombstone(user)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_tombstone_differs_between_users(self):
        u1 = User.objects.create_user("t1", password="pass-phrase-12")
        u2 = User.objects.create_user("t2", password="pass-phrase-12")
        self.assertNotEqual(subject_tombstone(u1), subject_tombstone(u2))


class AnonymizeUserTests(BaseFixture):
    def test_anonymize_clears_pii_and_mfa(self):
        self.student.email = "student@test.dev"
        self.student.first_name = "Sam"
        self.student.last_name = "Student"
        self.student.mfa_secret = "ABCDEF"
        self.student.mfa_enabled = True
        self.student.is_staff = True
        self.student.save()

        tombstone = anonymize_user(self.student, actor=self.instructor, reason="test")
        self.student.refresh_from_db()

        self.assertEqual(tombstone, subject_tombstone(self.student))
        self.assertTrue(self.student.username.startswith("erased-"))
        self.assertEqual(self.student.email, "")
        self.assertEqual(self.student.first_name, "")
        self.assertEqual(self.student.last_name, "")
        self.assertFalse(self.student.is_active)
        self.assertFalse(self.student.is_staff)
        self.assertFalse(self.student.is_superuser)
        self.assertFalse(self.student.is_platform_operator)
        self.assertFalse(self.student.mfa_enabled)
        self.assertEqual(self.student.mfa_secret, "")
        self.assertFalse(self.student.has_usable_password())

    def test_anonymize_clears_accommodation_reason(self):
        TimingAccommodation.objects.create(
            organization=self.org,
            student=self.student,
            multiplier=Decimal("1.50"),
            reason="medical documentation on file",
        )
        anonymize_user(self.student, actor=self.instructor)
        self.assertEqual(
            TimingAccommodation.objects.get(student=self.student).reason,
            "",
        )

    def test_anonymize_deletes_notifications(self):
        Notification.objects.create(
            organization=self.org,
            user=self.student,
            title="Hello",
            body="Personal note",
        )
        anonymize_user(self.student, actor=self.instructor)
        self.assertFalse(Notification.objects.filter(user=self.student).exists())

    def test_anonymize_deactivates_memberships_and_logs(self):
        anonymize_user(self.student, actor=self.instructor, reason="gdpr")
        membership = OrganizationMembership.objects.get(
            user=self.student, organization=self.org
        )
        self.assertFalse(membership.is_active)
        self.assertTrue(
            AdminActionLog.objects.filter(
                organization=self.org,
                action="user.anonymized",
            ).exists()
        )
