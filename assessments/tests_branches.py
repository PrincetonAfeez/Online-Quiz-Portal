"""Phase 4: final branch coverage — error paths and filter permutations."""

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.forms import AssignmentForm, ChoiceFormSet, QuestionForm, QuizForm
from assessments.models import (
    Choice,
    Cohort,
    Course,
    Enrollment,
    IntegrityEvent,
    Question,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
    QuizAssignment,
)
from assessments.services import finalize_attempt, publish_quiz, start_attempt


class BranchFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Org", slug="org")
        self.admin = User.objects.create_user("adm", "adm@t.dev", "pass-phrase-12")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        self.student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        for user, role in (
            (self.admin, OrganizationMembership.Role.ORG_ADMIN),
            (self.instructor, OrganizationMembership.Role.INSTRUCTOR),
            (self.student, OrganizationMembership.Role.STUDENT),
        ):
            OrganizationMembership.objects.create(
                user=user, organization=self.org, role=role
            )
        self.course = Course.objects.create(organization=self.org, name="Course")
        self.cohort = Cohort.objects.create(
            organization=self.org, course=self.course, name="Cohort"
        )
        Enrollment.objects.create(
            organization=self.org, student=self.student,
            course=self.course, cohort=self.cohort,
        )
        quiz = Quiz.objects.create(
            organization=self.org, title="Q", created_by=self.instructor,
            shuffle_questions=False, shuffle_choices=False,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="P", question_type="single", points=1
        )
        Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
        self.version = publish_quiz(quiz, self.instructor)


