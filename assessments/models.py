"""Assessment domain models: quizzes, attempts, pools, and audit evidence."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.models import Organization


class TenantScopedQuerySet(models.QuerySet):
    def for_user(self, user):
        """Deny-by-default tenant scope.

        Cross-tenant visibility is granted only by the explicit
        ``is_platform_operator`` flag -- never by ``is_staff``, which merely
        grants admin-site access and is routinely held by a single tenant's
        operators (finding 1).
        """
        if not user.is_authenticated:
            return self.none()
        if getattr(user, "is_platform_operator", False):
            return self
        org_ids = user.memberships.filter(is_active=True).values("organization_id")
        return self.filter(organization_id__in=org_ids)

    def for_organization(self, organization):
        return self.filter(organization=organization)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    pass


class OrganizationScopedModel(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    objects = TenantScopedManager()

    class Meta:
        abstract = True


class Program(OrganizationScopedModel):
    name = models.CharField(max_length=160)

    def __str__(self) -> str:
        return self.name


class Course(OrganizationScopedModel):
    program = models.ForeignKey(
        Program, on_delete=models.SET_NULL, null=True, blank=True, related_name="courses"
    )
    name = models.CharField(max_length=160)
    instructors = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="instructed_courses"
    )
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class Cohort(OrganizationScopedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="cohorts")
    name = models.CharField(max_length=160)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.course}: {self.name}"


class Enrollment(OrganizationScopedModel):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrollments"
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="enrollments")
    cohort = models.ForeignKey(
        Cohort, on_delete=models.SET_NULL, null=True, blank=True, related_name="enrollments"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("student", "course", "cohort")]


class TimingAccommodation(OrganizationScopedModel):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="timing_accommodations"
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, blank=True)
    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, null=True, blank=True)
    multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    extra_minutes = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=240, blank=True)
    # An accommodation that never lapses is an audit problem of its own (finding 20).
    is_active = models.BooleanField(default=True)
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["organization", "student", "is_active"],
                name="accommodation_lookup_idx",
            )
        ]

    def applies_at(self, moment) -> bool:
        if not self.is_active:
            return False
        if self.effective_from and moment < self.effective_from:
            return False
        if self.effective_until and moment > self.effective_until:
            return False
        return True

    def accommodated_minutes(self, base_minutes: int) -> Decimal:
        """Total minutes this accommodation yields for ``base_minutes`` of quiz time."""
        return Decimal(base_minutes) * self.multiplier + Decimal(self.extra_minutes)


class QuestionTag(OrganizationScopedModel):
    name = models.CharField(max_length=80)

    class Meta:
        unique_together = [("organization", "name")]

    def __str__(self) -> str:
        return self.name


class LearningObjective(OrganizationScopedModel):
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.title


class QuestionBank(OrganizationScopedModel):
    name = models.CharField(max_length=160)
    is_shared = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    def __str__(self) -> str:
        return self.name


class QuestionBankItem(OrganizationScopedModel):
    class QuestionType(models.TextChoices):
        SINGLE = "single", "Single correct"
        MULTI = "multi", "Multi select"
        TRUE_FALSE = "true_false", "True/false"

    class Difficulty(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"
        EXPERT = "expert", "Expert"

    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="items")
    prompt = models.TextField()
    question_type = models.CharField(max_length=16, choices=QuestionType.choices)
    difficulty = models.CharField(
        max_length=16, choices=Difficulty.choices, default=Difficulty.EASY
    )
    points = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    explanation = models.TextField(blank=True)
    tags = models.ManyToManyField(QuestionTag, blank=True)
    learning_objective = models.ForeignKey(
        LearningObjective, on_delete=models.SET_NULL, null=True, blank=True
    )
    source_type = models.CharField(max_length=80, default="original")
    source_name = models.CharField(max_length=180, blank=True)
    source_url = models.URLField(blank=True)
    license_type = models.CharField(max_length=80, default="internal")
    reuse_allowed = models.BooleanField(default=True)
    attribution_required = models.BooleanField(default=False)
    review_status = models.CharField(max_length=32, default="approved")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_bank_items",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_bank_items"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.prompt[:80]


class QuestionChoice(models.Model):
    item = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class ItemPool(OrganizationScopedModel):
    name = models.CharField(max_length=160)
    items = models.ManyToManyField(QuestionBankItem, through="ItemPoolItem", related_name="pools")
    required_question_count = models.PositiveIntegerField(default=5)
    # {"easy": 2, "hard": 1} -- how many items to draw per difficulty stratum.
    # Empty means "draw the whole form from one stratum" (finding 15).
    blueprint = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if not self.blueprint:
            return
        valid = {choice for choice, _ in QuestionBankItem.Difficulty.choices}
        for key, count in self.blueprint.items():
            if key not in valid:
                raise ValidationError({"blueprint": f"Unknown difficulty {key!r}."})
            if not isinstance(count, int) or count < 0:
                raise ValidationError(
                    {"blueprint": f"Count for {key!r} must be a non-negative integer."}
                )
        if sum(self.blueprint.values()) != self.required_question_count:
            raise ValidationError(
                {"blueprint": "Blueprint counts must sum to required_question_count."}
            )


class ItemPoolItem(models.Model):
    pool = models.ForeignKey(ItemPool, on_delete=models.CASCADE)
    item = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE)
    weight = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = [("pool", "item")]


class Quiz(OrganizationScopedModel):
    class FeedbackPolicy(models.TextChoices):
        IMMEDIATE = "immediate", "Immediate per question"
        AT_END = "at_end", "At end of attempt"
        MANUAL = "manual", "Manual release"

    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)
    attempt_limit = models.PositiveIntegerField(default=1)
    shuffle_questions = models.BooleanField(default=True)
    shuffle_choices = models.BooleanField(default=True)
    feedback_policy = models.CharField(
        max_length=16, choices=FeedbackPolicy.choices, default=FeedbackPolicy.AT_END
    )
    pass_mark_percent = models.PositiveIntegerField(default=70)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title


class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    bank_item = models.ForeignKey(
        QuestionBankItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    prompt = models.TextField()
    question_type = models.CharField(max_length=16, choices=QuestionBankItem.QuestionType.choices)
    points = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    explanation = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class QuizVersion(OrganizationScopedModel):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)
    attempt_limit = models.PositiveIntegerField(default=1)
    shuffle_questions = models.BooleanField(default=True)
    shuffle_choices = models.BooleanField(default=True)
    feedback_policy = models.CharField(max_length=16, choices=Quiz.FeedbackPolicy.choices)
    pass_mark_percent = models.PositiveIntegerField(default=70)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("quiz", "version_number")]
        ordering = ["-published_at"]

    def __str__(self) -> str:
        return f"{self.title} v{self.version_number}"


class VersionQuestion(models.Model):
    quiz_version = models.ForeignKey(
        QuizVersion, on_delete=models.CASCADE, related_name="version_questions"
    )
    source_item = models.ForeignKey(
        QuestionBankItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    prompt = models.TextField()
    question_type = models.CharField(max_length=16, choices=QuestionBankItem.QuestionType.choices)
    points = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    explanation = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class VersionChoice(models.Model):
    version_question = models.ForeignKey(
        VersionQuestion, on_delete=models.CASCADE, related_name="version_choices"
    )
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class QuizAssignment(OrganizationScopedModel):
    class FormMode(models.TextChoices):
        FIXED = "fixed", "Fixed form"
        SAMPLED = "sampled", "Sampled form"

    title = models.CharField(max_length=180)
    form_mode = models.CharField(max_length=16, choices=FormMode.choices, default=FormMode.FIXED)
    quiz_version = models.ForeignKey(
        QuizVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="assignments"
    )
    item_pool = models.ForeignKey(
        ItemPool, on_delete=models.PROTECT, null=True, blank=True, related_name="assignments"
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, blank=True)
    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, null=True, blank=True)
    explicit_students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="explicit_assignments"
    )
    opens_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    allow_late = models.BooleanField(default=True)
    # Blank inherits from the quiz version; 0 means unlimited (practice archives).
    attempt_limit = models.PositiveIntegerField(null=True, blank=True, default=1)
    time_limit_minutes = models.PositiveIntegerField(null=True, blank=True)
    grace_seconds = models.PositiveIntegerField(default=30)
    # Resolved on the assignment so sampled forms -- which have no quiz_version --
    # are governed by a policy too (finding 17). Blank defers to the version.
    feedback_policy = models.CharField(
        max_length=16, choices=Quiz.FeedbackPolicy.choices, blank=True
    )
    # Optional override so sampled forms (no quiz_version) can still have a pass mark.
    pass_mark_percent = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        indexes = [
            models.Index(
                fields=["organization", "is_active", "opens_at"],
                name="assignment_visible_idx",
            )
        ]

    def __str__(self) -> str:
        return self.title

    def clean(self):
        """Reject invalid or cross-tenant wiring at authoring time (finding 6).

        Without this the error surfaces to a student mid-start as a 500 or a
        confusing message, long after the mistake was made.
        """
        errors = {}
        if self.form_mode == self.FormMode.FIXED and not self.quiz_version_id:
            errors["quiz_version"] = "Fixed-form assignments require a published quiz version."
        if self.form_mode == self.FormMode.SAMPLED and not self.item_pool_id:
            errors["item_pool"] = "Sampled-form assignments require an item pool."
        if self.form_mode == self.FormMode.FIXED and self.item_pool_id:
            errors["item_pool"] = "Fixed-form assignments must not reference an item pool."
        if self.form_mode == self.FormMode.SAMPLED and self.quiz_version_id:
            errors["quiz_version"] = "Sampled-form assignments must not reference a quiz version."
        if self.closes_at and self.opens_at and self.closes_at <= self.opens_at:
            errors["closes_at"] = "The close time must be after the open time."
        if self.due_at and self.closes_at and self.due_at > self.closes_at:
            errors["due_at"] = "The due date cannot fall after the close time."
        if self.cohort_id and self.course_id and self.cohort.course_id != self.course_id:
            errors["cohort"] = "The cohort does not belong to the selected course."
        if self.organization_id:
            for name in ("quiz_version", "item_pool", "course", "cohort"):
                related = getattr(self, name, None)
                if related is not None and related.organization_id != self.organization_id:
                    errors[name] = "Cross-organization reference is not permitted."
        if self.pass_mark_percent is not None and self.pass_mark_percent > 100:
            errors["pass_mark_percent"] = "Threshold must be between 0 and 100."  # noqa: S105
        # Immediate feedback on a fixed form with retakes left discloses the key
        # before the next attempt (audit F03).
        policy = self.feedback_policy
        if not policy and self.quiz_version_id:
            policy = self.quiz_version.feedback_policy
        limit = self.attempt_limit
        if limit is None and self.quiz_version_id:
            limit = self.quiz_version.attempt_limit
        if (
            self.form_mode == self.FormMode.FIXED
            and policy == Quiz.FeedbackPolicy.IMMEDIATE
            and (limit is None or limit == 0 or limit > 1)
        ):
            errors["feedback_policy"] = (
                "Immediate feedback cannot be used on a fixed form when retakes "
                "remain available. Use attempt limit 1, or choose at-end / manual."
            )
        if errors:
            raise ValidationError(errors)


class Attempt(OrganizationScopedModel):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"
        EXPIRED = "expired", "Expired"
        FINALIZED = "finalized", "Finalized"
        VOIDED_BY_ADMIN = "voided_by_admin", "Voided by admin"

    #: Statuses that still accept answer saves.
    OPEN_STATUSES = ("in_progress",)
    #: Statuses that carry a score.
    SCORED_STATUSES = ("submitted", "expired", "finalized")

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attempts"
    )
    assignment = models.ForeignKey(
        QuizAssignment, on_delete=models.PROTECT, related_name="attempts"
    )
    quiz_version = models.ForeignKey(
        QuizVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="attempts"
    )
    # Set only when the attempt *is* the daily challenge. Inferring this from a
    # shared quiz_version wrongly credits ordinary assignment attempts.
    daily_challenge = models.ForeignKey(
        "DailyChallenge",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField(auto_now_add=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalization_reason = models.CharField(max_length=32, blank=True)
    score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    max_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    applied_time_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    applied_extra_minutes = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=0)
    sampled_seed = models.CharField(max_length=64, blank=True)
    sampling_criteria = models.JSONField(default=dict, blank=True)
    is_late = models.BooleanField(default=False)
    # Durable once the student was shown the answer key or IMMEDIATE feedback
    # (audit R4-01). Survives void + feedback_policy edits.
    answer_key_disclosed = models.BooleanField(default=False)

    class Meta:
        unique_together = [("student", "assignment", "attempt_number")]
        ordering = ["-started_at"]
        indexes = [
            # The expiry sweep filters on exactly this pair (finding 32).
            models.Index(fields=["status", "deadline_at"], name="attempt_sweep_idx"),
            models.Index(
                fields=["organization", "student", "-started_at"], name="attempt_student_idx"
            ),
        ]

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.IN_PROGRESS

    @property
    def score_percent(self) -> Decimal:
        """Percent of this attempt's own max, so sampled forms compare fairly."""
        if not self.max_score:
            return Decimal("0")
        return (self.score / self.max_score * 100).quantize(Decimal("0.1"))


