"""HOD Mock Interview Reports views.

Dedicated Head of Department dashboard for analysing mock-interview
performance across all students in the HOD's department.

Security: every view first verifies the caller is an authorized Faculty/HOD
ERP user (shared rule from :mod:`mock_interview.services.access`), then
resolves and pins the HOD's department. The department scope is enforced in
the service layer (``hod_reports_service``), so manipulating query parameters
can never make the HOD see another department's students.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from mock_interview.models import InterviewReport, InterviewSession
from mock_interview.services.access import is_hod_user
from mock_interview.services.hod_reports_service import (
    HODDepartmentError,
    build_dataset,
    compute_summary,
    filter_options,
    hod_add_department,
    parse_filters,
    student_rows,
)

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100

def _is_hod(user):
    """Return True when the caller carries an HOD ERP role (superuser = True)."""
    return is_hod_user(user)


def _hod_required(view_func):
    """Decorator restricting a view to HOD-role (or superuser) accounts."""
    from functools import wraps

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return redirect("login_view")
        if not _is_hod(request.user):
            return JsonResponse(
                {"error": "HOD access required."}, status=403
            )
        return view_func(request, *args, **kwargs)

    return wrapped


def _resolve_hod_department(user):
    """Return the HOD's pinned department (or None for superuser).

    Raises ``HODDepartmentError`` when the account cannot be mapped.
    """
    return hod_add_department(user)


@login_required
@_hod_required
def hod_mock_interviews(request):
    """HOD dashboard page for mock interview reports."""
    try:
        hod_dept = _resolve_hod_department(request.user)
    except HODDepartmentError as exc:
        messages.error(request, str(exc))
        return redirect("faculty_dashboard")

    user = request.user
    is_super = bool(getattr(user, "is_superuser", False))
    hod_dept_name = ""
    if hod_dept is not None:
        hod_dept_name = str(getattr(hod_dept, "Department", "") or "")
    else:
        login_department = getattr(user, "Department", None)
        if getattr(login_department, "Department", None):
            hod_dept_name = str(login_department.Department) or ""

    # Read the initial filter state from the URL so browser back/forward and
    # refresh keep the selection (see getActiveQuerystring() in the template).
    filters = parse_filters(request.GET, hod_dept=hod_dept)
    options = filter_options(hod_dept, filters)

    dataset = build_dataset(hod_dept, filters)
    summary = compute_summary(dataset)
    page = max(1, int(request.GET.get("page", 1) or 1))
    per_page = min(
        MAX_PER_PAGE,
        max(1, int(request.GET.get("per_page", DEFAULT_PER_PAGE) or DEFAULT_PER_PAGE)),
    )
    table = student_rows(dataset, page=page, per_page=per_page)

    return render(request, "mock_interview/hod_mock_interviews.html", {
        "hod_dept_name": hod_dept_name,
        "is_superuser": is_super,
        "filters": filters,
        "options": options,
        "summary": summary,
        "students": table["students"],
        "pagination": table["pagination"],
        "user": user,
    })


@login_required
@_hod_required
@require_GET
def hod_mock_interviews_api(request):
    """JSON API returning the filtered dashboard data.

    Query parameters (all optional, ``all``/empty = no constraint):
      department, batch, year, section, semester, interview_status,
      performance, page, per_page, sort_by

    Response includes summary metrics, the filtered student page and
    pagination metadata. Counts are always computed over the full filtered
    population, never over the current page.
    """
    try:
        hod_dept = _resolve_hod_department(request.user)
    except HODDepartmentError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    filters = parse_filters(request.GET, hod_dept=hod_dept)
    dataset = build_dataset(hod_dept, filters)
    summary = compute_summary(dataset)

    try:
        page = max(1, int(request.GET.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.GET.get("per_page", DEFAULT_PER_PAGE) or DEFAULT_PER_PAGE)
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    per_page = min(MAX_PER_PAGE, max(1, per_page))

    sort_by = request.GET.get("sort_by", "reg_no")
    table = student_rows(dataset, page=page, per_page=per_page, sort_by=sort_by)

    return JsonResponse({
        "summary": summary,
        "students": table["students"],
        "pagination": table["pagination"],
        "filters": filters,
    })


@login_required
@_hod_required
@require_GET
def hod_filter_options_api(request):
    """Return dependent (filtered) dropdown options for the UI."""
    try:
        hod_dept = _resolve_hod_department(request.user)
    except HODDepartmentError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    filters = parse_filters(request.GET, hod_dept=hod_dept)
    options = filter_options(hod_dept, filters)
    return JsonResponse(options)


def _can_view_student(user, student):
    """Server-side authorization: HOD may only view students in their scope."""
    try:
        hod_dept = _resolve_hod_department(user)
    except HODDepartmentError:
        return False
    if hod_dept is None:  # superuser
        return True
    return bool(student and student.department_id == hod_dept.id)


@login_required
@_hod_required
def hod_student_report_detail(request, session_public_id):
    """HOD view of one student's complete mock interview report.

    Only sessions belonging to students inside the HOD's department scope are
    reachable. Reuses the shared report-content partial used by faculty.
    """
    session = get_object_or_404(
        InterviewSession.objects.select_related("resume"),
        public_id=session_public_id,
    )

    # Resolve the owning student via reg_no -> StudentDetails.
    from user_accounts.models import StudentDetails
    student = None
    if session.student_employee_id:
        student = (
            StudentDetails.objects.select_related("department")
            .filter(reg_no=session.student_employee_id)
            .first()
        )
    if not _can_view_student(request.user, student):
        return JsonResponse({"error": "Unauthorized."}, status=403)

    report_data = InterviewReport.objects.filter(session=session).first()
    if not report_data:
        return render(request, "mock_interview/hod_report_detail.html", {
            "session": session,
            "student": student,
            "report": None,
            "questions": [],
            "user": request.user,
        })

    evaluations = (
        session.questions.filter(answer__evaluation__isnull=False)
        .select_related("answer__evaluation")
        .order_by("sequence_number")
    )

    return render(request, "mock_interview/hod_report_detail.html", {
        "session": session,
        "student": student,
        "report": report_data,
        "questions": evaluations,
        "user": request.user,
    })


@login_required
@_hod_required
@require_GET
def hod_report_pdf(request, session_public_id):
    """Download a student's interview report PDF (HOD-only)."""
    session = get_object_or_404(
        InterviewSession,
        public_id=session_public_id,
    )

    from user_accounts.models import StudentDetails
    student = None
    if session.student_employee_id:
        student = (
            StudentDetails.objects.select_related("department")
            .filter(reg_no=session.student_employee_id)
            .first()
        )
    if not _can_view_student(request.user, student):
        return JsonResponse({"error": "Unauthorized."}, status=403)

    report_data = InterviewReport.objects.filter(session=session).first()
    if not report_data:
        return JsonResponse({"error": "Report not found for this session."}, status=404)
    if not report_data.pdf_file:
        return JsonResponse({"error": "PDF report is unavailable."}, status=404)

    return FileResponse(
        report_data.pdf_file.open("rb"),
        content_type="application/pdf",
        as_attachment=True,
        filename=f"mock-interview-{session.public_id}.pdf",
    )


