from types import SimpleNamespace
import io
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from mock_interview.ai.interview_engine import (
    DEFAULT_RUBRIC,
    _calibrate_scores,
    _normalize_rubric,
    evaluate_answer_and_generate_question,
    generate_question,
    generate_swot_analysis,
    weighted_total,
)
from mock_interview.ai.ollama_client import LocalModelError
from mock_interview.evaluation_benchmark.metrics import compare_predictions
from mock_interview.evaluation_benchmark.runner import run_benchmark
from mock_interview.services.access import (
    is_faculty_or_hod,
    is_student_user,
    student_identity,
)
from mock_interview.services.information_graph import (
    build_resume_information_graph,
    graph_prompt_context,
)
from mock_interview.services.resume_parser import (
    ResumeValidationError,
    validate_resume,
)
from mock_interview.speech.metrics import calculate_speech_metrics
from mock_interview.speech.stt import assess_transcript_quality, transcribe_audio
from mock_interview.speech.tts import _selected_backend


class ScoringTests(SimpleTestCase):
    def test_rubric_is_normalized_to_one_hundred(self):
        rubric = _normalize_rubric({"correct": 2, "complete": 1})
        self.assertAlmostEqual(sum(rubric.values()), 100)
        self.assertEqual(rubric["correct"], 66.67)

    def test_weighted_total_is_bounded_by_dimension_scores(self):
        scores = {dimension: 8 for dimension in DEFAULT_RUBRIC}
        self.assertEqual(weighted_total(scores, DEFAULT_RUBRIC), 80)

    def test_vague_answer_scores_are_calibrated_downward(self):
        question = SimpleNamespace(
            expected_concepts=[
                "specific project goal",
                "implementation detail",
                "measurable outcome",
            ]
        )
        transcript = (
            "I made project using Python. It is useful for data. I used database "
            "and model. It gives output and helps users. The project was good "
            "and I learned many things."
        )
        scores = {
            "technical_correctness": 8.0,
            "completeness": 8.0,
            "relevance": 8.0,
            "structure": 8.0,
            "practical_example": 8.0,
        }

        calibrated = _calibrate_scores(question, transcript, scores)

        self.assertLessEqual(calibrated["technical_correctness"], 5.5)
        self.assertLessEqual(calibrated["practical_example"], 5.5)
        self.assertLessEqual(calibrated["relevance"], 6.0)

    def test_non_answer_phrase_is_scored_near_zero(self):
        question = SimpleNamespace(
            question_text="Explain the design and outcomes of an email automation system.",
            expected_concepts=[
                "email automation architecture",
                "delivery outcome",
            ],
        )
        transcript = (
            "I don't know the design and outcomes of email automation system."
        )
        scores = {dimension: 8 for dimension in DEFAULT_RUBRIC}

        calibrated = _calibrate_scores(question, transcript, scores)
        total = weighted_total(calibrated, DEFAULT_RUBRIC)

        self.assertEqual(calibrated["technical_correctness"], 1.0)
        self.assertLessEqual(total, 15)

    def test_off_topic_answer_is_capped_low(self):
        question = SimpleNamespace(
            question_text="Explain the design and outcomes of an email automation system.",
            expected_concepts=[
                "email automation architecture",
                "delivery outcome",
            ],
        )
        transcript = (
            "I like cricket and my favourite player is Dhoni. "
            "I went to the ground last week and watched the match."
        )
        scores = {dimension: 8 for dimension in DEFAULT_RUBRIC}

        calibrated = _calibrate_scores(question, transcript, scores)
        total = weighted_total(calibrated, DEFAULT_RUBRIC)

        self.assertEqual(calibrated["technical_correctness"], 2.0)
        self.assertEqual(calibrated["relevance"], 2.5)
        self.assertLessEqual(total, 25)

    def test_long_non_answer_is_not_given_half_marks(self):
        question = SimpleNamespace(
            question_text="Explain the design and outcomes of an email automation system.",
            expected_concepts=[
                "email automation architecture",
                "delivery outcome",
            ],
        )
        transcript = (
            "I don't know, I never studied email automation. We did not cover "
            "that topic in class and I have no idea about the design."
        )
        scores = {dimension: 8 for dimension in DEFAULT_RUBRIC}

        calibrated = _calibrate_scores(question, transcript, scores)
        total = weighted_total(calibrated, DEFAULT_RUBRIC)

        self.assertLessEqual(total, 35)

    def test_speech_metrics_are_deterministic(self):
        metrics = calculate_speech_metrics(
            "Um I actually built a useful project",
            30,
            [
                {"start": 0, "end": 1},
                {"start": 2, "end": 3},
            ],
        )
        self.assertEqual(metrics["word_count"], 7)
        self.assertEqual(metrics["words_per_minute"], 14)
        self.assertEqual(metrics["filler_count"], 2)
        self.assertEqual(metrics["pause_seconds"], 1)


