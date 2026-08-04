"""Instructor-facing forms.

Everything here is tenant-scoped at construction: a form is built for one
organization and its choice querysets never reach outside it, so a crafted POST
cannot attach another tenant's course, cohort, version or pool.
"""

from __future__ import annotations

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from accounts.models import OrganizationMembership, User
from assessments.models import (
    Choice,
    Cohort,
    Course,
    ItemPool,
    ItemPoolItem,
    Question,
    QuestionBankItem,
    Quiz,
    QuizAssignment,
    QuizVersion,
)


class OrganizationScopedForm(forms.ModelForm):
    """Base that pins every relation to one organization."""

    scoped_fields: dict[str, type] = {}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization is None:
            raise ValueError("organization is required to build a scoped form")
        self.organization = organization
        for field_name, model in self.scoped_fields.items():
            if field_name in self.fields:
                self.fields[field_name].queryset = model.objects.filter(
                    organization=organization
                )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.organization = self.organization
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class QuizForm(OrganizationScopedForm):
    class Meta:
        model = Quiz
        fields = [
            "title",
            "description",
            "time_limit_minutes",
            "attempt_limit",
            "shuffle_questions",
            "shuffle_choices",
            "feedback_policy",
            "pass_mark_percent",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "time_limit_minutes": "Leave blank for an untimed quiz.",
            "feedback_policy": "Manual release keeps results hidden until you open them.",
            "pass_mark_percent": "Shown on the result page as pass or below-threshold.",
        }

    def clean_pass_mark_percent(self):
        value = self.cleaned_data["pass_mark_percent"]
        if value > 100:
            raise forms.ValidationError("A pass mark cannot exceed 100%.")
        return value

    def clean_time_limit_minutes(self):
        value = self.cleaned_data.get("time_limit_minutes")
        if value is not None and value < 1:
            raise forms.ValidationError("A timed quiz needs at least one minute.")
        return value

    def clean(self):
        cleaned = super().clean()
        policy = cleaned.get("feedback_policy")
        limit = cleaned.get("attempt_limit")
        if (
            policy == Quiz.FeedbackPolicy.IMMEDIATE
            and (limit is None or limit == 0 or limit > 1)
        ):
            self.add_error(
                "feedback_policy",
                "Immediate feedback cannot be used when retakes remain available. "
                "Set attempt limit to 1, or choose at-end / manual release.",
            )
        return cleaned


class QuestionForm(forms.ModelForm):
    """Order is assigned by the view, not typed by the instructor."""

    class Meta:
        model = Question
        fields = ["prompt", "question_type", "points", "explanation"]
        widgets = {
            "prompt": forms.Textarea(attrs={"rows": 3}),
            "explanation": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "explanation": "Shown with results once the answer key is released.",
        }

    def clean_points(self):
        points = self.cleaned_data["points"]
        if points <= 0:
            raise forms.ValidationError("A question must be worth more than zero points.")
        return points


