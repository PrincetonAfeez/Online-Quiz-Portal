"""Core assessment services: start, save, finalize, void, and visibility."""

from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Max, Q
from django.utils import timezone

from accounts.models import OrganizationMembership
from assessments.models import (
    AdminActionLog,
    Attempt,
    AttemptAudit,
    AttemptChoiceSnapshot,
    AttemptFinalization,
    AttemptItemSnapshot,
    DailyChallenge,
    DailyParticipation,
    IntegrityEvent,
    ItemPool,
    ItemPoolItem,
    QuestionBankItem,
    Quiz,
    QuizAssignment,
    QuizVersion,
    Response,
    ResponseEvent,
    ResultRelease,
    StreakCache,
    TimingAccommodation,
    VersionChoice,
    VersionQuestion,
)

logger = logging.getLogger(__name__)


class IntegrityReportingError(Exception):
    """Base for rejections that must leave an integrity record.

    ``save_response`` runs in a transaction, so an IntegrityEvent written before
    raising is rolled back with everything else -- exactly the events the
    integrity dashboard exists to show. The record travels on the exception and
    is committed by the caller once the transaction has unwound.
    """

    def __init__(self, message, kind=None, detail=None, attempt_id=None, organization_id=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail or {}
        self.attempt_id = attempt_id
        self.organization_id = organization_id


class AttemptConflict(IntegrityReportingError):
    pass


class AttemptExpired(IntegrityReportingError):
    pass


def record_integrity_event(error: IntegrityReportingError) -> None:
    """Persist the diagnostic an aborted save could not commit itself."""
    if error.kind is None or error.organization_id is None:
        return
    try:
        IntegrityEvent.objects.create(
            organization_id=error.organization_id,
            attempt_id=error.attempt_id,
            kind=error.kind,
            detail=error.detail,
        )
    except Exception:
        logger.warning("could not record integrity event", exc_info=True)


@dataclass(frozen=True)
class StudentChoice:
    id: int
    text: str


@dataclass(frozen=True)
class StudentQuestion:
    id: int
    prompt: str
    question_type: str
    points: Decimal
    choices: list[StudentChoice]
    selected_choice_ids: set[int]
    is_locked: bool


@dataclass(frozen=True)
class ImmediateFeedback:
    is_correct: bool
    explanation: str


@dataclass(frozen=True)
class SaveOutcome:
    response: Response
    revision: int
    feedback: ImmediateFeedback | None = None


@dataclass(frozen=True)
class ResultVisibility:
    """What a viewer may see of a finalized attempt (findings 3 and 4)."""

    show_score: bool = False
    show_answer_key: bool = False
    reason: str = ""
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def is_platform_operator(user) -> bool:
    """Cross-tenant access is an explicit grant, never a side effect of is_staff."""
    return bool(getattr(user, "is_platform_operator", False))


def assert_instructor(user, organization) -> None:
    if is_platform_operator(user):
        return
    if not OrganizationMembership.objects.filter(
        user=user,
        organization=organization,
        role__in=[
            OrganizationMembership.Role.INSTRUCTOR,
            OrganizationMembership.Role.ORG_ADMIN,
        ],
        is_active=True,
    ).exists():
        raise PermissionDenied("Instructor access is required.")


def assert_org_admin(user, organization) -> None:
    if is_platform_operator(user):
        return
    if not OrganizationMembership.objects.filter(
        user=user,
        organization=organization,
        role=OrganizationMembership.Role.ORG_ADMIN,
        is_active=True,
    ).exists():
        raise PermissionDenied("Organization administrator access is required.")


def assert_student(user, organization) -> None:
    if not OrganizationMembership.objects.filter(
        user=user,
        organization=organization,
        role=OrganizationMembership.Role.STUDENT,
        is_active=True,
    ).exists():
        raise PermissionDenied("Student membership is required.")


def is_instructor(user, organization) -> bool:
    try:
        assert_instructor(user, organization)
    except PermissionDenied:
        return False
    return True


def is_org_admin(user, organization) -> bool:
    try:
        assert_org_admin(user, organization)
    except PermissionDenied:
        return False
    return True


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


@transaction.atomic
def publish_quiz(quiz: Quiz, actor) -> QuizVersion:
    assert_instructor(actor, quiz.organization)
    questions = list(quiz.questions.prefetch_related("choices"))
    if not questions:
        raise ValidationError("A quiz must have at least one question.")

    for question in questions:
        choices = list(question.choices.all())
        correct_count = sum(1 for choice in choices if choice.is_correct)
        if question.question_type == QuestionBankItem.QuestionType.TRUE_FALSE and len(choices) != 2:
            raise ValidationError("True/false questions must have exactly two choices.")
        if question.question_type == QuestionBankItem.QuestionType.SINGLE and correct_count != 1:
            raise ValidationError("Single-correct questions must have exactly one answer.")
        if question.question_type == QuestionBankItem.QuestionType.MULTI and correct_count < 1:
            raise ValidationError("Multi-select questions need at least one correct answer.")
        if question.question_type == QuestionBankItem.QuestionType.MULTI and correct_count == len(
            choices
        ):
            raise ValidationError("Multi-select questions need at least one incorrect choice.")

    # Two concurrent publishes would otherwise collide on the unique constraint
    # and surface as a 500; retry the number a bounded number of times (finding 38).
    for attempt_no in range(5):
        version_number = (
            QuizVersion.objects.filter(quiz=quiz).aggregate(latest=Max("version_number"))["latest"]
            or 0
        ) + 1
        try:
            with transaction.atomic():
                version = QuizVersion.objects.create(
                    organization=quiz.organization,
                    quiz=quiz,
                    version_number=version_number,
                    title=quiz.title,
                    description=quiz.description,
                    time_limit_minutes=quiz.time_limit_minutes,
                    attempt_limit=quiz.attempt_limit,
                    shuffle_questions=quiz.shuffle_questions,
                    shuffle_choices=quiz.shuffle_choices,
                    feedback_policy=quiz.feedback_policy,
                    pass_mark_percent=quiz.pass_mark_percent,
                    published_by=actor,
                )
            break
        except IntegrityError:
            if attempt_no == 4:
                raise
            continue

    for question in questions:
        version_question = VersionQuestion.objects.create(
            quiz_version=version,
            source_item=question.bank_item,
            prompt=question.prompt,
            question_type=question.question_type,
            points=question.points,
            explanation=question.explanation,
            sort_order=question.sort_order,
        )
        for choice in question.choices.all():
            VersionChoice.objects.create(
                version_question=version_question,
                text=choice.text,
                is_correct=choice.is_correct,
                sort_order=choice.sort_order,
            )
    return version


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def effective_time_limit(assignment: QuizAssignment) -> int | None:
    if assignment.time_limit_minutes:
        return assignment.time_limit_minutes
    if assignment.quiz_version_id and assignment.quiz_version.time_limit_minutes:
        return assignment.quiz_version.time_limit_minutes
    return None


def effective_attempt_limit(assignment: QuizAssignment) -> int | None:
    """Return the attempt cap, or ``None`` when attempts are unlimited.

    ``attempt_limit=0`` means unlimited (practice archives). Blank/null on the
    assignment inherits from the quiz version when present, else defaults to 1.
    """
    if assignment.attempt_limit == 0:
        return None
    if assignment.attempt_limit is not None:
        return assignment.attempt_limit
    if assignment.quiz_version_id:
        version_limit = assignment.quiz_version.attempt_limit
        if version_limit == 0:
            return None
        if version_limit:
            return version_limit
    return 1


def mark_answer_key_disclosed(attempt: Attempt) -> None:
    """Persist that the student has been shown the answer key (audit R4-01)."""
    if attempt.answer_key_disclosed:
        return
    Attempt.objects.filter(pk=attempt.pk, answer_key_disclosed=False).update(
        answer_key_disclosed=True
    )
    attempt.answer_key_disclosed = True


def _key_already_disclosed(student, assignment: QuizAssignment) -> bool:
    """True once the student was shown the key or IMMEDIATE locked a response.

    Covers AT_END/MANUAL key release (durable flag) and IMMEDIATE locks,
    including answers locked while correctness was withheld (audit R3-02 / R4-01).
    """
    if Attempt.objects.filter(
        student=student,
        assignment=assignment,
        answer_key_disclosed=True,
    ).exists():
        return True
    return Response.objects.filter(
        attempt__student=student,
        attempt__assignment=assignment,
        is_locked=True,
    ).exists()


def attempts_counting_toward_limit(student, assignment: QuizAssignment) -> int:
    """Count attempts that consume the student's budget.

    Voided attempts are normally excluded (audit F04). Once the answer key has
    been disclosed — or the live policy is IMMEDIATE — voids keep consuming the
    budget so a keyed retake cannot be reopened (R2-07 / R3-02 / R4-01).
    """
    qs = Attempt.objects.filter(student=student, assignment=assignment)
    keep_voids = (
        effective_feedback_policy(assignment) == Quiz.FeedbackPolicy.IMMEDIATE
        or _key_already_disclosed(student, assignment)
    )
    if not keep_voids:
        qs = qs.exclude(status=Attempt.Status.VOIDED_BY_ADMIN)
    return qs.count()


def effective_feedback_policy(assignment: QuizAssignment) -> str:
    """Sampled forms have no quiz_version; they still need a policy (finding 17)."""
    if assignment.feedback_policy:
        return assignment.feedback_policy
    if assignment.quiz_version_id:
        return assignment.quiz_version.feedback_policy
    return Quiz.FeedbackPolicy.AT_END


def effective_pass_mark(assignment: QuizAssignment) -> int | None:
    if assignment.pass_mark_percent is not None:
        return assignment.pass_mark_percent
    if assignment.quiz_version_id:
        return assignment.quiz_version.pass_mark_percent
    return None


def _is_assignment_open(assignment: QuizAssignment, now) -> bool:
    if not assignment.is_active:
        return False
    if assignment.opens_at and now < assignment.opens_at:
        return False
    if assignment.closes_at and now > assignment.closes_at:
        return False
    return True


def _eligible_for_assignment(user, assignment: QuizAssignment) -> bool:
    if is_platform_operator(user):
        return True
    if assignment.explicit_students.filter(id=user.id).exists():
        return True
    enrollment_qs = user.enrollments.filter(
        organization=assignment.organization, is_active=True, course__is_active=True
    )
    if assignment.cohort_id:
        return enrollment_qs.filter(cohort=assignment.cohort).exists()
    if assignment.course_id:
        return enrollment_qs.filter(course=assignment.course).exists()
    return OrganizationMembership.objects.filter(
        user=user,
        organization=assignment.organization,
        role=OrganizationMembership.Role.STUDENT,
        is_active=True,
    ).exists()


def visible_assignments_for(user, organization):
    """Assignments a student may actually act on -- open, and they are eligible.

    Eligibility is resolved from three pre-loaded sets rather than by calling
    ``_eligible_for_assignment`` per row, which issued two to four queries each
    and made the dashboard scale linearly with the assignment count.
    """
    now = timezone.now()
    assignments = list(
        QuizAssignment.objects.for_user(user)
        .filter(organization=organization, is_active=True, opens_at__lte=now)
        .filter(Q(closes_at__isnull=True) | Q(closes_at__gte=now))
        .select_related("quiz_version", "item_pool", "course", "cohort")
        .order_by("due_at", "opens_at")
    )
    if not assignments:
        return []
    if is_platform_operator(user):
        return assignments

    enrolled_courses = set()
    enrolled_cohorts = set()
    for course_id, cohort_id in user.enrollments.filter(
        organization=organization, is_active=True, course__is_active=True
    ).values_list("course_id", "cohort_id"):
        enrolled_courses.add(course_id)
        if cohort_id:
            enrolled_cohorts.add(cohort_id)

    explicit_ids = set(
        QuizAssignment.objects.filter(
            id__in=[a.id for a in assignments], explicit_students=user
        ).values_list("id", flat=True)
    )
    is_org_student = OrganizationMembership.objects.filter(
        user=user,
        organization=organization,
        role=OrganizationMembership.Role.STUDENT,
        is_active=True,
    ).exists()

    visible = []
    for assignment in assignments:
        if assignment.id in explicit_ids:
            visible.append(assignment)
        elif assignment.cohort_id:
            if assignment.cohort_id in enrolled_cohorts:
                visible.append(assignment)
        elif assignment.course_id:
            if assignment.course_id in enrolled_courses:
                visible.append(assignment)
        elif is_org_student:
            visible.append(assignment)
    return visible


def _best_accommodation(user, assignment: QuizAssignment) -> tuple[Decimal, int]:
    """Pick the accommodation that yields the most time (finding 20).

    Ordering by multiplier alone ranked 2.0x above 1.0x + 120 minutes, which is
    wrong for any quiz shorter than two hours. Scope is expressed with Q objects
    so an unscoped assignment cannot inherit a course-specific grant.
    """
    now = timezone.now()
    scope = Q(course__isnull=True)
    if assignment.course_id:
        scope |= Q(course_id=assignment.course_id)
    cohort_scope = Q(cohort__isnull=True)
    if assignment.cohort_id:
        cohort_scope |= Q(cohort_id=assignment.cohort_id)

    rows = (
        TimingAccommodation.objects.filter(
            Q(organization=assignment.organization, student=user, is_active=True)
            & scope
            & cohort_scope
        )
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=now))
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gte=now))
        .distinct()
    )
    base = effective_time_limit(assignment) or 0
    best = max(
        (row for row in rows if row.applies_at(now)),
        key=lambda row: row.accommodated_minutes(base),
        default=None,
    )
    if best is None:
        return Decimal("1"), 0
    return best.multiplier, best.extra_minutes


