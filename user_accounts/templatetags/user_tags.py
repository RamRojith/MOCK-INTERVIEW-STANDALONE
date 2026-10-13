from django import template

from user_accounts.models import Department, Role, USER

register = template.Library()


@register.simple_tag
def user_data_count(Employee_id):
    try:
        return USER.objects.filter(Employee_id=Employee_id).count()
    except USER.DoesNotExist:
        return False


@register.simple_tag
def switch_user_data(Employee_id):
    try:
        return USER.objects.filter(Employee_id=Employee_id, is_active=True)
    except USER.DoesNotExist:
        return False


@register.filter
def is_faculty_or_hod(user):
    """Return True only for Faculty/HOD roles (see access service)."""
    from mock_interview.services.access import is_faculty_or_hod as _check

    return _check(user)


@register.filter
def is_hod(user):
    """Return True only for HOD-report users (or superuser accounts)."""
    from mock_interview.services.access import is_hod_user as _check

    return _check(user)


@register.filter
def is_roster_user(user):
    """Return True only for Mentor / Class Advisor (or HOD) users."""
    from mock_interview.services.access import is_roster_dashboard_user as _check

    return _check(user)
