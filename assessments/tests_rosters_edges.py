"""Edge-case coverage for roster CSV parsing and commit paths."""

from __future__ import annotations

from django.test import TestCase

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import AdminActionLog, Course, Enrollment
from assessments.rosters import commit_roster, parse_roster


class RosterEdgeFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Roster Org", slug="roster-org")
        self.admin = User.objects.create_user("roadmin", "ro@test.dev", "pass-phrase-12")
        self.course = Course.objects.create(organization=self.org, name="Roster Course")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )


class RosterHeaderEdgeTests(RosterEdgeFixture):
    def test_empty_file_reports_header_error(self):
        plan = parse_roster("", self.org)
        self.assertFalse(plan.can_commit)
        self.assertIn("empty", plan.header_errors[0].lower())

    def test_header_only_reports_no_rows(self):
        plan = parse_roster("username,email\n", self.org)
        self.assertFalse(plan.can_commit)
        self.assertIn("no rows", plan.header_errors[0].lower())

    def test_only_invalid_rows_cannot_commit(self):
        plan = parse_roster(
            "username,email\n"
            "bad,not-an-email\n"
            ",missing-user@test.dev\n",
            self.org,
        )
        self.assertFalse(plan.can_commit)
        self.assertEqual(len(plan.valid_rows), 0)
        self.assertEqual(len(plan.invalid_rows), 2)


class RosterCohortEdgeTests(RosterEdgeFixture):
    def test_cohort_column_without_course_is_invalid(self):
        plan = parse_roster(
            "username,email,cohort\n"
            "alice,alice@test.dev,Any\n",
            self.org,
            course=None,
        )
        self.assertEqual(len(plan.valid_rows), 0)
        self.assertIn("course to be selected", plan.invalid_rows[0].errors[0])

    def test_unknown_cohort_in_course_is_rejected(self):
        plan = parse_roster(
            "username,email,cohort\n"
            "bob,bob@test.dev,Missing\n",
            self.org,
            self.course,
        )
        self.assertEqual(len(plan.valid_rows), 0)
        self.assertIn("does not exist", plan.invalid_rows[0].errors[0])


class RosterCommitEdgeTests(RosterEdgeFixture):
    def test_commit_without_course_creates_membership_only(self):
        text = "username,email,role\nprof,prof@test.dev,instructor\n"
        plan = parse_roster(text, self.org, course=None)
        self.assertTrue(plan.can_commit)
        result = commit_roster(plan, self.org, self.admin, course=None)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.enrolled, 0)
        prof = User.objects.get(username="prof")
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=prof,
                organization=self.org,
                role=OrganizationMembership.Role.INSTRUCTOR,
            ).exists()
        )
        self.assertFalse(Enrollment.objects.filter(student=prof).exists())

    def test_instructor_import_does_not_create_enrollment(self):
        text = "username,email,role\n"
        text += "teach,teach@test.dev,instructor\n"
        plan = parse_roster(text, self.org, self.course)
        result = commit_roster(plan, self.org, self.admin, self.course)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.enrolled, 0)
        teach = User.objects.get(username="teach")
        self.assertFalse(
            Enrollment.objects.filter(student=teach, course=self.course).exists()
        )

    def test_student_import_with_course_enrolls(self):
        text = "username,email,role\n"
        text += "pupil,pupil@test.dev,student\n"
        plan = parse_roster(text, self.org, self.course)
        result = commit_roster(plan, self.org, self.admin, self.course)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.enrolled, 1)
        pupil = User.objects.get(username="pupil")
        self.assertTrue(
            Enrollment.objects.filter(student=pupil, course=self.course).exists()
        )

    def test_commit_only_valid_rows_when_mixed(self):
        text = (
            "username,email\n"
            "good,good@test.dev\n"
            "bad,not-an-email\n"
        )
        plan = parse_roster(text, self.org)
        result = commit_roster(plan, self.org, self.admin)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped, 1)
        self.assertTrue(User.objects.filter(username="good").exists())
        self.assertFalse(User.objects.filter(username="bad").exists())

    def test_commit_without_course_logs_empty_target_id(self):
        plan = parse_roster("username,email\nsolo,solo@test.dev\n", self.org)
        commit_roster(plan, self.org, self.admin, course=None)
        log = AdminActionLog.objects.get(action="roster.import", organization=self.org)
        self.assertEqual(log.target_id, "")

    def test_admin_role_in_spreadsheet_is_rejected(self):
        plan = parse_roster(
            "username,email,role\n"
            "boss,boss@test.dev,org_admin\n",
            self.org,
        )
        self.assertFalse(plan.can_commit)
        self.assertIn("cannot be set by import", plan.invalid_rows[0].errors[0])
