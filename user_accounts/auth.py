"""Password login backend for the standalone auth user.

Ported from the ERP's ``EmployeeIDBackend`` but bound to the local ``USER``
model (no ``.using('rit_approval_system')``), and it also accepts the ``username``
keyword that ``django.contrib.auth``'s ``AuthenticationForm`` passes.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password


class EmployeeIDBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, Employee_id=None, **kwargs):
        employee_id = str(Employee_id or username or "").strip()
        if not employee_id or password is None:
            return None
        User = get_user_model()
        employee = (
            User.objects.filter(Employee_id=employee_id, is_active=True)
            .order_by("id")
            .first()
        )
        if employee and check_password(password, employee.password):
            return employee
        return None

    def get_user(self, user_id):
        User = get_user_model()
        return User.objects.filter(pk=user_id).first()
