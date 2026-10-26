"""Regression tests for the voice interview loop.

These cover the failure modes that used to leave the interview room stuck:
stale question uploads racing the state machine, duplicate uploads re-running
inference, an STT failure that was never recoverable, and a TTS failure that
rendered a room without any way to record an answer.
"""

import io
import json
import struct
import sys
import wave
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mock_interview.models import (
    InterviewQuestion,
    InterviewSession,
    StudentAnswer,
)
from mock_interview.services.interview_service import (
    current_question,
    save_and_transcribe_answer,
)
from mock_interview.speech.stt import (
    SpeechToTextError,
    _load_model,
    transcribe_audio,
)
from mock_interview.speech.tts import (
    TextToSpeechError,
    _dummy_wav_bytes,
    _synthesize_probe,
    tts_status,
    validate_audio_bytes,
)
from mock_interview.views.dashboard import (
    room,
    transcribe_answer,
    tts_sample,
)

WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00\x01" * 1024

STT_RESULT = {
    "transcript": "I built a Django API with authentication.",
    "detected_language": "en",
    "confidence": 0.93,
    "words": [
        {"word": "I", "start": 0.0, "end": 0.1, "probability": 0.9},
        {"word": "built", "start": 0.15, "end": 0.4, "probability": 0.95},
        {"word": "a", "start": 0.45, "end": 0.5, "probability": 0.8},
        {"word": "Django", "start": 0.55, "end": 0.9, "probability": 0.97},
        {"word": "API", "start": 0.95, "end": 1.2, "probability": 0.96},
    ],
    "duration_seconds": 4,
    "quality": {"status": "ok", "issues": []},
    "model_name": "test-whisper",
}


def student_user():
    return SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_student=True,
        Employee_id="22CS001",
        username="Student One",
    )


