from django.contrib import admin

from user_accounts.models import Add_Department, Department, Role, StudentDetails, USER


@admin.register(USER)
class USERAdmin(admin.ModelAdmin):
    list_display = ("Employee_id", "username", "role", "Department", "is_student", "is_active")
    search_fields = ("Employee_id", "username")
    list_filter = ("is_student", "is_active", "role")


@admin.register(StudentDetails)
class StudentDetailsAdmin(admin.ModelAdmin):
    list_display = ("reg_no", "name", "department", "batch", "section", "is_active")
    search_fields = ("reg_no", "name")
    list_filter = ("batch", "section", "is_active")


admin.site.register(Department)
admin.site.register(Role)
admin.site.register(Add_Department)
