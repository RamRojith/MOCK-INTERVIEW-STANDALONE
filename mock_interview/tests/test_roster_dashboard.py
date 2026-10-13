"""Tests for the Mentor / Class Advisor Mock Interview Reports feature.

Focused on the security guarantee (a mentor can only ever see their own
mentees/class roster) and on data accuracy shared with the HOD reports
(unique-student counts, AND-logic filters, latest-score aggregation).
"""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import RequestFactory, TestCase
from django.utils import timezone

from mock_interview.models import InterviewReport, InterviewSession, MockInterview
from mock_interview.services import roster_reports_service as svc
from mock_interview.views import roster_dashboard as views
from user_accounts.models import Add_Department, StudentDetails

from faculty_management.models import general_information

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


def make_faculty(faculty_id, name):
    faculty, _ = general_information.objects.get_or_create(
        faculty_id=faculty_id,
        defaults={"name": name},
    )
    if faculty.name != name:
        faculty.name = name
        faculty.save(update_fields=["name"])
    return faculty


def make_student(reg_no, name, department, mentor=None, ca=None,
                 batch="2023-2027", year="3rd Year", section="A", semester="5"):
    return StudentDetails.objects.create(
        reg_no=reg_no,
        name=name,
        department=department,
        batch=batch,
        year=year,
        section=section,
        semester=semester,
        mentor=mentor,
        ca=ca,
        is_active=True,
    )


def make_mock_interview(subject_code):
    return MockInterview.objects.create(
        title=f"Interview {subject_code}",
        subject_code=subject_code,
        chapter="Basics",
        created_by="7001",
        created_by_name="Faculty",
        target_skills=[],
        status="open",
    )


def make_session(student, *, status="report_ready", score=80, role="Software Engineer",
                 mock_interview=None, created_at=None):
    session = InterviewSession.objects.create(
        student_employee_id=student.reg_no,
        student_name=student.name,
        role=role,
        interview_round="Technical",
        difficulty="Intermediate",
        status=status,
        overall_score=Decimal(str(score)),
        mock_interview=mock_interview,
    )
    if created_at is not None:
        InterviewSession.objects.filter(pk=session.pk).update(created_at=created_at)
    return session


def make_report(session, overall="80", technical="75", communication="85"):
    return InterviewReport.objects.create(
        session=session,
        overall_score=Decimal(overall),
        technical_score=Decimal(technical),
        communication_score=Decimal(communication),
        summary="Report summary.",
    )


def role_user(role_name, employee_id="7001", superuser=False):
    return SimpleNamespace(
        Employee_id=employee_id,
        username="Faculty User",
        is_authenticated=True,
        is_active=True,
        is_student=False,
        is_parent=False,
        is_superuser=superuser,
        role=SimpleNamespace(role=role_name),
    )


class ResolveRosterTests(TestCase):
    def setUp(self):
        self.faculty = make_faculty(7001, "Mentor One")

    def test_superuser_is_unscoped(self):
        self.assertIsNone(svc.resolve_roster(role_user("Office Staff", superuser=True)))

    def test_known_faculty_returns_profile_pk(self):
        self.assertEqual(svc.resolve_roster(role_user("Mentor")), self.faculty.pk)

    def test_unknown_faculty_raises(self):
        with self.assertRaises(svc.RosterScopeError):
            svc.resolve_roster(role_user("Mentor", employee_id="99999"))

    def test_missing_employee_id_raises(self):
        user = SimpleNamespace(
            is_authenticated=True, is_student=False,
            is_superuser=False, role=SimpleNamespace(role="Mentor"),
        )
        with self.assertRaises(svc.RosterScopeError):
            svc.resolve_roster(user)


class ParseFiltersTests(TestCase):
    def test_role_domain_student_and_dates_are_parsed(self):
        params = {
            "student": "Arun",
            "role": "Software Engineer",
            "domain": "CS301",
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
        }
        result = svc.parse_filters(params, profile=1)
        self.assertEqual(result["student"], "Arun")
        self.assertEqual(result["role"], "Software Engineer")
        self.assertEqual(result["domain"], "CS301")
        self.assertEqual(str(result["date_from"]), "2026-01-01")
        self.assertEqual(str(result["date_to"]), "2026-01-31")

    def test_empty_and_all_values_are_dropped(self):
        params = {"role": "", "domain": "all", "date_from": "", "date_to": "not-a-date"}
        self.assertEqual(svc.parse_filters(params, profile=1), {})

    def test_invalid_status_and_performance_are_ignored(self):
        params = {"interview_status": "hacked", "performance": "hacked"}
        self.assertEqual(svc.parse_filters(params, profile=1), {})


class RosterServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cse = make_department("CSE", "CS")
        cls.ece = make_department("ECE", "EC")
        cls.m1 = make_faculty(7001, "Mentor One")
        cls.c1 = make_faculty(7002, "CA One")
        cls.m2 = make_faculty(7003, "Mentor Two")

        # Mentor m1 roster: s1, s2 (mentees) + s4 (mentee AND CA student).
        cls.s1 = make_student("23CS001", "Arun Kumar", cls.cse, mentor=cls.m1, year="3rd Year", section="A")
        cls.s2 = make_student("23CS002", "Priya S", cls.cse, mentor=cls.m1, year="3rd Year", section="A")
        cls.s3 = make_student("23CS003", "Ravi T", cls.cse, ca=cls.c1, year="3rd Year", section="B")
        cls.s4 = make_student("23CS004", "Meena K", cls.cse, mentor=cls.m1, ca=cls.c1, year="2nd Year", section="A")
        # Roster of another mentor: must never leak into m1's scope.
        cls.s5 = make_student("23CS005", "Vijay R", cls.cse, mentor=cls.m2, year="2nd Year", section="B")
        cls.s6 = make_student("23EC001", "Ece Student", cls.ece, year="3rd Year", section="A")

        # Older attempt on s1 (faculty-created CS301 interview -> domain CS301).
        sess_a = make_session(
            cls.s1, status="report_ready", score=82, role="Software Engineer",
            mock_interview=make_mock_interview("CS301"),
            created_at=timezone.now() - timedelta(days=30),
        )
        make_report(sess_a, overall="82", technical="80", communication="88")

        # Latest attempt on s1 (self-practice -> role AND domain = role).
        cls.sess_b = make_session(
            cls.s1, status="report_ready", score=90, role="Data Analyst",
        )
        make_report(cls.sess_b, overall="90", technical="85", communication="95", )

        # Attempted but not completed.
        make_session(cls.s2, status="in_progress", score=60)

        # CA-only student with a weak completed report.
        sess_c = make_session(cls.s3, status="report_ready", score=40, role="Network Engineer")
        make_report(sess_c, overall="40", technical="45", communication="40")

        # Student that is both mentee and CA class-list member, with a report.
        sess_d = make_session(cls.s4, status="report_ready", score=70, role="Data Analyst")
        make_report(sess_d, overall="70", technical="68", communication="75")

        make_session(cls.s5, status="report_ready", score=85)

    def test_mentor_scope_is_limited_to_own_mentees(self):
        dataset = svc.build_dataset(self.m1.pk, {})
        self.assertEqual(svc.compute_summary(dataset)["total_students"], 3)
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(regs, {"23CS001", "23CS002", "23CS004"})

    def test_ca_scope_is_class_roster_only(self):
        dataset = svc.build_dataset(self.c1.pk, {})
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(regs, {"23CS003", "23CS004"})

    def test_superuser_sees_all_students(self):
        dataset = svc.build_dataset(None, {})
        self.assertEqual(svc.compute_summary(dataset)["total_students"], 6)

    def test_relation_labels(self):
        rows = svc.student_rows(svc.build_dataset(None, {}), page=1, per_page=100)["students"]
        by_reg = {r["reg_no"]: r for r in rows}
        self.assertEqual(by_reg["23CS001"]["relation"], "Mentor")
        self.assertEqual(by_reg["23CS003"]["relation"], "Class Advisor")
        self.assertEqual(by_reg["23CS004"]["relation"], "Mentor & CA")
        self.assertEqual(by_reg["23EC001"]["relation"], "")

    def test_counts_and_latest_score(self):
        dataset = svc.build_dataset(self.m1.pk, {})
        summary = svc.compute_summary(dataset)
        self.assertEqual(summary["attempted"], 3)
        self.assertEqual(summary["completed"], 2)  # s1, s4
        rows = svc.student_rows(dataset, page=1, per_page=100)["students"]
        by_reg = {r["reg_no"]: r for r in rows}
        self.assertEqual(by_reg["23CS001"]["latest_score"], 90.0)
        self.assertEqual(by_reg["23CS001"]["interview_count"], 2)
        self.assertEqual(by_reg["23CS002"]["status"], "attempted")

    def test_role_filter(self):
        dataset = svc.build_dataset(self.m1.pk, {"role": "Data Analyst"})
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(regs, {"23CS001", "23CS004"})

    def test_domain_filter_covers_faculty_subject_and_practice(self):
        dataset = svc.build_dataset(self.m1.pk, {"domain": "CS301"})
        self.assertEqual(len(dataset["matching"]), 1)
        dataset = svc.build_dataset(self.m1.pk, {"domain": "Data Analyst"})
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS001", "23CS004"})

    def test_date_range_filter(self):
        # Only sess_b (30 days ago) falls in the window.
        today = timezone.now().date()
        filters = {
            "date_from": today - timedelta(days=31),
            "date_to": today - timedelta(days=29),
        }
        dataset = svc.build_dataset(self.m1.pk, filters)
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS001"})

    def test_student_search_by_reg_no(self):
        dataset = svc.build_dataset(self.m1.pk, {"student": "23CS002"})
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS002"})

    def test_student_search_by_name(self):
        dataset = svc.build_dataset(self.m1.pk, {"student": "arun"})
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS001"})

    def test_performance_band_in_ca_scope(self):
        dataset = svc.build_dataset(self.c1.pk, {"performance": "needs_improvement"})
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS003"})

    def test_status_completed_in_ca_scope(self):
        dataset = svc.build_dataset(self.c1.pk, {"interview_status": "completed"})
        self.assertEqual({agg.student.reg_no for agg in dataset["matching"]},
                         {"23CS003", "23CS004"})

    def test_department_filter_cannot_escape_roster_scope(self):
        # s3 is not in m1's scope; requesting ECE must not widen the roster.
        dataset = svc.build_dataset(self.m1.pk, {"department": "ECE"})
        self.assertEqual(svc.compute_summary(dataset)["total_students"], 0)
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertNotIn("23EC001", regs)

    def test_role_domain_summary(self):
        dataset = svc.build_dataset(self.m1.pk, {})
        summary = svc.compute_role_domain_summary(dataset)
        roles = {item["label"] for item in summary["roles"]}
        domains = {item["label"] for item in summary["domains"]}
        self.assertIn("Data Analyst", roles)
        self.assertIn("Software Engineer", roles)
        self.assertIn("CS301", domains)
        self.assertIn("Data Analyst", domains)

    def test_scope_mode_maps_ca_and_mentor_roles(self):
        self.assertEqual(svc.scope_mode("Class Advisor"), "ca")
        self.assertEqual(svc.scope_mode("class adviser"), "ca")
        self.assertEqual(svc.scope_mode("CA"), "ca")
        self.assertEqual(svc.scope_mode("Mentor"), "mentor")
        self.assertIsNone(svc.scope_mode("HOD"))
        self.assertIsNone(svc.scope_mode("Faculty"))
        self.assertIsNone(svc.scope_mode(""))

    def test_strict_ca_scope_lists_only_class_students(self):
        dataset = svc.build_dataset(self.c1.pk, {}, mode="ca")
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(regs, {"23CS003", "23CS004"})
        labels = {
            r["reg_no"]: r["relation"]
            for r in svc.student_rows(dataset, page=1, per_page=100)["students"]
        }
        self.assertEqual(
            labels, {"23CS003": "Class Advisor", "23CS004": "Class Advisor"}
        )

    def test_strict_mentor_scope_lists_only_mentees(self):
        dataset = svc.build_dataset(self.m1.pk, {}, mode="mentor")
        regs = {agg.student.reg_no for agg in dataset["matching"]}
        self.assertEqual(regs, {"23CS001", "23CS002", "23CS004"})
        labels = {
            r["reg_no"]: r["relation"]
            for r in svc.student_rows(dataset, page=1, per_page=100)["students"]
        }
        self.assertEqual(
            labels,
            {"23CS001": "Mentor", "23CS002": "Mentor", "23CS004": "Mentor"},
        )

    def test_mentor_cannot_see_student_merely_via_ca_relationship(self):
        # s3 is CA-assigned to c1 (mentor is unset) - never in a mentor scope.
        dataset = svc.build_dataset(self.m1.pk, {}, mode="mentor")
        self.assertNotIn("23CS003", {agg.student.reg_no for agg in dataset["matching"]})

    def test_ca_cannot_see_student_merely_via_mentor_relationship(self):
        # s1/s2 are m1's mentees (no ca) - never in a CA scope.
        dataset = svc.build_dataset(self.c1.pk, {}, mode="ca")
        self.assertNotIn("23CS001", {agg.student.reg_no for agg in dataset["matching"]})
        self.assertNotIn("23CS002", {agg.student.reg_no for agg in dataset["matching"]})

    def test_union_scope_relationship_is_profile_relative(self):
        # s4 has BOTH a mentor (m1) and a CA (c1). Each viewer sees only their
        # own relationship, never "Mentor & CA" because of the other person.
        dataset = svc.build_dataset(self.m1.pk, {}, mode=None)
        rows = {
            r["reg_no"]: r["relation"]
            for r in svc.student_rows(dataset, page=1, per_page=100)["students"]
        }
        self.assertEqual(rows["23CS004"], "Mentor")
        ca_dataset = svc.build_dataset(self.c1.pk, {}, mode=None)
        ca_rows = {
            r["reg_no"]: r["relation"]
            for r in svc.student_rows(ca_dataset, page=1, per_page=100)["students"]
        }
        self.assertIn("23CS004", ca_rows)
        self.assertEqual(ca_rows["23CS004"], "Class Advisor")

    def test_filter_options_respect_strict_scope(self):
        # m1's mentor scope has no students in section B (s3/s5 only).
        options = svc.filter_options(self.m1.pk, {}, mode="mentor")
        self.assertNotIn("B", options["sections"])
        self.assertIn("A", options["sections"])
        options = svc.filter_options(self.c1.pk, {}, mode="ca")
        self.assertIn("B", options["sections"])  # s3 (CA-only student)
        self.assertNotIn("Software Engineer", options["roles"])  # s1 not in CA scope

    def test_active_scope_matches_role(self):
        profile, mode = svc.active_scope(role_user("Class Advisor", employee_id="7002"))
        self.assertEqual(mode, "ca")
        self.assertEqual(profile, self.c1.pk)
        profile, mode = svc.active_scope(role_user("Mentor"))
        self.assertEqual(mode, "mentor")
        self.assertEqual(profile, self.m1.pk)
        profile, mode = svc.active_scope(role_user("Office Staff", superuser=True))
        self.assertIsNone(profile)
        self.assertIsNone(mode)

    def test_pagination(self):
        dataset = svc.build_dataset(None, {})
        rows = svc.student_rows(dataset, page=1, per_page=2)
        self.assertEqual(len(rows["students"]), 2)
        self.assertEqual(rows["pagination"]["total"], 6)

    def test_dependent_filter_options(self):
        options = svc.filter_options(self.m1.pk, {})
        self.assertIn("A", options["sections"])
        self.assertNotIn("B", options["sections"])  # s3 is not m1's rowner
        self.assertIn("Software Engineer", options["roles"])
        self.assertIn("CS301", options["domains"])
        self.assertEqual(options["departments"], ["CSE"])

    def test_no_role_domains_when_no_sessions(self):
        reg = make_student("23CS099", "No Session", self.cse, mentor=self.m1).reg_no
        info = svc._load_role_domains([reg])
        self.assertEqual(info[reg]["roles"], set())
        self.assertEqual(info[reg]["domains"], set())