class VoiceWorkflowTests(TestCase):
    def setUp(self):
        self.session = InterviewSession.objects.create(
            student_employee_id="22CS001",
            student_name="Student One",
            role="Python Developer",
            interview_round="Technical",
            difficulty="Intermediate",
            question_count=2,
            consented_at=timezone.now(),
            status="in_progress",
        )
        self.q1 = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=1,
            question_text="Explain a Python project you built.",
            question_type="technical",
        )
        self.q2 = InterviewQuestion.objects.create(
            session=self.session,
            sequence_number=2,
            question_text="How do you test an API?",
            question_type="technical",
        )

    def _transcribe_request(self, question, audio_bytes=WEBM_BYTES):
        upload = SimpleUploadedFile(
            "answer.webm", audio_bytes, content_type="audio/webm"
        )
        request = RequestFactory().post(
            reverse(
                "mock_interview:transcribe_answer",
                args=[question.public_id],
            ),
            {"audio": upload},
        )
        request.user = student_user()
        return request

    @patch(
        "mock_interview.services.interview_service.transcribe_audio",
        return_value=dict(STT_RESULT),
    )
    def test_transcribe_endpoint_returns_transcript_and_submit_url(self, stt):
        response = transcribe_answer(
            self._transcribe_request(self.q1), self.q1.public_id
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["transcript"], STT_RESULT["transcript"])
        self.assertTrue(payload["submit_url"].endswith("/submit/"))
        self.assertEqual(stt.call_count, 1)

        answer = StudentAnswer.objects.get(question=self.q1)
        self.assertIsNotNone(answer.transcribed_at)
        self.assertIsNone(answer.submitted_at)

    @patch(
        "mock_interview.services.interview_service.transcribe_audio",
        return_value=dict(STT_RESULT),
    )
    def test_identical_audio_retry_reuses_transcript(self, stt):
        first = transcribe_answer(
            self._transcribe_request(self.q1), self.q1.public_id
        )
        second = transcribe_answer(
            self._transcribe_request(self.q1), self.q1.public_id
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # A browser retry uploads the same bytes: no duplicate inference, no
        # second answer row, and the reviewed transcript is preserved.
        self.assertEqual(stt.call_count, 1)
        self.assertEqual(StudentAnswer.objects.filter(question=self.q1).count(), 1)

    @patch(
        "mock_interview.services.interview_service.transcribe_audio",
        return_value=dict(STT_RESULT),
    )
    def test_upload_for_a_past_question_is_rejected(self, stt):
        # Q1 is still the active question; a stale tab posting Q2 must not
        # create an answer or advance the interview.
        response = transcribe_answer(
            self._transcribe_request(self.q2), self.q2.public_id
        )

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], "stale_question")
        self.assertEqual(stt.call_count, 0)
        self.assertFalse(StudentAnswer.objects.filter(question=self.q2).exists())
        self.assertEqual(current_question(self.session), self.q1)

    @patch(
        "mock_interview.services.interview_service.transcribe_audio",
        side_effect=SpeechToTextError(
            "Speech recognition model 'large-v3-turbo' could not be loaded on cpu."
        ),
    )
    def test_stt_failure_returns_503_and_keeps_audio_for_retry(self, stt):
        response = transcribe_answer(
            self._transcribe_request(self.q1), self.q1.public_id
        )

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], "transcription_failed")
        self.assertIn("could not be loaded", payload["error"])

        answer = StudentAnswer.objects.get(question=self.q1)
        self.assertEqual(answer.audio_file.size, len(WEBM_BYTES))
        self.assertIsNone(answer.transcribed_at)

        # The retained recording is enough to recover without re-recording.
        stt.side_effect = None
        stt.return_value = dict(STT_RESULT)
        retry = transcribe_answer(
            self._transcribe_request(self.q1), self.q1.public_id
        )
        self.assertEqual(retry.status_code, 200)
        answer.refresh_from_db()
        self.assertEqual(answer.original_transcript, STT_RESULT["transcript"])

    def test_new_recording_clears_the_previous_transcript(self):
        with patch(
            "mock_interview.services.interview_service.transcribe_audio",
            return_value=dict(STT_RESULT),
        ):
            answer = save_and_transcribe_answer(
                self.q1,
                SimpleUploadedFile(
                    "answer.webm", WEBM_BYTES, content_type="audio/webm"
                ),
            )
        self.assertEqual(answer.original_transcript, STT_RESULT["transcript"])

        with patch(
            "mock_interview.services.interview_service.transcribe_audio",
            side_effect=SpeechToTextError("model unavailable"),
        ):
            with self.assertRaises(SpeechToTextError):
                save_and_transcribe_answer(
                    self.q1,
                    SimpleUploadedFile(
                        "answer.webm",
                        WEBM_BYTES + b"\x00\xff",
                        content_type="audio/webm",
                    ),
                )
        answer.refresh_from_db()
        # The old transcript must never be submitted against new audio.
        self.assertEqual(answer.original_transcript, "")
        self.assertIsNone(answer.transcribed_at)

    @patch(
        "mock_interview.views.dashboard.synthesize_question",
        side_effect=TextToSpeechError("No local TTS backend."),
    )
    def test_room_without_tts_shows_audio_error_and_retry(self, _tts):
        request = RequestFactory().get(
            reverse("mock_interview:room", args=[self.session.public_id])
        )
        request.user = student_user()

        response = room(request, self.session.public_id)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        # The failure is visible, not silently replaced by text-only mode.
        self.assertIn("Question audio is unavailable", html)
        self.assertIn("No local TTS backend.", html)
        self.assertIn('id="retryAudioButton"', html)
        self.assertNotIn('id="questionAudio"', html)
        # The controls still exist, but the room script keeps recording locked
        # until the audio actually plays.
        self.assertIn('id="recordButton"', html)
        self.assertIn('id="stopButton"', html)
        self.assertIn('id="retryButton"', html)
        self.assertIn("SYNTHESIZING_AUDIO", html)
        self.assertIn("LISTENING", html)
        self.assertIn("audioPending = true", html)

    @patch(
        "mock_interview.views.dashboard.synthesize_question",
        side_effect=TextToSpeechError("No local TTS backend."),
    )
    def test_question_audio_endpoint_reports_unavailable_tts(self, _tts):
        from mock_interview.views.dashboard import question_audio

        request = RequestFactory().get("/")
        request.user = student_user()

        response = question_audio(request, self.q1.public_id)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], "tts_unavailable")
        self.assertIn("No local TTS backend.", payload["error"])


def _tts_settings(**overrides):
    config = dict(settings.MOCK_INTERVIEW)
    config.update(
        {
            "DUMMY_TTS": False,
            "TTS_BACKEND": "kokoro_onnx",
            "TTS_CACHE_ENABLED": False,
            "KOKORO_VOICE": "af_heart",
            "TTS_SPEED": 0.95,
        }
    )
    config.update(overrides)
    return config


