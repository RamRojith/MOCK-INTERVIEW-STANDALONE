import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from mock_interview.ai.ollama_client import LocalModelError
from mock_interview.models import ResumeDocument
from mock_interview.services.information_graph import (
    build_resume_information_graph,
    graph_prompt_context,
)
from mock_interview.services.interview_service import enrich_resume
from mock_interview.ai.interview_engine import (
    DEFAULT_RUBRIC,
    evaluate_answer_and_generate_question,
    generate_question,
)
from mock_interview.views.dashboard import upload_resume


def make_pdf(text):
    """Build a minimal valid single-page PDF that pypdf can parse."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
    ]
    stream = b"BT /F1 24 Tf 100 700 Td (" + text.encode() + b") Tj ET"
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write((str(index) + " 0 obj ").encode() + obj + b" endobj\n")
    xref_pos = out.tell()
    out.write(("xref\n0 " + str(len(objects) + 1) + "\n").encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(("%010d 00000 n \n" % offset).encode())
    out.write(
        (
            "trailer\n<< /Size "
            + str(len(objects) + 1)
            + " /Root 1 0 R >>\nstartxref\n"
            + str(xref_pos)
            + "\n%%EOF"
        ).encode()
    )
    return out.getvalue()


def _student_request():
    return SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_student=True,
        Employee_id="22CS001",
        username="Student One",
    )


def _attached_messages(request):
    request.session = SessionStore()
    messages = FallbackStorage(request)
    setattr(request, "_messages", messages)
    return messages


class ResumeUploadViewTests(TestCase):
    def setUp(self):
        self.url = reverse("mock_interview:upload_resume")
        self.factory = RequestFactory()

    @patch(
        "mock_interview.services.interview_service.normalize_resume",
        return_value={
            "skills": ["Python", "SQL"],
            "projects": ["Feedback classifier"],
            "education": [],
            "experience": [],
            "certifications": [],
        },
    )
    def test_upload_valid_pdf_creates_parsed_resume(self, _normalize):
        uploaded = SimpleUploadedFile(
            "resume.pdf",
            make_pdf("Feedback classifier built with Python and SQL"),
            content_type="application/pdf",
        )
        request = self.factory.post(self.url, {"resume": uploaded})
        request.user = _student_request()
        _attached_messages(request)

        response = upload_resume(request)

        self.assertEqual(response.status_code, 302)
        resume = ResumeDocument.objects.get()
        self.assertIn(str(resume.public_id), response.url)
        self.assertEqual(resume.original_filename, "resume.pdf")
        self.assertEqual(resume.status, "parsed")
        self.assertEqual(resume.student_employee_id, "22CS001")
        self.assertIn("Feedback classifier", resume.extracted_text)
        self.assertEqual(
            resume.structured_profile["projects"],
            ["Feedback classifier"],
        )
        self.assertEqual(resume.information_graph["graph_type"], "resume")

    def test_upload_invalid_file_does_not_create_resume(self):
        uploaded = SimpleUploadedFile(
            "resume.pdf",
            b"not-a-pdf",
            content_type="application/pdf",
        )
        request = self.factory.post(self.url, {"resume": uploaded})
        request.user = _student_request()
        messages = _attached_messages(request)

        response = upload_resume(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ResumeDocument.objects.count(), 0)
        self.assertEqual(len(messages), 1)

    def test_upload_without_file_redirects_back(self):
        request = self.factory.post(self.url, {})
        request.user = _student_request()
        messages = _attached_messages(request)

        response = upload_resume(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ResumeDocument.objects.count(), 0)
        self.assertEqual(len(messages), 1)

    def test_upload_requires_authentication(self):
        request = self.factory.post(self.url, {})
        request.user = SimpleNamespace(is_authenticated=False)

        response = upload_resume(request)

        self.assertEqual(response.status_code, 302)


class EnrichResumeTests(TestCase):
    def _resume(self, extracted_text="Feedback classifier built with Python and SQL."):
        return ResumeDocument.objects.create(
            student_employee_id="22CS001",
            student_name="Student One",
            file=SimpleUploadedFile(
                "resume.pdf",
                make_pdf(extracted_text),
                content_type="application/pdf",
            ),
            original_filename="resume.pdf",
            sha256="0" * 64,
            extracted_text=extracted_text,
            status="uploaded",
        )

    @patch(
        "mock_interview.services.interview_service.normalize_resume",
        return_value={
            "skills": ["Python", "SQL"],
            "projects": ["Feedback classifier"],
            "education": [],
            "experience": [],
            "certifications": [],
        },
    )
    def test_enrich_resume_normalizes_and_builds_graph(self, _normalize):
        resume = self._resume()
        enrich_resume(resume)
        resume.refresh_from_db()

        self.assertEqual(resume.status, "parsed")
        self.assertEqual(resume.error_message, "")
        self.assertEqual(
            resume.structured_profile["projects"],
            ["Feedback classifier"],
        )
        self.assertEqual(resume.information_graph["graph_type"], "resume")
        self.assertIn(
            "Python",
            graph_prompt_context(resume)["top_skills"],
        )

    @patch(
        "mock_interview.services.interview_service.normalize_resume",
        side_effect=RuntimeError("offline"),
    )
    def test_enrich_resume_reports_normalization_failure(self, _normalize):
        resume = self._resume()
        enrich_resume(resume)
        resume.refresh_from_db()

        self.assertEqual(resume.status, "parsed")
        self.assertEqual(resume.structured_profile, {})
        self.assertIn("unavailable", resume.error_message)

    def test_enrich_resume_without_text_is_unchanged(self):
        resume = self._resume(extracted_text="")
        result = enrich_resume(resume)
        self.assertIs(result, resume)
        self.assertEqual(resume.status, "uploaded")


class ResumeAwareQuestionTests(SimpleTestCase):
    MODEL_RESULT = {
        "question_text": "Walk me through the feedback classifier you built with Python and SQL.",
        "question_type": "technical",
        "source": "generated",
        "selection_reason": "Grounded in the student resume.",
        "expected_concepts": ["Python", "SQL"],
        "rubric": DEFAULT_RUBRIC,
    }

    def _session(self, resume=None):
        questions = SimpleNamespace(values_list=lambda *args, **kwargs: [])
        return SimpleNamespace(
            role="Python Developer",
            interview_round="Technical",
            difficulty="Intermediate",
            target_skills=["Python"],
            job_description="",
            question_count=5,
            company_name="",
            resume=resume,
            questions=questions,
        )

    def _resume(self):
        resume = SimpleNamespace(
            public_id="resume-1",
            student_employee_id="22CS001",
            student_name="Student One",
            original_filename="resume.pdf",
            extracted_text="Built a Feedback classifier with Python and SQL.",
            structured_profile={
                "skills": ["Python", "SQL"],
                "projects": ["Feedback classifier built with Python and SQL"],
                "education": [],
                "experience": [],
                "certifications": [],
            },
        )
        resume.information_graph = build_resume_information_graph(resume)
        return resume

    def test_generate_question_sends_resume_to_model(self):
        resume = self._resume()
        session = self._session(resume)

        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value=self.MODEL_RESULT,
        ) as chat:
            result = generate_question(session, 1)

        messages = chat.call_args.args[0]
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["resume_reference"], resume.extracted_text)
        self.assertEqual(
            payload["resume_information_graph"],
            graph_prompt_context(resume),
        )
        self.assertEqual(result["model_name"], "qwen3:1.7b")

    def test_combined_turn_sends_resume_context_for_next_question(self):
        resume = self._resume()
        session = self._session(resume)
        question = SimpleNamespace(
            session=session,
            question_text="Explain a Python project.",
            rubric=DEFAULT_RUBRIC,
            expected_concepts=["Python"],
        )
        answer = SimpleNamespace(
            corrected_transcript="I built a feedback classifier.",
            original_transcript="",
            speech_metrics={},
        )

        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value={
                "evaluation": {
                    "dimension_scores": {
                        "technical_correctness": 8,
                        "completeness": 8,
                        "relevance": 8,
                        "structure": 8,
                        "practical_example": 8,
                    },
                    "evidence": ["Built an API."],
                    "strengths": ["Specific example"],
                    "missing_concepts": [],
                    "improvement_actions": ["Explain testing."],
                    "improved_answer": "",
                },
                "next_question": self.MODEL_RESULT,
            },
        ) as chat:
            evaluation, next_question = evaluate_answer_and_generate_question(
                question, answer, 2
            )

        messages = chat.call_args.args[0]
        payload = json.loads(messages[1]["content"])
        self.assertEqual(
            payload["interview"]["resume_reference"],
            resume.extracted_text,
        )
        self.assertEqual(
            payload["interview"]["resume_information_graph"],
            graph_prompt_context(resume),
        )
        self.assertIsNotNone(next_question)
        self.assertEqual(evaluation["model_name"], "qwen3:1.7b")

    def test_resume_questions_never_leak_into_deterministic_fallback(self):
        session = self._session(self._resume())

        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            side_effect=LocalModelError("offline"),
        ):
            result = generate_question(session, 1)

        self.assertEqual(result["model_name"], "deterministic-fallback")
        self.assertNotIn("Feedback classifier", result["question_text"])
        self.assertIn("Python", result["question_text"])
