from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

# Roles allowed to access the Faculty Mock Interview module. Only Faculty and
# Head of Department (HOD) are authorized. Every other ERP role is denied.
AUTHORIZED_FACULTY_MOCK_INTERVIEW_ROLES = frozenset({
    "faculty",
    "hod",
    "head of department",
    "head of the department",
})

HOD_MOCK_INTERVIEW_ROLES = frozenset({
    "hod",
    "head of department",
    "head of the department",
})

FACULTY_MOCK_INTERVIEW_MANAGER_ROLES = AUTHORIZED_FACULTY_MOCK_INTERVIEW_ROLES

# Roles allowed to open the Mentor / Class Advisor Mock Interview Reports
# dashboard. Mentors view their mentees, Class Advisors their class roster.
# HODs may also open it (their scope is still their own mentor/CA students).
ROSTER_DASHBOARD_ROLES = frozenset({
    "mentor",
    "ca",
    "class advisor",
    "class adviser",
    "advisor",
    "hod",
    "head of department",
    "head of the department",
})


def is_student_user(user):
    employee_id = str(getattr(user, "Employee_id", "") or "").strip()
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", True)
        and getattr(user, "is_student", False)
        and employee_id
    )


def user_role_name(user):
    """Return the normalized (lowercased) ERP role name for a user."""
    role = getattr(user, "role", None)
    return str(getattr(role, "role", "") or "").strip().lower()


def is_faculty_or_hod(user):
    """Return True only for authorized users of the Faculty Mock Interview module.

    Only the Faculty and Head of Department (HOD) ERP roles may access the
    Faculty Mock Interview module. Superusers bypass role checks, matching the
    rest of the ERP RBAC. Students, parents, and every other ERP role are
    denied.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_student", False):
        return False
    if getattr(user, "is_parent", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user_role_name(user) in AUTHORIZED_FACULTY_MOCK_INTERVIEW_ROLES


def is_hod_user(user):
    """Return True for HOD-report users.

    HODs can view the department-scoped reports dashboard. Superusers keep the
    existing administrative bypass and can view all departments from the HOD
    report screen.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_student", False):
        return False
    if getattr(user, "is_parent", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user_role_name(user) in HOD_MOCK_INTERVIEW_ROLES


def is_mock_interview_faculty_manager(user):
    """Return True for users allowed to open the mock-interview dashboard.

    HOD users are allowed to create mock interviews from this workspace, but
    department-sensitive operations are pinned to their own department in the
    view/service layer.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_student", False):
        return False
    if getattr(user, "is_parent", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user_role_name(user) in FACULTY_MOCK_INTERVIEW_MANAGER_ROLES


def _faculty_profile_for(user):
    """Return the general_information row matching an employee ID, or None.

    Mentor / CA assignments are linked through the faculty profile, so this
    is the identity bridge between the ERP login (Employee_id) and the
    StudentDetails mentor/CA foreign keys.
    """
    employee_id = str(getattr(user, "Employee_id", "") or "").strip()
    if not employee_id:
        return None
    try:
        faculty_id = int(employee_id)
    except (TypeError, ValueError):
        return None
    try:
        from faculty_management.models import general_information
    except Exception:
        return None
    try:
        return general_information.objects.filter(faculty_id=faculty_id).only("pk").first()
    except Exception:
        return None


def user_has_mentor_or_ca_assignment(user):
    """Return True when the user holds any mentee or class-lead assignment.

    In this ERP mentors/Class Advisors are usually regular faculty whose role
    name is not literally "Mentor"/"Class Advisor"; the authoritative record is
    the StudentDetails.mentor / StudentDetails.ca foreign key pointing at the
    user's faculty profile. Any user with at least one such assignment is
    treated as a roster user, and the roster scope still only ever exposes
    those assigned students.
    """
    profile = _faculty_profile_for(user)
    if profile is None:
        return False
    try:
        from django.db.models import Q
        from user_accounts.models import StudentDetails
    except Exception:
        return False
    try:
        return StudentDetails.objects.filter(
            Q(mentor_id=profile.pk) | Q(ca_id=profile.pk)
        ).exists()
    except Exception:
        return False


def is_roster_dashboard_user(user):
    """Return True for users allowed to open the Mentor/CA Reports dashboard.

    Authorized when the caller carries a Mentor / Class Advisor / HOD ERP role
    (superusers bypass), or when they actually hold at least one mentee or
    class-lead assignment in StudentDetails. The student population is always
    scoped in the service layer to the caller's own mentees / class roster, so
    the authorization check alone can never grant access to another user's
    students.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_student", False):
        return False
    if getattr(user, "is_parent", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if user_role_name(user) in ROSTER_DASHBOARD_ROLES:
        return True
    return user_has_mentor_or_ca_assignment(user)


def student_identity(user):
    """Return database-safe identity fields for the authenticated ERP student."""
    if not is_student_user(user):
        raise PermissionDenied("A valid student Employee ID is required.")
    return {
        "student_employee_id": str(user.Employee_id).strip(),
        "student_name": str(getattr(user, "username", "") or "").strip()[:500],
    }


def student_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return redirect_to_login(request.get_full_path())
        if not is_student_user(request.user):
            raise PermissionDenied("Mock interviews are available to students only.")
        return view_func(request, *args, **kwargs)

    return wrapped