class AttemptItemSnapshot(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="item_snapshots")
    version_question = models.ForeignKey(
        VersionQuestion, on_delete=models.SET_NULL, null=True, blank=True
    )
    prompt = models.TextField()
    question_type = models.CharField(max_length=16, choices=QuestionBankItem.QuestionType.choices)
    points = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    explanation = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class AttemptChoiceSnapshot(models.Model):
    item_snapshot = models.ForeignKey(
        AttemptItemSnapshot, on_delete=models.CASCADE, related_name="choice_snapshots"
    )
    version_choice = models.ForeignKey(
        VersionChoice, on_delete=models.SET_NULL, null=True, blank=True
    )
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class Response(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="responses")
    item_snapshot = models.ForeignKey(AttemptItemSnapshot, on_delete=models.CASCADE)
    selected_choices = models.ManyToManyField(AttemptChoiceSnapshot, blank=True)
    is_locked = models.BooleanField(default=False)
    saved_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("attempt", "item_snapshot")]


class ResponseEvent(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="response_events")
    item_snapshot = models.ForeignKey(AttemptItemSnapshot, on_delete=models.CASCADE)
    selected_choice_ids = models.JSONField(default=list)
    revision_before = models.PositiveIntegerField()
    revision_after = models.PositiveIntegerField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class AttemptFinalization(models.Model):
    attempt = models.OneToOneField(
        Attempt, on_delete=models.CASCADE, related_name="finalization"
    )
    reason = models.CharField(max_length=32)
    score = models.DecimalField(max_digits=8, decimal_places=2)
    max_score = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)


