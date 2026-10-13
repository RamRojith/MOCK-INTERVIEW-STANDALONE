"""Tests for the fast interview pipeline (pre-planned pool + background work).

These tests explicitly enable MOCK_INTERVIEW["FAST_INTERVIEW_PIPELINE"]=True and
exercise the student-facing fast path and the background worker. The legacy
synchronous path stays covered by the other test modules (flag off in
mock_interview/tests/settings.py).
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory
from django.test import TestCase
from django.test import TransactionTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mock_interview.models import (
    AnswerEvaluation,
    InterviewAssignment,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    MockInterview,
    StudentAnswer,
    UploadedDocument,
)
from mock_interview.services import interview_worker
from mock_interview.services.interview_planner import (
    answer_quality,
    build_question_pool,
    next_pool_question,
)
from mock_interview.services.interview_service import (
    InterviewStateError,
    current_question,
    finish_attempt_fast,
    start_interview,
    submit_answer,
)
from mock_interview.ai.ollama_client import LocalModelError
from mock_interview.speech import tts as tts_module


def _fast_settings(**extra):
    base = dict(settings.MOCK_INTERVIEW)
    base["FAST_INTERVIEW_PIPELINE"] = True
    base["LATENCY_LOG"] = False
    base["INTERVIEW_WORKER_AUTO_START"] = False
    base["INTERVIEW_EVAL_MAX_ATTEMPTS"] = 3
    base["TTS_CACHE_ENABLED"] = False
    base.update(extra)
    return base


FAST = _fast_settings()

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


def make_plan(count):
    def ramp(i):
        return min(5, (i + 1) // 2)

    return {
        "questions": [
            {
                "text": f"Tell me about concept {i}?",
                "type": "technical",
                "topic": f"topic-{i % 3}",
                "difficulty": ramp(i),
                "rubric": {"technical_correctness": 100, "communication": 100},
                "expected_concepts": [f"concept-{i}"],
                "selection_reason": "Planned pool question.",
            }
            for i in range(1, count + 1)
        ]
    }


def make_session(question_count=3, **overrides):
    session = InterviewSession.objects.create(
        student_employee_id="22CS001",
        student_name="Student One",
        role="Python Developer",
        interview_round="Technical",
        difficulty="Intermediate",
        question_count=question_count,
        target_skills=["Python", "Django"],
        consented_at=timezone.now(),
        **overrides,
    )
    return session


def make_answer(question, transcript="I built an API."):
    return StudentAnswer.objects.create(
        question=question,
        audio_file=SimpleUploadedFile(
            "answer.webm",
            b"\x1a\x45\xdf\xa3fixture",
            content_type="audio/webm",
        ),
        original_transcript=transcript,
        corrected_transcript=transcript,
    )


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelinePlannerTests(TestCase):
    def setUp(self):
        self.session = make_session(question_count=4)

    @patch("mock_interview.services.interview_planner.chat_json", return_value=make_plan(4))
    def test_start_builds_full_pool_idempotently(self, _chat):
        start_interview(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "in_progress")
        rows = list(self.session.questions.order_by("sequence_number"))
        self.assertEqual(len(rows), 4)
        self.assertEqual([row.sequence_number for row in rows], [1, 2, 3, 4])
        self.assertTrue(all(row.source == "plan" for row in rows))
        self.assertTrue(all(row.difficulty_level for row in rows))
        self.assertEqual(self.session.interview_state["pool"]["built"], True)

        start_interview(self.session)
        self.assertEqual(self.session.questions.count(), 4)
        self.assertEqual(_chat.call_count, 1)

    @patch(
        "mock_interview.services.interview_planner.chat_json",
        side_effect=LocalModelError("offline"),
    )
    def test_plan_falls_back_deterministically(self, _chat):
        start_interview(self.session)
        self.session.refresh_from_db()
        rows = list(self.session.questions.order_by("sequence_number"))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row.question_text for row in rows))
        self.assertEqual(self.session.interview_state["pool"]["built"], True)

    @patch("mock_interview.services.interview_service.generate_question")
    @patch("mock_interview.services.interview_planner.chat_json", return_value=make_plan(3))
    def test_submit_never_generates_questions_inline(self, _chat, generate_question):
        start_interview(self.session)
        question = self.session.questions.get(sequence_number=1)
        answer = make_answer(question)

        evaluation, next_question = submit_answer(answer, "Improved transcript.")

        self.assertIsNone(evaluation)
        self.assertIsNotNone(next_question)
        self.assertEqual(next_question.sequence_number, 2)
        generate_question.assert_not_called()


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineSubmitTests(TestCase):
    def setUp(self):
        self.session = make_session(question_count=3)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(3),
        ):
            start_interview(self.session)
        self.q1 = self.session.questions.get(sequence_number=1)
        self.q2 = self.session.questions.get(sequence_number=2)
        self.q3 = self.session.questions.get(sequence_number=3)
        self.answer = make_answer(self.q1, transcript="I built an API.")

    def test_submit_returns_instantly_and_queues_evaluation(self):
        evaluation, next_question = submit_answer(
            self.answer, "I built a Django API."
        )
        self.assertIsNone(evaluation)
        self.assertEqual(next_question, self.q2)

        self.answer.refresh_from_db()
        self.assertEqual(self.answer.corrected_transcript, "I built a Django API.")
        self.assertEqual(self.answer.original_transcript, "I built an API.")
        self.assertTrue(self.answer.transcript_changed)
        self.assertIsNotNone(self.answer.submitted_at)
        self.assertEqual(self.answer.evaluation_status, "pending")
        self.assertFalse(
            AnswerEvaluation.objects.filter(answer=self.answer).exists()
        )

    def test_duplicate_submit_is_rejected(self):
        submit_answer(self.answer, "I built a Django API.")
        with self.assertRaises(InterviewStateError):
            submit_answer(self.answer, "I built a Django API. Again.")

    def test_final_submit_defers_completion_to_worker(self):
        eval_two, _ = submit_answer(self.answer, "A.")
        answer_two = make_answer(self.q2)
        eval_three, next_question = submit_answer(answer_two, "B.")
        answer_three = make_answer(self.q3)
        eval_last, next_question = submit_answer(answer_three, "C.")

        self.assertIsNone(next_question)
        self.assertIsNone(eval_last)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "in_progress")

    def test_next_pool_question_skips_answered_and_skipped(self):
        self.assertEqual(next_pool_question(self.session), self.q1)
        submit_answer(self.answer, "A.")
        self.assertEqual(next_pool_question(self.session), self.q2)
        self.q3.skipped = True
        self.q3.save(update_fields=("skipped",))
        self.assertEqual(next_pool_question(self.session), self.q2)

    def test_current_question_accounts_for_submitted_answers(self):
        self.assertEqual(current_question(self.session), self.q1)
        submit_answer(self.answer, "A.")
        self.session.refresh_from_db()
        self.assertEqual(current_question(self.session), self.q2)


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineWorkerTests(TestCase):
    def setUp(self):
        self.session = make_session(question_count=2)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(2),
        ):
            start_interview(self.session)
        self.q1 = self.session.questions.get(sequence_number=1)
        self.q2 = self.session.questions.get(sequence_number=2)
        self.a1 = make_answer(self.q1, transcript="Alpha.")
        self.a2 = make_answer(self.q2, transcript="Beta.")

    @patch(
        "mock_interview.services.interview_worker.evaluate_answer",
        return_value=EVALUATION_DATA,
    )
    def test_worker_evaluates_pending_answers_and_finalizes(self, evaluate):
        submit_answer(self.a1, "Alpha improved.")
        submit_answer(self.a2, "Beta improved.")

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "in_progress")

        interview_worker.process_evaluations()
        self.a1.refresh_from_db()
        self.assertEqual(
            self.a1.evaluation_status, "ready"
        )
        self.a2.refresh_from_db()
        self.assertEqual(self.a2.evaluation_status, "ready")
        evaluation = AnswerEvaluation.objects.get(answer=self.a1)
        self.assertEqual(float(evaluation.total_score), 80)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.original_transcript, "Alpha.")
        self.assertEqual(self.a1.corrected_transcript, "Alpha improved.")

        finalized = interview_worker.finalize_completed_sessions()
        self.assertEqual(finalized, 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "completed")

    @patch(
        "mock_interview.services.interview_worker.evaluate_answer",
        side_effect=RuntimeError("boom"),
    )
    def test_worker_retries_then_marks_failed(self, evaluate):
        submit_answer(self.a1, "Alpha improved.")
        for _ in range(3):
            interview_worker.process_evaluations()
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.evaluation_status, "failed")
        self.assertEqual(self.a1.evaluation_attempts, 3)
        self.assertIn("boom", self.a1.evaluation_error)


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineEndTests(TestCase):
    def setUp(self):
        self.session = make_session(question_count=3)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(3),
        ):
            start_interview(self.session)
        self.q1 = self.session.questions.get(sequence_number=1)
        self.q2 = self.session.questions.get(sequence_number=2)
        self.q3 = self.session.questions.get(sequence_number=3)

    def test_finish_attempt_fast_skips_remaining(self):
        answer = make_answer(self.q1)
        submit_answer(answer, "A.")
        finish_attempt_fast(self.session)
        self.q2.refresh_from_db()
        self.q3.refresh_from_db()
        self.assertTrue(self.q2.skipped)
        self.assertTrue(self.q3.skipped)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "in_progress")

    def test_finish_attempt_requires_one_submitted_answer(self):
        with self.assertRaisesMessage(
            InterviewStateError, "At least one answer is required to finish."
        ):
            finish_attempt_fast(self.session)


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineQualityTests(TestCase):
    def test_answer_quality_probe_is_deterministic_and_fast(self):
        question = InterviewQuestion(
            question_text="Explain a Python project you built.",
            expected_concepts=["Python", "API", "database", "testing"],
        )
        weak = answer_quality(question, "I don't know.")
        strong = answer_quality(
            question,
            "I built a Django REST API as my final year project. I designed "
            "the database schema, wrote unit tests with pytest, optimized my "
            "queries, and reduced latency by 40 percent using a caching layer "
            "on top of Redis.",
        )
        self.assertLess(weak[1], strong[1])
        self.assertLessEqual(weak[1], 3.0)
        self.assertEqual(strong[1], 10.0)


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineReportTests(TestCase):
    def setUp(self):
        self.session = make_session(question_count=1)
        self.session.status = "completed"
        self.session.completed_at = timezone.now()
        self.session.save(update_fields=("status", "completed_at"))
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text="Question one.",
            question_type="technical",
        )
        answer = make_answer(question)
        answer.submitted_at = timezone.now()
        answer.evaluation_status = "ready"
        answer.save()
        AnswerEvaluation.objects.create(answer=answer, **EVALUATION_DATA)

    def _request(self, session_public_id, user):
        request = RequestFactory().post(
            reverse("mock_interview:generate_report", args=[session_public_id]),
            content_type="application/json",
        )
        request.user = user
        return request

    @patch(
        "mock_interview.services.interview_service.generate_report_text",
        return_value={
            "summary": "A summary.",
            "strengths": ["Good example"],
            "improvement_areas": ["Add testing"],
            "learning_plan": ["Do practice"],
            "dimension_analysis": {},
            "swot_analysis": {},
            "model_name": "test-report",
        },
    )
    def test_generate_report_generates_inline(self, _report_text):
        from mock_interview.views.dashboard import generate_report

        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
            Employee_id="22CS001",
            username="Student One",
        )
        response = generate_report(
            self._request(self.session.public_id, user),
            self.session.public_id,
        )
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)
        self.assertEqual(result["status"], "report_ready")
        self.assertTrue(result["report_url"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "report_ready")
        self.assertFalse(self.session.report_generation)
        self.assertTrue(
            InterviewReport.objects.filter(session=self.session).exists()
        )

    @patch(
        "mock_interview.services.interview_service.generate_report_text",
        return_value={
            "summary": "A summary.",
            "strengths": [],
            "improvement_areas": [],
            "learning_plan": [],
            "dimension_analysis": {},
            "swot_analysis": {},
            "model_name": "test-report",
        },
    )
    def test_generate_report_recovers_stuck_evaluating_session(
        self, _report_text
    ):
        from mock_interview.views.dashboard import generate_report

        self.session.status = "evaluating"
        self.session.report_generation = True
        self.session.save(update_fields=("status", "report_generation"))

        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
            Employee_id="22CS001",
            username="Student One",
        )
        response = generate_report(
            self._request(self.session.public_id, user),
            self.session.public_id,
        )
        self.assertEqual(response.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "report_ready")
        self.assertFalse(self.session.report_generation)

    @patch(
        "mock_interview.services.interview_worker."
        "interview_service.finish_interview"
    )
    def test_worker_produces_report(self, finish):
        def fake_finish(session):
            session.status = "report_ready"
            session.save(update_fields=("status",))
            return SimpleNamespace(pk=1)

        finish.side_effect = fake_finish
        self.session.status = "evaluating"
        self.session.report_generation = True
        self.session.save(update_fields=("status", "report_generation"))

        produced = interview_worker.process_report_requests()

        self.assertEqual(produced, 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "report_ready")
        self.assertFalse(self.session.report_generation)


@override_settings(MOCK_INTERVIEW=_fast_settings(LATENCY_LOG=True))
class TelemetryTests(TestCase):
    def test_record_latency_emits_structured_event(self):
        from mock_interview.services import telemetry

        with self.assertLogs("mock_interview.services.telemetry", level="INFO") as captured:
            telemetry.record_latency(
                "test.event",
                session_id="abc",
                question_id="q",
                answer_id="a",
                duration_ms=1.23,
                notes="probe",
            )
        self.assertTrue(
            any(
                "[latency-metrics] event=test.event session=abc question=q "
                "answer=a duration_ms=1.23 notes=probe" in line
                for line in captured.output
            )
        )

    def test_timer_context_records_duration(self):
        from mock_interview.services import telemetry

        with self.assertLogs("mock_interview.services.telemetry", level="INFO") as captured:
            with telemetry.timed_latency("span.test", session_id="s"):
                pass
        self.assertTrue(
            any("event=span.test session=s" in line for line in captured.output)
        )


@override_settings(MOCK_INTERVIEW=_fast_settings(LATENCY_LOG=False))
class TelemetryDisabledTests(TestCase):
    def test_latency_log_disabled_is_silent(self):
        from mock_interview.services import telemetry

        with self.assertNoLogs("mock_interview.services.telemetry", level="INFO"):
            telemetry.record_latency("muted", duration_ms=5.0)


@override_settings(MOCK_INTERVIEW=FAST)
class WorkerAutocommitParityTests(TransactionTestCase):
    """Production runs in autocommit (no outer test transaction).

    _claim uses select_for_update(), which Django rejects outside a
    transaction. Claim + evaluate must therefore be self-contained. This
    reproduces the TransactionManagementError that broke evaluation (and
    therefore report generation) in production.
    """

    def test_evaluate_one_works_without_an_outer_transaction(self):
        self.session = make_session(question_count=1)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(1),
        ):
            start_interview(self.session)
        question = self.session.questions.get(sequence_number=1)
        answer = make_answer(question)
        with patch(
            "mock_interview.services.interview_worker.evaluate_answer",
            return_value=EVALUATION_DATA,
        ):
            submit_answer(answer, "A.")
            interview_worker.process_evaluations()
        answer.refresh_from_db()
        self.assertEqual(answer.evaluation_status, "ready")
        self.assertTrue(
            AnswerEvaluation.objects.filter(answer=answer).exists()
        )


class TtsCacheTests(TestCase):
    def test_cache_key_is_deterministic(self):
        key_a = tts_module._tts_cache_key("Same text", "af_heart", "en", 0.95, "kokoro")
        key_b = tts_module._tts_cache_key("Same text", "af_heart", "en", 0.95, "kokoro")
        key_c = tts_module._tts_cache_key("Same text", "af_heart", "en", 0.95, "piper")
        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)

    def test_cache_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(
                MOCK_INTERVIEW={**_fast_settings(), "TTS_CACHE_DIR": tmp, "TTS_CACHE_ENABLED": True}
            ):
                path = tts_module._tts_cache_path("Hello", "af_heart", "en", 0.95, "kokoro")
                tts_module._write_tts_cache(path, b"wav-bytes")
                self.assertEqual(tts_module._read_tts_cache(path), b"wav-bytes")
                self.assertTrue(Path(path).is_file())


class RagPipelineTests(TestCase):
    @override_settings(MOCK_INTERVIEW=FAST)
    def test_rag_pool_builds_from_document(self):
        document = UploadedDocument.objects.create(
            faculty_employee_id="FAC1",
            faculty_name="Faculty One",
            file=SimpleUploadedFile("notes.txt", b"content", content_type="text/plain"),
            original_filename="notes.txt",
            mime_type="text/plain",
            file_size=7,
            sha256="d41d8cd98f00b204e9800998ecf8427e",
            extracted_text="Database normalization and indexing are core topics.",
            subject_code="CS202",
            chapter="Databases",
            status="ready",
        )
        interview = MockInterview.objects.create(
            created_by="FAC1",
            created_by_name="Faculty One",
            title="Database Interview",
            description="Grounded interview about databases.",
            subject_code="CS202",
            chapter="Databases",
            document=document,
            difficulty="medium",
            interview_mode="technical",
            question_count=3,
            target_skills=["SQL", "Indexing"],
        )
        session = make_session(question_count=3)
        session.mock_interview = interview
        session.save(update_fields=("mock_interview",))

        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(3),
        ):
            build_question_pool(session)

        self.assertEqual(session.questions.count(), 3)
        self.assertEqual(session.interview_state["pool"]["built"], True)

    @override_settings(MOCK_INTERVIEW=FAST)
    def test_rag_worker_uses_rag_evaluator(self):
        document = UploadedDocument.objects.create(
            faculty_employee_id="FAC1",
            faculty_name="Faculty One",
            file=SimpleUploadedFile("notes.txt", b"content", content_type="text/plain"),
            original_filename="notes.txt",
            mime_type="text/plain",
            file_size=7,
            sha256="d41d8cd98f00b204e9800998ecf8427e",
            extracted_text="Normalization improves data integrity.",
            subject_code="CS202",
            chapter="Databases",
            status="ready",
        )
        interview = MockInterview.objects.create(
            created_by="FAC1",
            created_by_name="Faculty One",
            title="Database Interview",
            description="Grounded interview.",
            subject_code="CS202",
            chapter="Databases",
            document=document,
            difficulty="medium",
            interview_mode="technical",
            question_count=1,
        )
        session = make_session(question_count=1)
        session.mock_interview = interview
        session.save(update_fields=("mock_interview",))
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(1),
        ):
            start_interview(session)
        question = session.questions.get(sequence_number=1)
        answer = make_answer(question)
        with patch(
            "mock_interview.services.interview_worker.RAGEvaluator"
        ) as rag_klass:
            rag_klass.return_value.evaluate_answer.return_value = EVALUATION_DATA
            submit_answer(answer, "An answer.")
            interview_worker.process_evaluations()
            rag_klass.assert_called_once()

        answer.refresh_from_db()
        self.assertEqual(answer.evaluation_status, "ready")
        self.assertTrue(AnswerEvaluation.objects.filter(answer=answer).exists())

    @override_settings(MOCK_INTERVIEW=FAST)
    def test_management_command_runs_one_round(self):
        with patch(
            "mock_interview.services.interview_worker.process_pending_tasks_once"
        ) as once:
            call_command("run_interview_worker", "--once")
            once.assert_called_once()


@override_settings(MOCK_INTERVIEW=FAST)
class FastPipelineAssignmentTests(TestCase):
    def test_assignment_completed_by_worker_finalization(self):
        from mock_interview.models import InterviewAssignment

        self.session = make_session(question_count=1)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=make_plan(1),
        ):
            start_interview(self.session)
        assignment = InterviewAssignment.objects.create(
            interview=MockInterview.objects.create(
                created_by="FAC1",
                created_by_name="Faculty One",
                title="Python Interview",
                subject_code="CS101",
                chapter="Basics",
                difficulty="easy",
                interview_mode="technical",
                question_count=1,
            ),
            student_employee_id="22CS001",
            student_name="Student One",
            status="in_progress",
            session=self.session,
        )
        question = self.session.questions.get(sequence_number=1)
        answer = make_answer(question)
        with patch(
            "mock_interview.services.interview_worker.evaluate_answer",
            return_value=EVALUATION_DATA,
        ):
            submit_answer(answer, "A.")
            interview_worker.process_evaluations()
            interview_worker.finalize_completed_sessions()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "completed")
        self.assertIsNotNone(assignment.completed_at)