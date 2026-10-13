from unittest.mock import patch
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from mock_interview.models import (
    AnswerEvaluation,
    InterviewQuestion,
    InterviewSession,
    StudentAnswer,
)
from mock_interview.services.interview_service import (
    InterviewStateError,
    _validate_answer_audio,
    current_question,
    finish_interview,
    save_and_transcribe_answer,
    start_interview,
    submit_answer,
)
from mock_interview.views.dashboard import _owned_session


QUESTION_DATA = {
    "question_text": "Explain a Python project you built.",
    "question_type": "technical",
    "source": "generated",
    "selection_reason": "Tests role-relevant experience.",
    "rubric": {"technical_correctness": 100},
    "expected_concepts": ["Python"],
    "model_name": "test-model",
}

EVALUATION_DATA = {
    "dimension_scores": {"technical_correctness": 8},
    "total_score": 80,
    "evidence": ["Built an API."],
    "strengths": ["Specific example"],
    "missing_concepts": [],
    "improvement_actions": ["Explain testing."],
    "improved_answer": "",
    "model_name": "test-model",
}


class InterviewWorkflowTests(TestCase):
    def setUp(self):
        self.session = InterviewSession.objects.create(
            student_employee_id="22CS001",
            student_name="Student One",
            role="Python Developer",
            interview_round="Technical",
            difficulty="Intermediate",
            question_count=3,
            consented_at=timezone.now(),
        )

    @patch(
        "mock_interview.services.interview_service.synthesize_question",
        return_value=None,
    )
    @patch(
        "mock_interview.services.interview_service.generate_question",
        return_value=QUESTION_DATA,
    )
    def test_start_creates_first_question(self, _generate, _synthesize):
        start_interview(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "in_progress")
        self.assertEqual(self.session.questions.count(), 1)

        # A repeated POST is idempotent and does not generate question 1 twice.
        start_interview(self.session)
        self.assertEqual(self.session.questions.count(), 1)
        self.assertEqual(_generate.call_count, 1)

    def test_failed_repeat_of_started_session_is_recovered(self):
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
        )
        self.session.status = "failed"
        self.session.started_at = timezone.now()
        self.session.error_message = "This interview cannot be started again."
        self.session.save(
            update_fields=("status", "started_at", "error_message")
        )

        recovered = start_interview(self.session)

        self.assertEqual(recovered.status, "in_progress")
        self.assertEqual(recovered.questions.get(), question)
        self.assertEqual(recovered.error_message, "")

    def test_completed_session_cannot_restart(self):
        self.session.status = "report_ready"
        self.session.started_at = timezone.now()
        self.session.completed_at = timezone.now()
        self.session.save(
            update_fields=("status", "started_at", "completed_at")
        )
        with self.assertRaisesMessage(
            InterviewStateError,
            "cannot be started again",
        ):
            start_interview(self.session)

    def test_transcribed_but_unsubmitted_answer_keeps_question_current(self):
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
        )
        StudentAnswer.objects.create(
            question=question,
            audio_file=SimpleUploadedFile(
                "answer.webm",
                b"\x1a\x45\xdf\xa3fixture",
                content_type="audio/webm",
            ),
            original_transcript="Draft transcript",
        )
        self.assertEqual(current_question(self.session), question)

    @patch(
        "mock_interview.services.interview_service.synthesize_question",
        return_value=None,
    )
    @patch(
        "mock_interview.services.interview_service.generate_question",
        return_value=QUESTION_DATA,
    )
    @patch(
        "mock_interview.services.interview_service."
        "evaluate_answer_and_generate_question",
        return_value=(EVALUATION_DATA, QUESTION_DATA),
    )
    def test_submit_marks_answer_and_creates_next_question(
        self,
        _evaluate,
        _generate,
        _synthesize,
    ):
        self.session.status = "in_progress"
        self.session.save(update_fields=("status",))
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
        )
        answer = StudentAnswer.objects.create(
            question=question,
            audio_file=SimpleUploadedFile(
                "answer.webm",
                b"\x1a\x45\xdf\xa3fixture",
                content_type="audio/webm",
            ),
            original_transcript="I built an API.",
        )

        evaluation, next_question = submit_answer(answer, "I built an API.")

        answer.refresh_from_db()
        self.assertIsNotNone(answer.submitted_at)
        self.assertEqual(evaluation.total_score, 80)
        self.assertEqual(next_question.sequence_number, 2)
        self.assertTrue(
            AnswerEvaluation.objects.filter(answer=answer).exists()
        )

    @patch(
        "mock_interview.services.interview_service.finish_interview"
    )
    @patch(
        "mock_interview.services.interview_service.evaluate_answer",
        return_value=EVALUATION_DATA,
    )
    def test_final_submit_redirect_stage_does_not_generate_report_inline(
        self,
        _evaluate,
        finish_now,
    ):
        self.session.status = "in_progress"
        self.session.question_count = 1
        self.session.save(update_fields=("status", "question_count"))
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
            rubric={"technical_correctness": 100},
        )
        answer = StudentAnswer.objects.create(
            question=question,
            audio_file=SimpleUploadedFile(
                "answer.webm",
                b"\x1a\x45\xdf\xa3fixture",
                content_type="audio/webm",
            ),
            original_transcript="I built an API.",
        )

        evaluation, next_question = submit_answer(answer, "I built an API.")

        self.session.refresh_from_db()
        self.assertEqual(evaluation.total_score, 80)
        self.assertIsNone(next_question)
        self.assertEqual(self.session.status, "completed")
        finish_now.assert_not_called()

    @patch(
        "mock_interview.services.interview_service._generate_report_pdf"
    )
    @patch(
        "mock_interview.services.interview_service.generate_report_text",
        return_value={
            "summary": "Grounded summary.",
            "strengths": ["Specific example"],
            "improvement_areas": ["Explain testing"],
            "learning_plan": ["Practice test design"],
            "model_name": "test-report-model",
        },
    )
    def test_completed_session_generates_report_idempotently(
        self,
        generate_report,
        _pdf,
    ):
        self.session.status = "completed"
        self.session.completed_at = timezone.now()
        self.session.save(update_fields=("status", "completed_at"))
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
        )
        answer = StudentAnswer.objects.create(
            question=question,
            audio_file=SimpleUploadedFile(
                "answer.webm",
                b"\x1a\x45\xdf\xa3fixture",
                content_type="audio/webm",
            ),
            original_transcript="I built an API.",
            corrected_transcript="I built an API.",
            submitted_at=timezone.now(),
        )
        AnswerEvaluation.objects.create(answer=answer, **EVALUATION_DATA)

        first_report = finish_interview(self.session)
        second_report = finish_interview(self.session)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "report_ready")
        self.assertEqual(first_report.pk, second_report.pk)
        self.assertEqual(generate_report.call_count, 1)
        self.assertEqual(first_report.information_graph["library"], "networkx")
        self.assertTrue(
            first_report.information_graph["insights"]["priority_improvements"]
        )

    def test_invalid_audio_is_rejected_before_stt(self):
        uploaded = SimpleUploadedFile(
            "answer.webm",
            b"not browser audio",
            content_type="audio/webm",
        )
        with self.assertRaisesMessage(ValueError, "Unsupported answer audio"):
            _validate_answer_audio(uploaded)

    def test_student_cannot_read_another_students_session(self):
        request = RequestFactory().get("/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
            Employee_id="22CS999",
            username="Another Student",
        )
        with self.assertRaises(Http404):
            _owned_session(request, self.session.public_id)

    @patch(
        "mock_interview.services.interview_service.transcribe_audio",
        return_value={
            "transcript": "I built a Django API.",
            "detected_language": "en",
            "confidence": 0.95,
            "words": [],
            "duration_seconds": 4,
            "model_name": "test-whisper",
        },
    )
    def test_valid_webm_is_saved_and_transcribed(self, _transcribe):
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text=QUESTION_DATA["question_text"],
            question_type="technical",
        )
        uploaded = SimpleUploadedFile(
            "answer.webm",
            b"\x1a\x45\xdf\xa3browser-audio",
            content_type="audio/webm",
        )
        answer = save_and_transcribe_answer(question, uploaded)
        self.assertEqual(answer.original_transcript, "I built a Django API.")
        self.assertIsNone(answer.submitted_at)
