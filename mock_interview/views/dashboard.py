import json
import importlib.util
import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from mock_interview.models import (
    InterviewAssignment,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    ResumeDocument,
    StudentAnswer,
)
from mock_interview.ai.ollama_client import configured_model, health, model_available
from mock_interview.services.access import (
    is_mock_interview_faculty_manager,
    is_student_user,
    student_identity,
    student_required,
)
from mock_interview.services.interview_service import (
    InterviewStateError,
    current_question,
    enrich_resume,
    ensure_report_evaluations,
    fast_pipeline_enabled,
    finish_attempt_fast,
    finish_interview,
    prepare_interview_for_report,
    save_and_transcribe_answer,
    skip_question,
    start_interview,
    submit_answer,
)
from mock_interview.speech.tts import (
    TextToSpeechError,
    synthesize_probe,
    synthesize_question,
    tts_status,
    validate_audio_bytes,
)
from mock_interview.services.resume_parser import (
    ResumeValidationError,
    extract_resume_text,
    file_sha256,
    validate_resume,
)
from mock_interview.speech.stt import SpeechToTextError

logger = logging.getLogger(__name__)


def _owned_session(request, public_id):
    return get_object_or_404(
        InterviewSession.objects.select_related("resume"),
        public_id=public_id,
        student_employee_id=student_identity(request.user)["student_employee_id"],
    )


def _owned_question(request, public_id):
    return get_object_or_404(
        InterviewQuestion.objects.select_related("session"),
        public_id=public_id,
        session__student_employee_id=student_identity(request.user)[
            "student_employee_id"
        ],
    )


def _json_error(message, status=400, code="invalid_request"):
    return JsonResponse({"ok": False, "error": message, "code": code}, status=status)