class EvaluationBenchmarkTests(SimpleTestCase):
    def test_benchmark_metrics_compare_ai_scores_to_expert_scores(self):
        report = compare_predictions(
            [
                {
                    "id": "case-1",
                    "status": "ok",
                    "question": "Explain your API.",
                    "expert_total_score": 80,
                    "predicted_total_score": 86,
                    "expert_dimension_scores": {"technical_correctness": 8},
                    "predicted_dimension_scores": {"technical_correctness": 8.5},
                },
                {
                    "id": "case-2",
                    "status": "ok",
                    "question": "Explain your database.",
                    "expert_total_score": 60,
                    "predicted_total_score": 74,
                    "expert_dimension_scores": {"technical_correctness": 6},
                    "predicted_dimension_scores": {"technical_correctness": 7.5},
                },
            ],
            total_tolerance=10,
            dimension_tolerance=1,
        )

        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["mean_absolute_error"], 10)
        self.assertEqual(report["within_tolerance_accuracy"], 50)
        self.assertEqual(report["over_scored_cases"][0]["id"], "case-2")
        self.assertEqual(
            report["dimension_metrics"]["technical_correctness"][
                "within_tolerance_accuracy"
            ],
            50,
        )

    def test_benchmark_runner_uses_existing_evaluator_contract(self):
        def fake_evaluator(question, answer):
            self.assertEqual(question.question_text, "Explain your project.")
            self.assertEqual(answer.corrected_transcript, "I built a Python API.")
            return {
                "dimension_scores": {"technical_correctness": 8},
                "total_score": 80,
                "model_name": "fake-evaluator",
            }

        result = run_benchmark(
            [
                {
                    "id": "case-1",
                    "question": "Explain your project.",
                    "transcript": "I built a Python API.",
                    "rubric": {"technical_correctness": 100},
                    "expert_total_score": 82,
                    "expert_dimension_scores": {"technical_correctness": 8},
                }
            ],
            evaluator=fake_evaluator,
        )

        self.assertEqual(result["summary"]["successful_cases"], 1)
        self.assertEqual(result["summary"]["mean_absolute_error"], 2)
        self.assertEqual(
            result["predictions"][0]["model_name"],
            "fake-evaluator",
        )


class AccessTests(SimpleTestCase):
    def test_only_active_authenticated_student_is_accepted(self):
        student = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
            Employee_id=" 22CS001 ",
            username="Student One",
        )
        faculty = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=False,
        )
        self.assertTrue(is_student_user(student))
        self.assertFalse(is_student_user(faculty))
        self.assertEqual(
            student_identity(student),
            {
                "student_employee_id": "22CS001",
                "student_name": "Student One",
            },
        )

    def test_student_without_employee_id_is_rejected(self):
        student = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_student=True,
        )
        self.assertFalse(is_student_user(student))

    def test_faculty_or_hod_roles_are_allowed(self):
        def role_user(role_name):
            return SimpleNamespace(
                is_authenticated=True,
                is_student=False,
                is_superuser=False,
                role=SimpleNamespace(role=role_name),
            )

        self.assertTrue(is_faculty_or_hod(role_user("Faculty")))
        self.assertTrue(is_faculty_or_hod(role_user("faculty")))
        self.assertTrue(is_faculty_or_hod(role_user("HOD")))
        self.assertTrue(is_faculty_or_hod(role_user("Head of Department")))

    def test_other_erp_roles_are_denied(self):
        def role_user(role_name):
            return SimpleNamespace(
                is_authenticated=True,
                is_student=False,
                is_superuser=False,
                role=SimpleNamespace(role=role_name),
            )

        for role in (
            "Lab Technician",
            "Physical Director",
            "Office Staff",
            "Librarian",
            "Administrative Staff",
            "Placement Staff",
            "Hostel Warden",
            "Accounts Staff",
            "Non-Teaching Staff",
            "Student",
            "Parent",
            "Guardian",
            "",
        ):
            self.assertFalse(is_faculty_or_hod(role_user(role)))

    def test_student_parent_and_anonymous_users_are_denied(self):
        student = SimpleNamespace(
            is_authenticated=True,
            is_student=True,
            role=SimpleNamespace(role="Faculty"),
        )
        parent = SimpleNamespace(
            is_authenticated=True,
            is_student=False,
            is_parent=True,
            role=SimpleNamespace(role="HOD"),
        )
        anonymous = SimpleNamespace(
            is_authenticated=False,
            is_student=False,
            role=SimpleNamespace(role="Faculty"),
        )
        self.assertFalse(is_faculty_or_hod(student))
        self.assertFalse(is_faculty_or_hod(parent))
        self.assertFalse(is_faculty_or_hod(anonymous))

    def test_superuser_is_allowed(self):
        superuser = SimpleNamespace(
            is_authenticated=True,
            is_student=False,
            is_superuser=True,
            role=SimpleNamespace(role="Office Staff"),
        )
        self.assertTrue(is_faculty_or_hod(superuser))


