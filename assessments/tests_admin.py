"""Phase 2b feature tests: rosters, people, accommodations, integrity, progress."""

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    AdminActionLog,
    Choice,
    Cohort,
    Course,
    DailyChallenge,
    DailyParticipation,
    Enrollment,
    IntegrityEvent,
    Question,
    Quiz,
    QuizAssignment,
    TimingAccommodation,
)
from assessments.rosters import commit_roster, parse_roster
from assessments.services import (
    _best_accommodation,
    finalize_attempt,
    publish_quiz,
    save_response,
    start_attempt,
    start_practice_attempt,
    today_for_org,
)


def make_version(org, instructor, minutes=30):
    quiz = Quiz.objects.create(
        organization=org, title="Q", created_by=instructor, time_limit_minutes=minutes
    )
    question = Question.objects.create(
        quiz=quiz, prompt="P", question_type="single", points=1
    )
    Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
    Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
    return publish_quiz(quiz, instructor)


class AdminConsoleFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Acme", slug="acme")
        self.admin = User.objects.create_user("orgadmin", "oa@test.dev", "pass-phrase-12")
        self.instructor = User.objects.create_user("teach", "t@test.dev", "pass-phrase-12")
        self.student = User.objects.create_user("pupil", "p@test.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin, organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        OrganizationMembership.objects.create(
            user=self.instructor, organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        OrganizationMembership.objects.create(
            user=self.student, organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.course = Course.objects.create(organization=self.org, name="Course A")
        self.cohort = Cohort.objects.create(
            organization=self.org, course=self.course, name="Autumn"
        )
        self.client = Client()
        self.client.force_login(self.admin)


class RosterImportTests(AdminConsoleFixture):
    GOOD = (
        "username,email,first_name,last_name,role\n"
        "alice,alice@test.dev,Alice,Anders,student\n"
        "bob,bob@test.dev,Bob,Brown,student\n"
    )

    def test_parse_reports_actions_without_writing(self):
        plan = parse_roster(self.GOOD, self.org, self.course)
        self.assertTrue(plan.can_commit)
        self.assertEqual(plan.create_count, 2)
        self.assertEqual(plan.update_count, 0)
        # Nothing was written by parsing.
        self.assertFalse(User.objects.filter(username="alice").exists())

    def test_commit_creates_accounts_memberships_and_enrolments(self):
        plan = parse_roster(self.GOOD, self.org, self.course)
        result = commit_roster(plan, self.org, self.admin, self.course)
        self.assertEqual(result.created, 2)
        self.assertEqual(result.enrolled, 2)
        alice = User.objects.get(username="alice")
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=alice, organization=self.org,
                role=OrganizationMembership.Role.STUDENT,
            ).exists()
        )
        self.assertTrue(Enrollment.objects.filter(student=alice, course=self.course).exists())
        # Temporary passwords are returned once, never stored in readable form.
        self.assertEqual(len(result.credentials), 2)
        self.assertTrue(alice.has_usable_password())

    def test_commit_is_idempotent(self):
        plan = parse_roster(self.GOOD, self.org, self.course)
        commit_roster(plan, self.org, self.admin, self.course)
        second = parse_roster(self.GOOD, self.org, self.course)
        result = commit_roster(second, self.org, self.admin, self.course)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 2)
        self.assertEqual(User.objects.filter(username="alice").count(), 1)
        self.assertEqual(Enrollment.objects.filter(course=self.course).count(), 2)

    def test_missing_columns_are_reported_not_guessed(self):
        plan = parse_roster("user,mail\nx,y\n", self.org)
        self.assertFalse(plan.can_commit)
        self.assertIn("Missing required column", plan.header_errors[0])

    def test_invalid_rows_are_skipped_and_explained(self):
        text = (
            "username,email\n"
            "good,good@test.dev\n"
            "bad,not-an-email\n"
            ",orphan@test.dev\n"
            "noemail,\n"
        )
        plan = parse_roster(text, self.org)
        self.assertEqual(len(plan.valid_rows), 1)
        self.assertEqual(len(plan.invalid_rows), 3)
        result = commit_roster(plan, self.org, self.admin)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped, 3)
        self.assertFalse(User.objects.filter(username="bad").exists())

    def test_duplicate_rows_within_a_file_are_rejected(self):
        text = (
            "username,email\n"
            "dup,one@test.dev\n"
            "dup,two@test.dev\n"
            "other,one@test.dev\n"
        )
        plan = parse_roster(text, self.org)
        problems = " ".join(e for row in plan.invalid_rows for e in row.errors)
        self.assertIn("duplicate username", problems)
        self.assertIn("duplicate email", problems)

    def test_email_belonging_to_another_account_is_rejected(self):
        User.objects.create_user("existing", "taken@test.dev", "pass-phrase-12")
        plan = parse_roster("username,email\nnewbie,taken@test.dev\n", self.org)
        self.assertEqual(len(plan.invalid_rows), 1)
        self.assertIn("another account", plan.invalid_rows[0].errors[0])

    def test_unknown_cohort_is_rejected(self):
        plan = parse_roster(
            "username,email,cohort\nx,x@test.dev,Nonexistent\n", self.org, self.course
        )
        self.assertIn("does not exist", plan.invalid_rows[0].errors[0])

    def test_bom_prefixed_file_still_parses(self):
        plan = parse_roster("﻿" + self.GOOD, self.org)
        self.assertTrue(plan.can_commit)

    def test_import_is_audited(self):
        plan = parse_roster(self.GOOD, self.org, self.course)
        commit_roster(plan, self.org, self.admin, self.course)
        self.assertTrue(
            AdminActionLog.objects.filter(action="roster.import", organization=self.org).exists()
        )

    def test_upload_flow_requires_org_admin(self):
        upload = SimpleUploadedFile("r.csv", self.GOOD.encode(), content_type="text/csv")
        client = Client()
        client.force_login(self.instructor)
        response = client.post("/manage/roster/", {"csv_file": upload})
        self.assertEqual(response.status_code, 403)

    def test_non_csv_upload_is_rejected(self):
        upload = SimpleUploadedFile("r.txt", b"username,email\n", content_type="text/plain")
        response = self.client.post("/manage/roster/", {"csv_file": upload})
        self.assertContains(response, "Upload a .csv file")

    def test_non_utf8_upload_is_rejected(self):
        upload = SimpleUploadedFile(
            "r.csv", b"username,email\n\xff\xfe bad bytes\n", content_type="text/csv"
        )
        response = self.client.post("/manage/roster/", {"csv_file": upload})
        self.assertContains(response, "not UTF-8")