def _bounded_int(value, *, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


INTERVIEW_TYPES = {"mixed", "resume", "role"}
INTERVIEW_ROUNDS = {
    "Technical",
    "HR",
    "Behavioural",
    "Project Discussion",
    "Managerial",
    "Mixed",
}
DIFFICULTIES = {"Beginner", "Intermediate", "Advanced"}
ENGLISH_VOICES = {"af_heart", "am_adam"}
SESSION_DELETABLE_STATUSES = {"draft", "planning", "ready", "in_progress"}


def module_entry(request):
    """Route ERP users to the correct mock interview dashboard."""
    if not getattr(request.user, "is_authenticated", False):
        return redirect("login_view")
    if is_mock_interview_faculty_manager(request.user):
        return redirect("mock_interview:faculty_dashboard")
    if is_student_user(request.user):
        return dashboard(request)
    raise PermissionDenied("Mock interviews are available to students and faculty only.")


@student_required
def dashboard(request):
    identity = student_identity(request.user)
    employee_id = identity["student_employee_id"]

    assignments = InterviewAssignment.objects.filter(
        student_employee_id=employee_id,
    ).select_related("interview").order_by("-assigned_at")

    from mock_interview.services.scheduler import InterviewScheduler
    scheduler = InterviewScheduler()
    assigned_interviews = []
    for assignment in assignments:
        interview = assignment.interview
        can_start, message = scheduler.can_student_start(interview)
        status_info = scheduler.get_time_info(interview)
        now = timezone.now()
        if interview.end_time and now > interview.end_time:
            time_status = "expired"
        elif interview.start_time and now < interview.start_time:
            time_status = "upcoming"
        elif can_start:
            time_status = "active"
        else:
            time_status = "unknown"
        assigned_interviews.append({
            "assignment": assignment,
            "interview": interview,
            "can_start": can_start,
            "message": message,
            "status_info": status_info,
            "time_status": time_status,
        })

    return render(
        request,
        "mock_interview/dashboard.html",
        {
            "resumes": ResumeDocument.objects.filter(
                student_employee_id=employee_id
            )[:5],
            "sessions": InterviewSession.objects.filter(
                student_employee_id=employee_id
            )[:10],
            "assigned_interviews": assigned_interviews,
        },
    )


@student_required
@require_POST
def upload_resume(request):
    identity = student_identity(request.user)
    uploaded = request.FILES.get("resume")
    if not uploaded:
        messages.error(request, "Choose a PDF or DOCX resume.")
        return redirect("mock_interview:dashboard")
    try:
        filename = validate_resume(uploaded)
        digest = file_sha256(uploaded)
        extracted_text = extract_resume_text(uploaded)
        uploaded.seek(0)
        resume = ResumeDocument.objects.create(
            **identity,
            file=uploaded,
            original_filename=filename,
            mime_type=getattr(uploaded, "content_type", "") or "",
            file_size=uploaded.size,
            sha256=digest,
            extracted_text=extracted_text,
            status="parsed",
        )
        enrich_resume(resume)
    except ResumeValidationError as exc:
        messages.error(request, str(exc))
        return redirect("mock_interview:dashboard")
    messages.success(request, "Resume uploaded and parsed successfully.")
    return redirect(f"{reverse('mock_interview:setup')}?resume={resume.public_id}")


@student_required
def setup(request):
    identity = student_identity(request.user)
    resumes = ResumeDocument.objects.filter(
        student_employee_id=identity["student_employee_id"],
        status="parsed",
    )
    selected_resume = None
    resume_id = request.GET.get("resume") or request.POST.get("resume")
    if resume_id:
        selected_resume = get_object_or_404(
            resumes,
            public_id=resume_id,
        )

    if request.method == "POST":
        role = (request.POST.get("role") or "").strip()
        interview_round = (request.POST.get("interview_round") or "").strip()
        difficulty = (request.POST.get("difficulty") or "").strip()
        interview_type = (request.POST.get("interview_type") or "role").strip()
        language_mode = (request.POST.get("language_mode") or "en").strip()
        interviewer_voice = (
            request.POST.get("interviewer_voice") or "af_heart"
        ).strip()
        if (
            not role
            or interview_round not in INTERVIEW_ROUNDS
            or difficulty not in DIFFICULTIES
            or interview_type not in INTERVIEW_TYPES
        ):
            messages.error(request, "Choose a valid role, round, and difficulty.")
        elif language_mode != "en":
            messages.error(
                request,
                "Tamil voice mode is not enabled until the local Indic-TTS "
                "service is deployed.",
            )
        elif interviewer_voice not in ENGLISH_VOICES:
            messages.error(request, "Choose a valid interviewer voice.")
        else:
            target_skills = [
                value.strip()
                for value in (request.POST.get("target_skills") or "").split(",")
                if value.strip()
            ][:20]
            session = InterviewSession.objects.create(
                **identity,
                resume=selected_resume,
                interview_type=interview_type,
                role=role[:150],
                company_name=(request.POST.get("company_name") or "").strip()[:150],
                job_description=(request.POST.get("job_description") or "").strip()[
                    :8000
                ],
                interview_round=interview_round[:100],
                difficulty=difficulty[:50],
                target_skills=target_skills,
                language_mode=language_mode,
                interviewer_voice=interviewer_voice,
                question_count=_bounded_int(
                    request.POST.get("question_count"),
                    default=5,
                    minimum=3,
                    maximum=15,
                ),
                duration_minutes=_bounded_int(
                    request.POST.get("duration_minutes"),
                    default=15,
                    minimum=5,
                    maximum=45,
                ),
            )
            logger.info(
                "Selected interview round for session %s: %s (role=%s, "
                "question_count=%s)",
                session.public_id,
                session.interview_round,
                session.role,
                session.question_count,
            )
            return redirect("mock_interview:device_check", public_id=session.public_id)
    return render(
        request,
        "mock_interview/setup.html",
        {"resumes": resumes, "selected_resume": selected_resume},
    )


@student_required
def device_check(request, public_id):
    session = _owned_session(request, public_id)
    return render(
        request,
        "mock_interview/device_check.html",
        {"session": session},
    )


@student_required
def instructions(request, public_id):
    session = _owned_session(request, public_id)
    if session.status == "in_progress":
        return redirect("mock_interview:room", public_id=session.public_id)
    if session.status == "report_ready":
        return redirect("mock_interview:report", public_id=session.public_id)
    if session.status in {"completed", "evaluating"}:
        return redirect("mock_interview:processing", public_id=session.public_id)
    if request.method == "POST":
        required = {"consent_devices", "consent_recording", "consent_data", "consent_rules"}
        if not all(request.POST.get(name) == "on" for name in required):
            messages.error(request, "All consent items must be accepted.")
        else:
            session.consented_at = timezone.now()
            session.consent_version = "1.0"
            session.save(update_fields=("consented_at", "consent_version", "updated_at"))
            try:
                start_interview(session)
            except InterviewStateError as exc:
                # A state conflict is a user/workflow condition. Never overwrite
                # the existing session state, because it may still be resumable.
                messages.error(request, f"Interview could not start: {exc}")
            except Exception as exc:
                session.status = "failed"
                session.error_message = str(exc)[:255]
                session.save(update_fields=("status", "error_message", "updated_at"))
                messages.error(
                    request,
                    "Interview preparation failed. You can safely retry. "
                    f"Technical detail: {exc}",
                )
            else:
                return redirect("mock_interview:room", public_id=session.public_id)
    return render(
        request,
        "mock_interview/instructions.html",
        {"session": session},
    )


def _question_audio_problem(question):
    """Return why the stored WAV cannot be spoken, or ``None`` when it is valid.

    Older deployments generated a 2-second silent WAV in dummy mode, and a
    file can also disappear or be truncated. Validating the samples (not just
    the file's existence) is what lets the room repair those questions instead
    of playing silence.
    """
    field = question.audio_file
    if not field:
        return "no question audio has been generated yet"
    try:
        if not field.storage.exists(field.name):
            return f"the audio file {field.name!r} is missing"
        with field.open("rb") as handle:
            audio_bytes = handle.read(8 * 1024 * 1024)
        validate_audio_bytes(audio_bytes, minimum_seconds=0.2)
    except TextToSpeechError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read TTS audio for question %s: %s",
            question.public_id,
            exc,
        )
        return f"the audio file could not be read ({exc})"
    return None


