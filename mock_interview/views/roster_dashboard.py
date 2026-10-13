"""Mentor / Class Advisor Mock Interview Reports views.

Dedicated dashboard for Mentors and Class Advisors to analyse mock-interview
performance of only their own assigned students (mentees / class roster).

Security: every view first verifies the caller carries an allowed ERP role
(shared rule from :mod:`mock_interview.services.access`), then resolves the
caller's faculty profile and pins the roster scope in the service layer
(``roster_reports_service``). The roster scope is enforced inside the
service, so manipulating query parameters can never make a Mentor see another
user's students.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from mock_interview.models import InterviewReport, InterviewSession
from mock_interview.services.access import is_roster_dashboard_user
from mock_interview.services.roster_reports_service import (
    PERFORMANCE_BANDS,
    RosterScopeError,
    active_scope,
    build_dataset,
    compute_role_domain_summary,
    compute_role_leaderboard,
    compute_summary,
    filter_options,
    parse_filters,
    student_rows,
)

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


def _is_roster(user):
    """Return True when the caller is a Mentor/CA/HOD (role or assignment based)."""
    return is_roster_dashboard_user(user)


def _relation_summary(mode, relation_counts):
    """Relationship chips for the dashboard: pinned to the active role.

    A CA user only ever sees "Class Advisor"; a Mentor user only ever sees
    "Mentor". The union view (HOD / superuser / assignment-authorized
    faculty) shows the profile-relative Mentor / Class Advisor / Both mix.
    """
    relation_counts = relation_counts or {"mentor": 0, "ca": 0, "both": 0}
    if mode == "ca":
        return [{"label": "Class Advisor", "count": relation_counts["ca"], "badge": "bg-info"}]
    if mode == "mentor":
        return [{"label": "Mentor", "count": relation_counts["mentor"], "badge": "bg-primary"}]
    items = [
        {"label": "Mentor", "count": relation_counts["mentor"], "badge": "bg-primary"},
        {"label": "Class Advisor", "count": relation_counts["ca"], "badge": "bg-info"},
    ]
    if relation_counts.get("both"):
        items.append({"label": "Mentor & CA", "count": relation_counts["both"], "badge": "bg-success"})
    return items


def _roster_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return redirect("login_view")
        if not _is_roster(request.user):
            return JsonResponse(
                {"error": "Mentor/Class Advisor access required."}, status=403
            )
        return view_func(request, *args, **kwargs)

    return wrapped


def _resolve_roster(user):
    """Return ``(profile_pk_or_None, relation_mode)`` for the caller's scope."""
    return active_scope(user)


def _roster_label(user, profile):
    """Human-readable scope label for the dashboard header."""
    if profile is None:
        return "All students &middot; superuser view"
    try:
        from faculty_management.models import general_information
        name = (
            general_information.objects.filter(pk=profile)
            .values_list("name", flat=True)
            .first()
        )
    except Exception:
        name = None
    return name or "Your assigned students"


@login_required
@_roster_required
def mentor_ca_mock_interviews(request):
    """Mentor / Class Advisor dashboard page for mock interview reports."""
    try:
        profile, mode = _resolve_roster(request.user)
    except RosterScopeError as exc:
        messages.error(request, str(exc))
        return redirect("faculty_dashboard")

    filters = parse_filters(request.GET, profile=profile)
    options = filter_options(profile, filters, mode=mode)

    dataset = build_dataset(profile, filters, mode=mode)
    summary = compute_summary(dataset)
    role_domains = compute_role_domain_summary(dataset)
    page = max(1, int(request.GET.get("page", 1) or 1))
    per_page = min(
        MAX_PER_PAGE,
        max(1, int(request.GET.get("per_page", DEFAULT_PER_PAGE) or DEFAULT_PER_PAGE)),
    )
    table = student_rows(dataset, page=page, per_page=per_page)

    return render(request, "mock_interview/mentor_ca_dashboard.html", {
        "roster_label": _roster_label(request.user, profile),
        "is_superuser": bool(getattr(request.user, "is_superuser", False)),
        "filters": filters,
        "options": options,
        "summary": summary,
        "role_domains": role_domains,
        "relation_counts": dataset["relation_counts"],
        "relation_summary": _relation_summary(mode, dataset["relation_counts"]),
        "students": table["students"],
        "pagination": table["pagination"],
        "user": request.user,
    })


@login_required
@_roster_required
@require_GET
def mentor_ca_mock_interviews_api(request):
    """JSON API returning the filtered dashboard data.

    Query parameters (all optional, ``all``/empty = no constraint):
      department, batch, year, section, semester, student, role, domain,
      date_from, date_to, interview_status, performance, page, per_page,
      sort_by

    Response includes summary metrics, role/domain summaries, the filtered
    student page and pagination metadata. Counts are always computed over the
    full filtered roster, never over the current page.
    """
    try:
        profile, mode = _resolve_roster(request.user)
    except RosterScopeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    filters = parse_filters(request.GET, profile=profile)
    dataset = build_dataset(profile, filters, mode=mode)
    summary = compute_summary(dataset)
    role_domains = compute_role_domain_summary(dataset)

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
        "role_domains": role_domains,
        "relation_counts": dataset["relation_counts"],
        "relation_summary": _relation_summary(mode, dataset["relation_counts"]),
        "students": table["students"],
        "pagination": table["pagination"],
        "filters": filters,
    })


@login_required
@_roster_required
@require_GET
def mentor_ca_filter_options_api(request):
    """Return dependent (filtered) dropdown options for the roster UI."""
    try:
        profile, mode = _resolve_roster(request.user)
    except RosterScopeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    filters = parse_filters(request.GET, profile=profile)
    options = filter_options(profile, filters, mode=mode)
    return JsonResponse(options)


