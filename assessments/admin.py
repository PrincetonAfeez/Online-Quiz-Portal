"""Django admin.

Two rules are enforced here, because the admin was the widest hole in the
product's integrity story:

1. Tenant scope applies to rows *and* to every foreign-key widget (finding 6).
2. Assessment evidence -- snapshots, responses, finalizations, audit and
   integrity logs -- is read-only. Corrections go through
   ``services.void_attempt``, which writes an audit trail (finding 5).
"""

from django.contrib import admin
from django.utils import timezone

from accounts.models import OrganizationMembership, User
from assessments import models
from assessments.flags import FeatureFlag, invalidate


class TenantScopedAdmin(admin.ModelAdmin):
    """Restricts both the changelist and the related-object pickers."""

    org_fields: tuple[str, ...] = ()
    org_m2m_fields: tuple[str, ...] = ()
    # ForeignKeys to User that must be limited to members of orgs the staff
    # user can see (Enrollment.student, TimingAccommodation.student, …).
    membership_user_fields: tuple[str, ...] = ()
    list_filter = ["organization"]

    def _unrestricted(self, request) -> bool:
        return getattr(request.user, "is_platform_operator", False)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self._unrestricted(request):
            return qs
        return qs.for_user(request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in self.org_fields and not self._unrestricted(request):
            model = db_field.remote_field.model
            manager = model.objects
            if hasattr(manager, "for_user"):
                kwargs["queryset"] = manager.for_user(request.user)
        if (
            db_field.name in self.membership_user_fields
            and not self._unrestricted(request)
            and db_field.remote_field.model is User
        ):
            org_ids = OrganizationMembership.objects.filter(
                user=request.user, is_active=True
            ).values_list("organization_id", flat=True)
            kwargs["queryset"] = User.objects.filter(
                memberships__organization_id__in=org_ids,
                memberships__is_active=True,
            ).distinct()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name in self.org_m2m_fields and not self._unrestricted(request):
            model = db_field.remote_field.model
            manager = model.objects
            if hasattr(manager, "for_user"):
                kwargs["queryset"] = manager.for_user(request.user)
        if (
            db_field.name in self.membership_user_fields
            and not self._unrestricted(request)
            and db_field.remote_field.model is User
        ):
            org_ids = OrganizationMembership.objects.filter(
                user=request.user, is_active=True
            ).values_list("organization_id", flat=True)
            kwargs["queryset"] = User.objects.filter(
                memberships__organization_id__in=org_ids,
                memberships__is_active=True,
            ).distinct()
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    # No save_model override: ModelForm._post_clean already calls
    # instance.full_clean(), so Model.clean() runs and its errors are attached
    # to the form. Re-running it here raised an unhandled ValidationError (a
    # 500) instead of showing the field error.


class ReadOnlyAdmin(admin.ModelAdmin):
    """Assessment evidence: viewable, never editable, never deletable."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        if hasattr(qs, "for_user"):
            return qs.for_user(request.user)
        return qs


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# --------------------------------------------------------------------------
# Org structure
# --------------------------------------------------------------------------


@admin.register(models.Program)
class ProgramAdmin(TenantScopedAdmin):
    list_display = ["name", "organization"]
    search_fields = ["name"]


@admin.register(models.Course)
class CourseAdmin(TenantScopedAdmin):
    list_display = ["name", "organization", "program", "is_active"]
    search_fields = ["name"]
    filter_horizontal = ["instructors"]
    org_fields = ("program",)
    membership_user_fields = ("instructors",)


@admin.register(models.Cohort)
class CohortAdmin(TenantScopedAdmin):
    list_display = ["name", "course", "organization", "starts_on", "ends_on"]
    org_fields = ("course",)


@admin.register(models.Enrollment)
class EnrollmentAdmin(TenantScopedAdmin):
    list_display = ["student", "course", "cohort", "is_active"]
    list_filter = ["organization", "course", "cohort", "is_active"]
    org_fields = ("course", "cohort")
    membership_user_fields = ("student",)


@admin.register(models.TimingAccommodation)
class TimingAccommodationAdmin(TenantScopedAdmin):
    list_display = [
        "student",
        "organization",
        "course",
        "cohort",
        "multiplier",
        "extra_minutes",
        "is_active",
        "effective_until",
    ]
    list_filter = ["organization", "is_active"]
    org_fields = ("course", "cohort")
    membership_user_fields = ("student",)


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


@admin.register(models.QuestionBank)
class QuestionBankAdmin(TenantScopedAdmin):
    list_display = ["name", "organization", "is_shared", "created_by"]
    search_fields = ["name"]


class QuestionChoiceInline(admin.TabularInline):
    model = models.QuestionChoice
    extra = 0


@admin.register(models.QuestionBankItem)
class QuestionBankItemAdmin(TenantScopedAdmin):
    list_display = ["prompt", "bank", "question_type", "difficulty", "review_status"]
    list_filter = ["organization", "bank", "question_type", "difficulty", "review_status"]
    search_fields = ["prompt"]
    inlines = [QuestionChoiceInline]
    org_fields = ("bank", "learning_objective")
    org_m2m_fields = ("tags",)


@admin.register(models.QuestionTag)
class QuestionTagAdmin(TenantScopedAdmin):
    list_display = ["name", "organization"]


@admin.register(models.LearningObjective)
class LearningObjectiveAdmin(TenantScopedAdmin):
    list_display = ["title", "organization"]


class ItemPoolItemInline(admin.TabularInline):
    model = models.ItemPoolItem
    extra = 0


@admin.register(models.ItemPool)
class ItemPoolAdmin(TenantScopedAdmin):
    list_display = ["name", "organization", "required_question_count", "approved_item_count"]
    inlines = [ItemPoolItemInline]

    @admin.display(description="Approved items")
    def approved_item_count(self, obj):
        """Surfaces an undersized pool before a student meets it (finding 14)."""
        count = obj.items.filter(review_status="approved", reuse_allowed=True).count()
        return f"{count} / {obj.required_question_count} required"


class ChoiceInline(admin.TabularInline):
    model = models.Choice
    extra = 0


class QuestionInline(admin.StackedInline):
    model = models.Question
    extra = 0


@admin.register(models.Quiz)
class QuizAdmin(TenantScopedAdmin):
    list_display = ["title", "organization", "created_by", "time_limit_minutes", "attempt_limit"]
    search_fields = ["title"]
    inlines = [QuestionInline]


@admin.register(models.Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ["prompt", "quiz", "question_type", "points", "sort_order"]
    inlines = [ChoiceInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(quiz__in=models.Quiz.objects.for_user(request.user))


# --------------------------------------------------------------------------
# Published versions: immutable by construction
# --------------------------------------------------------------------------


class VersionChoiceInline(ReadOnlyInline):
    model = models.VersionChoice
    fields = ["text", "is_correct", "sort_order"]
    readonly_fields = fields


@admin.register(models.VersionQuestion)
class VersionQuestionAdmin(ReadOnlyAdmin):
    list_display = ["prompt", "quiz_version", "question_type", "points", "sort_order"]
    inlines = [VersionChoiceInline]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(quiz_version__in=models.QuizVersion.objects.for_user(request.user))


@admin.register(models.QuizVersion)
class QuizVersionAdmin(ReadOnlyAdmin):
    list_display = ["title", "version_number", "organization", "published_by", "published_at"]
    list_filter = ["organization"]


@admin.register(models.QuizAssignment)
class QuizAssignmentAdmin(TenantScopedAdmin):
    list_display = [
        "title",
        "organization",
        "form_mode",
        "opens_at",
        "due_at",
        "closes_at",
        "allow_late",
        "attempt_limit",
    ]
    list_filter = ["organization", "form_mode", "is_active", "allow_late"]
    filter_horizontal = ["explicit_students"]
    org_fields = ("quiz_version", "item_pool", "course", "cohort")
    membership_user_fields = ("explicit_students",)


@admin.register(models.ResultRelease)
class ResultReleaseAdmin(TenantScopedAdmin):
    list_display = ["assignment", "organization", "is_open", "opened_at", "opened_by"]
    list_filter = ["organization", "is_open"]
    org_fields = ("assignment",)


# --------------------------------------------------------------------------
# Attempt evidence: read-only
# --------------------------------------------------------------------------


class AttemptChoiceSnapshotInline(ReadOnlyInline):
    model = models.AttemptChoiceSnapshot
    fields = ["text", "is_correct", "sort_order"]
    readonly_fields = fields


@admin.register(models.AttemptItemSnapshot)
class AttemptItemSnapshotAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "prompt", "question_type", "points", "sort_order"]
    inlines = [AttemptChoiceSnapshotInline]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(attempt__in=models.Attempt.objects.for_user(request.user))


@admin.register(models.Attempt)
class AttemptAdmin(ReadOnlyAdmin):
    list_display = [
        "student",
        "assignment",
        "status",
        "score",
        "max_score",
        "is_late",
        "started_at",
        "deadline_at",
    ]
    list_filter = ["organization", "status", "is_late", "assignment"]
    search_fields = ["student__username", "assignment__title"]
    actions = ["void_attempts"]

    def has_change_permission(self, request, obj=None):
        # Needed so the admin action runs; every field stays read-only.
        return True

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    @admin.action(description="Void selected attempts (audited)")
    def void_attempts(self, request, queryset):
        from assessments.services import void_attempt

        voided = 0
        for attempt in queryset:
            void_attempt(attempt, request.user, reason="Voided from admin action")
            voided += 1
        self.message_user(request, f"Voided {voided} attempt(s); each change was audited.")


@admin.register(models.Response)
class ResponseAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "item_snapshot", "is_locked", "saved_at"]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(attempt__in=models.Attempt.objects.for_user(request.user))


@admin.register(models.ResponseEvent)
class ResponseEventAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "item_snapshot", "revision_before", "revision_after", "created_at"]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(attempt__in=models.Attempt.objects.for_user(request.user))


@admin.register(models.AttemptFinalization)
class AttemptFinalizationAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "reason", "score", "max_score", "created_at"]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(attempt__in=models.Attempt.objects.for_user(request.user))


@admin.register(models.AttemptAudit)
class AttemptAuditAdmin(ReadOnlyAdmin):
    list_display = ["attempt", "event_type", "actor", "created_at"]
    list_filter = ["event_type"]

    def get_queryset(self, request):
        qs = admin.ModelAdmin.get_queryset(self, request)
        if getattr(request.user, "is_platform_operator", False):
            return qs
        return qs.filter(attempt__in=models.Attempt.objects.for_user(request.user))


@admin.register(models.IntegrityEvent)
class IntegrityEventAdmin(ReadOnlyAdmin):
    list_display = ["kind", "organization", "attempt", "created_at"]
    list_filter = ["organization", "kind"]


@admin.register(models.AdminActionLog)
class AdminActionLogAdmin(ReadOnlyAdmin):
    list_display = ["action", "organization", "actor", "target_type", "target_id", "created_at"]
    list_filter = ["organization", "action"]


# --------------------------------------------------------------------------
# Daily challenge, exports, notifications
# --------------------------------------------------------------------------


@admin.register(models.DailyChallenge)
class DailyChallengeAdmin(TenantScopedAdmin):
    list_display = ["title", "organization", "release_date", "is_archived"]
    org_fields = ("quiz_version",)


@admin.register(models.DailyParticipation)
class DailyParticipationAdmin(ReadOnlyAdmin):
    list_display = ["student", "challenge", "credit_date", "earned_credit"]
    list_filter = ["organization", "earned_credit"]


@admin.register(models.StreakCache)
class StreakCacheAdmin(ReadOnlyAdmin):
    list_display = ["student", "organization", "current_streak", "longest_streak", "updated_at"]
    list_filter = ["organization"]


@admin.register(models.ExportArtifact)
class ExportArtifactAdmin(ReadOnlyAdmin):
    list_display = ["name", "organization", "requested_by", "expires_at", "downloaded_at"]
    list_filter = ["organization"]


@admin.register(models.Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ["title", "user", "kind", "organization", "read_at", "created_at"]
    list_filter = ["organization", "kind"]


class PlatformOperatorAdminMixin:
    """Global operational models: platform operators / superusers only."""

    def _allowed(self, request) -> bool:
        user = request.user
        return bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "is_platform_operator", False)
        )

    def has_module_permission(self, request):
        return self._allowed(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self._allowed(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request) and super().has_delete_permission(request, obj)


@admin.register(models.JobFailure)
class JobFailureAdmin(PlatformOperatorAdminMixin, admin.ModelAdmin):
    """The dead-letter queue operators watch."""

    list_display = ["task_name", "error", "created_at", "acknowledged_at"]
    list_filter = ["task_name", "acknowledged_at"]
    readonly_fields = ["task_name", "task_id", "error", "traceback", "created_at"]
    actions = ["acknowledge"]

    def has_add_permission(self, request, obj=None):
        return False

    @admin.action(description="Acknowledge selected failures")
    def acknowledge(self, request, queryset):
        updated = queryset.filter(acknowledged_at__isnull=True).update(
            acknowledged_at=timezone.now()
        )
        self.message_user(request, f"Acknowledged {updated} failure(s).")


@admin.register(FeatureFlag)
class FeatureFlagAdmin(PlatformOperatorAdminMixin, admin.ModelAdmin):
    list_display = ["key", "enabled", "updated_at"]
    filter_horizontal = ["enabled_organizations"]
    search_fields = ["key", "description"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # The short TTL would eventually pick this up; invalidating makes a
        # rollout switch take effect immediately.
        invalidate(obj.key)