class ResumeValidationTests(SimpleTestCase):
    def test_pdf_signature_is_validated(self):
        fake_pdf = SimpleUploadedFile(
            "resume.pdf",
            b"not-a-pdf",
            content_type="application/pdf",
        )
        with self.assertRaises(ResumeValidationError):
            validate_resume(fake_pdf)

    def test_supported_pdf_is_accepted_before_parsing(self):
        fake_pdf = SimpleUploadedFile(
            "resume.pdf",
            b"%PDF-1.4\nfixture",
            content_type="application/pdf",
        )
        self.assertEqual(validate_resume(fake_pdf), "resume.pdf")


class InformationGraphTests(SimpleTestCase):
    def test_resume_graph_connects_projects_to_skills(self):
        resume = SimpleNamespace(
            public_id="resume-1",
            student_employee_id="22CS001",
            student_name="Student One",
            original_filename="resume.pdf",
            structured_profile={
                "skills": ["Python", "SQL"],
                "projects": ["Feedback classifier built with Python and SQL"],
                "education": [],
                "experience": [],
                "certifications": [],
            },
        )

        graph = build_resume_information_graph(resume)

        self.assertEqual(graph["library"], "networkx")
        self.assertEqual(graph["graph_type"], "resume")
        self.assertIn("Python", graph["insights"]["top_skills"])
        self.assertTrue(
            any(edge["relation"] == "uses_skill" for edge in graph["edges"])
        )
        self.assertEqual(graph_prompt_context(resume)["top_skills"], [])

    def test_prompt_context_returns_compact_resume_graph_insights(self):
        resume = SimpleNamespace(
            information_graph={
                "insights": {
                    "top_skills": ["Python"],
                    "project_focus": ["Feedback classifier"],
                    "question_focus": ["Ask about the classifier."],
                }
            }
        )

        context = graph_prompt_context(resume)

        self.assertEqual(context["top_skills"], ["Python"])
        self.assertEqual(context["question_focus"], ["Ask about the classifier."])


class SpeechInputTests(SimpleTestCase):
    def test_repeated_whisper_hallucination_is_flagged(self):
        transcript = (
            "we have a lot of good communities "
            "and we have a lot of good communities "
        ) * 8
        segment = SimpleNamespace(avg_logprob=-0.5)
        quality = assess_transcript_quality(transcript, 27, [segment])
        self.assertEqual(quality["status"], "needs_review")
        self.assertGreaterEqual(
            quality["maximum_repeated_four_word_phrase"],
            3,
        )

    def test_natural_transcript_can_pass_quality_check(self):
        transcript = (
            "I automated customer feedback classification with Python and SQL. "
            "The pipeline surfaced urgent bugs in real time and reduced delays."
        )
        segment = SimpleNamespace(avg_logprob=-0.35)
        quality = assess_transcript_quality(transcript, 18, [segment])
        self.assertEqual(quality["status"], "ok")

    @override_settings(
        MOCK_INTERVIEW={
            "WHISPER_MODEL": "small",
            "WHISPER_DEVICE": "cpu",
            "WHISPER_COMPUTE_TYPE": "int8",
            "WHISPER_BEAM_SIZE": 1,
        }
    )
    @patch("mock_interview.speech.stt._load_model")
    def test_stt_preserves_file_like_input(self, load_model):
        segment = SimpleNamespace(
            text=" hello",
            words=[],
            end=1.0,
        )
        model = load_model.return_value
        model.transcribe.return_value = (
            iter([segment]),
            SimpleNamespace(language="en"),
        )
        audio = io.BytesIO(b"RIFFfixture")

        result = transcribe_audio(audio, "en")

        self.assertIs(model.transcribe.call_args.args[0], audio)
        self.assertEqual(result["transcript"], "hello")


class SpeechOutputTests(SimpleTestCase):
    @override_settings(MOCK_INTERVIEW={"TTS_BACKEND": "auto"})
    @patch(
        "mock_interview.speech.tts._kokoro_onnx_ready",
        return_value=True,
    )
    @patch("mock_interview.speech.tts.importlib.util.find_spec")
    def test_auto_backend_prefers_kokoro_onnx_on_python_313(
        self,
        find_spec,
        _onnx_ready,
    ):
        find_spec.side_effect = (
            lambda module_name: None if module_name == "kokoro" else object()
        )
        self.assertEqual(_selected_backend(), "kokoro_onnx")


