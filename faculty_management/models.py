"""Standalone stub of the ERP ``faculty_management`` models.

The ``mock_interview`` app only needs ``general_information`` — the faculty
profile row that bridges an ``Employee_id`` to a department and to the
mentor / class-advisor assignments on ``StudentDetails``.
"""

from django.db import models


class general_information(models.Model):
    faculty_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    designation = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=254, blank=True, default="")
    department = models.ForeignKey(
        "user_accounts.Add_Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faculty_profiles",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "faculty_management_general_information"
        verbose_name = "Faculty profile"

    def __str__(self):
        return f"{self.name} ({self.faculty_id})" if self.name else str(self.faculty_id)
