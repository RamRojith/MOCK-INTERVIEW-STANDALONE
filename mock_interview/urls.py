from django.urls import path

from .views import ats as ats_views
from .views import dashboard as views
from .views import faculty
from .views import hod
from .views import roster_dashboard

app_name = "mock_interview"

urlpatterns = [
    # ── Student Interview Flow (existing) ────────────────────────────
    path("", views.module_entry, name="dashboard"),
    path("resume/upload/", views.upload_resume, name="upload_resume"),
    path("setup/", views.setup, name="setup"),
    path(
        "session/<uuid:public_id>/device-check/",
        views.device_check,
        name="device_check",
    ),
    path(
        "session/<uuid:public_id>/instructions/",
        views.instructions,
        name="instructions",
    ),
    path("session/<uuid:public_id>/", views.room, name="room"),
    path(
        "session/<uuid:public_id>/processing/",
        views.processing,
        name="processing",
    ),
    path(
        "session/<uuid:public_id>/report/",
        views.report,
        name="report",
    ),
    path(
        "api/questions/<uuid:public_id>/transcribe/",
        views.transcribe_answer,
        name="transcribe_answer",
    ),
    path(
        "api/answers/<uuid:public_id>/submit/",
        views.submit_answer_view,
        name="submit_answer",
    ),
    path(
        "api/questions/<uuid:public_id>/skip/",
        views.skip_question_view,
        name="skip_question",
    ),
    path(
        "api/questions/<uuid:public_id>/audio/",
        views.question_audio,
        name="question_audio",
    ),
    path(
        "api/sessions/<uuid:public_id>/end/",
        views.end_interview,
        name="end_interview",
    ),
    path(
        "api/sessions/<uuid:public_id>/status/",
        views.session_status,
        name="session_status",
    ),
    path(
        "api/sessions/<uuid:public_id>/focus-violation/",
        views.record_focus_violation,
        name="focus_violation",
    ),
    path(
        "api/sessions/<uuid:public_id>/delete/",
        views.delete_session,
        name="delete_session",
    ),
    path(
        "api/sessions/<uuid:public_id>/generate-report/",
        views.generate_report,
        name="generate_report",
    ),
    path("api/runtime/status/", views.runtime_status, name="runtime_status"),
    path(
        "api/runtime/tts/sample/",
        views.tts_sample,
        name="runtime_tts_sample",
    ),
    path(
        "api/sessions/<uuid:public_id>/report.pdf",
        views.report_pdf,
        name="report_pdf",
    ),

    # ── Standalone ATS Resume Checker ──────────────────────────────────
    path("ats/", ats_views.ats_checker, name="ats_checker"),
    path(
        "ats/checks/<uuid:public_id>/",
        ats_views.ats_report,
        name="ats_report",
    ),
    path(
        "api/ats/checks/<uuid:public_id>/",
        ats_views.ats_check_detail,
        name="ats_check_detail",
    ),
    path(
        "api/ats/checks/<uuid:public_id>/download.docx",
        ats_views.ats_download_docx,
        name="ats_download_docx",
    ),
    path(
        "api/ats/checks/<uuid:public_id>/download.pdf",
        ats_views.ats_download_pdf,
        name="ats_download_pdf",
    ),

    # ── Faculty Document & Interview Management ──────────────────────
    path("faculty/", faculty.faculty_dashboard, name="faculty_dashboard"),
    path("faculty/upload/", faculty.upload_document_page, name="upload_document_page"),
    path("faculty/api/upload/", faculty.upload_document_api, name="upload_document_api"),
    path(
        "faculty/api/documents/<uuid:document_id>/delete/",
        faculty.delete_document_api,
        name="delete_document_api",
    ),
    path("faculty/api/status/", faculty.runtime_status_api, name="faculty_runtime_status"),
    path(
        "faculty/interview/create/",
        faculty.create_interview_page,
        name="create_interview_page",
    ),
    path(
        "faculty/api/interview/create/",
        faculty.create_interview_api,
        name="create_interview_api",
    ),
    path(
        "faculty/api/interview/<uuid:interview_id>/schedule/",
        faculty.schedule_interview_api,
        name="schedule_interview_api",
    ),
    path(
        "faculty/api/interview/<uuid:interview_id>/assign/",
        faculty.assign_interview_api,
        name="assign_interview_api",
    ),
    path(
        "faculty/api/interview/<uuid:interview_id>/delete/",
        faculty.delete_interview_api,
        name="delete_interview_api",
    ),
    path(
        "faculty/interview/<uuid:interview_id>/",
        faculty.interview_detail_page,
        name="interview_detail_page",
    ),
    path(
        "faculty/interview/<uuid:interview_id>/performance/",
        faculty.interview_performance_page,
        name="interview_performance_page",
    ),
    path(
        "faculty/interview/<uuid:interview_id>/report/<uuid:session_public_id>/",
        faculty.student_report_detail,
        name="student_report_detail",
    ),
    path(
        "faculty/interview/<uuid:interview_id>/report/<uuid:session_public_id>/download/",
        faculty.faculty_report_pdf,
        name="faculty_report_pdf",
    ),

    # ── AJAX Endpoints for Auto-Assignment ────────────────────────────
    path(
        "faculty/api/batches/",
        faculty.get_batches_api,
        name="get_batches_api",
    ),
    path(
        "faculty/api/sections/",
        faculty.get_sections_api,
        name="get_sections_api",
    ),

    # ── HOD Mock Interview Reports Dashboard ─────────────────────────
    path(
        "hod/reports/",
        hod.hod_mock_interviews,
        name="hod_reports",
    ),
    path(
        "hod/api/reports/",
        hod.hod_mock_interviews_api,
        name="hod_reports_api",
    ),
    path(
        "hod/api/filter-options/",
        hod.hod_filter_options_api,
        name="hod_filter_options_api",
    ),
    path(
        "hod/report/<uuid:session_public_id>/",
        hod.hod_student_report_detail,
        name="hod_report_detail",
    ),
    path(
        "hod/report/<uuid:session_public_id>/download/",
        hod.hod_report_pdf,
        name="hod_report_pdf",
    ),

    # ── Mentor / Class Advisor Mock Interview Reports Dashboard ────────
    path(
        "mentor/reports/",
        roster_dashboard.mentor_ca_mock_interviews,
        name="mentor_ca_reports",
    ),
    path(
        "mentor/api/reports/",
        roster_dashboard.mentor_ca_mock_interviews_api,
        name="mentor_ca_reports_api",
    ),
    path(
        "mentor/api/filter-options/",
        roster_dashboard.mentor_ca_filter_options_api,
        name="mentor_ca_filter_options_api",
    ),
    path(
        "mentor/api/role-leaderboard/",
        roster_dashboard.mentor_ca_role_leaderboard_api,
        name="mentor_ca_role_leaderboard_api",
    ),
    path(
        "mentor/reports/leaderboard/",
        roster_dashboard.mentor_ca_role_leaderboard_page,
        name="mentor_ca_role_leaderboard_page",
    ),
    path(
        "mentor/report/<uuid:session_public_id>/",
        roster_dashboard.mentor_ca_student_report_detail,
        name="mentor_ca_report_detail",
    ),
    path(
        "mentor/report/<uuid:session_public_id>/download/",
        roster_dashboard.mentor_ca_report_pdf,
        name="mentor_ca_report_pdf",
    ),

    # ── Student Assigned Interview Endpoints ─────────────────────────
    path(
        "student/",
        faculty.student_interview_dashboard,
        name="student_interview_dashboard",
    ),
    path(
        "student/interview/<uuid:interview_id>/start/",
        faculty.start_assigned_interview,
        name="start_assigned_interview",
    ),
    path(
        "api/interview/<uuid:interview_id>/status/",
        faculty.interview_status_api,
        name="interview_status_api",
    ),
]
