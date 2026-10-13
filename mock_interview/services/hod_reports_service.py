"""HOD Mock Interview Reports service.

Builds the department-scoped, filterable student population and computes
unique-student aggregates and paginated report rows.

Counting rules (important for data accuracy):

* ``total_students``  - unique ``StudentDetails`` rows matching the active
  filters. A student is counted exactly once regardless of how many mock
  interviews they completed.
* ``attempted``       - unique students with at least one ``InterviewSession``.
* ``completed``       - unique students with at least one completed session
  (``completed`` / ``evaluating`` / ``report_ready``) or a generated
  ``InterviewReport``.
* ``not_attempted``   - ``total_students - attempted``.
* Score averages      - computed over each student's *latest* scored report or
  session (one value per student) so multiple attempts never inflate the
  aggregate.

Design: text filters (department/batch/year/section/semester) are pushed into
the database; interview-derived aggregates are computed from a bounded set of
session/report rows loaded in bulk. This keeps the SQL simple and portable,
avoids duplicate row multiplication from one-to-many joins, and makes the
"count students exactly once" rule easy to reason about.
"""

from __future__ import annotations

import math

from django.db.models import Q

from user_accounts.models import Add_Department, StudentDetails
from mock_interview.models import InterviewReport, InterviewSession

# Session statuses that prove a student "completed" a mock interview.
COMPLETED_SESSION_STATUSES = frozenset(
    {"completed", "evaluating", "report_ready"}
)

# Latest-score performance bands.
PERFORMANCE_BANDS = {
    "excellent": (85.0, 101.0),
    "good": (70.0, 85.0),
    "average": (50.0, 70.0),
    "needs_improvement": (0.0, 50.0),
}

VALID_FILTER_FIELDS = frozenset(
    {"department", "batch", "year", "section", "semester"}
)

# Only these annotated sub-queries are used at all by callers.
_SESSION_FIELDS = (
    "id", "public_id", "status", "overall_score", "created_at",
    "student_employee_id",
)
_REPORT_FIELDS = (
    "id", "overall_score", "technical_score", "communication_score",
    "session_id", "session__public_id", "session__student_employee_id",
    "session__created_at",
)


class HODDepartmentError(Exception):
    """Raised when the HOD account cannot be mapped to a department."""


def hod_add_department(user):
    """Resolve an HOD's ERP department to an academic ``Add_Department``.

    Superusers are not scoped to a single department (return ``None`` meaning
    "all") so the department filter is meaningful for them. Every other
    authorized user is always restricted to their mapped academic department.
    """
    if getattr(user, "is_superuser", False):
        return None

    employee_id = str(getattr(user, "Employee_id", "") or "").strip()
    if employee_id.isdigit():
        try:
            from faculty_management.models import general_information
            profile = (
                general_information.objects.select_related("department")
                .filter(faculty_id=int(employee_id))
                .first()
            )
            if profile and profile.department_id:
                return profile.department
        except Exception:
            # Fall back to the login department below; some isolated test
            # settings do not create the full faculty-management schema.
            pass

    login_department = getattr(user, "Department", None)
    dept_name = str(getattr(login_department, "Department", "") or "").strip()
    if not dept_name:
        raise HODDepartmentError(
            "No department is assigned to your account."
        )
    dept = (
        Add_Department.objects.filter(is_active=True)
        .filter(Q(Department__iexact=dept_name) | Q(Department_code__iexact=dept_name))
        .first()
    )
    if not dept:
        raise HODDepartmentError(
            "Your department could not be found in the academic records."
        )
    return dept


def parse_filters(query_params, *, hod_dept):
    """Read and sanitize filter values from request query parameters.

    Returns a dict of ``{field: value}`` for the fields that actually
    constrain the query. ``"All"`` / empty values are dropped.

    ``department`` is only honoured for superusers; a non-superuser HOD is
    always scoped to their own department by the caller.
    """
    filters = {}
    for field in VALID_FILTER_FIELDS:
        raw = str(query_params.get(field, "") or "").strip()
        if not raw or raw.lower() == "all":
            continue
        if field == "department" and hod_dept is not None:
            # Non-superuser HOD cannot escape their own department scope.
            continue
        filters[field] = raw

    status = str(
        query_params.get("interview_status", "") or ""
    ).strip().lower()
    if status and status != "all" and status in {"not_attempted", "attempted", "completed"}:
        filters["interview_status"] = status

    performance = str(
        query_params.get("performance", "") or ""
    ).strip().lower()
    if performance and performance != "all" and performance in PERFORMANCE_BANDS:
        filters["performance"] = performance

    return filters


