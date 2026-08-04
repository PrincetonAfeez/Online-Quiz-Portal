"""Roster import.

Onboarding a cohort by hand is the practical blocker to adoption: a 200-student
course is 200 admin forms. This parses a CSV, previews exactly what will change,
and only then commits — so an administrator never discovers a bad column mapping
by finding 200 wrong accounts.
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from accounts.models import OrganizationMembership
from assessments.models import AdminActionLog, Course, Enrollment

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"username", "email"}
OPTIONAL_COLUMNS = {"first_name", "last_name", "role", "cohort"}
MAX_ROWS = 5000

# Administrator rights are deliberately not grantable from a spreadsheet: a
# mistyped column would hand over the organization. Promote in the People UI,
# where the action is explicit and individually audited.
IMPORTABLE_ROLES = {
    OrganizationMembership.Role.STUDENT,
    OrganizationMembership.Role.INSTRUCTOR,
}


@dataclass
class RosterRow:
    line: int
    username: str
    email: str
    first_name: str = ""
    last_name: str = ""
    role: str = OrganizationMembership.Role.STUDENT
    cohort: str = ""
    action: str = "create"          # create | update | skip
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class RosterPlan:
    rows: list[RosterRow]
    header_errors: list[str] = field(default_factory=list)

    @property
    def valid_rows(self) -> list[RosterRow]:
        return [row for row in self.rows if row.is_valid]

    @property
    def invalid_rows(self) -> list[RosterRow]:
        return [row for row in self.rows if not row.is_valid]

    @property
    def create_count(self) -> int:
        return sum(1 for row in self.valid_rows if row.action == "create")

    @property
    def update_count(self) -> int:
        return sum(1 for row in self.valid_rows if row.action == "update")

    @property
    def can_commit(self) -> bool:
        return not self.header_errors and bool(self.valid_rows)


def parse_roster(text: str, organization, course: Course | None = None) -> RosterPlan:
    """Validate a roster without writing anything."""
    User = get_user_model()
    plan = RosterPlan(rows=[])

    # Strip a BOM: Excel writes one and it corrupts the first header name.
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        plan.header_errors.append("The file is empty.")
        return plan

    headers = {(name or "").strip().lower() for name in reader.fieldnames}
    missing = REQUIRED_COLUMNS - headers
    if missing:
        plan.header_errors.append(
            f"Missing required column(s): {', '.join(sorted(missing))}. "
            f"Expected: {', '.join(sorted(REQUIRED_COLUMNS | OPTIONAL_COLUMNS))}."
        )
        return plan

    cohort_names = {}
    if course is not None:
        cohort_names = {c.name.lower(): c for c in course.cohorts.all()}

    seen_usernames: set[str] = set()
    seen_emails: set[str] = set()

    # Read every row first, then resolve existing accounts in two queries. The
    # per-row lookups this replaces cost two queries each -- 10,000 for a
    # full-size import, all of them in the preview the admin is waiting on.
    raw_rows = []
    for raw in reader:
        if len(raw_rows) >= MAX_ROWS:
            plan.header_errors.append(
                f"File exceeds the {MAX_ROWS}-row limit; split it into batches."
            )
            break
        raw_rows.append(raw)

    usernames = {(r.get("username") or "").strip() for r in raw_rows}
    emails = {(r.get("email") or "").strip().lower() for r in raw_rows}
    existing_by_username = {
        u.username: u for u in User.objects.filter(username__in=[u for u in usernames if u])
    }
    email_owner = {
        (email or "").lower(): pk
        for pk, email in User.objects.filter(
            email__in=[e for e in emails if e]
        ).values_list("pk", "email")
    }

    for index, raw in enumerate(raw_rows, start=2):  # line 1 is the header
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        row = RosterRow(
            line=index,
            username=clean.get("username", ""),
            email=clean.get("email", ""),
            first_name=clean.get("first_name", ""),
            last_name=clean.get("last_name", ""),
            role=clean.get("role") or OrganizationMembership.Role.STUDENT,
            cohort=clean.get("cohort", ""),
        )

        if not row.username:
            row.errors.append("username is required")
        if not row.email:
            row.errors.append("email is required")
        else:
            try:
                validate_email(row.email)
            except ValidationError:
                row.errors.append(f"{row.email!r} is not a valid email address")

        if row.role not in IMPORTABLE_ROLES:
            row.errors.append(
                f"role {row.role!r} cannot be set by import; use "
                f"{' or '.join(sorted(IMPORTABLE_ROLES))}"
            )

        lowered_user = row.username.lower()
        if lowered_user in seen_usernames:
            row.errors.append("duplicate username in this file")
        seen_usernames.add(lowered_user)

        lowered_email = row.email.lower()
        if lowered_email and lowered_email in seen_emails:
            row.errors.append("duplicate email in this file")
        seen_emails.add(lowered_email)

        if row.cohort:
            if course is None:
                row.errors.append("a cohort column needs a course to be selected")
            elif row.cohort.lower() not in cohort_names:
                row.errors.append(f"cohort {row.cohort!r} does not exist in this course")

        existing = existing_by_username.get(row.username)
        owner_pk = email_owner.get(lowered_email)
        if existing:
            row.action = "update"
            # An email already used by a *different* account would break the
            # unique-when-present constraint at commit time.
            if row.email and owner_pk is not None and owner_pk != existing.pk:
                row.errors.append("email already belongs to another account")
        elif row.email and owner_pk is not None:
            row.errors.append("email already belongs to another account")

        plan.rows.append(row)

    if not plan.rows and not plan.header_errors:
        plan.header_errors.append("The file has a header but no rows.")
    return plan


@dataclass
class RosterResult:
    created: int = 0
    updated: int = 0
    enrolled: int = 0
    skipped: int = 0
    credentials: list[tuple[str, str]] = field(default_factory=list)


@transaction.atomic
def commit_roster(
    plan: RosterPlan, organization, actor, course: Course | None = None
) -> RosterResult:
    """Apply a validated plan. Invalid rows are skipped, never guessed at."""
    User = get_user_model()
    result = RosterResult(skipped=len(plan.invalid_rows))
    cohorts = {}
    if course is not None:
        cohorts = {c.name.lower(): c for c in course.cohorts.all()}

    for row in plan.valid_rows:
        user = User.objects.filter(username=row.username).first()
        if user is None:
            # A random unusable-until-reset password: never a predictable
            # default, and never mailed in plaintext by this code path.
            password = secrets.token_urlsafe(12)
            user = User.objects.create_user(
                username=row.username,
                email=row.email,
                password=password,
                first_name=row.first_name,
                last_name=row.last_name,
            )
            result.created += 1
            result.credentials.append((row.username, password))
        else:
            changed = False
            for attr, value in (
                ("email", row.email),
                ("first_name", row.first_name),
                ("last_name", row.last_name),
            ):
                if value and getattr(user, attr) != value:
                    setattr(user, attr, value)
                    changed = True
            if changed:
                user.save()
            result.updated += 1

        OrganizationMembership.objects.get_or_create(
            user=user,
            organization=organization,
            role=row.role,
            defaults={"is_active": True},
        )

        if course is not None and row.role == OrganizationMembership.Role.STUDENT:
            cohort = cohorts.get(row.cohort.lower()) if row.cohort else None
            _, created = Enrollment.objects.get_or_create(
                organization=organization,
                student=user,
                course=course,
                cohort=cohort,
                defaults={"is_active": True},
            )
            if created:
                result.enrolled += 1

    AdminActionLog.objects.create(
        organization=organization,
        actor=actor,
        action="roster.import",
        target_type="Course",
        target_id=str(course.id) if course else "",
        detail={
            "created": result.created,
            "updated": result.updated,
            "enrolled": result.enrolled,
            "skipped": result.skipped,
        },
    )
    logger.info(
        "roster imported",
        extra={
            "organization_id": organization.id,
            "user_id": getattr(actor, "id", None),
        },
    )
    return result
