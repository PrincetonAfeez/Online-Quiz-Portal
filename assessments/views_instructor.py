"""Instructor workspace: authoring, assignments, gradebook, analytics, exports.

Every view resolves the active organization first and authorizes against it, so
authorization and the query always concern the same tenant.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from assessments.analytics import assignment_analytics, gradebook_rows
from assessments.exports import gradebook_csv, item_analysis_csv, record_export
from assessments.forms import (
    AssignmentForm,
    ChoiceFormSet,
    ItemPoolForm,
    ItemPoolItemFormSet,
    QuestionForm,
    QuizForm,
)
from assessments.models import (
    Attempt,
    Choice,
    ItemPool,
    Question,
    QuestionBankItem,
    Quiz,
    QuizAssignment,
    QuizVersion,
)
from assessments.ratelimit import rate_limit
from assessments.services import (
    assert_instructor,
    effective_pass_mark,
    publish_quiz,
    void_attempt,
)
from assessments.tenancy import get_active_organization

logger = logging.getLogger(__name__)


def _instructor_org(request):
    organization = get_active_organization(request)
    if organization is None:
        raise PermissionDenied("You do not belong to an organization.")
    assert_instructor(request.user, organization)
    return organization


def _own_quiz(request, organization, quiz_id):
    return get_object_or_404(
        Quiz.objects.for_user(request.user).filter(organization=organization), id=quiz_id
    )


# ---------------------------------------------------------------------------
# Quiz authoring
# ---------------------------------------------------------------------------


@login_required
def quiz_list(request):
    organization = _instructor_org(request)
    quizzes = (
        Quiz.objects.for_user(request.user)
        .filter(organization=organization)
        .annotate(
            question_count=Count("questions", distinct=True),
            version_count=Count("versions", distinct=True),
        )
        .order_by("-created_at")
    )
    return render(
        request,
        "instructor/quiz_list.html",
        {"organization": organization, "quizzes": quizzes},
    )


@login_required
def quiz_create(request):
    organization = _instructor_org(request)
    form = QuizForm(request.POST or None, organization=organization)
    if request.method == "POST" and form.is_valid():
        quiz = form.save(commit=False)
        quiz.organization = organization
        quiz.created_by = request.user
        quiz.save()
        messages.success(request, f"Created “{quiz.title}”. Add questions next.")
        return redirect("quiz_detail", quiz_id=quiz.id)
    return render(
        request,
        "instructor/quiz_form.html",
        {"organization": organization, "form": form, "is_create": True},
    )


@login_required
def quiz_detail(request, quiz_id: int):
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    questions = quiz.questions.prefetch_related("choices").order_by("sort_order", "id")
    versions = quiz.versions.order_by("-version_number")
    # Show the same readiness rules publishing enforces, before they hit Publish.
    problems = _publish_readiness(questions)
    return render(
        request,
        "instructor/quiz_detail.html",
        {
            "organization": organization,
            "quiz": quiz,
            "questions": questions,
            "versions": versions,
            "problems": problems,
            "can_publish": not problems and questions.exists(),
        },
    )


def _next_sort_order(quiz) -> int:
    last = quiz.questions.order_by("-sort_order").values_list("sort_order", flat=True).first()
    return (last or 0) + 1


def _renumber_choices(question) -> None:
    for index, choice in enumerate(question.choices.order_by("sort_order", "id"), start=1):
        if choice.sort_order != index:
            choice.sort_order = index
            choice.save(update_fields=["sort_order"])


def _publish_readiness(questions) -> list[str]:
    problems = []
    for index, question in enumerate(questions, start=1):
        choices = list(question.choices.all())
        correct = [c for c in choices if c.is_correct]
        if len(choices) < 2:
            problems.append(f"Question {index} has fewer than two choices.")
        if question.question_type == "true_false" and len(choices) != 2:
            problems.append(f"Question {index} is true/false but has {len(choices)} choices.")
        if question.question_type in ("single", "true_false") and len(correct) != 1:
            problems.append(f"Question {index} must have exactly one correct choice.")
        if question.question_type == "multi":
            if not correct:
                problems.append(f"Question {index} has no correct choice.")
            elif len(correct) == len(choices):
                problems.append(f"Question {index} marks every choice correct.")
    return problems


@login_required
def quiz_edit(request, quiz_id: int):
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    form = QuizForm(request.POST or None, instance=quiz, organization=organization)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Quiz updated. Publish a new version to release the change.")
        return redirect("quiz_detail", quiz_id=quiz.id)
    return render(
        request,
        "instructor/quiz_form.html",
        {"organization": organization, "form": form, "quiz": quiz, "is_create": False},
    )


@login_required
def question_edit(request, quiz_id: int, question_id: int | None = None):
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    if question_id:
        question = get_object_or_404(Question, id=question_id, quiz=quiz)
    else:
        question = Question(quiz=quiz)

    if request.method == "POST":
        form = QuestionForm(request.POST, instance=question)
        if form.is_valid():
            # The formset validates against the submitted question type, so bind
            # it to the unsaved instance rather than the stored one.
            question = form.save(commit=False)
            question.quiz = quiz
            formset = ChoiceFormSet(request.POST, instance=question)
            if formset.is_valid():
                if question.pk is None:
                    question.sort_order = _next_sort_order(quiz)
                question.save()
                formset.instance = question
                choices = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                # Presentation order follows the order the rows were entered.
                for index, choice in enumerate(choices, start=1):
                    choice.sort_order = index
                    choice.save()
                _renumber_choices(question)
                messages.success(request, "Question saved.")
                return redirect("quiz_detail", quiz_id=quiz.id)
        else:
            formset = ChoiceFormSet(request.POST, instance=question)
    else:
        form = QuestionForm(instance=question)
        formset = ChoiceFormSet(instance=question)

    return render(
        request,
        "instructor/question_form.html",
        {
            "organization": organization,
            "quiz": quiz,
            "form": form,
            "formset": formset,
            "question": question if question_id else None,
        },
    )


@login_required
@require_POST
def question_delete(request, quiz_id: int, question_id: int):
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    question = get_object_or_404(Question, id=question_id, quiz=quiz)
    question.delete()
    messages.success(request, "Question removed from the draft.")
    return redirect("quiz_detail", quiz_id=quiz.id)


@login_required
@require_POST
def quiz_publish(request, quiz_id: int):
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    try:
        version = publish_quiz(quiz, request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("quiz_detail", quiz_id=quiz.id)
    messages.success(
        request,
        f"Published version {version.version_number}. Existing attempts are unaffected.",
    )
    return redirect("quiz_detail", quiz_id=quiz.id)


@login_required
def version_preview(request, version_id: int):
    """See exactly what a student will be served, answer key included."""
    organization = _instructor_org(request)
    version = get_object_or_404(
        QuizVersion.objects.for_user(request.user).filter(organization=organization),
        id=version_id,
    )
    questions = version.version_questions.prefetch_related("version_choices")
    return render(
        request,
        "instructor/version_preview.html",
        {"organization": organization, "version": version, "questions": questions},
    )


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


@login_required
def assignment_list(request):
    organization = _instructor_org(request)
    assignments = (
        QuizAssignment.objects.for_user(request.user)
        .filter(organization=organization)
        .select_related("quiz_version", "item_pool", "course", "cohort", "result_release")
        .annotate(
            attempt_total=Count("attempts", distinct=True),
            finalized_total=Count(
                "attempts",
                filter=Q(attempts__status__in=Attempt.SCORED_STATUSES),
                distinct=True,
            ),
        )
        .order_by("-opens_at")
    )
    return render(
        request,
        "instructor/assignment_list.html",
        {"organization": organization, "assignments": assignments, "now": timezone.now()},
    )


@login_required
def assignment_edit(request, assignment_id: int | None = None):
    organization = _instructor_org(request)
    instance = None
    if assignment_id:
        instance = get_object_or_404(
            QuizAssignment.objects.for_user(request.user).filter(organization=organization),
            id=assignment_id,
        )
    form = AssignmentForm(request.POST or None, instance=instance, organization=organization)
    if request.method == "POST" and form.is_valid():
        assignment = form.save(commit=False)
        assignment.organization = organization
        if instance is None:
            assignment.created_by = request.user
        try:
            assignment.full_clean(exclude=["created_by"])
        except ValidationError as exc:
            for field, errors in exc.message_dict.items():
                target = field if field in form.fields else None
                for error in errors:
                    form.add_error(target, error)
        else:
            assignment.save()
            form.save_m2m()
            messages.success(request, f"Saved “{assignment.title}”.")
            return redirect("assignment_list")
    return render(
        request,
        "instructor/assignment_form.html",
        {"organization": organization, "form": form, "assignment": instance},
    )


# ---------------------------------------------------------------------------
# Gradebook, analytics, exports
# ---------------------------------------------------------------------------


@login_required
def gradebook(request, assignment_id: int):
    organization = _instructor_org(request)
    assignment = get_object_or_404(
        QuizAssignment.objects.for_user(request.user).filter(organization=organization),
        id=assignment_id,
    )
    pass_mark = effective_pass_mark(assignment)
    rows = gradebook_rows(assignment, pass_mark)
    return render(
        request,
        "instructor/gradebook.html",
        {
            "organization": organization,
            "assignment": assignment,
            "rows": rows,
            "pass_mark": pass_mark,
        },
    )


@login_required
def assignment_analytics_view(request, assignment_id: int):
    organization = _instructor_org(request)
    assignment = get_object_or_404(
        QuizAssignment.objects.for_user(request.user).filter(organization=organization),
        id=assignment_id,
    )
    analytics = assignment_analytics(assignment, effective_pass_mark(assignment))
    max_bucket = max((count for _, count in analytics.distribution), default=0) or 1
    bars = [
        {"label": label, "count": count, "percent": int(count * 100 / max_bucket)}
        for label, count in analytics.distribution
    ]
    return render(
        request,
        "instructor/analytics.html",
        {
            "organization": organization,
            "assignment": assignment,
            "analytics": analytics,
            "bars": bars,
            "flagged": [item for item in analytics.items if item.flags],
        },
    )


@login_required
@rate_limit("export", limit=20, window_seconds=3600, message="Too many exports.")
def export_gradebook(request, assignment_id: int):
    organization = _instructor_org(request)
    assignment = get_object_or_404(
        QuizAssignment.objects.for_user(request.user).filter(organization=organization),
        id=assignment_id,
    )
    kind = request.GET.get("kind", "gradebook")
    if kind == "items":
        payload = item_analysis_csv(assignment)
        name = f"item-analysis-{assignment.id}.csv"
    else:
        payload = gradebook_csv(assignment)
        name = f"gradebook-{assignment.id}.csv"

    record_export(assignment, request.user, name, payload)
    response = HttpResponse(payload, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    # A gradebook must not sit in a shared cache.
    response["Cache-Control"] = "no-store, private"
    return response


@login_required
@require_POST
def void_attempt_view(request, attempt_id: int):
    organization = _instructor_org(request)
    attempt = get_object_or_404(
        Attempt.objects.for_user(request.user).filter(organization=organization), id=attempt_id
    )
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "A reason is required to void an attempt.")
    else:
        void_attempt(attempt, request.user, reason=reason)
        messages.success(request, "Attempt voided. The action was audited.")
    return redirect("gradebook", assignment_id=attempt.assignment_id)


@login_required
@require_POST
def add_bank_item_to_quiz(request, quiz_id: int, item_id: int):
    """Copy an approved bank item into a draft.

    A copy, never a reference: the bank is a source of reusable material, the
    draft is what gets published, and editing one must not silently rewrite the
    other (ADR-0010).
    """
    organization = _instructor_org(request)
    quiz = _own_quiz(request, organization, quiz_id)
    item = get_object_or_404(
        QuestionBankItem.objects.filter(organization=organization), id=item_id
    )
    if item.review_status != "approved" or not item.reuse_allowed:
        messages.error(request, "That item is not approved for reuse.")
        return redirect("question_bank")

    question = Question.objects.create(
        quiz=quiz,
        bank_item=item,
        prompt=item.prompt,
        question_type=item.question_type,
        points=item.points,
        explanation=item.explanation,
        sort_order=_next_sort_order(quiz),
    )
    for index, choice in enumerate(item.choices.order_by("sort_order", "id"), start=1):
        Choice.objects.create(
            question=question,
            text=choice.text,
            is_correct=choice.is_correct,
            sort_order=index,
        )
    messages.success(request, f"Added to “{quiz.title}”.")
    return redirect("quiz_detail", quiz_id=quiz.id)


# ---------------------------------------------------------------------------
# Item pools / blueprints (audit F14)
# ---------------------------------------------------------------------------


@login_required
def pool_list(request):
    organization = _instructor_org(request)
    pools = (
        ItemPool.objects.for_user(request.user)
        .filter(organization=organization)
        .annotate(item_count=Count("items", distinct=True))
        .order_by("name")
    )
    return render(
        request,
        "instructor/pool_list.html",
        {"organization": organization, "pools": pools},
    )


@login_required
def pool_edit(request, pool_id: int | None = None):
    organization = _instructor_org(request)
    instance = None
    if pool_id:
        instance = get_object_or_404(
            ItemPool.objects.for_user(request.user).filter(organization=organization),
            id=pool_id,
        )

    staging = instance or ItemPool(organization=organization, created_by=request.user)
    if request.method == "POST":
        form = ItemPoolForm(request.POST, instance=instance, organization=organization)
        formset = ItemPoolItemFormSet(
            request.POST, instance=staging, organization=organization
        )
        # Validate both before persisting so a bad formset cannot leave an
        # orphan pool or a half-updated metadata row (audit R2-04).
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    pool = form.save(commit=False)
                    pool.organization = organization
                    if instance is None:
                        pool.created_by = request.user
                    pool.full_clean()
                    pool.save()
                    formset.instance = pool
                    formset.save()
            except ValidationError as exc:
                for field, errors in exc.message_dict.items():
                    target = field if field in form.fields else None
                    for error in errors:
                        form.add_error(target, error)
            else:
                messages.success(request, f"Saved pool “{pool.name}”.")
                return redirect("pool_list")
    else:
        form = ItemPoolForm(instance=instance, organization=organization)
        formset = ItemPoolItemFormSet(instance=staging, organization=organization)

    return render(
        request,
        "instructor/pool_form.html",
        {
            "organization": organization,
            "form": form,
            "formset": formset,
            "pool": instance,
        },
    )
