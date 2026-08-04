"""Context processor coverage — navigation() for roles and notifications."""

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.context_processors import navigation
from assessments.models import Notification


class NavigationContextTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.org_a = Organization.objects.create(name="Alpha", slug="alpha")
        self.org_b = Organization.objects.create(name="Beta", slug="beta")
        self.student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        self.admin = User.objects.create_user("adm", "adm@t.dev", "pass-phrase-12")

    def _request(self, user, organization=None):
        request = self.factory.get("/")
        request.user = user
        if organization is not None:
            request.organization = organization
        return request

    def test_anonymous_user_gets_empty_context(self):
        request = self._request(AnonymousUser())
        self.assertEqual(navigation(request), {})

    def test_authenticated_without_organization_flags_are_false(self):
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org_a,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        request = self._request(self.instructor)
        ctx = navigation(request)
        self.assertFalse(ctx["is_instructor"])
        self.assertFalse(ctx["is_org_admin"])
        self.assertEqual(ctx["active_organization"], None)

    def test_single_membership_cannot_switch(self):
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org_a,
            role=OrganizationMembership.Role.STUDENT,
        )
        request = self._request(self.student, organization=self.org_a)
        ctx = navigation(request)
        self.assertFalse(ctx["can_switch_organization"])
        self.assertEqual(len(ctx["user_memberships"]), 1)

    def test_multi_membership_can_switch(self):
        for org in (self.org_a, self.org_b):
            OrganizationMembership.objects.create(
                user=self.instructor,
                organization=org,
                role=OrganizationMembership.Role.INSTRUCTOR,
            )
        request = self._request(self.instructor, organization=self.org_a)
        ctx = navigation(request)
        self.assertTrue(ctx["can_switch_organization"])
        self.assertEqual(len(ctx["user_memberships"]), 2)
        self.assertEqual(ctx["active_organization"], self.org_a)

    def test_instructor_flag_when_active_org_matches(self):
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org_a,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        request = self._request(self.instructor, organization=self.org_a)
        ctx = navigation(request)
        self.assertTrue(ctx["is_instructor"])
        self.assertFalse(ctx["is_org_admin"])

    def test_org_admin_flag(self):
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org_a,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        request = self._request(self.admin, organization=self.org_a)
        ctx = navigation(request)
        self.assertTrue(ctx["is_org_admin"])
        self.assertTrue(ctx["is_instructor"])

    def test_unread_notifications_count(self):
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org_a,
            role=OrganizationMembership.Role.STUDENT,
        )
        Notification.objects.create(
            organization=self.org_a,
            user=self.student,
            kind=Notification.Kind.DUE_SOON,
            title="Due",
            body="Soon",
        )
        Notification.objects.create(
            organization=self.org_a,
            user=self.student,
            kind=Notification.Kind.DAILY_LIVE,
            title="Daily",
            body="Live",
            read_at=None,
        )
        Notification.objects.create(
            organization=self.org_a,
            user=self.student,
            kind=Notification.Kind.RESULT_RELEASED,
            title="Read",
            body="Done",
            read_at=timezone.now(),
        )

        request = self._request(self.student, organization=self.org_a)
        ctx = navigation(request)
        self.assertEqual(ctx["unread_notifications"], 2)