# ---------------------------------------------------------------------------
# Attempt form generation
# ---------------------------------------------------------------------------


def _snapshot_from_version(attempt: Attempt, version: QuizVersion) -> None:
    question_qs = list(version.version_questions.prefetch_related("version_choices"))
    rng = random.Random(attempt.sampled_seed or str(attempt.id))
    if version.shuffle_questions:
        rng.shuffle(question_qs)
    for index, question in enumerate(question_qs, start=1):
        item = AttemptItemSnapshot.objects.create(
            attempt=attempt,
            version_question=question,
            prompt=question.prompt,
            question_type=question.question_type,
            points=question.points,
            explanation=question.explanation,
            sort_order=index,
        )
        choices = list(question.version_choices.all())
        if version.shuffle_choices:
            rng.shuffle(choices)
        for choice_index, choice in enumerate(choices, start=1):
            AttemptChoiceSnapshot.objects.create(
                item_snapshot=item,
                version_choice=choice,
                text=choice.text,
                is_correct=choice.is_correct,
                sort_order=choice_index,
            )


def _weighted_sample(entries: list[ItemPoolItem], count: int, rng: random.Random):
    """Deterministic weighted draw without replacement (finding 15)."""
    remaining = list(entries)
    picked = []
    for _ in range(count):
        total = sum(max(entry.weight, 1) for entry in remaining)
        threshold = rng.uniform(0, total)
        cumulative = 0
        for index, entry in enumerate(remaining):
            cumulative += max(entry.weight, 1)
            if threshold <= cumulative:
                picked.append(remaining.pop(index))
                break
        else:  # float drift only
            picked.append(remaining.pop())
    return picked