def _question_audio_exists(question):
    return _question_audio_problem(question) is None


def _ensure_question_audio(question):
    """Guarantee the room has a genuinely playable WAV for this question.

    A missing/unreadable/silent file is deleted and synthesized again. Returns
    ``(ready, problem)`` so the room can show the actual reason instead of
    silently continuing without the interviewer's voice.
    """
    problem = _question_audio_problem(question)
    if problem is None:
        return True, ""

    if question.audio_file:
        logger.warning(
            "Question %s audio is unusable (%s); deleting %r and regenerating",
            question.public_id,
            problem,
            question.audio_file.name,
        )
        try:
            question.audio_file.delete(save=False)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not delete unusable TTS audio for question %s",
                question.public_id,
                exc_info=True,
            )
        question.audio_file = None
        question.save(update_fields=("audio_file",))

    try:
        synthesize_question(question)
    except TextToSpeechError as exc:
        logger.error(
            "TTS unavailable for question %s (session %s): %s",
            question.public_id,
            question.session.public_id,
            exc,
        )
        return False, str(exc)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Unexpected TTS failure for question %s (session %s)",
            question.public_id,
            question.session.public_id,
        )
        return False, "Unexpected TTS failure. Check the server log."

    problem = _question_audio_problem(question)
    if problem is not None:
        logger.error(
            "TTS produced unusable audio for question %s (session %s): %s",
            question.public_id,
            question.session.public_id,
            problem,
        )
        return False, problem

    logger.info(
        "TTS ready for question %s (session %s): %s",
        question.public_id,
        question.session.public_id,
        question.audio_file.name,
    )
    return True, ""


@student_required
def room(request, public_id):
    session = _owned_session(request, public_id)
    if session.status == "report_ready":
        return redirect("mock_interview:report", public_id=session.public_id)
    if session.status != "in_progress":
        messages.error(request, "This interview is not currently in progress.")
        return redirect("mock_interview:dashboard")
    answered_ids = StudentAnswer.objects.filter(
        question__session=session,
        submitted_at__isnull=False,
    ).values_list("question_id", flat=True)
    question = (
        session.questions.filter(skipped=False)
        .exclude(id__in=answered_ids)
        .order_by("sequence_number")
        .first()
    )
    if not question:
        messages.info(request, "No unanswered question remains.")
        return redirect("mock_interview:processing", public_id=session.public_id)
    audio_ready, audio_error = _ensure_question_audio(question)
    if not audio_ready:
        logger.warning(
            "Room for session %s cannot speak question Q%s: %s",
            session.public_id,
            question.sequence_number,
            audio_error,
        )
    draft_answer = (
        StudentAnswer.objects.filter(
            question=question,
            submitted_at__isnull=True,
        )
        .order_by("-transcribed_at")
        .first()
    )
    focus_guard = (session.interview_state or {}).get("focus_guard") or {}
    return render(
        request,
        "mock_interview/room.html",
        {
            "session": session,
            "question": question,
            "draft_answer": draft_answer,
            "is_final_question": question.sequence_number >= session.question_count,
            "focus_guard_count": int(focus_guard.get("count") or 0),
            "focus_guard_locked": bool(focus_guard.get("locked")),
            "audio_error": audio_error,
        },
    )


