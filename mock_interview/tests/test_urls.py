import uuid
from pathlib import Path
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from mock_interview.views.dashboard import module_entry


class MockInterviewUrlTests(SimpleTestCase):
    def test_uuid_session_routes_reverse(self):
        public_id = uuid.uuid4()
        self.assertEqual(
            reverse("mock_interview:room", args=[public_id]),
            f"/mock-interview/session/{public_id}/",
        )
        self.assertEqual(
            reverse("mock_interview:session_status", args=[public_id]),
            f"/mock-interview/api/sessions/{public_id}/status/",
        )
        self.assertEqual(
            reverse("mock_interview:generate_report", args=[public_id]),
            f"/mock-interview/api/sessions/{public_id}/generate-report/",
        )
        self.assertEqual(
            reverse("mock_interview:runtime_status"),
            "/mock-interview/api/runtime/status/",
        )
        self.assertEqual(
            reverse("mock_interview:runtime_tts_sample"),
            "/mock-interview/api/runtime/tts/sample/",
        )

    def test_hod_dashboard_uses_uuid_safe_report_url_placeholder(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "mock_interview"
            / "hod_mock_interviews.html"
        )
        template = template_path.read_text(encoding="utf-8")

        self.assertNotIn("SESSION_PUBLACEHOLDER", template)
        self.assertIn("00000000-0000-0000-0000-000000000000", template)


class MockInterviewEntryRoutingTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_faculty_module_root_redirects_to_faculty_dashboard(self):
        request = self.factory.get(reverse("mock_interview:dashboard"))
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_student=False,
            is_parent=False,
            is_superuser=False,
            role=SimpleNamespace(role="Faculty"),
        )

        response = module_entry(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mock_interview:faculty_dashboard"))

    def test_hod_module_root_redirects_to_faculty_dashboard(self):
        request = self.factory.get(reverse("mock_interview:dashboard"))
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_student=False,
            is_parent=False,
            is_superuser=False,
            role=SimpleNamespace(role="HOD"),
        )

        response = module_entry(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mock_interview:faculty_dashboard"))

