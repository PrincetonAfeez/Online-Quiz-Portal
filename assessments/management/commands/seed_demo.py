"""Seed a demo organization with sample quizzes and users."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Choice,
    Cohort,
    Course,
    DailyChallenge,
    Enrollment,
    ItemPool,
    ItemPoolItem,
    Program,
    Question,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
    QuizAssignment,
    TimingAccommodation,
)
from assessments.services import publish_quiz, today_for_org


class Command(BaseCommand):
    help = "Seed a complete QuizOps demo organization, users, assignments, and questions."

    def handle(self, *args, **options):
        # Long enough to satisfy the configured MinimumLengthValidator, so the
        # documented demo password is one a user could actually set.
        password = "quizops-demo-2026"  # noqa: S105 - documented demo credential
        org, _ = Organization.objects.update_or_create(
            slug="demo-academy",
            defaults={"name": "Demo Academy", "timezone": "America/Los_Angeles"},
        )

        student = self._user("student", "student@example.com", "Sam", "Learner", password)
        instructor = self._user(
            "instructor", "instructor@example.com", "Iris", "Instructor", password, is_staff=False
        )
        admin = self._user("admin", "admin@example.com", "Pat", "Admin", password, is_staff=True)
        admin.is_superuser = True
        # Superuser already implies full access; the flag makes the deliberate
        # cross-tenant grant explicit for support tooling and tests.
        admin.is_platform_operator = True
        admin.save(update_fields=["is_superuser", "is_platform_operator"])

        OrganizationMembership.objects.update_or_create(
            user=student,
            organization=org,
            role=OrganizationMembership.Role.STUDENT,
            defaults={"is_active": True},
        )
        OrganizationMembership.objects.update_or_create(
            user=instructor,
            organization=org,
            role=OrganizationMembership.Role.INSTRUCTOR,
            defaults={"is_active": True},
        )
        OrganizationMembership.objects.update_or_create(
            user=admin,
            organization=org,
            role=OrganizationMembership.Role.ORG_ADMIN,
            defaults={"is_active": True},
        )

        program, _ = Program.objects.update_or_create(
            organization=org, name="Python Bootcamp"
        )
        course, _ = Course.objects.update_or_create(
            organization=org,
            name="Assessment Engineering 101",
            defaults={"program": program, "is_active": True},
        )
        course.instructors.set([instructor])
        cohort, _ = Cohort.objects.update_or_create(
            organization=org, course=course, name="July 2026 Cohort"
        )
        Enrollment.objects.update_or_create(
            organization=org,
            student=student,
            course=course,
            cohort=cohort,
            defaults={"is_active": True},
        )
        TimingAccommodation.objects.update_or_create(
            organization=org,
            student=student,
            course=course,
            defaults={"multiplier": "1.50", "extra_minutes": 2, "reason": "Demo extended time"},
        )

        bank, _ = QuestionBank.objects.update_or_create(
            organization=org,
            name="Python Fundamentals",
            defaults={"created_by": instructor, "is_shared": True},
        )
        bank_items = [
            self._bank_item(
                org,
                bank,
                instructor,
                "Which data structure preserves insertion order and maps keys to values in modern Python?",
                QuestionBankItem.QuestionType.SINGLE,
                ["list", "dict", "set", "tuple"],
                [1],
                "Dictionaries preserve insertion order in modern Python and map keys to values.",
            ),
            self._bank_item(
                org,
                bank,
                instructor,
                "Select every immutable built-in collection type.",
                QuestionBankItem.QuestionType.MULTI,
                ["list", "tuple", "frozenset", "dict"],
                [1, 2],
                "Tuples and frozensets are immutable collection types.",
            ),
            self._bank_item(
                org,
                bank,
                instructor,
                "True or false: Django templates should receive answer-key correctness during in-progress attempts.",
                QuestionBankItem.QuestionType.TRUE_FALSE,
                ["True", "False"],
                [1],
                "Correctness is excluded from the student read model until results are released.",
            ),
            self._bank_item(
                org,
                bank,
                instructor,
                "What is the purpose of a database transaction around attempt finalization?",
                QuestionBankItem.QuestionType.SINGLE,
                ["Make CSS faster", "Avoid duplicate or inconsistent scoring", "Disable CSRF", "Change the client clock"],
                [1],
                "The transaction keeps scoring, audit, and state changes consistent and retry-safe.",
            ),
            self._bank_item(
                org,
                bank,
                instructor,
                "Which events should be server-authoritative in a timed assessment?",
                QuestionBankItem.QuestionType.MULTI,
                ["Deadline calculation", "Scoring", "Attempt limit checks", "Browser clock display"],
                [0, 1, 2],
                "The browser can display time, but the server owns integrity-sensitive facts.",
            ),
        ]

        quiz = self._quiz_from_items(org, instructor, bank_items[:3])
        version = publish_quiz(quiz, instructor)

        now = timezone.now()
        QuizAssignment.objects.update_or_create(
            organization=org,
            title="Python Integrity Check",
            defaults={
                "form_mode": QuizAssignment.FormMode.FIXED,
                "quiz_version": version,
                "item_pool": None,
                "course": course,
                "cohort": cohort,
                "opens_at": now - timedelta(days=1),
                "due_at": now + timedelta(days=2),
                "closes_at": now + timedelta(days=7),
                "attempt_limit": 2,
                "time_limit_minutes": 12,
                "created_by": instructor,
            },
        )

        pool, _ = ItemPool.objects.update_or_create(
            organization=org,
            name="Assessment Integrity Pool",
            defaults={"required_question_count": 3, "created_by": instructor},
        )
        for item in bank_items:
            ItemPoolItem.objects.get_or_create(pool=pool, item=item)
        QuizAssignment.objects.update_or_create(
            organization=org,
            title="Sampled Integrity Drill",
            defaults={
                "form_mode": QuizAssignment.FormMode.SAMPLED,
                "quiz_version": None,
                "item_pool": pool,
                "course": course,
                "cohort": cohort,
                "opens_at": now - timedelta(days=1),
                "due_at": now + timedelta(days=3),
                "closes_at": now + timedelta(days=10),
                "attempt_limit": 2,
                "time_limit_minutes": 10,
                "created_by": instructor,
            },
        )
        # The lookup uses the organization's local date, so the seed must too --
        # a UTC date creates a challenge that is invisible until tomorrow
        # (finding 19).
        DailyChallenge.objects.update_or_create(
            organization=org,
            release_date=today_for_org(org),
            defaults={"quiz_version": version, "title": "Daily: Server-Owned Timing"},
        )

        self.stdout.write(self.style.SUCCESS("Seeded QuizOps demo."))
        self.stdout.write("Users: student / instructor / admin")
        self.stdout.write(f"Password: {password}")

    def _user(self, username, email, first, last, password, is_staff=False):
        user, _ = User.objects.update_or_create(
            username=username,
            defaults={
                "email": email,
                "first_name": first,
                "last_name": last,
                "is_staff": is_staff,
                "is_active": True,
            },
        )
        user.set_password(password)
        user.save()
        return user

    def _bank_item(self, org, bank, instructor, prompt, question_type, choices, correct_indexes, explanation):
        item, _ = QuestionBankItem.objects.update_or_create(
            organization=org,
            bank=bank,
            prompt=prompt,
            defaults={
                "question_type": question_type,
                "difficulty": QuestionBankItem.Difficulty.EASY,
                "points": 1,
                "explanation": explanation,
                "source_type": "original",
                "source_name": "QuizOps demo seed",
                "review_status": "approved",
                "reviewed_by": instructor,
                "reviewed_at": timezone.now(),
                "created_by": instructor,
                "reuse_allowed": True,
            },
        )
        item.choices.all().delete()
        for index, text in enumerate(choices):
            QuestionChoice.objects.create(
                item=item,
                text=text,
                is_correct=index in correct_indexes,
                sort_order=index,
            )
        return item

    def _quiz_from_items(self, org, instructor, items):
        quiz, _ = Quiz.objects.update_or_create(
            organization=org,
            title="Python Integrity Quiz",
            defaults={
                "description": "A short assessment demonstrating immutable publishing and server scoring.",
                "created_by": instructor,
                "time_limit_minutes": 10,
                "attempt_limit": 2,
                "feedback_policy": Quiz.FeedbackPolicy.AT_END,
                "pass_mark_percent": 70,
            },
        )
        quiz.questions.all().delete()
        for question_index, item in enumerate(items):
            question = Question.objects.create(
                quiz=quiz,
                bank_item=item,
                prompt=item.prompt,
                question_type=item.question_type,
                points=item.points,
                explanation=item.explanation,
                sort_order=question_index,
            )
            for choice in item.choices.all():
                Choice.objects.create(
                    question=question,
                    text=choice.text,
                    is_correct=choice.is_correct,
                    sort_order=choice.sort_order,
                )
        return quiz
