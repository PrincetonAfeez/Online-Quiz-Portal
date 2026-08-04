"""Organization administration: rosters, people, accommodations, integrity.

Distinct from the Django admin: these are product surfaces an org admin uses
day to day, scoped to their own organization and audited.
"""

from __future__ import annotations

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import OrganizationMembership
from assessments.models import (
    AdminActionLog,
    Attempt,
    Course,
    IntegrityEvent,
    QuestionBank,
    QuestionBankItem,
    QuizAssignment,
    TimingAccommodation,
)
from assessments.ratelimit import rate_limit
from assessments.rosters import commit_roster, parse_roster
from assessments.services import assert_instructor, assert_org_admin
from assessments.tenancy import get_active_organization

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def _org_admin(request):
    organization = get_active_organization(request)
    if organization is None:
        raise PermissionDenied("You do not belong to an organization.")
    assert_org_admin(request.user, organization)
    return organization


def _instructor_org(request):
    organization = get_active_organization(request)
    if organization is None:
        raise PermissionDenied("You do not belong to an organization.")
    assert_instructor(request.user, organization)
    return organization


# ---------------------------------------------------------------------------
# Roster import
# ---------------------------------------------------------------------------


class RosterUploadForm(forms.Form):
    course = forms.ModelChoiceField(
        queryset=Course.objects.none(),
        required=False,
        help_text="Optional. Selecting a course also enrols the students in it.",
    )
    csv_file = forms.FileField(
        label="Roster CSV",
        help_text="Columns: username, email, first_name, last_name, role, cohort.",
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["course"].queryset = Course.objects.filter(
            organization=organization, is_active=True
        )

    def clean_csv_file(self):
        upload = self.cleaned_data["csv_file"]
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("That file is larger than 2 MB.")
        if not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file.")
        raw = upload.read()
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise forms.ValidationError(
                "The file is not UTF-8 text. Re-export it as CSV UTF-8."
            ) from exc


@login_required
@rate_limit("roster_import", limit=20, window_seconds=3600, message="Too many imports.")
def roster_import(request):
    organization = _org_admin(request)
    plan = None
    result = None
    form = RosterUploadForm(organization=organization)

    if request.method == "POST":
        form = RosterUploadForm(request.POST, request.FILES, organization=organization)
        if form.is_valid():
            course = form.cleaned_data["course"]
            plan = parse_roster(form.cleaned_data["csv_file"], organization, course)
            # Two-step by design: preview first, commit only on an explicit
            # second submit that re-uploads the same file.
            if request.POST.get("commit") == "1" and plan.can_commit:
                result = commit_roster(plan, organization, request.user, course)
                messages.success(
                    request,
                    f"Imported {result.created} new and {result.updated} existing "
                    f"account(s); {result.enrolled} enrolment(s) created.",
                )

    return render(
        request,
        "admin_console/roster_import.html",
        {"organization": organization, "form": form, "plan": plan, "result": result},
    )


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@login_required
def people(request):
    organization = _org_admin(request)
    query = (request.GET.get("q") or "").strip()
    role = request.GET.get("role") or ""
    memberships = (
        OrganizationMembership.objects.filter(organization=organization)
        .select_related("user")
        .order_by("user__username")
    )
    if query:
        memberships = memberships.filter(
            Q(user__username__icontains=query)
            | Q(user__email__icontains=query)
            | Q(user__first_name__icontains=query)
            | Q(user__last_name__icontains=query)
        )
    if role:
        memberships = memberships.filter(role=role)

    return render(
        request,
        "admin_console/people.html",
        {
            "organization": organization,
            "memberships": memberships[:200],
            "query": query,
            "role": role,
            "roles": OrganizationMembership.Role.choices,
        },
    )


@login_required
@require_POST
def toggle_membership(request, membership_id: int):
    organization = _org_admin(request)
    membership = get_object_or_404(
        OrganizationMembership, id=membership_id, organization=organization
    )
    if membership.user_id == request.user.id:
        messages.error(request, "You cannot deactivate your own membership.")
        return redirect("people")

    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active"])
    AdminActionLog.objects.create(
        organization=organization,
        actor=request.user,
        action="membership.activated" if membership.is_active else "membership.deactivated",
        target_type="OrganizationMembership",
        target_id=str(membership.id),
        detail={"username": membership.user.username, "role": membership.role},
    )
    messages.success(
        request,
        f"{membership.user.username} is now "
        f"{'active' if membership.is_active else 'inactive'} as {membership.get_role_display()}.",
    )
    return redirect("people")


# ---------------------------------------------------------------------------
# Timing accommodations
# ---------------------------------------------------------------------------


class AccommodationForm(forms.ModelForm):
    username = forms.CharField(
        help_text="The student's username. They must already be a member of this organization."
    )

    class Meta:
        model = TimingAccommodation
        fields = [
            "course",
            "cohort",
            "multiplier",
            "extra_minutes",
            "reason",
            "is_active",
            "effective_from",
            "effective_until",
        ]
        widgets = {
            "effective_from": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "effective_until": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
        help_texts = {
            "multiplier": "1.50 gives 50% extra time; combined with extra minutes.",
            "reason": "Recorded for audit. Never shown to the student.",
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["course"].queryset = Course.objects.filter(organization=organization)
        self.fields["cohort"].required = False
        self.fields["course"].required = False
        from assessments.models import Cohort

        self.fields["cohort"].queryset = Cohort.objects.filter(organization=organization)
        for name in ("effective_from", "effective_until"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        membership = (
            OrganizationMembership.objects.filter(
                organization=self.organization, user__username=username, is_active=True
            )
            .select_related("user")
            .first()
        )
        if membership is None:
            raise forms.ValidationError(
                f"{username!r} is not an active member of this organization."
            )
        self.student = membership.user
        return username

    def clean_multiplier(self):
        multiplier = self.cleaned_data["multiplier"]
        if multiplier <= 0:
            raise forms.ValidationError("The multiplier must be greater than zero.")
        if multiplier > 10:
            raise forms.ValidationError("A multiplier above 10x is almost certainly a typo.")
        return multiplier


@login_required
def accommodations(request):
    organization = _instructor_org(request)
    rows = (
        TimingAccommodation.objects.filter(organization=organization)
        .select_related("student", "course", "cohort")
        .order_by("student__username")
    )
    form = AccommodationForm(organization=organization)
    if request.method == "POST":
        form = AccommodationForm(request.POST, organization=organization)
        if form.is_valid():
            accommodation = form.save(commit=False)
            accommodation.organization = organization
            accommodation.student = form.student
            accommodation.save()
            AdminActionLog.objects.create(
                organization=organization,
                actor=request.user,
                action="accommodation.created",
                target_type="TimingAccommodation",
                target_id=str(accommodation.id),
                # The clinical reason is deliberately not copied into the log.
                detail={
                    "student": accommodation.student.username,
                    "multiplier": str(accommodation.multiplier),
                    "extra_minutes": accommodation.extra_minutes,
                },
            )
            messages.success(
                request,
                "Accommodation saved. It applies to attempts started from now.",
            )
            return redirect("accommodations")

    return render(
        request,
        "admin_console/accommodations.html",
        {"organization": organization, "rows": rows, "form": form},
    )


@login_required
@require_POST
def revoke_accommodation(request, accommodation_id: int):
    organization = _instructor_org(request)
    accommodation = get_object_or_404(
        TimingAccommodation, id=accommodation_id, organization=organization
    )
    accommodation.is_active = False
    accommodation.save(update_fields=["is_active"])
    AdminActionLog.objects.create(
        organization=organization,
        actor=request.user,
        action="accommodation.revoked",
        target_type="TimingAccommodation",
        target_id=str(accommodation.id),
        detail={"student": accommodation.student.username},
    )
    messages.success(request, "Accommodation revoked. Attempts already started keep their time.")
    return redirect("accommodations")


# ---------------------------------------------------------------------------
# Integrity monitoring
# ---------------------------------------------------------------------------


@login_required
def integrity_dashboard(request):
    organization = _instructor_org(request)
    assignment_id = request.GET.get("assignment") or ""
    events = (
        IntegrityEvent.objects.filter(organization=organization)
        .select_related("attempt", "attempt__student", "attempt__assignment")
        .order_by("-created_at")
    )
    if assignment_id.isdigit():
        events = events.filter(attempt__assignment_id=int(assignment_id))

    summary = (
        IntegrityEvent.objects.filter(organization=organization)
        .values("kind")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    labels = dict(IntegrityEvent.Kind.choices)
    return render(
        request,
        "admin_console/integrity.html",
        {
            "organization": organization,
            "events": events[:100],
            "summary": [
                {"label": labels.get(row["kind"], row["kind"]), "total": row["total"]}
                for row in summary
            ],
            "assignments": QuizAssignment.objects.filter(organization=organization).order_by(
                "-opens_at"
            )[:50],
            "selected_assignment": assignment_id,
            "voided": Attempt.objects.filter(
                organization=organization, status=Attempt.Status.VOIDED_BY_ADMIN
            ).count(),
        },
    )


@login_required
def admin_activity(request):
    """Who changed what. Read-only by construction."""
    organization = _org_admin(request)
    entries = (
        AdminActionLog.objects.filter(organization=organization)
        .select_related("actor")
        .order_by("-created_at")[:200]
    )
    return render(
        request,
        "admin_console/activity.html",
        {"organization": organization, "entries": entries},
    )


# ---------------------------------------------------------------------------
# Question bank workspace
# ---------------------------------------------------------------------------


@login_required
def question_bank(request):
    organization = _instructor_org(request)
    query = (request.GET.get("q") or "").strip()
    difficulty = request.GET.get("difficulty") or ""
    bank_id = request.GET.get("bank") or ""
    status = request.GET.get("status") or ""

    items = (
        QuestionBankItem.objects.filter(organization=organization)
        .select_related("bank", "learning_objective")
        .prefetch_related("tags")
        .order_by("-created_at")
    )
    if query:
        items = items.filter(Q(prompt__icontains=query) | Q(source_name__icontains=query))
    if difficulty:
        items = items.filter(difficulty=difficulty)
    if bank_id.isdigit():
        items = items.filter(bank_id=int(bank_id))
    if status:
        items = items.filter(review_status=status)

    return render(
        request,
        "admin_console/question_bank.html",
        {
            "organization": organization,
            "items": items[:100],
            "total": items.count(),
            "banks": QuestionBank.objects.filter(organization=organization),
            "difficulties": QuestionBankItem.Difficulty.choices,
            "query": query,
            "difficulty": difficulty,
            "bank_id": bank_id,
            "status": status,
            "now": timezone.now(),
        },
    )
