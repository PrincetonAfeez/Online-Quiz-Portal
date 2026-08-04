"""Form validation corners — QuizForm, ItemPoolForm, formsets, AssignmentForm."""

from django.test import TestCase

from accounts.models import Organization, OrganizationMembership, User
from assessments.forms import (
    AssignmentForm,
    ItemPoolForm,
    ItemPoolItemFormSet,
    QuizForm,
)
from assessments.models import (
    ItemPool,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
)


class FormCornerFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", slug="org")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )

    def quiz_data(self, **overrides):
        data = {
            "title": "Corner Quiz",
            "description": "",
            "time_limit_minutes": "",
            "attempt_limit": "1",
            "shuffle_questions": "on",
            "shuffle_choices": "on",
            "feedback_policy": Quiz.FeedbackPolicy.AT_END,
            "pass_mark_percent": "70",
        }
        data.update(overrides)
        return data


class OrganizationScopedFormTests(FormCornerFixture):
    def test_missing_organization_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            QuizForm()
        self.assertIn("organization is required", str(ctx.exception))


class QuizFormValidationTests(FormCornerFixture):
    def test_pass_mark_over_100_is_rejected(self):
        form = QuizForm(
            data=self.quiz_data(pass_mark_percent="101"),
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pass_mark_percent", form.errors)
        self.assertIn("100%", form.errors["pass_mark_percent"][0])

    def test_pass_mark_at_100_is_allowed(self):
        form = QuizForm(
            data=self.quiz_data(pass_mark_percent="100"),
            organization=self.org,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_immediate_feedback_rejects_multiple_attempts(self):
        form = QuizForm(
            data=self.quiz_data(
                feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
                attempt_limit="2",
            ),
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("feedback_policy", form.errors)
        self.assertIn("retakes", form.errors["feedback_policy"][0].lower())

    def test_immediate_feedback_rejects_unlimited_attempts(self):
        form = QuizForm(
            data=self.quiz_data(
                feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
                attempt_limit="0",
            ),
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("feedback_policy", form.errors)

    def test_immediate_feedback_rejects_blank_attempt_limit(self):
        form = QuizForm(
            data=self.quiz_data(
                feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
                attempt_limit="",
            ),
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("feedback_policy", form.errors)

    def test_immediate_feedback_allows_single_attempt(self):
        form = QuizForm(
            data=self.quiz_data(
                feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
                attempt_limit="1",
            ),
            organization=self.org,
        )
        self.assertTrue(form.is_valid(), form.errors)


class ItemPoolFormBlueprintTests(FormCornerFixture):
    def test_empty_blueprint_becomes_empty_dict(self):
        form = ItemPoolForm(
            data={
                "name": "Pool",
                "required_question_count": "3",
                "blueprint": "",
            },
            organization=self.org,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["blueprint"], {})

    def test_null_blueprint_value_becomes_empty_dict(self):
        form = ItemPoolForm(
            data={"name": "Pool", "required_question_count": "3", "blueprint": ""},
            organization=self.org,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["blueprint"], {})

    def test_invalid_json_is_rejected(self):
        form = ItemPoolForm(
            data={
                "name": "Pool",
                "required_question_count": "3",
                "blueprint": "{not json",
            },
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("blueprint", form.errors)
        self.assertIn("valid JSON", form.errors["blueprint"][0])

    def test_non_object_json_is_rejected(self):
        form = ItemPoolForm(
            data={
                "name": "Pool",
                "required_question_count": "3",
                "blueprint": "[1, 2, 3]",
            },
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("blueprint", form.errors)
        self.assertIn("JSON object", form.errors["blueprint"][0])

    def test_valid_json_object_is_parsed(self):
        form = ItemPoolForm(
            data={
                "name": "Pool",
                "required_question_count": "5",
                "blueprint": '{"easy": 2, "hard": 3}',
            },
            organization=self.org,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["blueprint"], {"easy": 2, "hard": 3})


class ItemPoolItemFormSetTests(FormCornerFixture):
    def setUp(self):
        super().setUp()
        self.bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )

    def _bank_item(self, prompt, difficulty):
        item = QuestionBankItem.objects.create(
            organization=self.org,
            bank=self.bank,
            prompt=prompt,
            question_type=QuestionBankItem.QuestionType.SINGLE,
            difficulty=difficulty,
            points=1,
            review_status="approved",
            created_by=self.instructor,
        )
        QuestionChoice.objects.create(item=item, text="a", is_correct=True, sort_order=0)
        QuestionChoice.objects.create(item=item, text="b", is_correct=False, sort_order=1)
        return item

    def _formset_data(self, pool, items, *, required=3, blueprint=""):
        prefix = "itempoolitem_set"
        data = {
            f"{prefix}-TOTAL_FORMS": str(len(items)),
            f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
            "required_question_count": str(required),
            "blueprint": blueprint,
        }
        for index, item in enumerate(items):
            data[f"{prefix}-{index}-item"] = str(item.id)
            data[f"{prefix}-{index}-weight"] = "1"
        return data

    def test_blueprint_strata_short_is_rejected(self):
        easy = self._bank_item("Easy Q", QuestionBankItem.Difficulty.EASY)
        pool = ItemPool.objects.create(
            organization=self.org,
            name="Strata Pool",
            required_question_count=3,
            blueprint={"easy": 2, "hard": 1},
            created_by=self.instructor,
        )
        formset = ItemPoolItemFormSet(
            data=self._formset_data(
                pool,
                [easy],
                required=3,
                blueprint='{"easy": 2, "hard": 1}',
            ),
            instance=pool,
            organization=self.org,
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("Blueprint strata are short", str(formset.non_form_errors()))

    def test_blueprint_strata_satisfied_passes(self):
        easy1 = self._bank_item("Easy 1", QuestionBankItem.Difficulty.EASY)
        easy2 = self._bank_item("Easy 2", QuestionBankItem.Difficulty.EASY)
        hard = self._bank_item("Hard 1", QuestionBankItem.Difficulty.HARD)
        pool = ItemPool.objects.create(
            organization=self.org,
            name="Full Pool",
            required_question_count=3,
            blueprint={"easy": 2, "hard": 1},
            created_by=self.instructor,
        )
        formset = ItemPoolItemFormSet(
            data=self._formset_data(
                pool,
                [easy1, easy2, hard],
                required=3,
                blueprint='{"easy": 2, "hard": 1}',
            ),
            instance=pool,
            organization=self.org,
        )
        self.assertTrue(formset.is_valid(), formset.errors)

    def test_min_item_count_without_blueprint(self):
        easy = self._bank_item("Only one", QuestionBankItem.Difficulty.EASY)
        pool = ItemPool.objects.create(
            organization=self.org,
            name="Sparse Pool",
            required_question_count=3,
            blueprint={},
            created_by=self.instructor,
        )
        formset = ItemPoolItemFormSet(
            data=self._formset_data(pool, [easy], required=3, blueprint=""),
            instance=pool,
            organization=self.org,
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("Add at least 3 items", str(formset.non_form_errors()))

    def test_blueprint_from_invalid_json_falls_back_to_instance(self):
        """_blueprint_from_data ignores bad JSON and uses instance blueprint."""
        easy1 = self._bank_item("E1", QuestionBankItem.Difficulty.EASY)
        easy2 = self._bank_item("E2", QuestionBankItem.Difficulty.EASY)
        pool = ItemPool.objects.create(
            organization=self.org,
            name="Fallback Pool",
            required_question_count=2,
            blueprint={"easy": 2},
            created_by=self.instructor,
        )
        formset = ItemPoolItemFormSet(
            data=self._formset_data(
                pool,
                [easy1, easy2],
                required=2,
                blueprint="not-json",
            ),
            instance=pool,
            organization=self.org,
        )
        self.assertTrue(formset.is_valid(), formset.errors)


class AssignmentFormPassMarkTests(FormCornerFixture):
    def test_pass_mark_over_100_is_rejected(self):
        form = AssignmentForm(
            data={"title": "A", "pass_mark_percent": "150"},
            organization=self.org,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pass_mark_percent", form.errors)

    def test_blank_pass_mark_is_allowed(self):
        form = AssignmentForm(
            data={"title": "A", "pass_mark_percent": ""},
            organization=self.org,
        )
        # Other required assignment fields may fail; pass_mark alone must not.
        if form.is_valid():
            self.assertIsNone(form.cleaned_data.get("pass_mark_percent"))
        else:
            self.assertNotIn("pass_mark_percent", form.errors)
