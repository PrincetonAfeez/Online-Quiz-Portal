"""URL routes for student, instructor, and org-admin surfaces."""

from django.urls import path

from assessments import views
from assessments import views_admin as org_admin
from assessments import views_instructor as instructor

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("organization/switch/", views.switch_organization, name="switch_organization"),
    path("instructor/", views.instructor_dashboard, name="instructor_dashboard"),
    path(
        "instructor/assignments/<int:assignment_id>/release/",
        views.toggle_result_release,
        name="toggle_result_release",
    ),
    path("assignments/<int:assignment_id>/start/", views.start_assignment, name="start_assignment"),
    path("attempts/<int:attempt_id>/", views.attempt_detail, name="attempt_detail"),
    path("attempts/<int:attempt_id>/save/<int:item_id>/", views.save_answer, name="save_answer"),
    path("attempts/<int:attempt_id>/submit/", views.submit_attempt, name="submit_attempt"),
    path("attempts/<int:attempt_id>/result/", views.result_detail, name="result_detail"),
    path("daily/", views.daily_challenge, name="daily_challenge"),
    path(
        "daily/<int:challenge_id>/start/",
        views.start_daily_challenge,
        name="start_daily_challenge",
    ),
    path("notifications/", views.notifications, name="notifications"),
    path(
        "notifications/read/",
        views.mark_notifications_read,
        name="mark_notifications_read",
    ),
    # Instructor workspace
    path("instructor/quizzes/", instructor.quiz_list, name="quiz_list"),
    path("instructor/quizzes/new/", instructor.quiz_create, name="quiz_create"),
    path("instructor/quizzes/<int:quiz_id>/", instructor.quiz_detail, name="quiz_detail"),
    path("instructor/quizzes/<int:quiz_id>/edit/", instructor.quiz_edit, name="quiz_edit"),
    path(
        "instructor/quizzes/<int:quiz_id>/questions/new/",
        instructor.question_edit,
        name="question_create",
    ),
    path(
        "instructor/quizzes/<int:quiz_id>/questions/<int:question_id>/",
        instructor.question_edit,
        name="question_edit",
    ),
    path(
        "instructor/quizzes/<int:quiz_id>/questions/<int:question_id>/delete/",
        instructor.question_delete,
        name="question_delete",
    ),
    path(
        "instructor/quizzes/<int:quiz_id>/publish/",
        instructor.quiz_publish,
        name="quiz_publish",
    ),
    path(
        "instructor/versions/<int:version_id>/preview/",
        instructor.version_preview,
        name="version_preview",
    ),
    path("instructor/assignments/", instructor.assignment_list, name="assignment_list"),
    path(
        "instructor/assignments/new/",
        instructor.assignment_edit,
        name="assignment_create",
    ),
    path(
        "instructor/assignments/<int:assignment_id>/edit/",
        instructor.assignment_edit,
        name="assignment_edit",
    ),
    path("instructor/pools/", instructor.pool_list, name="pool_list"),
    path("instructor/pools/new/", instructor.pool_edit, name="pool_create"),
    path("instructor/pools/<int:pool_id>/edit/", instructor.pool_edit, name="pool_edit"),
    path(
        "instructor/assignments/<int:assignment_id>/gradebook/",
        instructor.gradebook,
        name="gradebook",
    ),
    path(
        "instructor/assignments/<int:assignment_id>/analytics/",
        instructor.assignment_analytics_view,
        name="assignment_analytics",
    ),
    path(
        "instructor/assignments/<int:assignment_id>/export/",
        instructor.export_gradebook,
        name="export_gradebook",
    ),
    path(
        "instructor/attempts/<int:attempt_id>/void/",
        instructor.void_attempt_view,
        name="void_attempt",
    ),
    path("progress/", views.my_progress, name="my_progress"),
    path(
        "daily/<int:challenge_id>/practice/",
        views.start_practice_challenge,
        name="start_practice_challenge",
    ),
    path(
        "instructor/quizzes/<int:quiz_id>/bank/<int:item_id>/add/",
        instructor.add_bank_item_to_quiz,
        name="add_bank_item_to_quiz",
    ),
    # Organization administration
    path("manage/roster/", org_admin.roster_import, name="roster_import"),
    path("manage/people/", org_admin.people, name="people"),
    path(
        "manage/people/<int:membership_id>/toggle/",
        org_admin.toggle_membership,
        name="toggle_membership",
    ),
    path("manage/accommodations/", org_admin.accommodations, name="accommodations"),
    path(
        "manage/accommodations/<int:accommodation_id>/revoke/",
        org_admin.revoke_accommodation,
        name="revoke_accommodation",
    ),
    path("manage/integrity/", org_admin.integrity_dashboard, name="integrity_dashboard"),
    path("manage/activity/", org_admin.admin_activity, name="admin_activity"),
    path("manage/question-bank/", org_admin.question_bank, name="question_bank"),
    path("legal/terms/", views.terms, name="terms"),
    path("legal/privacy/", views.privacy, name="privacy"),
    path("healthz/", views.healthz, name="healthz"),
    path("readyz/", views.readyz, name="readyz"),
]
