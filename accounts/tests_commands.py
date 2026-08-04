"""Tests for accounts management commands."""

from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import Organization, OrganizationMembership, User
from accounts.privacy import subject_tombstone


class ManagementCommandFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Cmd Org", slug="cmd-org")
        self.user = User.objects.create_user(
            "cmduser", "cmd@test.dev", "pass-phrase-12"
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )


class ExportUserDataCommandTests(ManagementCommandFixture):
    def test_export_to_stdout(self):
        out = StringIO()
        call_command("export_user_data", "cmduser", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["account"]["username"], "cmduser")
        self.assertIn("organizations", payload)

    def test_export_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.json"
            out = StringIO()
            call_command(
                "export_user_data",
                "cmduser",
                output=str(path),
                stdout=out,
            )
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["account"]["email"], "cmd@test.dev")
            self.assertIn("Wrote", out.getvalue())

    def test_export_unknown_user_raises(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("export_user_data", "missing-user")
        self.assertIn("No user named", str(ctx.exception))


class DeleteUserCommandTests(ManagementCommandFixture):
    def test_delete_requires_confirmation_flag(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("delete_user", "cmduser")
        self.assertIn("--yes", str(ctx.exception))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_delete_with_yes_anonymizes(self):
        out = StringIO()
        call_command(
            "delete_user",
            "cmduser",
            "--yes",
            "--reason=operator request",
            stdout=out,
        )
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.username.startswith("erased-"))
        self.assertIn(subject_tombstone(self.user), out.getvalue())

    def test_delete_unknown_user_raises(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("delete_user", "nobody", "--yes")
        self.assertIn("No user named", str(ctx.exception))
