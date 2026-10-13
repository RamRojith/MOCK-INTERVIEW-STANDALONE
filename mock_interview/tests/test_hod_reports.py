"""Tests for the HOD Mock Interview Reports service and routes.

Focused on data accuracy: unique-student counts (no duplicates from multiple
interview attempts), AND-logic filters, department scoping and pagination.
"""

from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase

from mock_interview.models import InterviewReport, InterviewSession
from mock_interview.services import hod_reports_service as svc
from mock_interview.views.faculty import _create_interview
from user_accounts.models import Add_Department, StudentDetails

try:  # django.urls.reverse exists on all supported versions
    from django.urls import reverse
except ImportError:  # pragma: no cover
    reverse = None


def make_department(name, code):
    dept, _ = Add_Department.objects.get_or_create(
        Department=name,
        defaults={"Department_code": code, "is_active": True},
    )
    if dept.Department_code != code:
        dept.Department_code = code
        dept.save(update_fields=["Department_code"])
    return dept


def make_student(reg_no, name, department, batch="2023-2027", year="3rd Year",
                 section="A", semester="5"):
    return StudentDetails.objects.create(
        reg_no=reg_no,
        name=name,
        department=department,
        batch=batch,
        year=year,
        section=section,
        semester=semester,
        is_active=True,
    )


def make_session(student, *, status="report_ready", score=80):
    session = InterviewSession.objects.create(
        student_employee_id=student.reg_no,
        student_name=student.name,
        role="Software Engineer",
        interview_round="Technical",
        difficulty="Intermediate",
        status=status,
        overall_score=Decimal(str(score)),
    )
    return session


def make_report(session, overall="80", technical="75", communication="85"):
    return InterviewReport.objects.create(
        session=session,
        overall_score=Decimal(overall),
        technical_score=Decimal(technical),
        communication_score=Decimal(communication),
        summary="Report summary.",
    )


class ParseFiltersTests(TestCase):
    def setUp(self):
        self.dept = make_department("CSE", "CS")
        self.hod = SimpleNamespace(
            is_superuser=False,
            Department=SimpleNamespace(Department="CSE"),
            is_authenticated=True,
        )

    def test_empty_params_yield_no_filters(self):
        self.assertEqual(svc.parse_filters({}, hod_dept=self.dept), {})

    def test_all_and_empty_are_dropped(self):
        params = {"batch": "All", "year": "", "section": "all", "department": "1"}
        self.assertEqual(svc.parse_filters(params, hod_dept=self.dept), {})

    def test_valid_filters_are_parsed(self):
        params = {"batch": "2023-2027", "year": "3rd Year", "section": "A"}
        result = svc.parse_filters(params, hod_dept=self.dept)
        self.assertEqual(result["batch"], "2023-2027")
        self.assertEqual(result["year"], "3rd Year")
        self.assertEqual(result["section"], "A")

    def test_hod_cannot_escape_own_department_scope(self):
        params = {"department": str(999)}
        result = svc.parse_filters(params, hod_dept=self.dept)
        self.assertNotIn("department", result)

    def test_superuser_can_filter_department(self):
        params = {"department": "5"}
        result = svc.parse_filters(params, hod_dept=None)
        self.assertEqual(result["department"], "5")

    def test_invalid_status_and_performance_are_ignored(self):
        params = {"interview_status": "hacked", "performance": "hacked"}
        self.assertEqual(svc.parse_filters(params, hod_dept=self.dept), {})


class ServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cse = make_department("CSE", "CS")
        cls.ece = make_department("ECE", "EC")
        # CSE population
        cls.s1 = make_student("23CS001", "Arun Kumar", cls.cse, year="3rd Year", section="A")
        cls.s2 = make_student("23CS002", "Priya S", cls.cse, year="3rd Year", section="A")
        cls.s3 = make_student("23CS003", "Ravi T", cls.cse, year="3rd Year", section="B")
        cls.s4 = make_student("23CS004", "Meena K", cls.cse, year="2nd Year", section="A")
        cls.s5 = make_student("23CS005", "Vijay R", cls.cse, year="2nd Year", section="B")
        # Not in HOD's department
        cls.s6 = make_student("23EC001", "Ece Student", cls.ece, year="3rd Year", section="A")

        # Students with multiple completed sessions (duplicate-count guard)
        sess_1 = make_session(cls.s1, status="report_ready", score=82)
        make_report(sess_1, overall="82", technical="80", communication="88")
        sess_2 = make_session(cls.s1, status="report_ready", score=90)
        make_report(sess_2, overall="90", technical="85", communication="95")

        # A student still in progress (attempted but not completed)
        make_session(cls.s2, status="in_progress", score=60)

        # A completed student with a low score
        sess_3 = make_session(cls.s3, status="report_ready", score=40)
        make_report(sess_3, overall="40", technical="45", communication="40")

        # An attempted-but-not-completed 2nd year student
        make_session(cls.s4, status="completed", score=70)

        cls.filters = {}
        cls.dataset = svc.build_dataset(cls.cse, cls.filters)
        cls.summary = svc.compute_summary(cls.dataset)

    def test_no_filters_total_is_department_scoped(self):
        # 5 active CSE students; ECE student is excluded.
        self.assertEqual(self.summary["total_students"], 5)

    def test_unique_students_despite_multiple_attempts(self):
        # s1 has 2 sessions but must be counted once.
        self.assertEqual(self.summary["attempted"], 4)
        self.assertEqual(self.summary["completed"], 3)  # s1, s3, s4

    def test_not_attempted_is_total_minus_attempted(self):
        self.assertEqual(self.summary["not_attempted"], 5 - 4)

    def test_avg_uses_latest_attempt_per_student(self):
        # Latest per student: s1=90 (report), s2=60 (in-progress session),
        # s3=40 (report), s4=70 (completed session) -> 260/4 = 65.0
        self.assertEqual(self.summary["avg_interview_score"], 65.0)

    def test_avg_technical(self):
        # Latest technical: s1=85, s3=45 -> avg 65.0
        self.assertEqual(self.summary["avg_technical_score"], 65.0)

    def test_avg_communication(self):
        # Latest communication: s1=95, s3=40 -> avg 67.5
        self.assertEqual(self.summary["avg_communication_score"], 67.5)

    def test_and_logic_filters(self):
        dataset = svc.build_dataset(self.cse, {"year": "3rd Year", "section": "A"})
        summary = svc.compute_summary(dataset)
        students = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(summary["total_students"], 2)
        self.assertIn("23CS001", students)
        self.assertIn("23CS002", students)

    def test_interview_status_completed(self):
        dataset = svc.build_dataset(self.cse, {"interview_status": "completed"})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 3)  # s1, s3, s4

    def test_interview_status_attempted(self):
        # Attempted-but-not-completed: only s2 (in_progress).
        dataset = svc.build_dataset(self.cse, {"interview_status": "attempted"})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 1)

    def test_interview_status_not_attempted(self):
        dataset = svc.build_dataset(self.cse, {"interview_status": "not_attempted"})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 1)  # only s5

    def test_performance_band_excellent(self):
        dataset = svc.build_dataset(self.cse, {"performance": "excellent"})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 1)  # s1 (90)

    def test_performance_band_needs_improvement(self):
        dataset = svc.build_dataset(self.cse, {"performance": "needs_improvement"})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 1)  # s3 (40)

    def test_pagination_returns_expected_slice(self):
        rows = svc.student_rows(self.dataset, page=1, per_page=2)
        self.assertEqual(len(rows["students"]), 2)
        self.assertEqual(rows["pagination"]["total"], 5)
        self.assertEqual(rows["pagination"]["total_pages"], 3)
        rows2 = svc.student_rows(self.dataset, page=3, per_page=2)
        self.assertEqual(len(rows2["students"]), 1)

    def test_row_latest_score_from_latest_attempt(self):
        rows = svc.student_rows(self.dataset, page=1, per_page=100)["students"]
        by_reg = {r["reg_no"]: r for r in rows}
        self.assertEqual(by_reg["23CS001"]["latest_score"], 90.0)
        self.assertEqual(by_reg["23CS001"]["interview_count"], 2)
        self.assertEqual(by_reg["23CS002"]["status"], "attempted")
        self.assertEqual(by_reg["23CS005"]["status"], "not_attempted")

    def test_department_scope_never_leaks(self):
        # Even if a department param is supplied for a scoped HOD, it is
        # ignored and only CSE students are returned.
        params = {"department": str(self.ece.id)}
        filters = svc.parse_filters(params, hod_dept=self.cse)
        dataset = svc.build_dataset(self.cse, filters)
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 5)

    def test_superuser_can_filter_by_department_name(self):
        # Superuser passes hod_dept=None; the department dropdown posts names.
        params = {"department": "ECE", "year": "3rd Year"}
        filters = svc.parse_filters(params, hod_dept=None)
        dataset = svc.build_dataset(None, filters)
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["total_students"], 1)
        self.assertEqual(dataset["matching"][0].student.reg_no, "23EC001")

    def test_dependent_filter_options(self):
        options = svc.filter_options(self.cse, {"year": "3rd Year"})
        self.assertIn("A", options["sections"])
        self.assertIn("B", options["sections"])
        self.assertNotIn("2024-2028", options["batches"])  # none exist

    def test_missing_semester_defaults(self):
        student = make_student("23CS099", "No Sem", self.cse, semester=None)
        dataset = svc.build_dataset(self.cse, {"section": "Z"})
        self.assertEqual(svc.compute_summary(dataset)["total_students"], 0)

    def test_hod_create_interview_defaults_to_own_department(self):
        hod = SimpleNamespace(
            Employee_id="7001",
            username="CSE HOD",
            is_authenticated=True,
            is_student=False,
            is_parent=False,
            is_superuser=False,
            role=SimpleNamespace(role="HOD"),
            Department=SimpleNamespace(Department="CSE"),
        )

        interview, auto_assigned = _create_interview(hod, {
            "title": "Department Mock Interview",
            "subject_code": "CS301",
            "department_id": "",
            "target_batch": "2023-2027",
        })

        self.assertEqual(interview.target_department_id, self.cse.id)
        self.assertEqual(auto_assigned, 5)
        assigned = set(interview.assignments.values_list("student_employee_id", flat=True))
        self.assertIn("23CS001", assigned)
        self.assertNotIn("23EC001", assigned)

    def test_hod_create_interview_rejects_other_department(self):
        hod = SimpleNamespace(
            Employee_id="7001",
            username="CSE HOD",
            is_authenticated=True,
            is_student=False,
            is_parent=False,
            is_superuser=False,
            role=SimpleNamespace(role="HOD"),
            Department=SimpleNamespace(Department="CSE"),
        )

        with self.assertRaisesMessage(ValueError, "HOD access is limited"):
            _create_interview(hod, {
                "title": "Bad Department Mock Interview",
                "subject_code": "CS301",
                "department_id": str(self.ece.id),
            })


class URLTests(TestCase):
    def test_hod_routes_resolve(self):
        if reverse is None:
            self.skipTest("django.urls.reverse unavailable")
        self.assertTrue(reverse("mock_interview:hod_reports"))
        self.assertTrue(reverse("mock_interview:hod_reports_api"))
        self.assertTrue(reverse("mock_interview:hod_filter_options_api"))
        self.assertTrue(
            reverse(
                "mock_interview:hod_report_detail",
                kwargs={"session_public_id": "11111111-2222-3333-4444-555555555555"},
            )
        )
        self.assertTrue(reverse("mock_interview:faculty_dashboard"))

