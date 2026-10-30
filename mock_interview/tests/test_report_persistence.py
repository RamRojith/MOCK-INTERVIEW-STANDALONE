from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from mock_interview.models import (
    AnswerEvaluation,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    StudentAnswer,
)
from mock_interview.services import interview_service


class ReportPersistenceTests(TestCase):
    def setUp(self):
        self.session = InterviewSession.objects.create(
            student_employee_id="22CS001",
            student_name="Test Student",
            role="Python Developer",
            interview_round="Technical",
            difficulty="Intermediate",
            target_skills=["Python"],
            question_count=1,
            duration_minutes=10,
            status="completed",
        )
        self.question = InterviewQuestion.objects.create(
            session=self.session,
            question_text="Explain Python lists.",
            question_type="technical",
            sequence_number=1,
            rubric={},
            expected_concepts=["lists"],
        )
        self.answer = StudentAnswer.objects.create(
            question=self.question,
            audio_file="test.wav",
            original_transcript="Lists are mutable.",
            corrected_transcript="Lists are mutable.",
            submitted_at=timezone.now(),
            evaluation_status="ready",
        )
        AnswerEvaluation.objects.create(
            answer=self.answer,
            dimension_scores={"technical_correctness": 8, "communication": 7},
            total_score=Decimal("80.00"),
            evidence=["Lists mutable"],
            strengths=["Clear"],
            missing_concepts=[],
            improvement_actions=["Give example"],
            improved_answer="",
            model_name="test",
        )

    @override_settings(MOCK_INTERVIEW={"ALLOW_DETERMINISTIC_FALLBACK": True})
    @patch("mock_interview.services.interview_service.generate_report_text")
    def test_all_six_coaching_fields_persisted(self, mock_report):
        mock_report.return_value = {
            "summary": "Good overall.",
            "strengths": ["Clear explanations"],
            "improvement_areas": ["Add examples"],
            "learning_plan": ["Practice coding"],
            "swot_analysis": {"strengths": ["s"], "weaknesses": ["w"], "opportunities": ["o"], "threats": ["t"]},
            "fix_this_first": {"issue": "Lack of examples", "action": "Add one example per answer", "time_minutes": 10},
            "competency_breakdown": [{"name": "Technical", "score": 80, "status": "Good"}],
            "seven_day_plan": [{"day": 1, "task": "Practice lists", "time_minutes": 20}],
            "communication_intelligence": {"clarity": 75, "structure": 70, "fillers": 5},
            "ready_for_next": {"verdict": "Almost ready", "must_fix": ["examples"]},
            "answer_coaching": [{"question": "Q1", "feedback": "Add example", "rewrite": "Better answer"}],
            "model_name": "test-model",
        }
        report = interview_service.finish_interview(self.session)
        report.refresh_from_db()
        self.assertIsNotNone(report.answer_coaching)
        self.assertIsInstance(report.answer_coaching, list)
        self.assertIsNotNone(report.communication_intelligence)
        self.assertIsInstance(report.communication_intelligence, dict)
        self.assertIsNotNone(report.competency_breakdown)
        self.assertIsInstance(report.competency_breakdown, list)
        self.assertIsNotNone(report.fix_this_first)
        self.assertIsInstance(report.fix_this_first, dict)
        self.assertIsNotNone(report.ready_for_next)
        self.assertIsInstance(report.ready_for_next, dict)
        self.assertIsNotNone(report.seven_day_plan)
        self.assertIsInstance(report.seven_day_plan, list)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "report_ready")

    @override_settings(MOCK_INTERVIEW={"ALLOW_DETERMINISTIC_FALLBACK": True})
    @patch("mock_interview.services.interview_service.generate_report_text")
    def test_missing_keys_use_safe_defaults(self, mock_report):
        mock_report.return_value = {
            "summary": "Good overall.",
            "strengths": ["Clear explanations"],
            "improvement_areas": ["Add examples"],
            "learning_plan": ["Practice coding"],
            "swot_analysis": {"strengths": ["s"], "weaknesses": ["w"], "opportunities": ["o"], "threats": ["t"]},
            "model_name": "test-model",
        }
        report = interview_service.finish_interview(self.session)
        report.refresh_from_db()
        self.assertEqual(report.answer_coaching, [])
        self.assertEqual(report.communication_intelligence, {})
        self.assertEqual(report.competency_breakdown, [])
        self.assertEqual(report.fix_this_first, {})
        self.assertEqual(report.ready_for_next, {})
        self.assertEqual(report.seven_day_plan, [])

    @override_settings(MOCK_INTERVIEW={"ALLOW_DETERMINISTIC_FALLBACK": True})
    @patch("mock_interview.services.interview_service.generate_report_text")
    def test_null_values_use_safe_defaults(self, mock_report):
        mock_report.return_value = {
            "summary": "Good overall.",
            "strengths": ["Clear explanations"],
            "improvement_areas": ["Add examples"],
            "learning_plan": ["Practice coding"],
            "swot_analysis": {"strengths": ["s"], "weaknesses": ["w"], "opportunities": ["o"], "threats": ["t"]},
            "answer_coaching": None,
            "communication_intelligence": None,
            "competency_breakdown": None,
            "fix_this_first": None,
            "ready_for_next": None,
            "seven_day_plan": None,
            "model_name": "test-model",
        }
        report = interview_service.finish_interview(self.session)
        report.refresh_from_db()
        self.assertEqual(report.answer_coaching, [])
        self.assertEqual(report.communication_intelligence, {})
        self.assertEqual(report.competency_breakdown, [])
        self.assertEqual(report.fix_this_first, {})
        self.assertEqual(report.ready_for_next, {})
        self.assertEqual(report.seven_day_plan, [])

    @override_settings(MOCK_INTERVIEW={"ALLOW_DETERMINISTIC_FALLBACK": True})
    @patch("mock_interview.services.interview_service.generate_report_text")
    def test_update_or_create_preserves_existing_report(self, mock_report):
        mock_report.return_value = {
            "summary": "First version.",
            "strengths": ["s1"],
            "improvement_areas": ["i1"],
            "learning_plan": ["l1"],
            "swot_analysis": {"strengths": ["s"], "weaknesses": ["w"], "opportunities": ["o"], "threats": ["t"]},
            "answer_coaching": [{"q": 1}],
            "model_name": "test-model",
        }
        report1 = interview_service.finish_interview(self.session)
        report1.refresh_from_db()
        self.assertEqual(report1.answer_coaching, [{"q": 1}])
        report2 = interview_service.finish_interview(self.session)
        self.assertEqual(report1.pk, report2.pk)