class AttemptAudit(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="audits")
    event_type = models.CharField(max_length=80)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class DailyChallenge(OrganizationScopedModel):
    quiz_version = models.ForeignKey(QuizVersion, on_delete=models.PROTECT)
    release_date = models.DateField()
    title = models.CharField(max_length=180)
    is_archived = models.BooleanField(default=False)
    # Owned one-to-one so two students starting at once cannot race
    # get_or_create into two parallel assignments.
    assignment = models.OneToOneField(
        "QuizAssignment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_challenge_for",
    )

    class Meta:
        unique_together = [("organization", "release_date")]
        ordering = ["-release_date"]


class DailyParticipation(OrganizationScopedModel):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    challenge = models.ForeignKey(DailyChallenge, on_delete=models.PROTECT)
    attempt = models.OneToOneField(Attempt, on_delete=models.PROTECT)
    credit_date = models.DateField()
    earned_credit = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("student", "challenge")]
        indexes = [
            models.Index(
                fields=["organization", "student", "-credit_date"],
                name="participation_streak_idx",
            )
        ]


class StreakCache(OrganizationScopedModel):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    current_streak = models.PositiveIntegerField(default=0)
    longest_streak = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "student")]


class ExportArtifact(OrganizationScopedModel):
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    file_path = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()
    downloaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Notification(OrganizationScopedModel):
    class Kind(models.TextChoices):
        DUE_SOON = "due_soon", "Assignment due soon"
        DAILY_LIVE = "daily_live", "Daily challenge is live"
        STREAK_AT_RISK = "streak_at_risk", "Streak at risk"
        RESULT_RELEASED = "result_released", "Results released"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.DUE_SOON)
    title = models.CharField(max_length=180)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    # Idempotency key, so a re-run of a notification job cannot double-send.
    dedupe_key = models.CharField(max_length=200, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="notification_dedupe_per_user",
            )
        ]

    def __str__(self) -> str:
        return self.title