class PeopleManagementTests(AdminConsoleFixture):
    def test_search_and_filter(self):
        response = self.client.get("/manage/people/", {"q": "pupil"})
        self.assertContains(response, "pupil")
        self.assertNotContains(response, "teach</span>")

    def test_deactivate_and_reactivate_membership(self):
        membership = OrganizationMembership.objects.get(
            user=self.student, organization=self.org
        )
        self.client.post(f"/manage/people/{membership.id}/toggle/")
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)
        self.assertTrue(
            AdminActionLog.objects.filter(action="membership.deactivated").exists()
        )
        self.client.post(f"/manage/people/{membership.id}/toggle/")
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)

    def test_cannot_lock_yourself_out(self):
        membership = OrganizationMembership.objects.get(
            user=self.admin, organization=self.org
        )
        self.client.post(f"/manage/people/{membership.id}/toggle/")
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)

    def test_other_organizations_people_are_invisible(self):
        other = Organization.objects.create(name="Other", slug="other")
        stranger = User.objects.create_user("stranger", "s@test.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=stranger, organization=other,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.assertNotContains(self.client.get("/manage/people/"), "stranger")


class AccommodationConsoleTests(AdminConsoleFixture):
    def test_grant_accommodation_and_apply_it(self):
        client = Client()
        client.force_login(self.instructor)
        response = client.post(
            "/manage/accommodations/",
            {"username": "pupil", "multiplier": "1.50", "extra_minutes": "10",
             "reason": "Documented need", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        accommodation = TimingAccommodation.objects.get()
        self.assertEqual(accommodation.student, self.student)

        version = make_version(self.org, self.instructor, minutes=20)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Timed", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        multiplier, extra = _best_accommodation(self.student, assignment)
        self.assertEqual((str(multiplier), extra), ("1.50", 10))

    def test_reason_is_not_copied_into_the_admin_log(self):
        client = Client()
        client.force_login(self.instructor)
        client.post(
            "/manage/accommodations/",
            {"username": "pupil", "multiplier": "2.00", "extra_minutes": "0",
             "reason": "Sensitive medical detail", "is_active": "on"},
        )
        entry = AdminActionLog.objects.get(action="accommodation.created")
        self.assertNotIn("Sensitive", str(entry.detail))

    def test_unknown_student_is_rejected(self):
        client = Client()
        client.force_login(self.instructor)
        response = client.post(
            "/manage/accommodations/",
            {"username": "ghost", "multiplier": "1.50", "extra_minutes": "0", "is_active": "on"},
        )
        self.assertContains(response, "not an active member")
        self.assertFalse(TimingAccommodation.objects.exists())

    def test_absurd_multiplier_is_rejected(self):
        client = Client()
        client.force_login(self.instructor)
        response = client.post(
            "/manage/accommodations/",
            {"username": "pupil", "multiplier": "99", "extra_minutes": "0", "is_active": "on"},
        )
        self.assertContains(response, "typo")

    def test_revoking_does_not_shorten_a_running_attempt(self):
        accommodation = TimingAccommodation.objects.create(
            organization=self.org, student=self.student, multiplier="2.00", extra_minutes=0
        )
        version = make_version(self.org, self.instructor, minutes=10)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Running", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        deadline = attempt.deadline_at

        client = Client()
        client.force_login(self.instructor)
        client.post(f"/manage/accommodations/{accommodation.id}/revoke/")
        attempt.refresh_from_db()
        self.assertEqual(attempt.deadline_at, deadline)

    def test_students_cannot_reach_accommodations(self):
        client = Client()
        client.force_login(self.student)
        self.assertEqual(client.get("/manage/accommodations/").status_code, 403)


class IntegrityDashboardTests(AdminConsoleFixture):
    def test_events_are_listed_and_summarised(self):
        version = make_version(self.org, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Watched", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.first()
        student_client = Client()
        student_client.force_login(self.student)
        url = f"/attempts/{attempt.id}/save/{item.id}/"
        student_client.post(url, {"choices": [item.choice_snapshots.first().id], "revision": "0"})
        # A stale revision must leave a durable record: the event is written
        # after the save transaction rolls back, not inside it.
        conflict = student_client.post(url, {"choices": [], "revision": "0"})
        self.assertEqual(conflict.status_code, 409)

        client = Client()
        client.force_login(self.instructor)
        response = client.get("/manage/integrity/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            IntegrityEvent.objects.filter(
                organization=self.org, kind=IntegrityEvent.Kind.REVISION_CONFLICT
            ).exists()
        )
        self.assertContains(response, "Revision conflict")

    def test_dashboard_is_organization_scoped(self):
        other = Organization.objects.create(name="Other", slug="other2")
        IntegrityEvent.objects.create(
            organization=other, kind=IntegrityEvent.Kind.LATE_SAVE_REJECTED, detail={}
        )
        client = Client()
        client.force_login(self.instructor)
        response = client.get("/manage/integrity/")
        self.assertEqual(len(response.context["events"]), 0)


class StudentProgressTests(AdminConsoleFixture):
    def test_progress_page_summarises_history(self):
        version = make_version(self.org, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="History", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.first()
        save_response(attempt, self.student, item.id,
                      [item.choice_snapshots.get(text="a").id], 0)
        finalize_attempt(attempt, actor=self.student)

        client = Client()
        client.force_login(self.student)
        response = client.get("/progress/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scored_count"], 1)
        self.assertEqual(response.context["average_percent"], 100)

    def test_progress_only_shows_your_own_attempts(self):
        other = User.objects.create_user("other", "o@test.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=other, organization=self.org, role=OrganizationMembership.Role.STUDENT
        )
        version = make_version(self.org, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Theirs", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        start_attempt(other, assignment)
        client = Client()
        client.force_login(self.student)
        self.assertEqual(client.get("/progress/").context["total"], 0)


class PracticeModeTests(AdminConsoleFixture):
    def test_archived_challenge_practice_earns_no_credit(self):
        version = make_version(self.org, self.instructor)
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=version,
            release_date=today_for_org(self.org) - timedelta(days=3),
            title="Old challenge",
        )
        attempt = start_practice_attempt(self.student, challenge)
        # Practice attempts are not linked to the challenge, so no credit.
        self.assertIsNone(attempt.daily_challenge_id)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertFalse(DailyParticipation.objects.filter(student=self.student).exists())

    def test_todays_challenge_cannot_be_practised(self):
        version = make_version(self.org, self.instructor)
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=version,
            release_date=today_for_org(self.org), title="Live",
        )
        with self.assertRaises(PermissionDenied):
            start_practice_attempt(self.student, challenge)


class QuestionBankWorkspaceTests(AdminConsoleFixture):
    def test_bank_search_is_scoped_and_filterable(self):
        from assessments.models import QuestionBank, QuestionBankItem, QuestionChoice

        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="Photosynthesis basics",
            question_type="single", points=1, difficulty="easy",
            created_by=self.instructor,
        )
        QuestionChoice.objects.create(item=item, text="a", is_correct=True)
        QuestionChoice.objects.create(item=item, text="b", is_correct=False)

        other = Organization.objects.create(name="Other", slug="other3")
        other_ins = User.objects.create_user("oi2", "oi2@test.dev", "pass-phrase-12")
        other_bank = QuestionBank.objects.create(
            organization=other, name="Foreign", created_by=other_ins
        )
        QuestionBankItem.objects.create(
            organization=other, bank=other_bank, prompt="Foreign secret item",
            question_type="single", points=1, created_by=other_ins,
        )

        client = Client()
        client.force_login(self.instructor)
        response = client.get("/manage/question-bank/", {"q": "Photosynthesis"})
        self.assertContains(response, "Photosynthesis")
        self.assertNotContains(response, "Foreign secret item")

    def test_adding_a_bank_item_copies_it_into_the_draft(self):
        from assessments.models import QuestionBank, QuestionBankItem, QuestionChoice

        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="Reusable prompt",
            question_type="single", points=2, created_by=self.instructor,
        )
        QuestionChoice.objects.create(item=item, text="right", is_correct=True, sort_order=1)
        QuestionChoice.objects.create(item=item, text="wrong", is_correct=False, sort_order=2)
        quiz = Quiz.objects.create(
            organization=self.org, title="Target", created_by=self.instructor
        )

        client = Client()
        client.force_login(self.instructor)
        client.post(f"/instructor/quizzes/{quiz.id}/bank/{item.id}/add/")

        question = quiz.questions.get()
        self.assertEqual(question.prompt, "Reusable prompt")
        self.assertEqual(question.choices.count(), 2)
        # A copy: editing the draft must not touch the bank item.
        question.prompt = "Edited in the draft"
        question.save()
        item.refresh_from_db()
        self.assertEqual(item.prompt, "Reusable prompt")

    def test_unapproved_items_cannot_be_reused(self):
        from assessments.models import QuestionBank, QuestionBankItem

        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="Draft item",
            question_type="single", points=1, review_status="pending",
            created_by=self.instructor,
        )
        quiz = Quiz.objects.create(
            organization=self.org, title="Target", created_by=self.instructor
        )
        client = Client()
        client.force_login(self.instructor)
        client.post(f"/instructor/quizzes/{quiz.id}/bank/{item.id}/add/")
        self.assertEqual(quiz.questions.count(), 0)