def _pool_entries(pool: ItemPool):
    return list(
        ItemPoolItem.objects.filter(
            pool=pool,
            item__organization=pool.organization,
            item__review_status="approved",
            item__reuse_allowed=True,
        )
        .select_related("item")
        .prefetch_related("item__choices")
        .order_by("item_id")
    )


def sample_pool_items(pool: ItemPool, rng: random.Random) -> tuple[list, dict]:
    """Draw a form from the pool honouring blueprint strata and item weights.

    An undersized pool now raises instead of silently shrinking the exam, which
    previously gave different students different-length papers scored on one
    scale (finding 14).
    """
    entries = _pool_entries(pool)
    if not entries:
        raise ValidationError(f"Item pool '{pool.name}' has no approved, reusable questions.")

    criteria = {"pool_id": pool.id, "weighted": True, "blueprint": pool.blueprint or None}
    if pool.blueprint:
        chosen = []
        for difficulty, count in sorted(pool.blueprint.items()):
            stratum = [entry for entry in entries if entry.item.difficulty == difficulty]
            if len(stratum) < count:
                raise ValidationError(
                    f"Item pool '{pool.name}' needs {count} approved '{difficulty}' items "
                    f"but has {len(stratum)}."
                )
            chosen.extend(_weighted_sample(stratum, count, rng))
    else:
        if len(entries) < pool.required_question_count:
            raise ValidationError(
                f"Item pool '{pool.name}' has {len(entries)} approved items but the form "
                f"requires {pool.required_question_count}."
            )
        chosen = _weighted_sample(entries, pool.required_question_count, rng)
    criteria["item_ids"] = [entry.item_id for entry in chosen]
    return [entry.item for entry in chosen], criteria