class FormValidationTests(BranchFixture):
    def test_scoped_form_requires_an_organization(self):
        with self.assertRaises(ValueError):
            QuizForm()

    def test_question_form_rejects_zero_points(self):
        form = QuestionForm(
            data={"prompt": "p", "question_type": "single", "points": "0", "explanation": ""}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("points", form.errors)

    def test_choice_formset_requires_two_choices(self):
        question = Question(quiz=self.version.quiz, question_type="single")
        formset = ChoiceFormSet(
            data={
                "choices-TOTAL_FORMS": "2", "choices-INITIAL_FORMS": "0",
                "choices-MIN_NUM_FORMS": "2", "choices-MAX_NUM_FORMS": "1000",
                "choices-0-text": "only", "choices-0-is_correct": "on",
                "choices-1-text": "",
            },
            instance=question,
        )
        self.assertFalse(formset.is_valid())

    def test_true_false_formset_rejects_three_choices(self):
        question = Question(quiz=self.version.quiz, question_type="true_false")
        formset = ChoiceFormSet(
            data={
                "choices-TOTAL_FORMS": "3", "choices-INITIAL_FORMS": "0",
                "choices-MIN_NUM_FORMS": "2", "choices-MAX_NUM_FORMS": "1000",
                "choices-0-text": "t", "choices-0-is_correct": "on",
                "choices-1-text": "f",
                "choices-2-text": "maybe",
            },
            instance=question,
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("exactly two choices", str(formset.non_form_errors()))

    def test_assignment_form_scopes_its_querysets(self):
        other = Organization.objects.create(name="Other", slug="other")
        other_course = Course.objects.create(organization=other, name="Foreign course")
        form = AssignmentForm(organization=self.org)
        self.assertNotIn(other_course, form.fields["course"].queryset)
        self.assertIn(self.course, form.fields["course"].queryset)

    def test_assignment_form_prefills_local_times_on_edit(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Prefill", quiz_version=self.version,
            opens_at=timezone.now(), created_by=self.instructor,
        )
        form = AssignmentForm(instance=assignment, organization=self.org)
        self.assertIn("T", form.initial["opens_at"])


class AdminConsoleBranchTests(BranchFixture):
    def test_people_filters_combine(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get("/manage/people/", {"q": "ins", "role": "instructor"})
        self.assertContains(response, "ins")
        response = client.get("/manage/people/", {"role": "student"})
        self.assertNotContains(response, ">ins<")

    def test_question_bank_filter_permutations(self):
        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="Findable prompt",
            question_type="single", points=1, difficulty="hard",
            review_status="approved", source_name="Textbook",
            created_by=self.instructor,
        )
        QuestionChoice.objects.create(item=item, text="a", is_correct=True)
        client = Client()
        client.force_login(self.instructor)

        for params in (
            {"q": "Findable"},
            {"difficulty": "hard"},
            {"bank": str(bank.id)},
            {"status": "approved"},
            {"q": "Textbook", "difficulty": "hard", "bank": str(bank.id)},
        ):
            response = client.get("/manage/question-bank/", params)
            self.assertContains(response, "Findable prompt", msg_prefix=str(params))

        self.assertNotContains(
            client.get("/manage/question-bank/", {"difficulty": "easy"}), "Findable prompt"
        )

    def test_integrity_filter_by_assignment(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Filtered", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        IntegrityEvent.objects.create(
            organization=self.org, attempt=attempt,
            kind=IntegrityEvent.Kind.REVISION_CONFLICT, detail={},
        )
        client = Client()
        client.force_login(self.instructor)
        response = client.get("/manage/integrity/", {"assignment": str(assignment.id)})
        self.assertEqual(len(response.context["events"]), 1)
        other = QuizAssignment.objects.create(
            organization=self.org, title="Other", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        response = client.get("/manage/integrity/", {"assignment": str(other.id)})
        self.assertEqual(len(response.context["events"]), 0)
        # A non-numeric filter is ignored rather than crashing.
        self.assertEqual(
            client.get("/manage/integrity/", {"assignment": "abc"}).status_code, 200
        )

    def test_roster_upload_rejects_an_oversized_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        big = SimpleUploadedFile(
            "big.csv", b"username,email\n" + b"x" * (3 * 1024 * 1024), content_type="text/csv"
        )
        client = Client()
        client.force_login(self.admin)
        response = client.post("/manage/roster/", {"csv_file": big})
        self.assertContains(response, "larger than 2 MB")

    def test_roster_commit_requires_a_valid_plan(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("bad.csv", b"nope\n1\n", content_type="text/csv")
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            "/manage/roster/", {"csv_file": bad, "commit": "1"}
        )
        self.assertContains(response, "Missing required column")
        self.assertEqual(User.objects.count(), 3)

    def test_accommodation_with_a_course_scope(self):
        client = Client()
        client.force_login(self.instructor)
        client.post(
            "/manage/accommodations/",
            {"username": "stu", "course": str(self.course.id), "multiplier": "1.25",
             "extra_minutes": "5", "reason": "scoped", "is_active": "on"},
        )
        from assessments.models import TimingAccommodation

        self.assertEqual(TimingAccommodation.objects.get().course, self.course)


class InstructorBranchTests(BranchFixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.instructor)

    def test_publish_readiness_reports_each_problem_kind(self):
        quiz = Quiz.objects.create(
            organization=self.org, title="Problems", created_by=self.instructor
        )
        lonely = Question.objects.create(
            quiz=quiz, prompt="one choice", question_type="single", points=1
        )
        Choice.objects.create(question=lonely, text="a", is_correct=True)

        tf = Question.objects.create(
            quiz=quiz, prompt="tf", question_type="true_false", points=1
        )
        for text in ("a", "b", "c"):
            Choice.objects.create(question=tf, text=text, is_correct=(text == "a"))

        multi = Question.objects.create(
            quiz=quiz, prompt="multi", question_type="multi", points=1
        )
        Choice.objects.create(question=multi, text="a", is_correct=True)
        Choice.objects.create(question=multi, text="b", is_correct=True)

        response = self.client.get(f"/instructor/quizzes/{quiz.id}/")
        problems = " ".join(response.context["problems"])
        self.assertIn("fewer than two choices", problems)
        self.assertIn("true/false", problems)
        self.assertIn("every choice correct", problems)
        self.assertFalse(response.context["can_publish"])

    def test_multi_question_with_no_correct_choice_is_reported(self):
        quiz = Quiz.objects.create(
            organization=self.org, title="NoCorrect", created_by=self.instructor
        )
        multi = Question.objects.create(
            quiz=quiz, prompt="multi", question_type="multi", points=1
        )
        Choice.objects.create(question=multi, text="a", is_correct=False)
        Choice.objects.create(question=multi, text="b", is_correct=False)
        response = self.client.get(f"/instructor/quizzes/{quiz.id}/")
        self.assertIn("no correct choice", " ".join(response.context["problems"]))

    def test_question_form_get_for_a_new_question(self):
        response = self.client.get(
            f"/instructor/quizzes/{self.version.quiz_id}/questions/new/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["question"])

    def test_question_form_invalid_post_rerenders_with_errors(self):
        response = self.client.post(
            f"/instructor/quizzes/{self.version.quiz_id}/questions/new/",
            {"prompt": "", "question_type": "single", "points": "1",
             "choices-TOTAL_FORMS": "2", "choices-INITIAL_FORMS": "0",
             "choices-MIN_NUM_FORMS": "2", "choices-MAX_NUM_FORMS": "1000",
             "choices-0-text": "a", "choices-0-is_correct": "on",
             "choices-1-text": "b"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_assignment_list_annotates_finalized_totals(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Counted", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        response = self.client.get("/instructor/assignments/")
        row = next(
            a for a in response.context["assignments"] if a.id == assignment.id
        )
        self.assertEqual(row.attempt_total, 1)
        self.assertEqual(row.finalized_total, 1)

    def test_gradebook_without_a_pass_mark(self):
        quiz = Quiz.objects.create(
            organization=self.org, title="NoMark", created_by=self.instructor,
            pass_mark_percent=0,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="p", question_type="single", points=1
        )
        Choice.objects.create(question=question, text="a", is_correct=True)
        Choice.objects.create(question=question, text="b", is_correct=False)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="NoMarkAssign", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        response = self.client.get(f"/instructor/assignments/{assignment.id}/gradebook/")
        self.assertEqual(response.status_code, 200)

    def test_org_admin_can_use_the_instructor_workspace(self):
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get("/instructor/quizzes/").status_code, 200)

    def test_user_without_an_organization_is_denied(self):
        orphan = User.objects.create_user("orphan", "orphan@t.dev", "pass-phrase-12")
        client = Client()
        client.force_login(orphan)
        for url in ("/instructor/quizzes/", "/manage/people/"):
            self.assertEqual(client.get(url).status_code, 403, url)


class ExportBranchTests(BranchFixture):
    def test_csv_escapes_every_dangerous_prefix(self):
        from assessments.exports import _csv_safe

        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            self.assertTrue(_csv_safe(f"{prefix}danger").startswith("'"))
        self.assertEqual(_csv_safe("safe"), "safe")
        self.assertEqual(_csv_safe(None), "")
        self.assertEqual(_csv_safe(42), "42")

    def test_item_analysis_csv_with_no_attempts(self):
        from assessments.exports import item_analysis_csv

        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Empty", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        text = item_analysis_csv(assignment)
        self.assertIn("question,responses", text)

    def test_export_requires_instructor_rights(self):
        from assessments.exports import record_export

        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Guarded", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        with self.assertRaises(PermissionDenied):
            record_export(assignment, self.student, "x.csv", "payload")


class AnalyticsBranchTests(BranchFixture):
    def test_median_of_an_even_number_of_scores(self):
        from decimal import Decimal

        from assessments.analytics import _median

        self.assertEqual(_median([Decimal("10"), Decimal("20")]), Decimal("15"))
        self.assertEqual(_median([]), Decimal("0"))
        self.assertEqual(_median([Decimal("7")]), Decimal("7"))

    def test_distribution_buckets_every_band(self):
        from decimal import Decimal

        from assessments.analytics import _distribution

        result = dict(
            _distribution([Decimal(v) for v in (5, 25, 45, 65, 85, 100)])
        )
        self.assertEqual(result["0-19"], 1)
        self.assertEqual(result["20-39"], 1)
        self.assertEqual(result["40-59"], 1)
        self.assertEqual(result["60-79"], 1)
        self.assertEqual(result["80-100"], 2)

    def test_difficulty_labels_cover_the_range(self):
        from decimal import Decimal

        from assessments.analytics import ItemStat

        cases = {
            "0.95": "Very easy", "0.75": "Easy", "0.50": "Moderate",
            "0.30": "Hard", "0.10": "Very hard",
        }
        for value, label in cases.items():
            stat = ItemStat(
                prompt="p", responses=1, correct=1,
                difficulty=Decimal(value), discrimination=Decimal("0"),
            )
            self.assertEqual(stat.difficulty_label, label)

    def test_item_statistics_returns_empty_without_attempts(self):
        from assessments.analytics import item_statistics

        assignment = QuizAssignment.objects.create(
            organization=self.org, title="None", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        self.assertEqual(item_statistics(assignment), [])
