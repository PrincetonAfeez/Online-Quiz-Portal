"""Unit coverage for tenancy resolution and feature-flag resolution."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from accounts.models import Organization, OrganizationMembership, User
from assessments.flags import FeatureFlag, invalidate, is_enabled
from assessments.tenancy import (
    SESSION_KEY,
    get_active_organization,
    memberships_for,
    set_active_organization,
)

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tests-tenancy-flags",
    }
}


@override_settings(CACHES=LOCMEM_CACHES)
class TenancyFlagsFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.org_a = Organization.objects.create(name="Alpha Tenancy", slug="alpha-ten")
        self.org_b = Organization.objects.create(name="Beta Tenancy", slug="beta-ten")
        self.user = User.objects.create_user("tenuser", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org_a,
            role=OrganizationMembership.Role.STUDENT,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org_b,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )


class MembershipsForTests(TenancyFlagsFixture):
    def test_anonymous_user_has_no_memberships(self):
        self.assertEqual(memberships_for(AnonymousUser()).count(), 0)

    def test_inactive_memberships_are_excluded(self):
        OrganizationMembership.objects.filter(
            user=self.user, organization=self.org_a
        ).update(is_active=False)
        names = list(
            memberships_for(self.user).values_list("organization__slug", flat=True)
        )
        self.assertEqual(names, ["beta-ten"])

    def test_memberships_order_by_organization_name(self):
        slugs = list(
            memberships_for(self.user).values_list("organization__slug", flat=True)
        )
        self.assertEqual(slugs, ["alpha-ten", "beta-ten"])


class ActiveOrganizationTests(TenancyFlagsFixture):
    def test_get_active_organization_uses_session_selection(self):
        request = self.factory.get("/")
        request.user = self.user
        request.session = {SESSION_KEY: self.org_b.id}
        self.assertEqual(get_active_organization(request), self.org_b)

    def test_get_active_organization_falls_back_to_highest_precedence(self):
        request = self.factory.get("/")
        request.user = self.user
        request.session = {}
        # Instructor membership outranks student even though Alpha sorts first.
        self.assertEqual(get_active_organization(request), self.org_b)

    def test_set_active_organization_persists_valid_choice(self):
        request = self.factory.get("/")
        request.user = self.user
        request.session = {}
        self.assertTrue(set_active_organization(request, self.org_a.id))
        self.assertEqual(request.session[SESSION_KEY], self.org_a.id)
        self.assertEqual(get_active_organization(request), self.org_a)

    def test_set_active_organization_rejects_unknown_org(self):
        request = self.factory.get("/")
        request.user = self.user
        request.session = {}
        foreign = Organization.objects.create(name="Foreign", slug="foreign-ten")
        self.assertFalse(set_active_organization(request, foreign.id))
        self.assertNotIn(SESSION_KEY, request.session)


class FeatureFlagResolutionTests(TenancyFlagsFixture):
    def test_unknown_key_returns_caller_default(self):
        self.assertFalse(is_enabled("missing-flag"))
        self.assertTrue(is_enabled("missing-flag", default=True))

    def test_globally_enabled_flag_is_on_everywhere(self):
        FeatureFlag.objects.create(key="global-edge", enabled=True)
        self.assertTrue(is_enabled("global-edge"))
        self.assertTrue(is_enabled("global-edge", self.org_a))

    def test_org_specific_rollout_when_global_off(self):
        flag = FeatureFlag.objects.create(key="pilot-edge", enabled=False)
        flag.enabled_organizations.add(self.org_a)
        self.assertTrue(is_enabled("pilot-edge", self.org_a))
        self.assertFalse(is_enabled("pilot-edge", self.org_b))
        self.assertFalse(is_enabled("pilot-edge"))

    def test_disabled_global_stays_off_without_org_context(self):
        FeatureFlag.objects.create(key="off-edge", enabled=False)
        self.assertFalse(is_enabled("off-edge", self.org_a))

    def test_cache_hit_then_invalidation_refreshes_value(self):
        flag = FeatureFlag.objects.create(key="cached-edge", enabled=False)
        self.assertFalse(is_enabled("cached-edge", self.org_a))
        FeatureFlag.objects.filter(pk=flag.pk).update(enabled=True)
        self.assertFalse(is_enabled("cached-edge", self.org_a))
        invalidate("cached-edge")
        self.assertTrue(is_enabled("cached-edge", self.org_a))

    def test_lookup_failure_fails_open_to_default(self):
        with patch(
            "assessments.flags.FeatureFlag.objects.filter",
            side_effect=RuntimeError("db unavailable"),
        ):
            self.assertTrue(is_enabled("broken", default=True))
            self.assertFalse(is_enabled("broken", default=False))

    def test_cache_get_failure_still_resolves_from_database(self):
        FeatureFlag.objects.create(key="db-fallback", enabled=True)
        with patch("assessments.flags.cache.get", side_effect=RuntimeError("cache down")):
            self.assertTrue(is_enabled("db-fallback"))