def _snapshot_from_pool(attempt: Attempt) -> dict:
    pool = attempt.assignment.item_pool
    if pool is None:
        raise ValidationError("Sampled assignments require an item pool.")

    rng = random.Random(attempt.sampled_seed)
    sampled, criteria = sample_pool_items(pool, rng)
    for index, bank_item in enumerate(sampled, start=1):
        item = AttemptItemSnapshot.objects.create(
            attempt=attempt,
            prompt=bank_item.prompt,
            question_type=bank_item.question_type,
            points=bank_item.points,
            explanation=bank_item.explanation,
            sort_order=index,
        )
        choices = list(bank_item.choices.all())
        rng.shuffle(choices)
        for choice_index, choice in enumerate(choices, start=1):
            AttemptChoiceSnapshot.objects.create(
                item_snapshot=item,
                text=choice.text,
                is_correct=choice.is_correct,
                sort_order=choice_index,
            )
    return criteria


# ---------------------------------------------------------------------------
# Attempt lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def start_attempt(user, assignment: QuizAssignment) -> Attempt:
    assert_student(user, assignment.organization)
    assignment = QuizAssignment.objects.select_for_update().get(id=assignment.id)
    now = timezone.now()
    if not _is_assignment_open(assignment, now):
        raise PermissionDenied("This assignment is not currently available.")
    if not _eligible_for_assignment(user, assignment):
        raise PermissionDenied("You are not eligible for this assignment.")

    is_late = bool(assignment.due_at and now > assignment.due_at)
    if is_late and not assignment.allow_late:
        raise PermissionDenied("This assignment no longer accepts late attempts.")

    limit = effective_attempt_limit(assignment)
    if limit is not None and attempts_counting_toward_limit(user, assignment) >= limit:
        raise PermissionDenied("Attempt limit reached.")

    # Two simultaneously open attempts on one assignment let a student bank a
    # second paper and submit whichever scored better.
    open_attempt = Attempt.objects.filter(
        student=user, assignment=assignment, status=Attempt.Status.IN_PROGRESS
    ).first()
    if open_attempt is not None:
        if maybe_finalize_expired(open_attempt):
            open_attempt.refresh_from_db()
        else:
            raise PermissionDenied(
                "You already have an attempt in progress. Submit it before starting another."
            )

    multiplier, extra_minutes = _best_accommodation(user, assignment)
    deadline_at = None
    time_limit = effective_time_limit(assignment)
    if time_limit:
        scaled_minutes = int((Decimal(time_limit) * multiplier).to_integral_value())
        deadline_at = now + timedelta(minutes=scaled_minutes + extra_minutes)
        if assignment.closes_at and deadline_at > assignment.closes_at:
            deadline_at = assignment.closes_at

    # attempt_number must not be derived from a count: deleting an attempt would
    # make the next start collide on (student, assignment, attempt_number).
    highest = (
        Attempt.objects.filter(student=user, assignment=assignment).aggregate(
            top=Max("attempt_number")
        )["top"]
        or 0
    )
    attempt = Attempt.objects.create(
        organization=assignment.organization,
        student=user,
        assignment=assignment,
        quiz_version=assignment.quiz_version,
        attempt_number=highest + 1,
        deadline_at=deadline_at,
        applied_time_multiplier=multiplier,
        applied_extra_minutes=extra_minutes,
        sampled_seed=secrets.token_hex(16),
        is_late=is_late,
    )
    if assignment.form_mode == QuizAssignment.FormMode.SAMPLED:
        attempt.sampling_criteria = _snapshot_from_pool(attempt)
    elif assignment.quiz_version:
        _snapshot_from_version(attempt, assignment.quiz_version)
    else:
        raise ValidationError("Fixed assignments require a quiz version.")

    attempt.max_score = sum(
        (item.points for item in attempt.item_snapshots.all()), start=Decimal("0")
    )
    attempt.save(update_fields=["max_score", "sampling_criteria"])
    AttemptAudit.objects.create(
        attempt=attempt,
        actor=user,
        event_type="attempt.started",
        detail={
            "deadline_at": deadline_at.isoformat() if deadline_at else None,
            "closes_at": assignment.closes_at.isoformat() if assignment.closes_at else None,
            "multiplier": str(multiplier),
            "extra_minutes": extra_minutes,
            "sampled_seed": attempt.sampled_seed,
            "sampling_criteria": attempt.sampling_criteria,
            "feedback_policy": effective_feedback_policy(assignment),
            "is_late": is_late,
        },
    )
    return attempt


