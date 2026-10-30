from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from mock_interview.ai import ollama_client
from mock_interview.ai.ollama_client import LocalModelError, chat_json


class OllamaClientRouteTests(SimpleTestCase):
    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_MODEL": "qwen3:8b",
            "OLLAMA_INTERACTIVE_MODEL": "qwen3:4b",
            "OLLAMA_REPORT_MODEL": "qwen3:8b",
            "LLM_ROUTE_QUESTION": "llama-3.1:8b,qwen3:1.7b",
            "LLM_ROUTE_INTERACTIVE": "qwen3:4b",
            "LLM_ROUTE_EVALUATION": "qwen3:8b",
            "LLM_ROUTE_REPORT": "qwen3:8b",
        }
    )
    def test_role_question_uses_question_route(self):
        candidates = ollama_client._get_route_candidates(role="question")
        self.assertIn("llama-3.1:8b", candidates)
        self.assertEqual(candidates[0], "qwen3:4b")  # primary

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_INTERACTIVE_MODEL": "qwen3:4b",
            "LLM_ROUTE_INTERACTIVE": "qwen2.5:7b",
        }
    )
    def test_role_interactive_uses_interactive_route(self):
        candidates = ollama_client._get_route_candidates(role="interactive")
        self.assertIn("qwen2.5:7b", candidates)
        self.assertEqual(candidates[0], "qwen3:4b")

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_INTERACTIVE_MODEL": "qwen3:8b",
            "LLM_ROUTE_EVALUATION": "eval-backup:1",
        }
    )
    def test_role_evaluation_uses_evaluation_route(self):
        candidates = ollama_client._get_route_candidates(role="evaluation")
        self.assertIn("eval-backup:1", candidates)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_REPORT_MODEL": "report-primary:1",
            "LLM_ROUTE_REPORT": "report-backup:1",
        }
    )
    def test_role_report_uses_report_route(self):
        candidates = ollama_client._get_route_candidates(role="report")
        self.assertIn("report-backup:1", candidates)
        self.assertEqual(candidates[0], "report-primary:1")

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_QUESTION": "q-backup:1",
        }
    )
    def test_role_questions_plural_uses_question_route(self):
        candidates = ollama_client._get_route_candidates(role="questions")
        self.assertIn("q-backup:1", candidates)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_EVALUATION": "e-backup:1",
        }
    )
    def test_role_eval_uses_evaluation_route(self):
        candidates = ollama_client._get_route_candidates(role="eval")
        self.assertIn("e-backup:1", candidates)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": True,
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_EVALUATION": "e-backup:1",
        }
    )
    def test_role_evaluate_uses_evaluation_route(self):
        candidates = ollama_client._get_route_candidates(role="evaluate")
        self.assertIn("e-backup:1", candidates)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup1:1,backup2:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_primary_and_backup_ordering_with_failover(self, mock_post):
        # First attempt fails with retryable error, second succeeds
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=500)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]

        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)
        # First call should use primary
        first_call = mock_post.call_args_list[0]
        self.assertEqual(first_call.kwargs["json"]["model"], "primary:1")
        # Second call should use backup
        second_call = mock_post.call_args_list[1]
        self.assertEqual(second_call.kwargs["json"]["model"], "backup1:1")


class OllamaClientHttpErrorTests(SimpleTestCase):
    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_400_not_retryable(self, mock_post):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=400)
        )
        mock_post.return_value = resp
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_401_not_retryable(self, mock_post):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=401)
        )
        mock_post.return_value = resp
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_403_not_retryable(self, mock_post):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=403)
        )
        mock_post.return_value = resp
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_404_retryable_to_try_backup(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=404)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_408_retryable(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=408)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_422_not_retryable(self, mock_post):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=422)
        )
        mock_post.return_value = resp
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_425_retryable(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=425)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_429_retryable(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=429)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_http_500_retryable(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=500)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)


class OllamaClientFailureModesTests(SimpleTestCase):
    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_malformed_json_triggers_retry_then_fallback(self, mock_post):
        resp_bad = Mock()
        resp_bad.raise_for_status = Mock()
        resp_bad.json.return_value = {"message": {"content": "not json"}}
        mock_post.return_value = resp_bad
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_schema_validation_failure_triggers_retry(self, mock_post):
        resp_bad = Mock()
        resp_bad.raise_for_status = Mock()
        resp_bad.json.return_value = {"message": {"content": '{"wrong": "field"}'}}
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_bad, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_connection_error_triggers_retry(self, mock_post):
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [requests.exceptions.ConnectionError("conn"), resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "primary:1",
            "LLM_ROUTE_INTERACTIVE": "backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_timeout_triggers_retry(self, mock_post):
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [requests.exceptions.Timeout("timeout"), resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "p1:1",
            "LLM_ROUTE_INTERACTIVE": "b1:1,b2:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_successful_backup_recorded_in_metadata(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=500)
        )
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.side_effect = [resp_fail, resp_ok]
        result = chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(result["answer"], "ok")
        meta = ollama_client.get_last_request_meta()
        self.assertEqual(meta.get("successful_model"), "b1:1")
        self.assertTrue(meta.get("fallback_used"))
        self.assertEqual(ollama_client.last_used_model(), "b1:1")

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "p1:1",
            "LLM_ROUTE_INTERACTIVE": "b1:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_all_fail_raises_local_model_error_deterministic_fallback(self, mock_post):
        resp_fail = Mock()
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=500)
        )
        mock_post.return_value = resp_fail
        with self.assertRaises(LocalModelError):
            chat_json([{"role": "user", "content": "hi"}], {"required": ["answer"]}, role="interactive")
        self.assertEqual(mock_post.call_count, 2)  # primary + backup tried

    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_INTERACTIVE_MODEL": "p1:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_concurrency_isolation_for_metadata(self, mock_post):
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"answer": "ok"}'}}
        mock_post.return_value = resp_ok
        # Simulate sequential calls - ContextVar should be set per call
        result1 = chat_json([{"role": "user", "content": "hi1"}], {"required": ["answer"]}, role="interactive")
        meta1 = ollama_client.get_last_request_meta()
        result2 = chat_json([{"role": "user", "content": "hi2"}], {"required": ["answer"]}, role="report")
        meta2 = ollama_client.get_last_request_meta()
        self.assertEqual(result1["answer"], "ok")
        self.assertEqual(result2["answer"], "ok")
        self.assertNotEqual(meta1.get("role"), meta2.get("role"))


class OllamaClientReportTests(SimpleTestCase):
    @override_settings(
        MOCK_INTERVIEW={
            "DUMMY_MODE": False,
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_REPORT_MODEL": "report-model:1",
            "LLM_ROUTE_REPORT": "report-backup:1",
        }
    )
    @patch("mock_interview.ai.ollama_client.requests.post")
    def test_report_role_uses_report_route(self, mock_post):
        resp_ok = Mock()
        resp_ok.raise_for_status = Mock()
        resp_ok.json.return_value = {"message": {"content": '{"overall_score": 85}'}}
        mock_post.return_value = resp_ok
        result = chat_json([{"role": "user", "content": "report"}], {"required": ["overall_score"]}, role="report")
        self.assertEqual(result["overall_score"], 85)
        # Should use report model
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], "report-model:1")