class RosterHardeningTests(AdminConsoleFixture):
    """Phase 3: defects found by auditing the Phase 2 features."""

    def test_admin_role_cannot_be_granted_by_import(self):
        # A mistyped column must not be able to hand over the organization.
        plan = parse_roster("username,email,role\nesc,esc@test.dev,org_admin\n", self.org)
        self.assertEqual(len(plan.invalid_rows), 1)
        self.assertIn("cannot be set by import", plan.invalid_rows[0].errors[0])
        commit_roster(plan, self.org, self.admin)
        self.assertFalse(User.objects.filter(username="esc").exists())

    def test_instructor_role_is_still_importable(self):
        plan = parse_roster("username,email,role\ntutor,tutor@test.dev,instructor\n", self.org)
        self.assertTrue(plan.can_commit)
        commit_roster(plan, self.org, self.admin)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user__username="tutor", role=OrganizationMembership.Role.INSTRUCTOR
            ).exists()
        )

    def test_parse_does_not_scale_queries_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rows = "\n".join(f"u{i},u{i}@test.dev" for i in range(50))
        with CaptureQueriesContext(connection) as ctx:
            parse_roster("username,email\n" + rows, self.org)
        # Two lookups total, not two per row.
        self.assertLessEqual(len(ctx), 4)

    def test_row_cap_is_enforced_and_reported(self):
        rows = "\n".join(f"c{i},c{i}@test.dev" for i in range(5100))
        plan = parse_roster("username,email\n" + rows, self.org)
        self.assertEqual(len(plan.rows), 5000)
        self.assertTrue(any("limit" in e for e in plan.header_errors))

    def test_commit_is_atomic(self):
        plan = parse_roster(
            "username,email\ngood1,good1@test.dev\ngood2,good2@test.dev\n", self.org
        )
        self.assertEqual(len(plan.valid_rows), 2)
        commit_roster(plan, self.org, self.admin)
        self.assertEqual(User.objects.filter(username__startswith="good").count(), 2)