def student_read_model(attempt: Attempt) -> list[StudentQuestion]:
    """Projection that structurally excludes the answer key (spec 4.4)."""
    responses = {
        response.item_snapshot_id: response
        for response in attempt.responses.prefetch_related("selected_choices")
    }
    questions: list[StudentQuestion] = []
    items = attempt.item_snapshots.prefetch_related(
        # `.only()` keeps `is_correct` out of the object graph the template sees.
        models.Prefetch(
            "choice_snapshots",
            queryset=AttemptChoiceSnapshot.objects.only(
                "id", "text", "sort_order", "item_snapshot_id"
            ),
        )
    )
    for item in items:
        response = responses.get(item.id)
        # `.values_list()` bypasses the prefetch cache and re-queries per row;
        # iterating `.all()` uses it.
        selected = {choice.id for choice in response.selected_choices.all()} if response else set()
        questions.append(
            StudentQuestion(
                id=item.id,
                prompt=item.prompt,
                question_type=item.question_type,
                points=item.points,
                choices=[
                    StudentChoice(id=choice.id, text=choice.text)
                    for choice in item.choice_snapshots.all()
                ],
                selected_choice_ids=selected,
                is_locked=bool(response and response.is_locked),
            )
        )
    return questions


def attempt_cutoff(attempt: Attempt):
    """The instant after which no save or submit is honoured.

    Both the per-attempt deadline *and* the assignment's availability close
    apply -- spec 3.5 says no attempt may start **or continue** outside the
    window, and an untimed attempt previously had no cutoff at all.
    """
    candidates = [value for value in (attempt.deadline_at, attempt.assignment.closes_at) if value]
    if not candidates:
        return None
    return min(candidates) + timedelta(seconds=attempt.assignment.grace_seconds)


# Backwards-compatible alias used by older call sites.
_deadline_with_grace = attempt_cutoff


@transaction.atomic
def save_response(
    attempt: Attempt,
    user,
    item_id: int,
    choice_ids: list[int],
    revision: int,
    request=None,
) -> SaveOutcome:
    attempt = Attempt.objects.select_for_update().select_related("assignment").get(id=attempt.id)
    if attempt.student_id != user.id:
        raise PermissionDenied("This is not your attempt.")
    if attempt.status != Attempt.Status.IN_PROGRESS:
        raise AttemptExpired("This attempt is no longer accepting responses.")

    if not attempt.assignment.is_active:
        # Deactivating an assignment is an instructor's emergency stop; it has
        # to halt attempts already in flight, not just prevent new ones.
        raise AttemptExpired(
            "This assessment has been withdrawn by your instructor.",
            kind=IntegrityEvent.Kind.LATE_SAVE_REJECTED,
            detail={"item_id": item_id, "cause": "assignment_deactivated"},
            attempt_id=attempt.id,
            organization_id=attempt.organization_id,
        )

    cutoff = attempt_cutoff(attempt)
    if cutoff and timezone.now() > cutoff:
        raise AttemptExpired(
            "The attempt deadline has passed.",
            kind=IntegrityEvent.Kind.LATE_SAVE_REJECTED,
            detail={"item_id": item_id, "cutoff": cutoff.isoformat()},
            attempt_id=attempt.id,
            organization_id=attempt.organization_id,
        )

    if revision != attempt.revision:
        raise AttemptConflict(
            "A newer answer exists. Refresh the attempt before saving.",
            kind=IntegrityEvent.Kind.REVISION_CONFLICT,
            detail={"item_id": item_id, "sent": revision, "current": attempt.revision},
            attempt_id=attempt.id,
            organization_id=attempt.organization_id,
        )

    item = attempt.item_snapshots.get(id=item_id)
    response, _ = Response.objects.get_or_create(attempt=attempt, item_snapshot=item)
    if response.is_locked:
        raise AttemptConflict(
            "This response is locked by the feedback policy.",
            kind=IntegrityEvent.Kind.LOCKED_RESPONSE_WRITE,
            detail={"item_id": item_id},
            attempt_id=attempt.id,
            organization_id=attempt.organization_id,
        )

    valid_choices = list(
        item.choice_snapshots.filter(id__in=choice_ids).values_list("id", flat=True)
    )
    if len(valid_choices) != len(set(choice_ids)):
        raise ValidationError("One or more selected choices are invalid.")
    if (
        item.question_type
        in [QuestionBankItem.QuestionType.SINGLE, QuestionBankItem.QuestionType.TRUE_FALSE]
        and len(valid_choices) > 1
    ):
        raise ValidationError("This question accepts one choice.")

    before = attempt.revision
    after = before + 1
    response.selected_choices.set(valid_choices)

    policy = effective_feedback_policy(attempt.assignment)
    feedback = None
    if policy == Quiz.FeedbackPolicy.IMMEDIATE:
        # Withhold per-question correctness while a fixed-form retake remains,
        # otherwise the student learns the full key before attempt 2 (audit F03).
        limit = effective_attempt_limit(attempt.assignment)
        used = attempts_counting_toward_limit(attempt.student, attempt.assignment)
        fixed = attempt.assignment.form_mode == QuizAssignment.FormMode.FIXED
        retake_open = fixed and limit is not None and used < limit
        # Always lock under IMMEDIATE so a later policy edit cannot reopen a
        # retake after answers were committed (audit R4-01).
        response.is_locked = True
        if not retake_open:
            # Correctness may be disclosed only for the question just locked (spec 4.4).
            correct_ids = set(
                item.choice_snapshots.filter(is_correct=True).values_list("id", flat=True)
            )
            feedback = ImmediateFeedback(
                is_correct=set(valid_choices) == correct_ids,
                explanation=item.explanation,
            )
            mark_answer_key_disclosed(attempt)
    # An M2M write does not touch the row, so saved_at would otherwise still
    # report when the question was first opened (finding 24).
    response.save(update_fields=["is_locked", "saved_at"])

    attempt.revision = after
    attempt.save(update_fields=["revision"])
    ResponseEvent.objects.create(
        attempt=attempt,
        item_snapshot=item,
        selected_choice_ids=valid_choices,
        revision_before=before,
        revision_after=after,
        ip_address=_client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:300] if request else ""),
    )
    return SaveOutcome(response=response, revision=after, feedback=feedback)


