"""seed_demo management command — users, idempotency, authentication."""

from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from accounts.models import Organization, OrganizationMembership, User


class SeedDemoCommandTests(TestCase):
    PASSWORD = "quizops-demo-2026"
    SLUG = "demo-academy"

    def test_creates_demo_academy_users_and_memberships(self):
        call_command("seed_demo", stdout=StringIO())

        org = Organization.objects.get(slug=self.SLUG)
        self.assertEqual(org.name, "Demo Academy")

        for username, role in (
            ("student", OrganizationMembership.Role.STUDENT),
            ("instructor", OrganizationMembership.Role.INSTRUCTOR),
            ("admin", OrganizationMembership.Role.ORG_ADMIN),
        ):
            user = User.objects.get(username=username)
            membership = OrganizationMembership.objects.get(user=user, organization=org)
            self.assertEqual(membership.role, role)
            self.assertTrue(membership.is_active)

    def test_second_run_is_idempotent(self):
        call_command("seed_demo", stdout=StringIO())
        user_count = User.objects.count()
        membership_count = OrganizationMembership.objects.count()
        org_count = Organization.objects.count()

        call_command("seed_demo", stdout=StringIO())

        self.assertEqual(User.objects.count(), user_count)
        self.assertEqual(OrganizationMembership.objects.count(), membership_count)
        self.assertEqual(Organization.objects.count(), org_count)
        self.assertEqual(Organization.objects.filter(slug=self.SLUG).count(), 1)

    def test_documented_password_authenticates(self):
        call_command("seed_demo", stdout=StringIO())

        client = Client()
        for username in ("student", "instructor", "admin"):
            session_client = Client()
            response = session_client.post(
                "/accounts/login/",
                {"username": username, "password": self.PASSWORD},
            )
            self.assertEqual(response.status_code, 302, msg=username)

    def test_stdout_reports_success(self):
        out = StringIO()
        call_command("seed_demo", stdout=out)
        text = out.getvalue()
        self.assertIn("Seeded QuizOps demo", text)
        self.assertIn(self.PASSWORD, text)