@student_required
def processing(request, public_id):
    session = _owned_session(request, public_id)
    if session.status == "report_ready":
        return redirect("mock_interview:report", public_id=session.public_id)
    return render(
        request,
        "mock_interview/processing.html",
        {"session": session},
    )


@student_required
def report(request, public_id):
    session = _owned_session(request, public_id)
    if session.status != "report_ready":
        return redirect("mock_interview:processing", public_id=session.public_id)
    report_data = get_object_or_404(InterviewReport, session=session)
    evaluations = (
        session.questions.filter(answer__evaluation__isnull=False)
        .select_related("answer__evaluation")
        .order_by("sequence_number")
    )
    return render(
        request,
        "mock_interview/report.html",
        {
            "session": session,
            "report": report_data,
            "questions": evaluations,
        },
    )


@student_required
@require_POST
def transcribe_answer(request, public_id):
    question = _owned_question(request, public_id)
    session = question.session
    if session.status != "in_progress":
        return _json_error("Interview is not in progress.", 409, "invalid_state")

    # Only the question the room is actually showing may accept an upload. This
    # rejects stale tabs, duplicated requests and a second recording that races
    # the answer submission, so a late upload can never attach itself to an
    # earlier turn or unlock the next question ahead of time.
    active_question = current_question(session)
    if active_question is None or active_question.pk != question.pk:
        logger.warning(
            "Rejected stale STT upload for Q%s in session %s (active question is %s)",
            question.sequence_number,
            session.public_id,
            getattr(active_question, "sequence_number", None),
        )
        return _json_error(
            "This question is no longer active. Reload the interview room.",
            409,
            "stale_question",
        )

    uploaded = request.FILES.get("audio")
    if not uploaded:
        return _json_error("Audio file is required.")
    try:
        answer = save_and_transcribe_answer(question, uploaded)
    except InterviewStateError as exc:
        return _json_error(str(exc), 409, "invalid_state")
    except ValueError as exc:
        return _json_error(str(exc), 400, "invalid_audio")
    except SpeechToTextError as exc:
        logger.error(
            "STT request failed for Q%s (session %s): %s",
            question.sequence_number,
            session.public_id,
            exc,
        )
        return _json_error(str(exc), 503, "transcription_failed")
    logger.info(
        "STT response for Q%s (session %s): %d chars, %ss audio, quality=%s",
        question.sequence_number,
        session.public_id,
        len(answer.original_transcript or ""),
        answer.duration_seconds,
        (answer.speech_metrics or {}).get("transcript_quality", {}).get("status"),
    )
    return JsonResponse(
        {
            "ok": True,
            "answer_id": str(answer.public_id),
            "submit_url": reverse(
                "mock_interview:submit_answer", args=[answer.public_id]
            ),
            "transcript": answer.original_transcript,
            "detected_language": answer.detected_language,
            "confidence": answer.stt_confidence,
            "metrics": answer.speech_metrics,
            "quality": answer.speech_metrics.get("transcript_quality", {}),
        }
    )


@student_required
@require_POST
def submit_answer_view(request, public_id):
    answer = get_object_or_404(
        StudentAnswer.objects.select_related("question__session"),
        public_id=public_id,
        question__session__student_employee_id=student_identity(request.user)[
            "student_employee_id"
        ],
    )
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _json_error("Invalid JSON.")
    if payload.get("transcript_confirmed") is not True:
        return _json_error(
            "Confirm that the reviewed transcript matches your answer.",
            400,
"transcript_not_confirmed",
        )
    try:
        evaluation, next_question = submit_answer(
            answer,
            payload.get("transcript", ""),
        )
    except (ValueError, InterviewStateError) as exc:
        return _json_error(str(exc), 409, "invalid_state")
    session = answer.question.session
    return JsonResponse(
        {
            "ok": True,
            "score": (
                float(evaluation.total_score)
                if evaluation is not None else None
            ),
            "evaluation_pending": evaluation is None,
            "completed": next_question is None,
            "next_url": (
                reverse("mock_interview:processing", args=[session.public_id])
                if next_question is None
                else reverse("mock_interview:room", args=[session.public_id])
            ),
        }
    )


@student_required
@require_POST
def skip_question_view(request, public_id):
    question = _owned_question(request, public_id)
    try:
        next_question = skip_question(question)
    except InterviewStateError as exc:
        return _json_error(str(exc), 409, "invalid_state")
    return JsonResponse(
        {
            "ok": True,
            "completed": next_question is None,
            "next_url": reverse(
                "mock_interview:processing" if next_question is None else "mock_interview:room",
                args=[question.session.public_id],
            ),
        }
    )