def _client_ip(request):
    """Prefer the proxy-supplied client address when we are behind a trusted proxy."""
    if request is None:
        return None
    from django.conf import settings

    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR")


def _score_attempt(attempt: Attempt) -> tuple[Decimal, Decimal]:
    score = Decimal("0")
    max_score = Decimal("0")
    responses = {
        response.item_snapshot_id: {choice.id for choice in response.selected_choices.all()}
        for response in attempt.responses.prefetch_related("selected_choices")
    }
    for item in attempt.item_snapshots.prefetch_related("choice_snapshots"):
        max_score += item.points
        correct_ids = {
            choice.id for choice in item.choice_snapshots.all() if choice.is_correct
        }
        selected = responses.get(item.id, set())
        # An item with no correct choice is a content defect; never award points
        # to a student who simply left it blank.
        if correct_ids and selected == correct_ids:
            score += item.points
    return score, max_score


def _record_daily_participation(attempt: Attempt) -> DailyParticipation | None:
    """Streak source of truth, written inside the finalization transaction (spec 4.3).

    Keyed off the attempt's own ``daily_challenge`` link: a challenge and an
    ordinary assignment may publish the same quiz version, and only the former
    may earn streak credit.
    """
    if not attempt.daily_challenge_id:
        return None
    challenge = attempt.daily_challenge

    local_today = today_for_org(attempt.organization)
    # Credit requires finalizing inside the live window in the org's own calendar;
    # archived challenges are practice only.
    earned = (
        not challenge.is_archived
        and challenge.release_date == local_today
        and attempt.status != Attempt.Status.VOIDED_BY_ADMIN
    )
    participation, _ = DailyParticipation.objects.get_or_create(
        organization=attempt.organization,
        student=attempt.student,
        challenge=challenge,
        defaults={
            "attempt": attempt,
            "credit_date": challenge.release_date,
            "earned_credit": earned,
        },
    )
    return participation


@transaction.atomic
def finalize_attempt(
    attempt: Attempt, reason: str = "submitted", actor=None
) -> AttemptFinalization:
    attempt = Attempt.objects.select_for_update().select_related("assignment").get(id=attempt.id)
    if attempt.status == Attempt.Status.VOIDED_BY_ADMIN:
        # The expiry sweep (or any retry) would otherwise silently un-void an
        # attempt an instructor had deliberately struck out.
        raise PermissionDenied("A voided attempt cannot be finalized.")
    existing = getattr(attempt, "finalization", None)
    if existing:
        IntegrityEvent.objects.create(
            organization=attempt.organization,
            attempt=attempt,
            kind=IntegrityEvent.Kind.DUPLICATE_FINALIZE,
            detail={"reason": reason},
        )
        return existing

    score, max_score = _score_attempt(attempt)
    try:
        # Savepoint: without it the poisoned transaction makes the recovery
        # SELECT raise TransactionManagementError instead of returning the
        # winner's record (finding 11).
        with transaction.atomic():
            finalization = AttemptFinalization.objects.create(
                attempt=attempt, reason=reason, score=score, max_score=max_score
            )
    except IntegrityError:
        IntegrityEvent.objects.create(
            organization=attempt.organization,
            attempt=attempt,
            kind=IntegrityEvent.Kind.DUPLICATE_FINALIZE,
            detail={"reason": reason, "recovered": True},
        )
        return AttemptFinalization.objects.get(attempt=attempt)

    now = timezone.now()
    attempt.score = score
    attempt.max_score = max_score
    attempt.status = (
        Attempt.Status.EXPIRED if reason in ("expired", "sweep") else Attempt.Status.FINALIZED
    )
    attempt.submitted_at = attempt.submitted_at or now
    attempt.finalized_at = now
    attempt.finalization_reason = reason
    if attempt.assignment.due_at and attempt.submitted_at > attempt.assignment.due_at:
        attempt.is_late = True
    attempt.save(
        update_fields=[
            "score",
            "max_score",
            "status",
            "submitted_at",
            "finalized_at",
            "finalization_reason",
            "is_late",
        ]
    )
    participation = _record_daily_participation(attempt)
    AttemptAudit.objects.create(
        attempt=attempt,
        actor=actor,
        event_type="attempt.finalized",
        detail={
            "reason": reason,
            "score": str(score),
            "max_score": str(max_score),
            "is_late": attempt.is_late,
            "daily_credit": bool(participation and participation.earned_credit),
            "sampled_seed": attempt.sampled_seed,
            "sampling_criteria": attempt.sampling_criteria,
        },
    )
    if reason == "sweep":
        IntegrityEvent.objects.create(
            organization=attempt.organization,
            attempt=attempt,
            kind=IntegrityEvent.Kind.SWEEP_FINALIZED,
            detail={"reason": reason},
        )
    elif reason == "expired":
        IntegrityEvent.objects.create(
            organization=attempt.organization,
            attempt=attempt,
            kind=IntegrityEvent.Kind.EXPIRED_FINALIZED,
            detail={"reason": reason},
        )
    if participation:
        # The streak cache is recomputable and never authoritative, so its
        # refresh must not be able to roll back a score (spec 4.3).
        _schedule_streak_recompute(attempt.student_id, attempt.organization_id)
    # If the student may already see the answer key, stamp durable disclosure
    # so a subsequent void cannot reopen a keyed retake (audit R4-01).
    if result_visibility(attempt, attempt.student).show_answer_key:
        mark_answer_key_disclosed(attempt)
    return finalization