class BaseChoiceFormSet(forms.BaseInlineFormSet):
    """Validates the answer key at authoring time, not at publish time.

    Publishing already enforces these rules, but an instructor discovering them
    only when they hit Publish has to unpick every question at once.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        rows = [
            form.cleaned_data
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if not rows:
            raise forms.ValidationError("A question needs at least two choices.")
        if len(rows) < 2:
            raise forms.ValidationError("A question needs at least two choices.")

        correct = [row for row in rows if row.get("is_correct")]
        question_type = getattr(self.instance, "question_type", None)

        if question_type == QuestionBankItem.QuestionType.TRUE_FALSE and len(rows) != 2:
            raise forms.ValidationError("True/false questions must have exactly two choices.")
        if question_type in (
            QuestionBankItem.QuestionType.SINGLE,
            QuestionBankItem.QuestionType.TRUE_FALSE,
        ) and len(correct) != 1:
            raise forms.ValidationError("Exactly one choice must be marked correct.")
        if question_type == QuestionBankItem.QuestionType.MULTI:
            if len(correct) < 1:
                raise forms.ValidationError("Multi-select needs at least one correct choice.")
            if len(correct) == len(rows):
                raise forms.ValidationError(
                    "Multi-select needs at least one incorrect choice, or every answer is right."
                )

        texts = [(row.get("text") or "").strip().lower() for row in rows]
        if len(set(texts)) != len(texts):
            raise forms.ValidationError("Two choices have the same text.")


# sort_order is omitted deliberately: rendering it made a blank row look filled
# in, so Django demanded text for choices the instructor never touched.
ChoiceFormSet = inlineformset_factory(
    Question,
    Choice,
    fields=["text", "is_correct"],
    formset=BaseChoiceFormSet,
    extra=4,
    min_num=2,
    validate_min=True,
    can_delete=True,
)


class AssignmentForm(OrganizationScopedForm):
    scoped_fields = {
        "quiz_version": QuizVersion,
        "item_pool": ItemPool,
        "course": Course,
        "cohort": Cohort,
    }

    class Meta:
        model = QuizAssignment
        fields = [
            "title",
            "form_mode",
            "quiz_version",
            "item_pool",
            "course",
            "cohort",
            "explicit_students",
            "opens_at",
            "due_at",
            "closes_at",
            "allow_late",
            "attempt_limit",
            "time_limit_minutes",
            "grace_seconds",
            "feedback_policy",
            "pass_mark_percent",
            "is_active",
        ]
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "due_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "explicit_students": forms.SelectMultiple(attrs={"size": 8}),
        }
        help_texts = {
            "form_mode": (
                "Fixed serves one published version; sampled draws a fresh form "
                "per attempt."
            ),
            "explicit_students": (
                "Optional. Students listed here are eligible even without a "
                "course/cohort enrolment."
            ),
            "allow_late": "When off, no attempt may start after the due date.",
            "attempt_limit": "Blank inherits the quiz version. Zero means unlimited.",
            "grace_seconds": "Tolerance absorbing network latency at the deadline.",
            "feedback_policy": "Blank inherits the quiz version's policy.",
            "pass_mark_percent": (
                "Optional. Required for sampled forms if you want a pass threshold; "
                "blank inherits the quiz version when present."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["explicit_students"].queryset = User.objects.filter(
            memberships__organization=self.organization,
            memberships__role=OrganizationMembership.Role.STUDENT,
            memberships__is_active=True,
        ).distinct().order_by("username")
        self.fields["explicit_students"].required = False
        for name in ("opens_at", "due_at", "closes_at"):
            field = self.fields[name]
            # datetime-local inputs reject the seconds/timezone Django emits.
            field.input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
            if self.instance.pk and getattr(self.instance, name):
                local = timezone.localtime(getattr(self.instance, name))
                self.initial[name] = local.strftime("%Y-%m-%dT%H:%M")

    def clean(self):
        cleaned = super().clean()
        # Model.clean() carries the authoritative rules (form mode wiring,
        # cross-tenant references, window ordering); surface them here.
        return cleaned

    def clean_pass_mark_percent(self):
        value = self.cleaned_data.get("pass_mark_percent")
        if value is not None and value > 100:
            raise forms.ValidationError("A pass mark cannot exceed 100%.")
        return value


class ItemPoolForm(OrganizationScopedForm):
    class Meta:
        model = ItemPool
        fields = ["name", "required_question_count", "blueprint"]
        help_texts = {
            "required_question_count": "How many items each sampled attempt draws.",
            "blueprint": (
                'Optional JSON like {"easy": 2, "medium": 2, "hard": 1}. '
                "Counts must sum to required_question_count."
            ),
        }
        widgets = {
            "blueprint": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_blueprint(self):
        value = self.cleaned_data.get("blueprint")
        if value in ("", None):
            return {}
        if isinstance(value, str):
            import json

            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError("Blueprint must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError("Blueprint must be a JSON object.")
        return value


class BaseItemPoolItemFormSet(forms.BaseInlineFormSet):
    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        if self.organization is not None and "item" in form.fields:
            form.fields["item"].queryset = QuestionBankItem.objects.filter(
                organization=self.organization, review_status="approved"
            ).order_by("prompt")
        return form

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        items = [
            form.cleaned_data["item"]
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("item")
        ]
        required = self.instance.required_question_count or 1
        # On create the parent form's value is in POST, not yet on instance.
        raw = None
        blueprint = self.instance.blueprint or {}
        if self.data is not None:
            raw = self.data.get("required_question_count")
            blueprint = self._blueprint_from_data(blueprint)
        if raw not in (None, ""):
            try:
                required = int(raw)
            except (TypeError, ValueError):
                pass
        if blueprint:
            # Match sample_pool_items strata so save fails early (audit R4-04).
            from collections import Counter

            counts = Counter(item.difficulty for item in items)
            missing = []
            for difficulty, need in sorted(blueprint.items()):
                try:
                    need_n = int(need)
                except (TypeError, ValueError):
                    continue
                have = counts.get(difficulty, 0)
                if have < need_n:
                    missing.append(f"{difficulty}: need {need_n}, have {have}")
            if missing:
                raise forms.ValidationError(
                    "Blueprint strata are short: " + "; ".join(missing) + "."
                )
            return
        if len(items) < required:
            raise forms.ValidationError(
                f"Add at least {required} items so the pool can satisfy "
                f"required_question_count."
            )

    def _blueprint_from_data(self, fallback):
        raw = self.data.get("blueprint")
        if raw in (None, ""):
            return fallback or {}
        if isinstance(raw, dict):
            return raw
        import json

        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return fallback or {}
        return value if isinstance(value, dict) else (fallback or {})


ItemPoolItemFormSet = inlineformset_factory(
    ItemPool,
    ItemPoolItem,
    fields=["item", "weight"],
    formset=BaseItemPoolItemFormSet,
    extra=3,
    can_delete=True,
)


class QuestionBankItemForm(OrganizationScopedForm):
    class Meta:
        model = QuestionBankItem
        fields = [
            "bank",
            "prompt",
            "question_type",
            "difficulty",
            "points",
            "explanation",
            "learning_objective",
            "source_type",
            "source_name",
            "source_url",
            "license_type",
            "reuse_allowed",
            "attribution_required",
            "review_status",
        ]
        widgets = {
            "prompt": forms.Textarea(attrs={"rows": 3}),
            "explanation": forms.Textarea(attrs={"rows": 2}),
        }