class CrossTenantFeatureTests(AdminConsoleFixture):
    """Phase 3: every new surface must refuse another organization's objects."""

    def setUp(self):
        super().setUp()
        self.other = Organization.objects.create(name="Other", slug="otherco")
        self.other_ins = User.objects.create_user("oins", "oins@test.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.other_ins, organization=self.other,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        version = make_version(self.other, self.other_ins)
        self.foreign_assignment = QuizAssignment.objects.create(
            organization=self.other, title="Foreign", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.other_ins,
        )

    def test_gradebook_analytics_export_reject_foreign_ids(self):
        client = Client()
        client.force_login(self.instructor)
        for suffix in ("gradebook", "analytics", "export"):
            response = client.get(
                f"/instructor/assignments/{self.foreign_assignment.id}/{suffix}/"
            )
            self.assertEqual(response.status_code, 404, suffix)

    def test_accommodation_cannot_target_a_foreign_student(self):
        foreign_student = User.objects.create_user("fstu", "fstu@test.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=foreign_student, organization=self.other,
            role=OrganizationMembership.Role.STUDENT,
        )
        client = Client()
        client.force_login(self.instructor)
        client.post(
            "/manage/accommodations/",
            {"username": "fstu", "multiplier": "2.00", "extra_minutes": "0", "is_active": "on"},
        )
        self.assertFalse(TimingAccommodation.objects.filter(student=foreign_student).exists())

    def test_foreign_bank_item_cannot_be_copied_into_a_local_quiz(self):
        from assessments.models import QuestionBank, QuestionBankItem

        bank = QuestionBank.objects.create(
            organization=self.other, name="Foreign bank", created_by=self.other_ins
        )
        item = QuestionBankItem.objects.create(
            organization=self.other, bank=bank, prompt="Foreign item",
            question_type="single", points=1, created_by=self.other_ins,
        )
        quiz = Quiz.objects.create(
            organization=self.org, title="Local", created_by=self.instructor
        )
        client = Client()
        client.force_login(self.instructor)
        response = client.post(f"/instructor/quizzes/{quiz.id}/bank/{item.id}/add/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(quiz.questions.count(), 0)

    def test_integrity_and_activity_are_scoped(self):
        client = Client()
        client.force_login(self.instructor)
        self.assertEqual(len(client.get("/manage/integrity/").context["events"]), 0)
        admin_client = Client()
        admin_client.force_login(self.admin)
        self.assertEqual(admin_client.get("/manage/activity/").status_code, 200)


class AdminStudentPickerScopeTests(TestCase):
    def test_enrollment_student_picker_excludes_foreign_org_users(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from assessments.admin import EnrollmentAdmin

        org_a = Organization.objects.create(name="A", slug="pick-a")
        org_b = Organization.objects.create(name="B", slug="pick-b")
        staff = User.objects.create_user("pickstaff", password="pass-phrase-12", is_staff=True)
        OrganizationMembership.objects.create(
            user=staff, organization=org_a, role=OrganizationMembership.Role.INSTRUCTOR
        )
        local = User.objects.create_user("localstu", password="pass-phrase-12")
        foreign = User.objects.create_user("foreignstu", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=local, organization=org_a, role=OrganizationMembership.Role.STUDENT
        )
        OrganizationMembership.objects.create(
            user=foreign, organization=org_b, role=OrganizationMembership.Role.STUDENT
        )
        request = RequestFactory().get("/admin/")
        request.user = staff
        field = Enrollment._meta.get_field("student")
        formfield = EnrollmentAdmin(Enrollment, AdminSite()).formfield_for_foreignkey(
            field, request
        )
        ids = set(formfield.queryset.values_list("id", flat=True))
        self.assertIn(local.id, ids)
        self.assertNotIn(foreign.id, ids)

    def test_assignment_explicit_students_m2m_excludes_foreign_users(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from assessments.admin import QuizAssignmentAdmin

        org_a = Organization.objects.create(name="A2", slug="pick-a2")
        org_b = Organization.objects.create(name="B2", slug="pick-b2")
        staff = User.objects.create_user("pickstaff2", password="pass-phrase-12", is_staff=True)
        OrganizationMembership.objects.create(
            user=staff, organization=org_a, role=OrganizationMembership.Role.INSTRUCTOR
        )
        local = User.objects.create_user("localstu2", password="pass-phrase-12")
        foreign = User.objects.create_user("foreignstu2", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=local, organization=org_a, role=OrganizationMembership.Role.STUDENT
        )
        OrganizationMembership.objects.create(
            user=foreign, organization=org_b, role=OrganizationMembership.Role.STUDENT
        )
        request = RequestFactory().get("/admin/")
        request.user = staff
        field = QuizAssignment._meta.get_field("explicit_students")
        formfield = QuizAssignmentAdmin(QuizAssignment, AdminSite()).formfield_for_manytomany(
            field, request
        )
        ids = set(formfield.queryset.values_list("id", flat=True))
        self.assertIn(local.id, ids)
        self.assertNotIn(foreign.id, ids)