def _schedule_streak_recompute(student_id: int, organization_id: int) -> None:
    def _run():
        from accounts.models import Organization, User

        try:
            # Two indexed queries -- cheaper to run than to enqueue, and it
            # keeps submit latency independent of broker health. This executes
            # *after* commit, so a failure here can never roll back a score
            # (spec 4.3); Beat's hourly job rebuilds the cache regardless.
            recompute_streak(
                User.objects.get(id=student_id), Organization.objects.get(id=organization_id)
            )
        except Exception:
            logger.warning(
                "streak recompute failed; cache will be rebuilt by the periodic job",
                exc_info=True,
                extra={"user_id": student_id, "organization_id": organization_id},
            )

    transaction.on_commit(_run)


@transaction.atomic
def void_attempt(attempt: Attempt, actor, reason: str) -> Attempt:
    """The supported way to undo an attempt -- never a direct field edit (finding 5)."""
    assert_instructor(actor, attempt.organization)
    attempt = Attempt.objects.select_for_update().get(id=attempt.id)
    attempt.status = Attempt.Status.VOIDED_BY_ADMIN
    attempt.finalization_reason = "voided_by_admin"
    attempt.save(update_fields=["status", "finalization_reason"])
    DailyParticipation.objects.filter(attempt=attempt).update(earned_credit=False)
    AttemptAudit.objects.create(
        attempt=attempt, actor=actor, event_type="attempt.voided", detail={"reason": reason}
    )
    IntegrityEvent.objects.create(
        organization=attempt.organization,
        attempt=attempt,
        kind=IntegrityEvent.Kind.ATTEMPT_VOIDED,
        detail={"reason": reason, "actor_id": getattr(actor, "id", None)},
    )
    AdminActionLog.objects.create(
        organization=attempt.organization,
        actor=actor,
        action="attempt.void",
        target_type="Attempt",
        target_id=str(attempt.id),
        detail={"reason": reason},
    )
    _schedule_streak_recompute(attempt.student_id, attempt.organization_id)
    return attempt


def is_past_cutoff(attempt: Attempt) -> bool:
    cutoff = attempt_cutoff(attempt)
    return bool(cutoff and timezone.now() > cutoff)


def maybe_finalize_expired(attempt: Attempt) -> bool:
    if attempt.status == Attempt.Status.IN_PROGRESS and is_past_cutoff(attempt):
        finalize_attempt(attempt, reason="expired")
        return True
    return False


def submit_attempt_for(attempt: Attempt, actor) -> AttemptFinalization:
    """Student-initiated submit.

    A submit that arrives after the cutoff is recorded as an expiry, not as an
    on-time submission -- the finalization reason is evidence, and it was
    previously stamped "submitted" no matter how late the request arrived.
    """
    reason = "expired" if is_past_cutoff(attempt) else "submitted"
    return finalize_attempt(attempt, reason=reason, actor=actor)


# ---------------------------------------------------------------------------
# Result release
# ---------------------------------------------------------------------------


@transaction.atomic
def set_result_release(assignment: QuizAssignment, actor, is_open: bool) -> ResultRelease:
    assert_instructor(actor, assignment.organization)
    now = timezone.now()
    release, _ = ResultRelease.objects.get_or_create(
        assignment=assignment, defaults={"organization": assignment.organization}
    )
    release.is_open = is_open
    if is_open:
        release.opened_at = now
        release.opened_by = actor
        release.closed_at = None
    else:
        release.closed_at = now
    release.save()
    AdminActionLog.objects.create(
        organization=assignment.organization,
        actor=actor,
        action="results.release" if is_open else "results.withhold",
        target_type="QuizAssignment",
        target_id=str(assignment.id),
        detail={"is_open": is_open},
    )
    return release


def result_visibility(attempt: Attempt, viewer) -> ResultVisibility:
    """Decide what a viewer may see (findings 3 and 4).

    Score and answer key are separate grants: releasing the key while another
    attempt at the same fixed form is still available simply hands over the
    answers.
    """
    assignment = attempt.assignment
    if is_platform_operator(viewer) or is_instructor(viewer, attempt.organization):
        return ResultVisibility(True, True, "staff")
    if attempt.status == Attempt.Status.VOIDED_BY_ADMIN:
        return ResultVisibility(False, False, "voided")
    if attempt.status not in Attempt.SCORED_STATUSES:
        return ResultVisibility(False, False, "in_progress")

    policy = effective_feedback_policy(assignment)
    if policy == Quiz.FeedbackPolicy.MANUAL:
        release = getattr(assignment, "result_release", None)
        if not (release and release.is_open):
            return ResultVisibility(False, False, "manual_release_withheld")

    now = timezone.now()
    window_closed = bool(assignment.closes_at and now > assignment.closes_at)
    limit = effective_attempt_limit(assignment)
    attempts_used = attempts_counting_toward_limit(attempt.student, assignment)
    # Unlimited (None) does not withhold the key — that mode is for practice.
    retakes_remain = limit is not None and attempts_used < limit
    # A sampled form regenerates per attempt, so sharing the key with oneself
    # gains nothing; a fixed form hands over the exact next paper.
    fixed_form = assignment.form_mode == QuizAssignment.FormMode.FIXED
    if fixed_form and retakes_remain and not window_closed:
        return ResultVisibility(
            True,
            False,
            "retake_available",
            {"attempts_used": attempts_used, "attempt_limit": limit},
        )
    return ResultVisibility(True, True, "released")


