"""Create throwaway accounts for ops/loadtest.py. Staging only."""

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from accounts.models import Organization, OrganizationMembership, User  # noqa: E402
from assessments.models import Cohort, Course, Enrollment  # noqa: E402

COUNT = int(os.environ.get("LOADTEST_USERS", "25"))
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "load-test-phrase-1")

org = Organization.objects.get(slug=os.environ.get("LOADTEST_ORG", "demo-academy"))
course = Course.objects.filter(organization=org).first()
# Enrol into the course's cohort: an assignment scoped to a cohort only admits
# students enrolled in that cohort, so a course-only enrolment is not eligible.
cohort = Cohort.objects.filter(organization=org, course=course).first() if course else None

for index in range(COUNT):
    user, _ = User.objects.get_or_create(
        username=f"load{index}", defaults={"email": f"load{index}@loadtest.invalid"}
    )
    user.set_password(PASSWORD)
    user.save()
    OrganizationMembership.objects.get_or_create(
        user=user, organization=org, role=OrganizationMembership.Role.STUDENT
    )
    if course:
        Enrollment.objects.get_or_create(
            organization=org, student=user, course=course, cohort=cohort
        )
print(f"Seeded {COUNT} load-test students in {org.slug}.")