def _non_silent_wav(seconds=0.5, sample_rate=24000, amplitude=3000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(
            struct.pack("<h", amplitude) * int(sample_rate * seconds)
        )
    return buffer.getvalue()


class TTSStatusTests(TestCase):
    """The device check must distinguish synthesis from dummy/silent audio."""

    def setUp(self):
        _synthesize_probe.cache_clear()

    def tearDown(self):
        _synthesize_probe.cache_clear()

    def test_dummy_tts_is_not_reported_ready(self):
        with override_settings(MOCK_INTERVIEW=_tts_settings(DUMMY_TTS=True)):
            status = tts_status()

        self.assertFalse(status["ready"])
        self.assertFalse(status["synthesis"])
        self.assertTrue(status["dummy"])
        self.assertEqual(status["engine"], "dummy")

    @patch(
        "mock_interview.speech.tts._synthesize_kokoro_onnx",
        return_value=_non_silent_wav(),
    )
    def test_real_tts_probe_reports_ready(self, synthesize):
        with override_settings(MOCK_INTERVIEW=_tts_settings()):
            status = tts_status()

        self.assertTrue(status["ready"])
        self.assertTrue(status["synthesis"])
        self.assertFalse(status["dummy"])
        self.assertEqual(status["engine"], "kokoro-onnx")
        self.assertGreaterEqual(status["probe"]["duration_seconds"], 0.5)
        self.assertGreater(status["probe"]["peak"], 0)
        synthesize.assert_called_once()

    @patch(
        "mock_interview.speech.tts._synthesize_kokoro_onnx",
        return_value=_dummy_wav_bytes(),
    )
    def test_silent_tts_output_is_not_ready(self, _synthesize):
        with override_settings(MOCK_INTERVIEW=_tts_settings()):
            status = tts_status()

        self.assertFalse(status["ready"])
        self.assertFalse(status["synthesis"])
        self.assertIn("silent", status["detail"].lower())

    def test_validate_audio_rejects_silent_audio(self):
        with self.assertRaises(TextToSpeechError):
            validate_audio_bytes(_dummy_wav_bytes())

    def test_validate_audio_rejects_too_short_audio(self):
        with self.assertRaises(TextToSpeechError):
            validate_audio_bytes(_non_silent_wav(seconds=0.1))

    def test_validate_audio_accepts_speech_like_wav(self):
        metadata = validate_audio_bytes(_non_silent_wav())

        self.assertGreater(metadata["duration_seconds"], 0)
        self.assertGreater(metadata["peak"], 0)
        self.assertEqual(metadata["sample_rate"], 24000)

    @patch("mock_interview.views.dashboard.synthesize_probe")
    def test_sample_endpoint_serves_wav(self, synthesize_probe):
        audio = _non_silent_wav()
        synthesize_probe.return_value = (
            audio,
            {"bytes": len(audio), "duration_seconds": 0.5, "sample_rate": 24000},
        )
        request = RequestFactory().get("/")
        request.user = student_user()

        response = tts_sample(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertEqual(response.content, audio)
        self.assertEqual(response["X-TTS-Duration"], "0.5")

    @patch(
        "mock_interview.views.dashboard.synthesize_probe",
        side_effect=TextToSpeechError("engine failed"),
    )
    def test_sample_endpoint_reports_synthesis_failure(self, _synthesize):
        request = RequestFactory().get("/")
        request.user = student_user()

        response = tts_sample(request)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], "tts_unavailable")
        self.assertIn("engine failed", payload["error"])


class _FakeWord:
    def __init__(self, word, start, end, probability=0.9):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class _FakeSegment:
    def __init__(self, text, start, end):
        self.text = text
        self.start = start
        self.end = end
        self.avg_logprob = -0.2
        step = (end - start) / max(len(text.split()), 1)
        self.words = [
            _FakeWord(word, start + index * step, start + (index + 1) * step)
            for index, word in enumerate(text.split())
        ]


class _FakeWhisperModel:
    def __init__(self, *args, **kwargs):
        self.model_name = args[0] if args else kwargs.get("model_name")

    def transcribe(self, audio, **kwargs):
        return iter([_FakeSegment("Real model transcript.", 0.0, 3.0)]), SimpleNamespace(
            language="en"
        )


class SpeechToTextHardeningTests(TestCase):
    def setUp(self):
        _load_model.cache_clear()

    def tearDown(self):
        _load_model.cache_clear()

    def test_dummy_stt_returns_the_canned_transcript(self):
        result = transcribe_audio(io.BytesIO(WEBM_BYTES), "en")

        self.assertEqual(result["model_name"], "dummy-transcriber")
        self.assertIn("Django web application", result["transcript"])

    @override_settings(
        MOCK_INTERVIEW={
            **settings.MOCK_INTERVIEW,
            "DUMMY_MODE": True,
            "DUMMY_STT": False,
        }
    )
    def test_real_engine_runs_while_global_dummy_mode_stays_on(self):
        fake_module = SimpleNamespace(WhisperModel=_FakeWhisperModel)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            result = transcribe_audio(io.BytesIO(WEBM_BYTES), "en")

        self.assertEqual(result["transcript"], "Real model transcript.")
        self.assertEqual(
            result["model_name"], settings.MOCK_INTERVIEW["WHISPER_MODEL"]
        )
        self.assertNotEqual(result["model_name"], "dummy-transcriber")

    @override_settings(
        MOCK_INTERVIEW={
            **settings.MOCK_INTERVIEW,
            "DUMMY_MODE": False,
            "DUMMY_STT": False,
        }
    )
    def test_model_load_failure_becomes_speech_error(self):
        class BrokenWhisperModel:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("model download failed")

        fake_module = SimpleNamespace(WhisperModel=BrokenWhisperModel)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            with self.assertRaisesMessage(
                SpeechToTextError, "could not be loaded"
            ):
                transcribe_audio(io.BytesIO(WEBM_BYTES), "en")