@student_required
@require_POST
def end_interview(request, public_id):
    session = _owned_session(request, public_id)
    try:
        if fast_pipeline_enabled():
            finish_attempt_fast(session)
        else:
            prepare_interview_for_report(session)
    except InterviewStateError as exc:
        return _json_error(str(exc), 409, "invalid_state")
    return JsonResponse(
        {
            "ok": True,
            "next_url": reverse(
                "mock_interview:processing", args=[session.public_id]
            ),
        }
    )


@student_required
@require_POST
def record_focus_violation(request, public_id):
    """Persist a focus/tab-switch violation for an in-progress interview.

    Stored under ``session.interview_state["focus_guard"]`` so the count
    survives per-question page reloads and can be surfaced to faculty.
    """
    session = _owned_session(request, public_id)
    if session.status != "in_progress":
        return _json_error("Interview is not in progress.", 409, "invalid_state")
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}
    event = str(payload.get("event") or "tab_switch")[:40]
    state = dict(session.interview_state or {})
    guard = dict(state.get("focus_guard") or {})
    guard["count"] = int(guard.get("count") or 0) + 1
    guard["locked"] = guard["count"] >= 3
    guard["last_event"] = event
    guard["last_seen_at"] = timezone.now().isoformat()
    history = list(guard.get("history") or [])
    history.append({"event": event, "at": timezone.now().isoformat()})
    guard["history"] = history[-50:]
    state["focus_guard"] = guard
    session.interview_state = state
    session.save(update_fields=["interview_state", "updated_at"])
    return JsonResponse(
        {
            "ok": True,
            "count": guard["count"],
            "locked": guard["locked"],
        }
    )


@student_required
@require_POST
def generate_report(request, public_id):
    session = _owned_session(request, public_id)
    if session.status == "report_ready":
        return JsonResponse(
            {
                "ok": True,
                "status": session.status,
                "report_url": reverse(
                    "mock_interview:report",
                    args=[session.public_id],
                ),
            }
        )
    if fast_pipeline_enabled():
        if session.status not in {"completed", "evaluating", "in_progress"}:
            return _json_error(
                "The interview is not ready for report generation.",
                409,
                "invalid_state",
            )
        try:
            # Report generation must not depend on the background worker
            # being alive: evaluate any still-pending submission inline, then
            # build the report synchronously exactly like the legacy flow.
            ensure_report_evaluations(session)
            # Recover sessions left in "evaluating" by a worker that
            # crashed/stopped after queuing a report (or by a previous async
            # request), otherwise finish_interview would refuse to proceed.
            if session.status == "evaluating":
                InterviewSession.objects.filter(pk=session.pk).update(
                    status="completed"
                )
                session.refresh_from_db()
            finish_interview(session)
        except InterviewStateError as exc:
            return _json_error(str(exc), 409, "invalid_state")
        except Exception as exc:
            return _json_error(
                f"Report generation failed. Reason: {exc}",
                503,
                "report_failed",
            )
        InterviewSession.objects.filter(pk=session.pk).update(
            report_generation=False
        )
        return JsonResponse(
            {
                "ok": True,
                "status": "report_ready",
                "report_url": reverse(
                    "mock_interview:report",
                    args=[session.public_id],
                ),
            }
        )
    if session.status == "evaluating":
        return JsonResponse(
            {"ok": True, "status": "evaluating", "report_url": None},
            status=202,
        )
    if session.status == "failed":
        return _json_error(
            f"Report generation failed. Reason: {session.error_message or 'Unknown error'}",
            409,
            "report_failed",
        )
    if session.status != "completed":
        return _json_error(
            "The interview is not ready for report generation.",
            409,
            "invalid_state",
        )
    try:
        finish_interview(session)
    except InterviewStateError as exc:
        return _json_error(str(exc), 409, "invalid_state")
    except Exception as exc:
        return _json_error(
            f"Report generation failed. Reason: {exc}",
            503,
            "report_failed",
        )
    return JsonResponse(
        {
            "ok": True,
            "status": "report_ready",
            "report_url": reverse(
                "mock_interview:report",
                args=[session.public_id],
            ),
        }
    )


