"""Standalone stub of the ERP ``user_accounts`` models.

Only the surface that the ``mock_interview`` app actually touches is provided:

* ``USER`` — the custom auth user (``AUTH_USER_MODEL``). Exposes ``Employee_id``
  (login id), ``role``, ``Department``, ``is_student`` and ``is_parent`` which
  the app's access layer reads.
* ``Role`` / ``Department`` / ``Add_Department`` — small lookup tables.
* ``StudentDetails`` — the student roster the interviews and reports are built
  from, including the ``mentor`` / ``ca`` faculty foreign keys.

The real ERP keeps ``USER`` and ``StudentDetails`` in a separate approval
database; here they live in the same local SQLite database as everything else.
"""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class Department(models.Model):
    Department = models.CharField(max_length=255, blank=True, default="")
    Department_code = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "user_accounts_department"

    def __str__(self):
        return self.Department or self.Department_code or f"Department #{self.pk}"


class Role(models.Model):
    role = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        db_table = "user_accounts_role"

    def __str__(self):
        return self.role or f"Role #{self.pk}"


class Add_Department(models.Model):
    Department = models.CharField(max_length=255, blank=True, default="")
    Department_code = models.CharField(max_length=50, blank=True, default="")
    is_academic = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        db_table = "user_accounts_add_department"

    def __str__(self):
        return self.Department or self.Department_code or f"Add_Department #{self.pk}"


class UserManager(BaseUserManager):
    def create_user(self, Employee_id, password=None, **extra):
        if not Employee_id:
            raise ValueError("Employee_id is required")
        user = self.model(Employee_id=str(Employee_id).strip(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, Employee_id, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self.create_user(Employee_id, password, **extra)


class USER(AbstractBaseUser, PermissionsMixin):
    """Local auth user mirror with the fields the mock_interview app reads."""

    username = models.CharField(max_length=150, blank=True, default="")
    Employee_id = models.CharField(max_length=100, unique=True, db_index=True)
    email = models.CharField(max_length=254, blank=True, default="")
    profile_img = models.CharField(max_length=255, blank=True, default="")

    role = models.ForeignKey(
        Role, on_delete=models.SET_NULL, null=True, blank=True, related_name="users"
    )
    Department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )

    is_student = models.BooleanField(default=False)
    is_parent = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    semester = models.CharField(max_length=50, blank=True, default="")

    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "Employee_id"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "user_accounts_user"

    def __str__(self):
        return f"{self.Employee_id} ({self.username})" if self.username else self.Employee_id

    def get_full_name(self):
        return self.username or self.Employee_id

    def get_short_name(self):
        return self.username or self.Employee_id


class StudentDetails(models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    reg_no = models.CharField(
        max_length=255, unique=True, null=True, blank=True, verbose_name="Register Number"
    )
    department = models.ForeignKey(
        Add_Department,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="students",
    )
    regulation = models.CharField(max_length=50, null=True, blank=True)
    batch = models.CharField(max_length=50, null=True, blank=True)
    year = models.CharField(max_length=50, null=True, blank=True)
    semester = models.CharField(max_length=50, null=True, blank=True)
    umis_id = models.CharField(max_length=50, null=True, blank=True)
    section = models.CharField(max_length=10, null=True, blank=True)
    email = models.CharField(max_length=254, null=True, blank=True)
    mobile_no = models.CharField(max_length=15, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)

    mentor = models.ForeignKey(
        "faculty_management.general_information",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mentor_students",
    )
    ca = models.ForeignKey(
        "faculty_management.general_information",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ca_students",
    )

    class Meta:
        db_table = "user_accounts_student_details"

    def __str__(self):
        return f"{self.name} ({self.reg_no})"
