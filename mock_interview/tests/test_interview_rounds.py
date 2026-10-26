"""Regression tests for the six interview rounds.

These tests check the actual generated questions and prompts, not just that a
round value survived a form POST.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mock_interview.ai.interview_engine import (
    BEHAVIOURAL_RUBRIC,
    DEFAULT_RUBRIC,
    _ROUND_REPORT_PROMPTS,
    _round_key,
    evaluate_answer_and_generate_question,
    generate_question,
)
from mock_interview.ai.ollama_client import LocalModelError
from mock_interview.models import InterviewQuestion, InterviewSession
from mock_interview.services.interview_planner import (
    _plan_system_prompt,
    _request_plan,
    build_question_pool,
)
from mock_interview.services.interview_rounds import (
    BEHAVIOURAL,
    HR,
    MANAGERIAL,
    MIXED,
    PROJECT,
    TECHNICAL,
    categories_for_count,
    normalize_interview_round,
    plan_matches_round,
    round_prompt_block,
)
from mock_interview.services.interview_service import (
    _create_question,
    save_and_transcribe_answer,
    start_interview,
)
from mock_interview.views.dashboard import setup as setup_view


ROUND_FOCUS_CUES = {
    TECHNICAL: "technical knowledge, coding, algorithms",
    HR: "motivation, career goals, communication",
    BEHAVIOURAL: "past experiences, teamwork, leadership",
    PROJECT: "candidate's actual projects, architecture",
    MANAGERIAL: "decision-making, prioritization, leadership",
    MIXED: "controlled combination",
}

FAST_ROUND_SETTINGS = {
    **settings.MOCK_INTERVIEW,
    "FAST_INTERVIEW_PIPELINE": True,
    "INTERVIEW_WORKER_AUTO_START": False,
    "TTS_CACHE_ENABLED": False,
    "LATENCY_LOG": False,
    "DUMMY_TTS": True,
    "DUMMY_STT": True,
}


CATEGORY_TEXT_CUES = {
    "technical": (
        "solve a practical",
        "implementation example",
        "debug",
        "design a small",
        "compare two ways",
    ),
    "hr": (
        "why are you interested",
        "career in the next",
        "complex idea",
        "team culture",
        "strength and one weakness",
    ),
    "behavioural": (
        "tell me about a time",
        "disagreement",
        "ownership",
        "coordinate or lead",
        "setback",
    ),
    "project": (
        "project",
        "architecture",
        "technology choices",
        "hardest technical challenge",
        "measurable outcome",
    ),
    "managerial": (
        "prioritize",
        "incomplete information",
        "allocate limited",
        "underperforming teammate",
        "stakeholders",
    ),
}


def session_namespace(round_label, *, count=5, public_id="round-session"):
    """A lightweight session for prompt/legacy-generation tests."""
    return SimpleNamespace(
        role="Python Developer",
        company_name="Acme",
        interview_round=round_label,
        difficulty="Intermediate",
        target_skills=["Python", "Django"],
        job_description="Build reliable APIs for customers.",
        question_count=count,
        language_mode="en",
        resume_id=None,
        resume=None,
        mock_interview_id=None,
        public_id=public_id,
        questions=SimpleNamespace(values_list=lambda *args, **kwargs: []),
    )


def plan_for_round(round_label, count):
    categories = categories_for_count(round_label, count)
    return {
        "questions": [
            {
                "text": f"{category.title()} interview question {index} for the target role?",
                "type": category,
                "topic": category,
                "difficulty": 2,
                "rubric": (
                    BEHAVIOURAL_RUBRIC
                    if category in {"hr", "behavioural"}
                    else DEFAULT_RUBRIC
                ),
                "expected_concepts": [category],
                "selection_reason": f"Assigned by the {round_label} round.",
            }
            for index, category in enumerate(categories, start=1)
        ]
    }


def question_result(category, text=None):
    return {
        "question_text": text or f"{category.title()} question about the role?",
        "question_type": category,
        "source": "generated",
        "selection_reason": "Test result.",
        "expected_concepts": [category],
        "rubric": (
            BEHAVIOURAL_RUBRIC
            if category in {"hr", "behavioural"}
            else DEFAULT_RUBRIC
        ),
        "model_name": "test-model",
    }


class RoundDefinitionTests(SimpleTestCase):
    def test_aliases_normalize_to_canonical_rounds(self):
        self.assertEqual(normalize_interview_round("technical"), TECHNICAL)
        self.assertEqual(normalize_interview_round("HR"), HR)
        self.assertEqual(normalize_interview_round("behavioral"), BEHAVIOURAL)
        self.assertEqual(normalize_interview_round("projects"), PROJECT)
        self.assertEqual(normalize_interview_round("manager"), MANAGERIAL)
        self.assertEqual(normalize_interview_round("unexpected-value"), MIXED)

    def test_specific_rounds_assign_every_question_to_one_category(self):
        expected = {
            TECHNICAL: "technical",
            HR: "hr",
            BEHAVIOURAL: "behavioural",
            PROJECT: "project",
            MANAGERIAL: "managerial",
        }
        for round_label, category in expected.items():
            with self.subTest(round=round_label):
                self.assertEqual(
                    categories_for_count(round_label, 5),
                    (category,) * 5,
                )

    def test_mixed_uses_a_controlled_cycle(self):
        self.assertEqual(
            categories_for_count(MIXED, 7),
            (
                "technical",
                "project",
                "behavioural",
                "hr",
                "managerial",
                "technical",
                "project",
            ),
        )

    def test_round_prompt_contains_focus_and_exact_contract(self):
        for round_label, focus_cue in ROUND_FOCUS_CUES.items():
            with self.subTest(round=round_label):
                prompt = round_prompt_block(round_label, 5)
                self.assertIn(f"INTERVIEW ROUND: {round_label}", prompt)
                self.assertIn(focus_cue, prompt)
                if round_label == MIXED:
                    self.assertIn("CONTROLLED MIX for this session", prompt)
                    self.assertIn("technical -> project -> behavioural -> hr", prompt)
                else:
                    category = categories_for_count(round_label, 1)[0]
                    self.assertIn(f"`{category}`", prompt)

    def test_plan_validation_rejects_off_round_items(self):
        good = plan_for_round(HR, 3)["questions"]
        bad = plan_for_round(TECHNICAL, 3)["questions"]

        self.assertTrue(plan_matches_round(HR, good, 3)[0])
        matches, detail = plan_matches_round(HR, bad, 3)
        self.assertFalse(matches)
        self.assertIn("expected type 'hr'", detail)


class ReportRoundMappingTests(SimpleTestCase):
    def test_each_round_maps_to_its_own_report_prompt(self):
        expected = {
            TECHNICAL: ("technical", "technical accuracy"),
            HR: ("hr", "career clarity"),
            BEHAVIOURAL: ("behavioural", "STAR"),
            PROJECT: ("project", "projects"),
            MANAGERIAL: ("managerial", "decision-making"),
            MIXED: ("mixed", "controlled combination"),
        }
        for round_label, (key, cue) in expected.items():
            with self.subTest(round=round_label):
                session = session_namespace(round_label)
                self.assertEqual(_round_key(session), key)
                self.assertEqual(
                    _ROUND_REPORT_PROMPTS[key],
                    _ROUND_REPORT_PROMPTS[_round_key(session)],
                )
                self.assertIn(cue, _ROUND_REPORT_PROMPTS[key])


class PlannerRoundPromptTests(SimpleTestCase):
    def test_planner_prompt_changes_for_each_round(self):
        for round_label in ROUND_FOCUS_CUES:
            session = session_namespace(round_label, count=5)
            with self.subTest(round=round_label), patch(
                "mock_interview.services.interview_planner.chat_json",
                return_value=plan_for_round(round_label, 5),
            ) as chat:
                plan = _request_plan(session)

            self.assertIsNotNone(plan)
            self.assertEqual(
                [item["question_type"] for item in plan],
                list(categories_for_count(round_label, 5)),
            )
            system_prompt = chat.call_args.args[0][0]["content"]
            self.assertIn(f"INTERVIEW ROUND: {round_label}", system_prompt)
            self.assertIn(ROUND_FOCUS_CUES[round_label], system_prompt)
            self.assertEqual(
                system_prompt,
                _plan_system_prompt(session),
            )

    def test_mixed_planner_prompt_lists_controlled_distribution(self):
        session = session_namespace(MIXED, count=5)
        prompt = _plan_system_prompt(session)
        self.assertIn(
            "1 technical, 1 project, 1 behavioural, 1 hr, 1 managerial",
            prompt,
        )
        self.assertIn(
            "technical -> project -> behavioural -> hr -> managerial",
            prompt,
        )

    def test_off_round_llm_plan_is_rejected(self):
        session = session_namespace(HR, count=3)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=plan_for_round(TECHNICAL, 3),
        ):
            plan = _request_plan(session)
        self.assertIsNone(plan)


class PlannerStoredRoundTests(TestCase):
    def _session(self, round_label, count=5):
        return InterviewSession.objects.create(
            student_employee_id="22CS001",
            student_name="Student One",
            role="Python Developer",
            company_name="Acme",
            job_description="Build reliable APIs.",
            interview_round=round_label,
            difficulty="Intermediate",
            target_skills=["Python", "Django"],
            question_count=count,
            consented_at=timezone.now(),
        )

    def test_fallback_pool_has_round_appropriate_questions_for_every_category(self):
        for round_label in (TECHNICAL, HR, BEHAVIOURAL, PROJECT, MANAGERIAL, MIXED):
            with self.subTest(round=round_label):
                session = self._session(round_label, count=5)
                with patch(
                    "mock_interview.services.interview_planner.chat_json",
                    side_effect=LocalModelError("offline"),
                ):
                    build_question_pool(session)

                session.refresh_from_db()
                rows = list(session.questions.order_by("sequence_number"))
                expected = categories_for_count(round_label, 5)
                self.assertEqual(
                    [row.question_type for row in rows],
                    list(expected),
                )
                self.assertEqual(session.interview_round, round_label)
                self.assertEqual(
                    session.interview_state["pool"]["round"],
                    round_label,
                )
                for row, category in zip(rows, expected):
                    self.assertTrue(
                        any(
                            cue.lower() in row.question_text.lower()
                            for cue in CATEGORY_TEXT_CUES[category]
                        ),
                        f"{row.question_text!r} does not look like {category}",
                    )
                    self.assertTrue(
                        any(
                            term in row.question_text
                            for term in ("Python", "Django", "Python Developer")
                        )
                    )
                    self.assertIn(round_label, row.selection_reason)

    def test_off_round_llm_plan_falls_back_to_round_questions(self):
        session = self._session(HR, count=3)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            return_value=plan_for_round(TECHNICAL, 3),
        ):
            build_question_pool(session)

        rows = list(session.questions.order_by("sequence_number"))
        self.assertEqual([row.question_type for row in rows], ["hr"] * 3)
        for row in rows:
            self.assertTrue(
                any(
                    cue.lower() in row.question_text.lower()
                    for cue in CATEGORY_TEXT_CUES["hr"]
                ),
                row.question_text,
            )

    def test_mixed_pool_questions_are_persisted_in_cycle_order(self):
        session = self._session(MIXED, count=7)
        with patch(
            "mock_interview.services.interview_planner.chat_json",
            side_effect=LocalModelError("offline"),
        ):
            build_question_pool(session)

        rows = list(session.questions.order_by("sequence_number"))
        self.assertEqual(
            [row.question_type for row in rows],
            list(categories_for_count(MIXED, 7)),
        )
        self.assertEqual(
            session.interview_state["pool"]["categories"],
            list(categories_for_count(MIXED, 7)),
        )


class SetupToQuestionRoundTests(TestCase):
    """Walk the real setup form path, then start the real generation path."""

    def _post_setup(self, round_label):
        request = RequestFactory().post(
            reverse("mock_interview:setup"),
            {
                "role": "AI Engineer",
                "company_name": "Acme",
                "job_description": "Build reliable ML APIs.",
                "interview_round": round_label,
                "difficulty": "Intermediate",
                "interview_type": "role",
                "target_skills": "Python, SQL",
                "language_mode": "en",
                "interviewer_voice": "af_heart",
                "question_count": "3",
                "duration_minutes": "15",
            },
        )
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
            Employee_id="22CS001",
            username="Student One",
        )
        request.session = SessionStore()
        setattr(request, "_messages", FallbackStorage(request))

        response = setup_view(request)

        self.assertEqual(response.status_code, 302)
        return (
            InterviewSession.objects.filter(student_employee_id="22CS001")
            .order_by("-created_at")
            .first()
        )

    @override_settings(MOCK_INTERVIEW=FAST_ROUND_SETTINGS)
    def test_selected_round_drives_every_question_after_real_setup(self):
        for round_label in (
            TECHNICAL,
            HR,
            BEHAVIOURAL,
            PROJECT,
            MANAGERIAL,
            MIXED,
        ):
            with self.subTest(round=round_label):
                session = self._post_setup(round_label)
                self.assertEqual(session.interview_round, round_label)
                session.consented_at = timezone.now()
                session.save(update_fields=("consented_at", "updated_at"))

                with patch(
                    "mock_interview.services.interview_planner.chat_json",
                    side_effect=LocalModelError("offline"),
                ), patch(
                    "mock_interview.services.interview_service.synthesize_question"
                ):
                    start_interview(session)

                session.refresh_from_db()
                rows = list(session.questions.order_by("sequence_number"))
                expected = categories_for_count(round_label, 3)
                self.assertEqual(
                    [row.question_type for row in rows],
                    list(expected),
                )
                self.assertEqual(session.interview_round, round_label)
                for row, category in zip(rows, expected):
                    self.assertTrue(
                        any(
                            cue.lower() in row.question_text.lower()
                            for cue in CATEGORY_TEXT_CUES[category]
                        ),
                        f"{round_label} produced an off-category question: "
                        f"{row.question_text!r}",
                    )


class LegacyGenerationRoundTests(SimpleTestCase):
    def test_legacy_fallback_keeps_the_round_for_every_question(self):
        for round_label in (TECHNICAL, HR, BEHAVIOURAL, PROJECT, MANAGERIAL, MIXED):
            session = session_namespace(round_label, count=5)
            expected = categories_for_count(round_label, 5)
            for sequence_number, category in enumerate(expected, start=1):
                with self.subTest(round=round_label, question=sequence_number):
                    with patch(
                        "mock_interview.ai.interview_engine.chat_json",
                        side_effect=LocalModelError("offline"),
                    ):
                        result = generate_question(session, sequence_number)

                    self.assertEqual(result["question_type"], category)
                    self.assertEqual(
                        result["model_name"],
                        "deterministic-fallback",
                    )
                    self.assertTrue(
                        any(
                            cue.lower() in result["question_text"].lower()
                            for cue in CATEGORY_TEXT_CUES[category]
                        )
                    )

    def test_legacy_prompt_contains_the_selected_round(self):
        for round_label in ROUND_FOCUS_CUES:
            session = session_namespace(round_label)
            category = categories_for_count(round_label, 5)[0]
            with self.subTest(round=round_label), patch(
                "mock_interview.ai.interview_engine.chat_json",
                return_value=question_result(category),
            ) as chat:
                result = generate_question(session, 1)

            self.assertEqual(result["question_type"], category)
            system_prompt = chat.call_args.args[0][0]["content"]
            self.assertIn(f"INTERVIEW ROUND: {round_label}", system_prompt)
            self.assertIn(ROUND_FOCUS_CUES[round_label], system_prompt)
            payload = json.loads(chat.call_args.args[0][1]["content"])
            self.assertEqual(payload["round"], round_label)
            self.assertEqual(payload["required_question_type"], category)

    def test_off_round_legacy_question_is_replaced(self):
        session = session_namespace(TECHNICAL)
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value=question_result("hr"),
        ):
            result = generate_question(session, 1)

        self.assertEqual(result["question_type"], "technical")
        self.assertEqual(result["model_name"], "deterministic-fallback")
        self.assertTrue(
            any(
                cue.lower() in result["question_text"].lower()
                for cue in CATEGORY_TEXT_CUES["technical"]
            )
        )

    def test_combined_turn_prompt_and_next_question_use_the_round(self):
        session = session_namespace(HR, public_id="combined-session")
        question = SimpleNamespace(
            session=session,
            question_text="Why are you interested in this role?",
            rubric=BEHAVIOURAL_RUBRIC,
            expected_concepts=["motivation"],
        )
        answer = SimpleNamespace(
            corrected_transcript="I want to build useful products.",
            original_transcript="",
            speech_metrics={},
        )
        result = {
            "evaluation": {
                "dimension_scores": {
                    "relevance": 8,
                    "star_structure": 7,
                    "specific_evidence": 7,
                    "reflection": 6,
                    "communication": 8,
                },
                "evidence": ["A clear motivation."],
                "strengths": ["Clear communication."],
                "missing_concepts": [],
                "improvement_actions": ["Give a concrete example."],
                "improved_answer": "",
            },
            "next_question": question_result("technical"),
        }

        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value=result,
        ) as chat:
            _evaluation, next_question = evaluate_answer_and_generate_question(
                question,
                answer,
                2,
            )

        self.assertEqual(next_question["question_type"], "hr")
        self.assertEqual(next_question["model_name"], "deterministic-fallback")
        system_prompt = chat.call_args.args[0][0]["content"]
        self.assertIn(f"INTERVIEW ROUND: {HR}", system_prompt)


class RoundTtsAndSttTests(TestCase):
    def setUp(self):
        self.session = InterviewSession.objects.create(
            student_employee_id="22CS001",
            student_name="Student One",
            role="Python Developer",
            interview_round=HR,
            difficulty="Intermediate",
            target_skills=["Python"],
            question_count=3,
            consented_at=timezone.now(),
        )

    def test_tts_receives_the_stored_question_row(self):
        question_data = question_result(
            "hr",
            text="Why are you interested in this Python Developer role?",
        )
        question_data["expected_concepts"] = ["motivation"]
        with patch(
            "mock_interview.services.interview_service.synthesize_question"
        ) as synthesize:
            question = _create_question(
                self.session,
                1,
                question_data=question_data,
            )

        self.assertEqual(question.question_text, question_data["question_text"])
        synthesize.assert_called_once_with(question)

    def test_stt_transcription_does_not_change_the_selected_round(self):
        question = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text="Why are you interested in this Python Developer role?",
            question_type="hr",
            source="test",
            selection_reason="Test.",
            rubric=BEHAVIOURAL_RUBRIC,
            expected_concepts=["motivation"],
            model_name="test",
        )
        audio = SimpleUploadedFile(
            "answer.webm",
            b"\x1a\x45\xdf\xa3fixture",
            content_type="audio/webm",
        )
        with patch(
            "mock_interview.services.interview_service.transcribe_audio",
            return_value={
                "transcript": "I enjoy building useful products.",
                "detected_language": "en",
                "confidence": 0.9,
                "words": [],
                "duration_seconds": 4.0,
            },
        ):
            answer = save_and_transcribe_answer(question, audio)

        self.session.refresh_from_db()
        self.assertEqual(self.session.interview_round, HR)
        self.assertEqual(answer.original_transcript, "I enjoy building useful products.")