class AccessControlTests(TestCase):
    def test_roster_roles_are_allowed(self):
        for role in ("Mentor", "CA", "class advisor", "Class Adviser", "advisor", "HOD", "Head of Department"):
            user = SimpleNamespace(
                is_authenticated=True, is_student=False, is_parent=False,
                is_superuser=False, role=SimpleNamespace(role=role),
            )
            self.assertTrue(views._is_roster(user), role)

    def test_other_roles_are_denied(self):
        for role in ("Office Staff", "Lab Technician", "Librarian", "Placement Staff", "Student", ""):
            user = SimpleNamespace(
                is_authenticated=True, is_student=False, is_parent=False,
                is_superuser=False, role=SimpleNamespace(role=role),
            )
            self.assertFalse(views._is_roster(user), role)

    def test_student_parent_and_anonymous_are_denied(self):
        student = SimpleNamespace(is_authenticated=True, is_student=True, role=SimpleNamespace(role="Mentor"))
        parent = SimpleNamespace(is_authenticated=True, is_parent=True, role=SimpleNamespace(role="CA"))
        self.assertFalse(views._is_roster(student))
        self.assertFalse(views._is_roster(parent))

    def test_faculty_with_actual_assignment_is_allowed(self):
        dept = make_department("CSE", "CS")
        faculty = make_faculty(7005, "Assigned Mentor")
        make_student("23CS050", "Assigned Student", dept, mentor=faculty)
        self.assertTrue(
            views._is_roster(role_user("Faculty", employee_id="7005"))
        )

    def test_faculty_without_assignment_is_denied(self):
        make_faculty(7006, "Plain Faculty")
        self.assertFalse(
            views._is_roster(role_user("Faculty", employee_id="7006"))
        )

    def test_advisor_role_is_allowed_without_assignment(self):
        self.assertTrue(
            views._is_roster(role_user("Class Advisor", employee_id="40404"))
        )

    def test_hod_role_is_allowed_without_assignment(self):
        self.assertTrue(
            views._is_roster(role_user("HOD", employee_id="40404"))
        )