# ---------------------------------------------------------------------------
# Daily challenge and streaks
# ---------------------------------------------------------------------------


def today_for_org(organization):
    return timezone.now().astimezone(organization.zoneinfo).date()


def current_daily_challenge(organization) -> DailyChallenge | None:
    """The challenge live *today* in the organization's own calendar."""
    return DailyChallenge.objects.filter(
        organization=organization,
        release_date=today_for_org(organization),
        is_archived=False,
    ).first()


def archived_daily_challenges(organization):
    return DailyChallenge.objects.filter(
        organization=organization, release_date__lt=today_for_org(organization)
    ).order_by("-release_date")


def daily_assignment_for(challenge: DailyChallenge) -> QuizAssignment:
    """The assignment backing a challenge, created once under a row lock."""
    challenge = DailyChallenge.objects.select_for_update().get(id=challenge.id)
    if challenge.assignment_id:
        return challenge.assignment
    organization = challenge.organization
    local_midnight = timezone.now()
    assignment = QuizAssignment.objects.create(
        organization=organization,
        title=f"Daily Challenge {challenge.release_date}",
        form_mode=QuizAssignment.FormMode.FIXED,
        quiz_version=challenge.quiz_version,
        opens_at=local_midnight,
        attempt_limit=1,
        created_by_id=challenge.quiz_version.published_by_id,
        feedback_policy=Quiz.FeedbackPolicy.AT_END,
    )
    challenge.assignment = assignment
    challenge.save(update_fields=["assignment"])
    return assignment


@transaction.atomic
def start_daily_attempt(user, challenge: DailyChallenge) -> Attempt:
    """Live daily challenge runs on the normal attempt engine, limit 1 (spec 3.7)."""
    assert_student(user, challenge.organization)
    today = today_for_org(challenge.organization)
    if challenge.is_archived or challenge.release_date != today:
        raise PermissionDenied("This challenge is not live today.")
    if DailyParticipation.objects.filter(student=user, challenge=challenge).exists():
        raise PermissionDenied("You have already taken today's challenge.")

    attempt = start_attempt(user, daily_assignment_for(challenge))
    attempt.daily_challenge = challenge
    attempt.save(update_fields=["daily_challenge"])
    return attempt


@transaction.atomic
def start_practice_attempt(user, challenge: DailyChallenge) -> Attempt:
    """Practice on a past challenge.

    Deliberately does *not* set ``daily_challenge`` on the attempt, which is what
    grants streak credit -- archived challenges are practice only (spec 3.7).
    """
    assert_student(user, challenge.organization)
    today = today_for_org(challenge.organization)
    if challenge.release_date >= today:
        raise PermissionDenied("Today's challenge is live; use the live entry instead.")

    assignment, _ = QuizAssignment.objects.get_or_create(
        organization=challenge.organization,
        title=f"Practice: {challenge.title} ({challenge.release_date})",
        defaults={
            "form_mode": QuizAssignment.FormMode.FIXED,
            "quiz_version": challenge.quiz_version,
            "opens_at": timezone.now(),
            "attempt_limit": 0,  # unlimited practice
            "created_by_id": challenge.quiz_version.published_by_id,
            "feedback_policy": Quiz.FeedbackPolicy.AT_END,
        },
    )
    # Pre-F01 rows may still carry attempt_limit=1 from defaults (audit R2-06).
    if assignment.attempt_limit != 0:
        assignment.attempt_limit = 0
        assignment.save(update_fields=["attempt_limit"])
    return start_attempt(user, assignment)


def recompute_streak(student, organization) -> StreakCache:
    dates = list(
        DailyParticipation.objects.filter(
            organization=organization, student=student, earned_credit=True
        )
        .order_by("-credit_date")
        .values_list("credit_date", flat=True)
    )
    # Duplicate credit dates would otherwise reset the run counter.
    unique_dates = sorted(set(dates), reverse=True)

    longest = 0
    run = 0
    previous = None
    for credit_date in unique_dates:
        if previous is None or (previous - credit_date).days == 1:
            run += 1
        else:
            longest = max(longest, run)
            run = 1
        previous = credit_date
    longest = max(longest, run)

    current = 0
    today = today_for_org(organization)
    if unique_dates and (today - unique_dates[0]).days in (0, 1):
        current = 1
        for index in range(1, len(unique_dates)):
            if (unique_dates[index - 1] - unique_dates[index]).days == 1:
                current += 1
            else:
                break

    cache, _ = StreakCache.objects.update_or_create(
        organization=organization,
        student=student,
        defaults={"current_streak": current, "longest_streak": longest},
    )
    return cache