def _apply_text_filters(qs, filters):
    """Apply department / batch / year / section / semester constraints."""
    if filters.get("department"):
        # The superuser department dropdown is populated with department
        # *names* (e.g. "CSE"), and the campus has multiple nickname
        # departments; match on the academic Add_Department name so the
        # correct FK is resolved regardless of the value passed in.
        qs = qs.filter(
            department__Department__iexact=str(filters["department"])
        )
    for field in ("batch", "year", "section", "semester"):
        value = filters.get(field)
        if value:
            qs = qs.filter(**{field: value})
    return qs


class _Aggregate:
    """Per-student interview aggregate (immutable-ish data holder)."""

    __slots__ = (
        "student",
        "interview_count",
        "has_session",
        "is_completed",
        "latest_score",
        "technical_score",
        "communication_score",
        "latest_session_status",
        "latest_session_public_id",
        "report_public_id",
    )

    def __init__(self, student):
        self.student = student
        self.interview_count = 0
        self.has_session = False
        self.is_completed = False
        self.latest_score = None
        self.technical_score = None
        self.communication_score = None
        self.latest_session_status = None
        self.latest_session_public_id = None
        self.report_public_id = None


def _load_aggregates(students_by_reg):
    """Compute one ``_Aggregate`` per unique student.

    Sessions and reports are bulk-loaded and grouped by
    ``student_employee_id`` (== ``StudentDetails.reg_no``). The "latest"
    values come from ordering by ``created_at`` descending.
    """
    reg_nos = list(students_by_reg.keys())
    if not reg_nos:
        return {}

    aggregates = {
        reg: _Aggregate(student)
        for reg, student in students_by_reg.items()
    }

    # Latest session per student (ordered most-recent first).
    sessions = (
        InterviewSession.objects.filter(student_employee_id__in=reg_nos)
        .order_by("-created_at")
        .values(*_SESSION_FIELDS)
    )
    for row in sessions:
        reg = row["student_employee_id"]
        agg = aggregates.get(reg)
        if agg is None:
            continue
        agg.interview_count += 1
        agg.has_session = True
        if row["status"] in COMPLETED_SESSION_STATUSES:
            agg.is_completed = True
        if agg.latest_session_public_id is None:
            agg.latest_session_status = row["status"]
            agg.latest_session_public_id = str(row["public_id"])

    # Latest report per student (ordered by session most-recent first).
    reports = (
        InterviewReport.objects.filter(
            session__student_employee_id__in=reg_nos
        )
        .order_by("-session__created_at")
        .values(*_REPORT_FIELDS)
    )
    for row in reports:
        reg = row["session__student_employee_id"]
        agg = aggregates.get(reg)
        if agg is None:
            continue
        agg.is_completed = True
        if agg.report_public_id is None:
            agg.report_public_id = str(row["session__public_id"])
            agg.latest_score = _decimal_float(row["overall_score"])
            agg.technical_score = _decimal_float(row["technical_score"])
            agg.communication_score = _decimal_float(row["communication_score"])

    # Students with a session score but no report yet still get a score.
    for reg, agg in aggregates.items():
        if agg.latest_score is None:
            for row in sessions:
                if row["student_employee_id"] != reg:
                    continue
                agg.latest_score = _decimal_float(row["overall_score"])
                if agg.latest_score is not None:
                    break

    return aggregates


def _decimal_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _student_queryset(hod_dept, filters):
    qs = StudentDetails.objects.filter(is_active=True)
    if hod_dept is not None:
        qs = qs.filter(department=hod_dept)
    return _apply_text_filters(qs, filters)


def _matches_status(agg, status):
    if status == "not_attempted":
        return not agg.has_session
    if status == "attempted":
        return agg.has_session and not agg.is_completed
    if status == "completed":
        return agg.is_completed
    return True


def _matches_performance(agg, performance):
    if not performance:
        return True
    if agg.latest_score is None:
        return False
    low, high = PERFORMANCE_BANDS[performance]
    return low <= agg.latest_score < high


def build_dataset(hod_dept, filters):
    """Return the filtered, aggregated dataset as plain structures.

    ``filters`` is the dict produced by :func:`parse_filters`.

    Returns a dict with:
      * ``students``  - list of unique student records matching filters
      * ``aggregates``- dict mapping ``reg_no`` -> ``_Aggregate``
      * ``counts``    - total / attempted / attempted-only / completed
    """
    students = list(
        _student_queryset(hod_dept, filters).select_related("department")
    )
    students_by_reg = {s.reg_no: s for s in students if s.reg_no}

    aggregates = _load_aggregates(students_by_reg)

    status = filters.get("interview_status")
    performance = filters.get("performance")

    matching = [
        agg for agg in aggregates.values()
        if _matches_status(agg, status) and _matches_performance(agg, performance)
    ]

    counts = {
        "total": len(matching),
        "attempted": sum(1 for a in matching if a.has_session),
        "completed": sum(1 for a in matching if a.is_completed),
    }
    counts["not_attempted"] = max(counts["total"] - counts["attempted"], 0)

    return {
        "students": students,
        "aggregates": aggregates,
        "matching": matching,
        "counts": counts,
    }