@override_settings(
    MOCK_INTERVIEW={
        "OLLAMA_MODEL": "qwen3:8b",
        "ALLOW_DETERMINISTIC_FALLBACK": True,
    }
)
class QuestionFallbackTests(SimpleTestCase):
    def _session(self, previous_questions=None):
        questions = SimpleNamespace(
            values_list=lambda *args, **kwargs: previous_questions or []
        )
        return SimpleNamespace(
            role="Python Developer",
            interview_round="Technical",
            difficulty="Intermediate",
            target_skills=["Python"],
            job_description="",
            question_count=5,
            company_name="",
            resume=None,
            questions=questions,
        )

    def test_local_model_failure_uses_labeled_fallback(self):
        session = self._session()
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            side_effect=LocalModelError("offline"),
        ):
            result = generate_question(session, 1)
        self.assertEqual(result["model_name"], "deterministic-fallback")
        self.assertIn("Python", result["question_text"])

    def test_repeated_model_question_is_replaced(self):
        repeated = "Explain a fundamental concept in Python and give a practical example."
        session = self._session([repeated])
        model_result = {
            "question_text": repeated,
            "question_type": "technical",
            "source": "generated",
            "selection_reason": "",
            "expected_concepts": [],
            "rubric": DEFAULT_RUBRIC,
        }
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value=model_result,
        ):
            result = generate_question(session, 2)
        self.assertNotEqual(result["question_text"], repeated)
        self.assertEqual(result["model_name"], "deterministic-fallback")

    def test_combined_failure_does_not_start_more_model_calls(self):
        session = self._session(["Explain your project."])
        question = SimpleNamespace(
            session=session,
            question_text="Explain your project.",
            rubric={"technical_correctness": 100},
            expected_concepts=["Python"],
        )
        answer = SimpleNamespace(
            corrected_transcript="I built a Python API.",
            original_transcript="",
            speech_metrics={},
        )
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            side_effect=LocalModelError("timeout"),
        ) as model_call:
            evaluation, next_question = evaluate_answer_and_generate_question(
                question,
                answer,
                2,
            )
        self.assertEqual(model_call.call_count, 1)
        self.assertEqual(evaluation["model_name"], "deterministic-fallback")
        self.assertEqual(next_question["model_name"], "deterministic-fallback")


class SwotAnalysisTests(SimpleTestCase):
    def _session(self):
        return SimpleNamespace(
            role="Backend Developer",
            interview_round="Technical",
        )

    def _rows(self):
        return [
            {
                "question": "Explain REST.",
                "score": 82.0,
                "dimension_scores": {
                    "technical_correctness": 8, "completeness": 7,
                    "relevance": 8, "structure": 4, "practical_example": 3,
                },
                "strengths": ["Clear definition of statelessness"],
                "improvement_actions": ["Give a concrete API example"],
            },
            {
                "question": "What is indexing?",
                "score": 45.0,
                "dimension_scores": {
                    "technical_correctness": 5, "completeness": 4,
                    "relevance": 6, "structure": 4, "practical_example": 3,
                },
                "strengths": [],
                "improvement_actions": ["Study B-tree vs hash indexes"],
            },
        ]

    def test_swot_fallback_has_all_four_quadrants(self):
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            side_effect=LocalModelError("offline"),
        ):
            swot = generate_swot_analysis(
                self._session(),
                self._rows(),
                63.5,
                {
                    "technical_correctness": 6.5, "completeness": 5.5,
                    "relevance": 7.0, "structure": 4.0, "practical_example": 3.0,
                },
            )
        for key in ("strengths", "weaknesses", "opportunities", "threats"):
            self.assertIn(key, swot)
            self.assertTrue(swot[key], f"{key} should not be empty")
            self.assertLessEqual(len(swot[key]), 4)

    def test_swot_uses_model_output_when_available(self):
        model_swot = {
            "strengths": ["Strong grasp of stateless design"],
            "weaknesses": ["Shallow database indexing knowledge"],
            "opportunities": ["Practise structuring answers with a lead-in"],
            "threats": ["Indexing gaps risk failing DB-heavy screens"],
        }
        with patch(
            "mock_interview.ai.interview_engine.chat_json",
            return_value=model_swot,
        ):
            swot = generate_swot_analysis(
                self._session(), self._rows(), 63.5,
                {"technical_correctness": 6.5, "structure": 4.0},
            )
        self.assertEqual(swot["strengths"], ["Strong grasp of stateless design"])
        self.assertEqual(
            swot["threats"], ["Indexing gaps risk failing DB-heavy screens"]
        )