class ResultRelease(OrganizationScopedModel):
    """Server-enforced manual result visibility (finding 3, spec 3.5/4.5)."""

    assignment = models.OneToOneField(
        QuizAssignment, on_delete=models.CASCADE, related_name="result_release"
    )
    is_open = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opened_result_releases",
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.assignment}: {'open' if self.is_open else 'withheld'}"


class IntegrityEvent(OrganizationScopedModel):
    """Assessment integrity diagnostics (spec 3.6). Append-only."""

    class Kind(models.TextChoices):
        LATE_SAVE_REJECTED = "late_save_rejected", "Late save rejected"
        REVISION_CONFLICT = "revision_conflict", "Revision conflict"
        DUPLICATE_FINALIZE = "duplicate_finalize", "Duplicate finalize prevented"
        SWEEP_FINALIZED = "sweep_finalized", "Expiry sweep finalized"
        EXPIRED_FINALIZED = "expired_finalized", "Attempt expired (interactive)"
        LOCKED_RESPONSE_WRITE = "locked_response_write", "Write to locked response"
        ATTEMPT_VOIDED = "attempt_voided", "Attempt voided by admin"
        POOL_UNDERSIZED = "pool_undersized", "Item pool too small for form"

    attempt = models.ForeignKey(
        Attempt, on_delete=models.CASCADE, related_name="integrity_events", null=True, blank=True
    )
    kind = models.CharField(max_length=40, choices=Kind.choices)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "kind", "-created_at"], name="integrity_scan_idx")
        ]


class JobFailure(models.Model):
    """Dead-letter record for a background job that exhausted its retries.

    Not organization-scoped: a failure is an operational fact about the
    platform, and the task arguments that would identify a tenant are
    deliberately not stored here.
    """

    task_name = models.CharField(max_length=200)
    task_id = models.CharField(max_length=100, blank=True)
    error = models.CharField(max_length=500)
    traceback = models.TextField(blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["acknowledged_at", "-created_at"], name="jobfailure_open_idx")
        ]

    def __str__(self) -> str:
        return f"{self.task_name}: {self.error[:60]}"


class AdminActionLog(OrganizationScopedModel):
    """Audit trail for sensitive administrative actions (spec 3.5/12)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_actions",
    )
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