def compute_summary(dataset):
    """Compute unique-student summary metrics for the filtered set."""
    counts = dataset["counts"]
    total = counts["total"]

    scores = [a.latest_score for a in dataset["matching"] if a.latest_score is not None]
    technical = [a.technical_score for a in dataset["matching"] if a.technical_score is not None]
    comm = [a.communication_score for a in dataset["matching"] if a.communication_score is not None]

    distribution = {
        "excellent": 0,
        "good": 0,
        "average": 0,
        "needs_improvement": 0,
    }
    for agg in dataset["matching"]:
        if agg.latest_score is None:
            continue
        for band, (low, high) in PERFORMANCE_BANDS.items():
            if low <= agg.latest_score < high:
                distribution[band] += 1
                break

    def _average(values):
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    completion_rate = round((counts["completed"] / total) * 100, 1) if total else 0.0

    return {
        "total_students": total,
        "attempted": counts["attempted"],
        "completed": counts["completed"],
        "not_attempted": counts["not_attempted"],
        "avg_interview_score": _average(scores),
        "avg_technical_score": _average(technical),
        "avg_communication_score": _average(comm),
        "completion_rate": completion_rate,
        "performance_distribution": distribution,
    }


def student_rows(dataset, *, page, per_page, sort_by="reg_no"):
    """Return one serializable row per matching student (paginated)."""
    by_reg = dataset["aggregates"]

    def row(agg):
        student = agg.student
        return {
            "id": student.pk,
            "name": student.name or "",
            "reg_no": student.reg_no or "",
            "department": (
                student.department.Department if student.department else ""
            ),
            "department_code": (
                student.department.Department_code if student.department else ""
            ),
            "batch": student.batch or "",
            "year": student.year or "",
            "section": student.section or "",
            "semester": student.semester or "",
            "interview_count": agg.interview_count,
            "latest_score": agg.latest_score,
            "technical_score": agg.technical_score,
            "communication_score": agg.communication_score,
            "status": (
                "completed" if agg.is_completed
                else "attempted" if agg.has_session
                else "not_attempted"
            ),
            "report_available": agg.is_completed and agg.report_public_id is not None,
            "report_public_id": (
                agg.report_public_id or agg.latest_session_public_id
            ),
        }

    matching = dataset["matching"]
    if sort_by == "latest_score":
        matching.sort(
            key=lambda a: (-(a.latest_score if a.latest_score is not None else -1), a.student.reg_no or "")
        )
    elif sort_by == "name":
        matching.sort(
            key=lambda a: ((a.student.name or "").lower(), a.student.reg_no or "")
        )
    else:
        matching.sort(key=lambda a: (a.student.reg_no or ""))

    total = len(matching)
    total_pages = math.ceil(total / per_page) if total else 1
    start = (page - 1) * per_page
    page_items = matching[start:start + per_page]

    return {
        "students": [row(a) for a in page_items],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    }


def _distinct_values(qs, field):
    return sorted(
        value
        for value in qs.exclude(**{f"{field}__isnull": True})
        .exclude(**{f"{field}": ""})
        .values_list(field, flat=True)
        .distinct()
    )


def filter_options(hod_dept, filters):
    """Return dependent filter options for the current selections.

    Each dimension lists values that actually exist among students matching
    ALL the *other* active filters, so the UI never offers options that are
    guaranteed to return zero results. An active selection is kept visible
    even though it may no longer appear in the dependent list.
    """
    def base_qs():
        qs = StudentDetails.objects.filter(is_active=True)
        if hod_dept is not None:
            qs = qs.filter(department=hod_dept)
        return qs

    def options_for(field):
        qs = base_qs()
        if filters.get("department") and field != "department":
            qs = qs.filter(
                department__Department__iexact=str(filters["department"])
            )
        for dim in ("batch", "year", "section", "semester"):
            if dim == field:
                continue
            value = filters.get(dim)
            if value:
                qs = qs.filter(**{dim: value})
        return _distinct_values(qs, field)

    batches = options_for("batch")
    years = options_for("year")
    sections = options_for("section")
    semesters = options_for("semester")

    for field, values in (
        ("batch", batches),
        ("year", years),
        ("section", sections),
        ("semester", semesters),
    ):
        active = filters.get(field)
        if active and active not in values:
            values.append(active)
            values.sort()

    departments = []
    if hod_dept is None:
        departments = list(
            Add_Department.objects.filter(is_active=True)
            .values_list("Department", flat=True)
            .distinct()
            .order_by("Department")
        )
    else:
        dept_name = str(getattr(hod_dept, "Department", "") or "")
        if dept_name:
            departments = [dept_name]
        else:
            departments = []

    return {
        "departments": departments,
        "batches": batches,
        "years": years,
        "sections": sections,
        "semesters": semesters,
    }