@login_required
@_roster_required
@require_GET
def mentor_ca_role_leaderboard_api(request):
    """Leaderboard of roster students for a selected role/domain.

    Query parameters:
      * ``scope``      - required role or domain label.
      * ``scope_type`` - ``role`` (default) or ``domain``.
      * ``limit``      - optional cap on rows (default 100, max 500).

    Returns the roster students who completed a mock interview for the
    selected role/domain, ranked best-to-worst by their latest overall score
    for that role/domain. Scope (Mentor/CA) is enforced in the service layer.
    """
    try:
        profile, mode = _resolve_roster(request.user)
    except RosterScopeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    scope = str(request.GET.get("scope", "") or "").strip()
    scope_type = str(request.GET.get("scope_type", "role") or "role").strip()
    if scope_type not in {"role", "domain"}:
        return JsonResponse({"error": "Invalid scope type."}, status=400)
    if not scope:
        return JsonResponse({"error": "A role or domain is required."}, status=400)

    try:
        limit = int(request.GET.get("limit", 100) or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = min(500, max(1, limit))

    rows = compute_role_leaderboard(
        profile,
        scope=scope,
        scope_type=scope_type,
        mode=mode,
        limit=limit,
    )
    return JsonResponse({
        "scope": scope,
        "scope_type": scope_type,
        "rows": rows,
    })


@login_required
@_roster_required
def mentor_ca_role_leaderboard_page(request):
    """Dedicated domain/role-wise performance leaderboard page.

    GET parameters: ``scope`` (required role/domain label) and ``scope_type``
    (``role`` or ``domain``). Renders the roster students who completed a mock
    interview for that role/domain, ranked best-to-worst by latest score.
    Scope (Mentor/CA) is enforced in the service layer.
    """
    try:
        profile, mode = _resolve_roster(request.user)
    except RosterScopeError as exc:
        messages.error(request, str(exc))
        return redirect("mock_interview:mentor_ca_reports")

    scope = str(request.GET.get("scope", "") or "").strip()
    scope_type = str(request.GET.get("scope_type", "role") or "role").strip()
    if scope_type not in {"role", "domain"}:
        messages.error(request, "Invalid leaderboard scope.")
        return redirect("mock_interview:mentor_ca_reports")
    if not scope:
        messages.error(request, "Select a role or domain to open its leaderboard.")
        return redirect("mock_interview:mentor_ca_reports")

    rows = compute_role_leaderboard(
        profile,
        scope=scope,
        scope_type=scope_type,
        mode=mode,
    )

    for row in rows:
        _, band_label, band_badge = _score_band_meta(row["overall_score"])
        row["band_label"] = band_label
        row["band_badge"] = band_badge
        row["rank_badge"] = _rank_badge_meta(row["rank"])
        row["completed_date"] = (row.get("completed_at") or "")[:10]

    scores = [row["overall_score"] for row in rows]
    distribution = {name: 0 for name in PERFORMANCE_BANDS}
    for row in rows:
        band, _, _ = _score_band_meta(row["overall_score"])
        if band:
            distribution[band] += 1

    return render(request, "mock_interview/mentor_ca_role_leaderboard.html", {
        "roster_label": _roster_label(request.user, profile),
        "scope": scope,
        "scope_type": scope_type,
        "rows": rows,
        "total": len(rows),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "best": rows[0] if rows else None,
        "distribution": distribution,
        "user": request.user,
    })


def _score_band_meta(score):
    """Map an overall score to its performance band label + badge class."""
    for name, (low, high) in PERFORMANCE_BANDS.items():
        if low <= score < high:
            label = {
                "excellent": "Excellent",
                "good": "Good",
                "average": "Average",
                "needs_improvement": "Needs Improvement",
            }[name]
            badge = {
                "excellent": "bg-success",
                "good": "bg-info",
                "average": "bg-warning text-dark",
                "needs_improvement": "bg-danger",
            }[name]
            return name, label, badge
    return None, "", "bg-light text-muted"


def _rank_badge_meta(rank):
    """Badge class for the podium ranks / others."""
    if rank == 1:
        return "bg-warning text-dark"
    if rank == 2:
        return "bg-secondary text-white"
    if rank == 3:
        return "bg-info text-white"
    return "bg-light text-dark"


def _can_view_student(user, student):
    """Server-side authorization: only the caller's own role-scoped students.

    Mirror of :func:`mock_interview.services.roster_reports_service.active_scope`
    - a CA user may only open sessions of their class-list students, a Mentor
    only their mentees, union users either, superusers anything.
    """
    try:
        profile, mode = _resolve_roster(user)
    except RosterScopeError:
        return False
    if profile is None:  # superuser
        return True
    if student is None:
        return False
    if mode == "ca":
        return student.ca_id == profile
    if mode == "mentor":
        return student.mentor_id == profile
    return student.mentor_id == profile or student.ca_id == profile


@login_required
@_roster_required
def mentor_ca_student_report_detail(request, session_public_id):
    """A mentor/CA view of one student's complete mock interview report.

    Only sessions belonging to the caller's own roster are reachable. Reuses
    the shared report-content partial used by faculty and HOD.
    """
    session = get_object_or_404(
        InterviewSession.objects.select_related("resume"),
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
        return render(request, "mock_interview/mentor_ca_report_detail.html", {
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

    return render(request, "mock_interview/mentor_ca_report_detail.html", {
        "session": session,
        "student": student,
        "report": report_data,
        "questions": evaluations,
        "user": request.user,
    })


@login_required
@_roster_required
@require_GET
def mentor_ca_report_pdf(request, session_public_id):
    """Download a student's interview report PDF (mentor/CA only)."""
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