@student_required
@require_GET
def session_status(request, public_id):
    session = _owned_session(request, public_id)
    return JsonResponse(
        {
            "ok": True,
            "status": session.status,
            "error": session.error_message,
            "completed_at": (
                session.completed_at.isoformat()
                if session.completed_at else None
            ),
            "report_url": (
                reverse("mock_interview:report", args=[session.public_id])
                if session.status == "report_ready"
                else None
            ),
        }
    )


@student_required
@require_POST
def delete_session(request, public_id):
    """Delete a student's own interview session.

    Allowed only while the session is pending (draft/planning/ready) or in
    progress. Completed, evaluating, report-ready, and failed sessions cannot
    be deleted by the student.
    """
    session = _owned_session(request, public_id)
    if session.status not in SESSION_DELETABLE_STATUSES:
        return _json_error(
            "This interview can no longer be deleted.",
            409,
            "invalid_state",
        )
    try:
        with transaction.atomic():
            assignment = InterviewAssignment.objects.filter(session=session).first()
            if assignment:
                assignment.session = None
                assignment.status = "assigned"
                assignment.started_at = None
                assignment.completed_at = None
                assignment.save(
                    update_fields=("session", "status", "started_at", "completed_at")
                )
            session.delete()
    except Exception:
        return _json_error(
            "Unable to delete this interview. Please try again later.",
            500,
            "delete_failed",
        )
    return JsonResponse({"status": "deleted"})


@student_required
@require_GET
def runtime_status(request):
    ollama_online = health()
    model_ready = model_available() if ollama_online else False
    tts = tts_status()
    # The sample URL is separate from the synthesis result: the browser can
    # only prove playback after the student presses the test button.
    tts["sample_url"] = reverse("mock_interview:runtime_tts_sample")
    return JsonResponse(
        {
            "ok": True,
            "llm": {
                "model": configured_model(),
                "server_online": ollama_online,
                "model_ready": model_ready,
                "fallback_enabled": bool(
                    getattr(settings, "MOCK_INTERVIEW", {}).get(
                        "ALLOW_DETERMINISTIC_FALLBACK", True
                    )
                ),
            },
            "stt": {
                "engine": "faster-whisper",
                "ready": importlib.util.find_spec("faster_whisper") is not None,
                "model": settings.MOCK_INTERVIEW.get("WHISPER_MODEL"),
                "dummy": bool(
                    settings.MOCK_INTERVIEW.get(
                        "DUMMY_STT",
                        settings.MOCK_INTERVIEW.get("DUMMY_MODE", False),
                    )
                ),
            },
            "tts": tts,
        }
    )


@student_required
@require_GET
def tts_sample(request):
    """Serve the cached interviewer-voice probe for the device check."""
    try:
        audio_bytes, metadata = synthesize_probe()
    except TextToSpeechError as exc:
        logger.warning("TTS sample requested but unavailable: %s", exc)
        return _json_error(
            f"Interviewer voice is unavailable: {exc}",
            503,
            "tts_unavailable",
        )
    logger.info(
        "Serving TTS sample (bytes=%s duration=%.2fs sample_rate=%s)",
        metadata["bytes"],
        metadata["duration_seconds"],
        metadata["sample_rate"],
    )
    response = HttpResponse(audio_bytes, content_type="audio/wav")
    response["Cache-Control"] = "no-store, max-age=0"
    response["X-TTS-Duration"] = str(metadata["duration_seconds"])
    response["X-TTS-Sample-Rate"] = str(metadata["sample_rate"])
    return response


@student_required
@require_GET
def question_audio(request, public_id):
    question = _owned_question(request, public_id)
    audio_ready, audio_error = _ensure_question_audio(question)
    if not audio_ready:
        logger.warning(
            "Question audio requested for Q%s in session %s, but no playable "
            "audio is available: %s",
            question.sequence_number,
            question.session.public_id,
            audio_error,
        )
        return _json_error(
            f"Local TTS audio is unavailable for this question: {audio_error}",
            503,
            "tts_unavailable",
        )
    return FileResponse(
        question.audio_file.open("rb"),
        content_type="audio/wav",
        filename=f"question-{question.sequence_number}.wav",
    )


@student_required
@require_GET
def report_pdf(request, public_id):
    session = _owned_session(request, public_id)
    report_data = get_object_or_404(InterviewReport, session=session)
    if not report_data.pdf_file:
        return _json_error("PDF report is unavailable.", 404, "pdf_unavailable")
    return FileResponse(
        report_data.pdf_file.open("rb"),
        content_type="application/pdf",
        as_attachment=True,
        filename=f"mock-interview-{session.public_id}.pdf",
    )

