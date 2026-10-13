"""Mentor / Class Advisor Mock Interview Reports service.

Builds the roster-scoped, filterable student population for mentors
(their mentees) and class advisors (their class roster) and computes the
same unique-student aggregates used by the HOD reports dashboard.

Scope rule (the security guarantee): the student population is strictly
role-scoped. A faculty logged in with the Class Advisor (CA) role sees only
``StudentDetails.ca`` pointing at them; a faculty logged in with the Mentor
role sees only ``StudentDetails.mentor`` pointing at them. Callers without an
explicit Mentor/CA role (e.g. HOD or assignment-authorized faculty) get the
union of their own mentor and CA assignments. A superuser is unscoped
(``profile=None`` = all students). Student-level text filters
(department/batch/year/section/semester and free-text student search) and
interview-derived filters (role / domain / date range / status / performance)
can only narrow that population, never widen it.

Relationship labels are always resolved relative to the caller's own profile
and scope mode: a CA user's rows are labelled "Class Advisor", a Mentor user's
rows "Mentor" - never inferred from the student's unrelated mentor/CA
assignments elsewhere in the system.

Counting rules are shared with the HOD service:

* ``total_students``  - unique roster students matching the active filters.
* ``attempted``       - unique students with at least one ``InterviewSession``.
* ``completed``       - unique students with at least one completed session
  or a generated ``InterviewReport``.
* ``not_attempted``   - ``total_students - attempted``.
* Score averages      - computed over each student's *latest* scored report or
  session (one value per student).

Domain model: every ``InterviewSession`` contributes one "role" (its free-text
`role`) and one "domain" label - the faculty ``MockInterview.subject_code``
when the session came from a faculty-created interview, otherwise the practice
role itself. The role/domain/date filters are inclusion filters: a student
matches when any of their sessions satisfies the constraint. Scores remain
the student's latest overall values.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from django.db.models import Q

from user_accounts.models import StudentDetails
from mock_interview.models import InterviewReport, InterviewSession
from mock_interview.services.hod_reports_service import (
    COMPLETED_SESSION_STATUSES,
    PERFORMANCE_BANDS,
    _Aggregate,
    _apply_text_filters,
    _decimal_float,
    _distinct_values,
    _load_aggregates,
    _matches_performance,
    _matches_status,
    compute_summary,
)


class RosterScopeError(Exception):
    """Raised when the Mentor/CA account cannot be mapped to a faculty profile."""


VALID_FILTER_FIELDS = frozenset(
    {"department", "batch", "year", "section", "semester"}
)

ROOT_RELATION_LABELS = {"Mentor": "bg-primary", "Class Advisor": "bg-info", "Mentor & CA": "bg-success"}

# Canonical ERP role names that map to a strict, single relationship scope.
MENTOR_ROLES = frozenset({"mentor"})
CA_ROLES = frozenset({"ca", "class advisor", "class adviser", "advisor"})


def scope_mode(role_name):
    """Return the strict relationship scope for a normalized ERP role name.

    ``"ca"``   - user may only manage/see their Class Advisor students.
    ``"mentor"`` - user may only manage/see their mentees.
    ``None``  - union scope (HOD, superuser, or assignment-authorized faculty):
                the user sees whichever of their mentor/CA assignments exist and
                relationship labels are resolved per student against their own
                profile.

    This is the single source of truth for role -> relationship mapping, so
    scope and labels never disagree with each other.
    """
    normalized = str(role_name or "").strip().lower()
    if normalized in CA_ROLES:
        return "ca"
    if normalized in MENTOR_ROLES:
        return "mentor"
    return None


def resolve_roster(user):
    """Return the current faculty profile PK for the roster scope.

    Superusers are unscoped and return ``None`` (all students). Every other
    authorized user must map their ``Employee_id`` to a
    ``faculty_management.general_information`` row via ``faculty_id``; the
    roster is the union of that faculty member's mentorships and class
    adviserships. Raises ``RosterScopeError`` when the account cannot be
    mapped, mirroring how the HOD service pins its department.
    """
    if getattr(user, "is_superuser", False):
        return None

    employee_id = str(getattr(user, "Employee_id", "") or "").strip()
    if not employee_id:
        raise RosterScopeError(
            "No employee ID is assigned to your account."
        )
    try:
        faculty_id = int(employee_id)
    except (TypeError, ValueError):
        raise RosterScopeError(
            "Your account does not carry a valid employee ID."
        )

    try:
        from faculty_management.models import general_information
        profile = (
            general_information.objects
            .filter(faculty_id=faculty_id)
            .order_by("faculty_id")
            .first()
        )
    except Exception:
        raise RosterScopeError(
            "Your mentor/class-advisor profile could not be located."
        )
    if profile is None:
        raise RosterScopeError(
            "No mentor/class-advisor profile is linked to your account."
        )
    return profile.pk


def active_scope(user):
    """Return ``(profile_pk_or_None, relation_mode)`` for an authorized caller.

    ``profile`` is the caller's faculty profile (``None`` for superusers);
    ``relation_mode`` is the strict role scope from :func:`scope_mode`. The
    mode is threaded through every data-building call so the dashboard page,
    JSON API, filter options, detail view authorization and PDF downloads all
    share the exact same relationship rule.
    """
    profile = resolve_roster(user)
    role_name = getattr(getattr(user, "role", None), "role", None)
    return profile, scope_mode(role_name)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def parse_filters(query_params, *, profile):
    """Read and sanitize filter values from request query parameters.

    Returns a dict of ``{field: value}`` for the fields that actually
    constrain the query. ``"All"`` / empty values are dropped. ``profile``
    is only used to guarantee that student-level text filters never escape
    the roster scope (they cannot because the population queryset is always
    roster-scoped).
    """
    filters = {}
    for field in VALID_FILTER_FIELDS:
        raw = str(query_params.get(field, "") or "").strip()
        if not raw or raw.lower() == "all":
            continue
        filters[field] = raw

    student = str(query_params.get("student", "") or "").strip()
    if student:
        filters["student"] = student

    role = str(query_params.get("role", "") or "").strip()
    if role and role.lower() != "all":
        filters["role"] = role

    domain = str(query_params.get("domain", "") or "").strip()
    if domain and domain.lower() != "all":
        filters["domain"] = domain

    date_from = _parse_date(query_params.get("date_from"))
    if date_from is not None:
        filters["date_from"] = date_from
    date_to = _parse_date(query_params.get("date_to"))
    if date_to is not None:
        filters["date_to"] = date_to

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


def _scoped_students(profile, mode=None):
    qs = StudentDetails.objects.filter(is_active=True)
    if profile is None:
        return qs  # superuser: all students
    if mode == "ca":
        return qs.filter(ca_id=profile)
    if mode == "mentor":
        return qs.filter(mentor_id=profile)
    return qs.filter(Q(mentor_id=profile) | Q(ca_id=profile))


def _student_queryset(profile, filters, mode=None):
    qs = _scoped_students(profile, mode)
    student = filters.get("student")
    if student:
        term = str(student).strip()
        qs = qs.filter(Q(reg_no__icontains=term) | Q(name__icontains=term))
    return _apply_text_filters(qs, filters)


def _load_role_domains(reg_nos):
    """Load per-student roles, domains and session dates in bulk."""
    if not reg_nos:
        return {}
    out = {reg: {"roles": set(), "domains": set(), "dates": set()}
           for reg in reg_nos}
    rows = (
        InterviewSession.objects
        .filter(student_employee_id__in=reg_nos)
        .select_related("mock_interview")
        .values(
            "student_employee_id", "role", "created_at",
            "mock_interview_id", "mock_interview__subject_code",
        )
    )
    for row in rows:
        info = out.get(row["student_employee_id"])
        if info is None:
            continue
        role = str(row["role"] or "").strip()
        if role:
            info["roles"].add(role)
        if row.get("mock_interview_id") and row.get("mock_interview__subject_code"):
            subject = str(row["mock_interview__subject_code"]).strip()
            if subject:
                info["domains"].add(subject)
        elif role:
            info["domains"].add(role)
        created_at = row.get("created_at")
        if created_at is not None:
            info["dates"].add(created_at.date())
    return out


def _in_date_range(dates, date_from, date_to):
    if date_from is None and date_to is None:
        return True
    for d in dates:
        if (date_from is None or d >= date_from) and (date_to is None or d <= date_to):
            return True
    return False


def build_dataset(profile, filters, mode=None):
    """Return the filtered, roster-scoped dataset as plain structures.

    ``filters`` is the dict produced by :func:`parse_filters`; ``mode`` is the
    strict relationship scope from :func:`scope_mode` (``None`` = union).

    Returns a dict with:
      * ``students``   - list of unique student records matching filters
      * ``aggregates`` - dict mapping ``reg_no`` -> ``_Aggregate``
      * ``role_domains`` - dict mapping ``reg_no`` -> roles/domains/dates
      * ``matching``   - the aggregated students passing all filters
      * ``counts``     - total / attempted / completed / not_attempted
      * ``relation_counts`` - per-relationship breakdown of matching students
      * ``profile`` / ``relation_mode`` - the scope used, so downstream label
        resolution (``student_rows``) agrees with the population query
    """
    students = list(_student_queryset(profile, filters, mode).select_related("department"))
    students_by_reg = {s.reg_no: s for s in students if s.reg_no}

    aggregates = _load_aggregates(students_by_reg)
    role_domains = _load_role_domains(list(students_by_reg.keys()))

    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    role_filter = str(filters.get("role", "") or "").strip()
    domain_filter = str(filters.get("domain", "") or "").strip()
    status = filters.get("interview_status")
    performance = filters.get("performance")

    matching = []
    for reg, agg in aggregates.items():
        if not _matches_status(agg, status):
            continue
        if not _matches_performance(agg, performance):
            continue
        info = role_domains.get(reg) or {"roles": set(), "domains": set(), "dates": set()}
        if role_filter and role_filter not in info["roles"]:
            continue
        if domain_filter and domain_filter not in info["domains"]:
            continue
        if not _in_date_range(info["dates"], date_from, date_to):
            continue
        matching.append(agg)

    counts = {
        "total": len(matching),
        "attempted": sum(1 for a in matching if a.has_session),
        "completed": sum(1 for a in matching if a.is_completed),
    }
    counts["not_attempted"] = max(counts["total"] - counts["attempted"], 0)

    relation_counts = {"mentor": 0, "ca": 0, "both": 0}
    for agg in matching:
        label = relation_label(agg.student, profile, mode)
        if label == "Mentor":
            relation_counts["mentor"] += 1
        elif label == "Class Advisor":
            relation_counts["ca"] += 1
        elif label == "Mentor & CA":
            relation_counts["both"] += 1

    return {
        "students": students,
        "aggregates": aggregates,
        "role_domains": role_domains,
        "matching": matching,
        "counts": counts,
        "relation_counts": relation_counts,
        "profile": profile,
        "relation_mode": mode,
    }


def relation_label(student, profile=None, mode=None):
    """Human label for the relationship between a student and the caller.

    Always resolved against the caller's own assignment and scope mode, never
    against the student's unrelated mentor/CA rows. A CA user is always
    "Class Advisor", a Mentor user always "Mentor". In union/superuser views
    the label reflects which of the caller's own relationships apply
    (``Mentor`` / ``Class Advisor`` / ``Mentor & CA``).
    """
    if mode == "ca":
        return "Class Advisor"
    if mode == "mentor":
        return "Mentor"
    if profile is None:
        is_mentor = bool(getattr(student, "mentor_id", None))
        is_ca = bool(getattr(student, "ca_id", None))
    else:
        is_mentor = getattr(student, "mentor_id", None) == profile
        is_ca = getattr(student, "ca_id", None) == profile
    if is_mentor and is_ca:
        return "Mentor & CA"
    if is_mentor:
        return "Mentor"
    if is_ca:
        return "Class Advisor"
    return ""


def compute_role_domain_summary(dataset):
    """Count the roles/domains students are preparing for (top-first)."""
    role_counts = Counter()
    domain_counts = Counter()
    for agg in dataset["matching"]:
        info = dataset["role_domains"].get(agg.student.reg_no)
        if not info:
            continue
        for role in info["roles"]:
            role_counts[role] += 1
        for domain in info["domains"]:
            domain_counts[domain] += 1

    def _ranked(counter):
        return [
            {"label": label, "count": count}
            for label, count in sorted(
                counter.items(), key=lambda kv: (-kv[1], str(kv[0]).lower())
            )
        ]

    return {
        "roles": _ranked(role_counts),
        "domains": _ranked(domain_counts),
    }


_LEADERBOARD_SESSION_FIELDS = (
    "public_id", "status", "overall_score", "created_at",
    "student_employee_id", "role",
    "mock_interview_id", "mock_interview__subject_code",
)
_LEADERBOARD_REPORT_FIELDS = (
    "session_id", "overall_score", "technical_score", "communication_score",
    "session__public_id", "session__student_employee_id",
)


def _session_domain(row):
    """Domain label of a session row: faculty subject code, else the role."""
    if row.get("mock_interview_id") and row.get("mock_interview__subject_code"):
        subject = str(row["mock_interview__subject_code"]).strip()
        if subject:
            return subject
    return str(row.get("role") or "").strip()


def _session_matches_scope(scope, scope_type, row):
    """True when a session row belongs to the selected role/domain label."""
    if not scope:
        return False
    if scope_type == "role":
        return str(row.get("role") or "").strip().lower() == scope.lower()
    if scope_type == "domain":
        return _session_domain(row).lower() == scope.lower()
    return False


def compute_role_leaderboard(profile, *, scope, scope_type="role", mode=None, limit=100):
    """Rank roster students who completed a mock interview for a role/domain.

    Only students who completed at least one scored mock interview for the
    selected role/domain are listed. Each student is ranked by the overall
    score of their *latest* completed attempt for that role/domain (report
    score preferred, session score as fallback), best-to-worst.

    ``scope`` is the role or domain label (case-insensitive); ``scope_type``
    is ``"role"`` (``InterviewSession.role``) or ``"domain"`` (faculty
    ``MockInterview.subject_code``, else the practice role). ``mode`` keeps
    the strict Mentor/CA relationship scope, exactly like ``build_dataset``.
    """
    scope = str(scope or "").strip()
    if not scope or scope_type not in {"role", "domain"}:
        return []

    students = {
        s.reg_no: s
        for s in _scoped_students(profile, mode).select_related("department")
        if s.reg_no
    }
    if not students:
        return []

    # Completed attempts scoped to the roster, most recent first.
    sessions = (
        InterviewSession.objects.filter(
            student_employee_id__in=list(students.keys()),
            status__in=COMPLETED_SESSION_STATUSES,
        )
        .order_by("-created_at")
        .values(*_LEADERBOARD_SESSION_FIELDS)
    )

    latest = {}  # reg_no -> latest matching session row
    attempt_counts = {}  # reg_no -> number of completed matching attempts
    for row in sessions:
        if not _session_matches_scope(scope, scope_type, row):
            continue
        reg = row["student_employee_id"]
        attempt_counts[reg] = attempt_counts.get(reg, 0) + 1
        if reg not in latest:
            latest[reg] = row

    if not latest:
        return []

    reports_by_session = {
        r["session__public_id"]: r
        for r in InterviewReport.objects.filter(
            session__public_id__in=[row["public_id"] for row in latest.values()]
        ).values(*_LEADERBOARD_REPORT_FIELDS)
    }

    records = []
    for reg, row in latest.items():
        student = students[reg]
        report = reports_by_session.get(row["public_id"])
        score = (
            _decimal_float(report["overall_score"])
            if report else _decimal_float(row["overall_score"])
        )
        if score is None:
            continue
        records.append({
            "reg_no": reg,
            "name": student.name or "",
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
            "relation": relation_label(student, profile, mode),
            "attempts": attempt_counts.get(reg, 0),
            "overall_score": score,
            "technical_score": (
                _decimal_float(report["technical_score"]) if report else None
            ),
            "communication_score": (
                _decimal_float(report["communication_score"]) if report else None
            ),
            "report_public_id": str(row["public_id"]),
            "completed_at": (
                row["created_at"].isoformat() if row.get("created_at") else None
            ),
        })

    records.sort(key=lambda r: (-r["overall_score"], r["reg_no"] or ""))

    # Competition ranking: students with an equal score share a rank.
    ranked = []
    last_score = None
    rank = 0
    for index, record in enumerate(records, start=1):
        if last_score is None or record["overall_score"] != last_score:
            rank = index
            last_score = record["overall_score"]
        ranked.append(dict(record, rank=rank))

    if limit and limit > 0:
        ranked = ranked[:limit]
    return ranked


def student_rows(dataset, *, page, per_page, sort_by="reg_no"):
    """Return one serializable row per matching student (paginated)."""
    by_reg = dataset["aggregates"]

    def row(agg):
        student = agg.student
        info = dataset["role_domains"].get(student.reg_no) or {}
        return {
            "id": student.pk,
            "name": student.name or "",
            "reg_no": student.reg_no or "",
            "relation": relation_label(
                student,
                dataset.get("profile"),
                dataset.get("relation_mode"),
            ),
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
            "roles": sorted(info.get("roles", ()), key=str.lower),
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
            key=lambda a: (-(a.latest_score if a.latest_score is not None else -1),
                           a.student.reg_no or "")
        )
    elif sort_by == "name":
        matching.sort(
            key=lambda a: ((a.student.name or "").lower(), a.student.reg_no or "")
        )
    else:
        matching.sort(key=lambda a: (a.student.reg_no or ""))

    total = len(matching)
    total_pages = (total + per_page - 1) // per_page if total else 1
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


def filter_options(profile, filters, mode=None):
    """Return dependent (filtered) dropdown options for the roster UI.

    Every dimension lists values that actually exist among the CALLER'S OWN
    students (role-scoped by ``mode``) matching all the *other* active
    filters, so the UI never offers options that are guaranteed to return
    zero results and never leaks students outside the roster scope.
    """
    def academic_qs():
        qs = _scoped_students(profile, mode)
        student = filters.get("student")
        if student:
            qs = qs.filter(
                Q(reg_no__icontains=student) | Q(name__icontains=student)
            )
        return qs

    def text_options_for(field):
        qs = academic_qs()
        for dim in ("department", "batch", "year", "section", "semester"):
            if dim == field:
                continue
            value = filters.get(dim)
            if value:
                if dim == "department":
                    qs = qs.filter(department__Department__iexact=str(value))
                else:
                    qs = qs.filter(**{dim: value})
        if field == "department":
            return sorted(
                value
                for value in qs.exclude(department__Department__isnull=True)
                .exclude(department__Department="")
                .values_list("department__Department", flat=True)
                .distinct()
            )
        return _distinct_values(qs, field)

    batches = text_options_for("batch")
    years = text_options_for("year")
    sections = text_options_for("section")
    semesters = text_options_for("semester")
    departments = text_options_for("department")

    reg_nos = list(
        _apply_text_filters(academic_qs(), filters)
        .values_list("reg_no", flat=True)
    )
    role_domains = _load_role_domains([r for r in reg_nos if r])
    roles = sorted({r for info in role_domains.values() for r in info.get("roles", ())}, key=str.lower)
    domains = sorted({d for info in role_domains.values() for d in info.get("domains", ())}, key=str.lower)

    for field, values in (
        ("department", departments),
        ("batch", batches),
        ("year", years),
        ("section", sections),
        ("semester", semesters),
        ("role", roles),
        ("domain", domains),
    ):
        active = filters.get(field)
        if active and active not in values:
            values.append(active)
            values.sort(key=str.lower if isinstance(active, str) else None)

    return {
        "departments": departments,
        "batches": batches,
        "years": years,
        "sections": sections,
        "semesters": semesters,
        "roles": roles,
        "domains": domains,
    }