class ErpTagStubMixin:
    """Stub the ERP template tags that query the unconfigured ERP database.

    Rendering base.html invokes user_data_count/switch_user_data, which query
    the live 'rit_approval_system' ERP database. The stub returns empty data
    so template-rendering tests run against the isolated sqlite test DB only.
    """

    def setUp(self):
        self.factory = RequestFactory()

        from user_accounts.templatetags import user_tags

        class _FakeErpQuerySet:
            def using(self, alias):
                return self

            def filter(self, **kwargs):
                return self

            def count(self):
                return 0

            def __iter__(self):
                return iter(())

        self._saved_erp_user = user_tags.USER
        user_tags.USER = SimpleNamespace(objects=_FakeErpQuerySet())
        self.addCleanup(self._restore_erp_user, user_tags)

    def _restore_erp_user(self, user_tags):
        user_tags.USER = self._saved_erp_user


class RosterViewTests(ErpTagStubMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cse = make_department("CSE", "CS")
        cls.mentor = make_faculty(7001, "Mentor One")
        cls.other_mentor = make_faculty(7004, "Mentor Four")
        cls.s1 = make_student("23CS001", "Arun Kumar", cls.cse, mentor=cls.mentor)
        cls.s2 = make_student("23CS009", "Other Student", cls.cse, mentor=cls.other_mentor)
        cls.sess_s1 = make_session(cls.s1, status="report_ready", score=88, role="Software Engineer")
        make_report(cls.sess_s1, overall="88", technical="84", communication="92")
        make_session(cls.s2, status="report_ready", score=60)

    def _messages(self, request):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.backends.db import SessionStore
        request.session = SessionStore()
        messages = FallbackStorage(request)
        setattr(request, "_messages", messages)
        return messages

    def test_dashboard_renders_for_mentor(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Mentor")
        response = views.mentor_ca_mock_interviews(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"23CS001", response.content)
        self.assertNotIn(b"23CS003", response.content)

    def test_dashboard_renders_for_ca_hod_and_superuser(self):
        for user in (role_user("Class Advisor"), role_user("HOD"), role_user("Office Staff", superuser=True)):
            request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
            request.user = user
            response = views.mentor_ca_mock_interviews(request)
            self.assertEqual(response.status_code, 200)

    def test_dashboard_denies_other_roles(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Office Staff", employee_id="40404")
        response = views.mentor_ca_mock_interviews(request)
        self.assertEqual(response.status_code, 403)

    def test_dashboard_redirects_unmapped_profile(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Mentor", employee_id="40404")
        self._messages(request)
        response = views.mentor_ca_mock_interviews(request)
        self.assertEqual(response.status_code, 302)

    def test_dashboard_renders_for_faculty_mentor_by_assignment(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Faculty", employee_id="7001")
        response = views.mentor_ca_mock_interviews(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"23CS001", response.content)
        self.assertNotIn(b"23CS009", response.content)

    def test_dashboard_denies_faculty_without_assignments(self):
        make_faculty(7005, "Unassigned Faculty")
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Faculty", employee_id="7005")
        response = views.mentor_ca_mock_interviews(request)
        self.assertEqual(response.status_code, 403)

    def test_is_roster_user_filter_allows_assigned_faculty(self):
        from user_accounts.templatetags import user_tags
        make_faculty(7005, "Unassigned Faculty")
        self.assertTrue(user_tags.is_roster_user(role_user("Faculty", employee_id="7001")))
        self.assertFalse(user_tags.is_roster_user(role_user("Faculty", employee_id="7005")))

    def test_api_returns_scoped_students(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports_api"))
        request.user = role_user("Mentor")
        response = views.mentor_ca_mock_interviews_api(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["summary"]["total_students"], 1)
        self.assertEqual(payload["students"][0]["reg_no"], "23CS001")

    def test_api_denied_for_other_roles(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports_api"))
        request.user = role_user("Office Staff", employee_id="40404")
        response = views.mentor_ca_mock_interviews_api(request)
        self.assertEqual(response.status_code, 403)

    def test_filter_options_api(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_filter_options_api"))
        request.user = role_user("Mentor")
        response = views.mentor_ca_filter_options_api(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIn("Software Engineer", payload["roles"])
        self.assertIn("Software Engineer", payload["domains"])

    def test_role_leaderboard_api_returns_ranked_students(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_api"),
            {"scope": "Software Engineer", "scope_type": "role"},
        )
        request.user = role_user("Mentor")
        response = views.mentor_ca_role_leaderboard_api(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["scope"], "Software Engineer")
        self.assertEqual(payload["scope_type"], "role")
        self.assertEqual([r["reg_no"] for r in payload["rows"]], ["23CS001"])
        self.assertEqual(payload["rows"][0]["rank"], 1)

    def test_role_leaderboard_api_denied_for_other_roles(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_api"),
            {"scope": "Software Engineer"},
        )
        request.user = role_user("Office Staff", employee_id="40404")
        response = views.mentor_ca_role_leaderboard_api(request)
        self.assertEqual(response.status_code, 403)

    def test_role_leaderboard_api_requires_scope(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_role_leaderboard_api"))
        request.user = role_user("Mentor")
        response = views.mentor_ca_role_leaderboard_api(request)
        self.assertEqual(response.status_code, 400)

    def test_role_leaderboard_api_invalid_scope_type(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_api"),
            {"scope": "Software Engineer", "scope_type": "bogus"},
        )
        request.user = role_user("Mentor")
        response = views.mentor_ca_role_leaderboard_api(request)
        self.assertEqual(response.status_code, 400)

    def test_role_leaderboard_page_renders_for_mentor(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_page"),
            {"scope": "Software Engineer", "scope_type": "role"},
        )
        request.user = role_user("Mentor")
        response = views.mentor_ca_role_leaderboard_page(request)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("23CS001", content)
        self.assertNotIn("23CS003", content)

    def test_role_leaderboard_page_redirects_without_scope(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_page")
        )
        request.user = role_user("Mentor")
        self._messages(request)
        response = views.mentor_ca_role_leaderboard_page(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("mock_interview:mentor_ca_reports"),
        )

    def test_role_leaderboard_page_redirects_invalid_scope_type(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_page"),
            {"scope": "Software Engineer", "scope_type": "bogus"},
        )
        request.user = role_user("Mentor")
        self._messages(request)
        response = views.mentor_ca_role_leaderboard_page(request)
        self.assertEqual(response.status_code, 302)

    def test_role_leaderboard_page_denied_for_other_roles(self):
        request = self.factory.get(
            reverse("mock_interview:mentor_ca_role_leaderboard_page"),
            {"scope": "Software Engineer"},
        )
        request.user = role_user("Office Staff", employee_id="40404")
        response = views.mentor_ca_role_leaderboard_page(request)
        self.assertEqual(response.status_code, 403)

    def test_detail_visible_for_own_student(self):
        request = self.factory.get(reverse(
            "mock_interview:mentor_ca_report_detail",
            kwargs={"session_public_id": self.sess_s1.public_id},
        ))
        request.user = role_user("Mentor")
        response = views.mentor_ca_student_report_detail(request, self.sess_s1.public_id)
        self.assertEqual(response.status_code, 200)

    def test_detail_denied_for_unrelated_student(self):
        other_session = InterviewSession.objects.get(
            student_employee_id="23CS009"
        )
        request = self.factory.get(reverse(
            "mock_interview:mentor_ca_report_detail",
            kwargs={"session_public_id": other_session.public_id},
        ))
        request.user = role_user("Mentor")
        response = views.mentor_ca_student_report_detail(request, other_session.public_id)
        self.assertEqual(response.status_code, 403)

    def test_pdf_denied_for_unrelated_student(self):
        other_session = InterviewSession.objects.get(
            student_employee_id="23CS009"
        )
        request = self.factory.get(reverse(
            "mock_interview:mentor_ca_report_pdf",
            kwargs={"session_public_id": other_session.public_id},
        ))
        request.user = role_user("Mentor")
        response = views.mentor_ca_report_pdf(request, other_session.public_id)
        self.assertEqual(response.status_code, 403)

    def test_pdf_404_when_no_report_file(self):
        request = self.factory.get(reverse(
            "mock_interview:mentor_ca_report_pdf",
            kwargs={"session_public_id": self.sess_s1.public_id},
        ))
        request.user = role_user("Mentor")
        response = views.mentor_ca_report_pdf(request, self.sess_s1.public_id)
        self.assertEqual(response.status_code, 404)


class RoleLeaderboardTests(TestCase):
    """Service tests for the per-role/domain performance leaderboard."""

    @classmethod
    def setUpTestData(cls):
        cls.cse = make_department("CSE", "CS")
        cls.m1 = make_faculty(7001, "Mentor One")
        cls.c1 = make_faculty(7002, "CA One")
        cls.s1 = make_student("23CS001", "Arun Kumar", cls.cse, mentor=cls.m1, section="A")
        cls.s2 = make_student("23CS002", "Priya S", cls.cse, mentor=cls.m1, section="B")
        cls.s3 = make_student("23CS003", "Ravi T", cls.cse, ca=cls.c1, section="C")
        cls.s4 = make_student("23CS004", "Meena K", cls.cse, mentor=cls.m1, section="A")
        cls.s5 = make_student("23CS005", "Vijay R", cls.cse, mentor=cls.m1, section="B")

        # s1: an old faculty-created CS301 attempt, plus three completed
        # role attempts for "Data Science Engineer" — the leaderboard must use
        # the latest (80), never the higher older scores.
        mock_cs301 = make_mock_interview("CS301")
        sess_cs = make_session(
            cls.s1, status="report_ready", score=70, role="Data Science Engineer",
            mock_interview=mock_cs301,
            created_at=timezone.now() - timedelta(days=20),
        )
        make_report(sess_cs, overall="70")
        for days, score in ((10, 88), (5, 95)):
            sess = make_session(
                cls.s1, status="report_ready", score=score,
                role="Data Science Engineer",
                created_at=timezone.now() - timedelta(days=days),
            )
            make_report(sess, overall=str(score))
        cls.s1_latest = make_session(
            cls.s1, status="report_ready", score=80,
            role="Data Science Engineer",
            created_at=timezone.now(),
        )
        make_report(cls.s1_latest, overall="80", technical="78", communication="85")

        # Attempted but never completed — must never appear on the leaderboard.
        make_session(cls.s1, status="in_progress", score=50, role="Data Science Engineer")

        sess2 = make_session(cls.s2, status="report_ready", score=75, role="Data Analyst")
        make_report(sess2, overall="75", technical="70", communication="78")

        sess3 = make_session(cls.s3, status="report_ready", score=90, role="Data Science Engineer")
        make_report(sess3, overall="90")

        sess4 = make_session(cls.s4, status="report_ready", score=90, role="Data Science Engineer")
        make_report(sess4, overall="90")

        sess5 = make_session(cls.s5, status="report_ready", score=95, role="Data Science Engineer")
        make_report(sess5, overall="95")

    def test_leaderboard_only_completed_students_ranked_best_first(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        self.assertEqual([r["reg_no"] for r in rows], ["23CS005", "23CS004", "23CS001"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["overall_score"], 95.0)
        self.assertEqual(rows[2]["overall_score"], 80.0)

    def test_latest_score_for_the_role_is_used(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        # 80 (latest Data Science Engineer attempt), never the older 95/88.
        self.assertEqual(rows[2]["overall_score"], 80.0)

    def test_different_roles_are_excluded(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Analyst", scope_type="role", mode="mentor"
        )
        self.assertEqual([r["reg_no"] for r in rows], ["23CS002"])
        self.assertEqual(rows[0]["overall_score"], 75.0)

    def test_domain_scope_matches_faculty_subject_not_role(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="CS301", scope_type="domain", mode="mentor"
        )
        self.assertEqual([r["reg_no"] for r in rows], ["23CS001"])
        self.assertEqual(rows[0]["overall_score"], 70.0)
        # The role scope must NOT surface the CS301 attempt for s1 (70).
        role_rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        self.assertEqual(role_rows[2]["overall_score"], 80.0)

    def test_practice_role_domain_falls_back_to_role(self):
        # A self-practice session (no MockInterview) uses the role as domain.
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Analyst", scope_type="domain", mode="mentor"
        )
        self.assertEqual([r["reg_no"] for r in rows], ["23CS002"])

    def test_ca_and_mentor_scopes_are_relationship_scoped(self):
        mentor_rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        # s3 is a CA-only student of c1, never in m1's mentor scope.
        self.assertNotIn("23CS003", [r["reg_no"] for r in mentor_rows])
        ca_rows = svc.compute_role_leaderboard(
            self.c1.pk, scope="Data Science Engineer", scope_type="role", mode="ca"
        )
        self.assertEqual([r["reg_no"] for r in ca_rows], ["23CS003"])
        self.assertEqual(ca_rows[0]["overall_score"], 90.0)

    def test_report_fields_and_attempts_are_present(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        s1_row = next(r for r in rows if r["reg_no"] == "23CS001")
        # 4 completed Data Science Engineer attempts: the CS301 faculty
        # session plus three role sessions.
        self.assertEqual(s1_row["attempts"], 4)
        self.assertEqual(s1_row["technical_score"], 78.0)
        self.assertEqual(s1_row["communication_score"], 85.0)
        self.assertEqual(s1_row["relation"], "Mentor")
        self.assertTrue(s1_row["report_public_id"])

    def test_ties_share_rank(self):
        s6 = make_student("23CS006", "Tied K", self.cse, mentor=self.m1, section="A")
        sess6 = make_session(s6, status="report_ready", score=90, role="Data Science Engineer")
        make_report(sess6, overall="90")
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role", mode="mentor"
        )
        by_reg = {r["reg_no"]: r["rank"] for r in rows}
        self.assertEqual(by_reg["23CS004"], by_reg["23CS006"])
        self.assertEqual(by_reg["23CS006"], 2)
        self.assertEqual(by_reg["23CS001"], 4)

    def test_limit_truncates_rows(self):
        rows = svc.compute_role_leaderboard(
            self.m1.pk, scope="Data Science Engineer", scope_type="role",
            mode="mentor", limit=2
        )
        self.assertEqual(len(rows), 2)

    def test_missing_or_invalid_scope_returns_empty(self):
        self.assertEqual(
            svc.compute_role_leaderboard(self.m1.pk, scope="", scope_type="role", mode="mentor"),
            [],
        )
        self.assertEqual(
            svc.compute_role_leaderboard(self.m1.pk, scope="Data Analyst", scope_type="bogus", mode="mentor"),
            [],
        )


class RoleScopedViewTests(ErpTagStubMixin, TestCase):
    """End-to-end role-based scoping through the rendered dashboard/API/PDF.

    Covers the security contract: a CA user only ever sees their own class
    students labelled "Class Advisor", a Mentor only their mentees labelled
    "Mentor", and a student with both relationships is labelled according to
    the logged-in viewer's actual relationship.
    """

    @classmethod
    def setUpTestData(cls):
        cls.cse = make_department("CSE", "CS")
        cls.mentor = make_faculty(7001, "Mentor One")
        cls.ca = make_faculty(7009, "CA One")
        cls.other_mentor = make_faculty(7004, "Mentor Four")

        # Mentee-only student (mentor's own).
        cls.mentee = make_student("23CS001", "Mentee A", cls.cse, mentor=cls.mentor)
        # Class-only student (CA's own; mentor elsewhere).
        cls.class_student = make_student(
            "23CS002", "Class B", cls.cse, ca=cls.ca, mentor=cls.other_mentor
        )
        # Student with BOTH a mentor and a CA relationship in the system.
        cls.both = make_student(
            "23CS003", "Both C", cls.cse, mentor=cls.mentor, ca=cls.ca
        )

        cls.s_mentee = make_session(cls.mentee, score=85, role="IT Analyst")
        make_report(cls.s_mentee, overall="85")
        cls.s_both = make_session(cls.both, score=80, role="Data Analyst")
        make_report(cls.s_both, overall="80")
        cls.s_class = make_session(cls.class_student, score=70, role="Network Engineer")
        make_report(cls.s_class, overall="70")

    def _click(self, user):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = user
        return views.mentor_ca_mock_interviews(request)

    def _api(self, user):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports_api"))
        request.user = user
        return views.mentor_ca_mock_interviews_api(request)

    def test_ca_dashboard_only_class_students(self):
        response = self._click(role_user("Class Advisor", employee_id="7009"))
        self.assertEqual(response.status_code, 200)
        body = response.content
        self.assertIn(b"23CS002", body)
        self.assertIn(b"23CS003", body)
        self.assertNotIn(b"23CS001", body)

    def test_mentor_dashboard_only_mentees(self):
        response = self._click(role_user("Mentor", employee_id="7001"))
        self.assertEqual(response.status_code, 200)
        body = response.content
        self.assertIn(b"23CS001", body)
        self.assertIn(b"23CS003", body)
        self.assertNotIn(b"23CS002", body)

    def test_api_and_ui_share_role_based_scope_and_labels(self):
        for emp, role, expect_regs in (
            ("7009", "Class Advisor", {"23CS002", "23CS003"}),
            ("7001", "Mentor", {"23CS001", "23CS003"}),
        ):
            payload = json.loads(self._api(role_user(role, employee_id=emp)).content)
            self.assertEqual(
                {s["reg_no"] for s in payload["students"]},
                expect_regs,
                role,
            )
            expected_label = "Class Advisor" if role == "Class Advisor" else "Mentor"
            self.assertTrue(all(
                s["relation"] == expected_label for s in payload["students"]
            ), (role, payload["students"]))
            summary = payload["relation_summary"]
            visible = [c["label"] for c in summary if c["count"]]
            self.assertEqual(visible, [expected_label], role)

        # Both-student is labelled by the viewer's own relationship in the UI.
        ca_payload = json.loads(self._api(role_user("Class Advisor", employee_id="7009")).content)
        self.assertEqual(
            {s["reg_no"]: s["relation"] for s in ca_payload["students"]},
            {"23CS002": "Class Advisor", "23CS003": "Class Advisor"},
        )
        mentor_payload = json.loads(self._api(role_user("Mentor", employee_id="7001")).content)
        self.assertEqual(
            {s["reg_no"]: s["relation"] for s in mentor_payload["students"]},
            {"23CS001": "Mentor", "23CS003": "Mentor"},
        )

    def test_ca_cannot_open_mentee_only_report(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Class Advisor", employee_id="7009")
        response = views.mentor_ca_student_report_detail(
            request, self.s_mentee.public_id
        )
        self.assertEqual(response.status_code, 403)

    def test_mentor_cannot_open_class_only_report(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Mentor", employee_id="7001")
        response = views.mentor_ca_student_report_detail(
            request, self.s_class.public_id
        )
        self.assertEqual(response.status_code, 403)

    def test_both_students_report_open_for_each_relationship(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Class Advisor", employee_id="7009")
        response = views.mentor_ca_student_report_detail(
            request, self.s_both.public_id
        )
        self.assertEqual(response.status_code, 200)

        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Mentor", employee_id="7001")
        response = views.mentor_ca_student_report_detail(
            request, self.s_both.public_id
        )
        self.assertEqual(response.status_code, 200)

    def test_mentor_cannot_download_class_only_report(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Mentor", employee_id="7001")
        response = views.mentor_ca_report_pdf(request, self.s_class.public_id)
        self.assertEqual(response.status_code, 403)

    def test_ca_cannot_download_mentee_only_report(self):
        request = self.factory.get(reverse("mock_interview:mentor_ca_reports"))
        request.user = role_user("Class Advisor", employee_id="7009")
        response = views.mentor_ca_report_pdf(request, self.s_mentee.public_id)
        self.assertEqual(response.status_code, 403)


class URLTests(TestCase):
    def test_roster_routes_resolve(self):
        if reverse is None:
            self.skipTest("django.urls.reverse unavailable")
        self.assertTrue(reverse("mock_interview:mentor_ca_reports"))
        self.assertTrue(reverse("mock_interview:mentor_ca_reports_api"))
        self.assertTrue(reverse("mock_interview:mentor_ca_filter_options_api"))
        self.assertTrue(reverse("mock_interview:mentor_ca_role_leaderboard_api"))
        self.assertTrue(reverse("mock_interview:mentor_ca_role_leaderboard_page"))
        self.assertTrue(
            reverse(
                "mock_interview:mentor_ca_report_detail",
                kwargs={"session_public_id": "11111111-2222-3333-4444-555555555555"},
            )
        )
        self.assertTrue(
            reverse(
                "mock_interview:mentor_ca_report_pdf",
                kwargs={"session_public_id": "11111111-2222-3333-4444-555555555555"},
            )
        )