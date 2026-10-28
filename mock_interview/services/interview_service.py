import hashlib
import logging
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


from mock_interview.ai.interview_engine import (
    evaluate_answer,
    evaluate_answer_and_generate_question,
    generate_question,
    generate_report_text,
    normalize_resume,
)
from mock_interview.rag.retriever import DocumentRetriever
from mock_interview.services.evaluator import RAGEvaluator
from mock_interview.services.information_graph import (
    build_resume_information_graph,
    build_session_information_graph,
)
from mock_interview.services.interview_planner import (
    answer_quality,
    build_question_pool,
    fast_pipeline_enabled,
    next_pool_question,
    record_submit_state,
)
from mock_interview.services.question_generator import QuestionGenerator
from mock_interview.services.telemetry import record_latency, timed_latency
from mock_interview.models import (
    AnswerEvaluation,
    InterviewAssignment,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    StudentAnswer,
)
from mock_interview.speech.metrics import calculate_speech_metrics
from mock_interview.speech.stt import transcribe_audio
from mock_interview.speech.tts import TextToSpeechError, synthesize_question


class InterviewStateError(ValueError):
    pass


def _validate_answer_audio(uploaded_audio):
    max_bytes = int(
        getattr(settings, "MOCK_INTERVIEW", {}).get(
            "MAX_ANSWER_AUDIO_BYTES", 25 * 1024 * 1024
        )
    )
    if uploaded_audio.size <= 0 or uploaded_audio.size > max_bytes:
        raise ValueError("Answer audio must be between 1 byte and 25 MB.")

    header = uploaded_audio.read(16)
    uploaded_audio.seek(0)
    is_supported = (
        header.startswith(b"\x1a\x45\xdf\xa3")  # WebM / Matroska
        or header.startswith(b"OggS")
        or (header.startswith(b"RIFF") and header[8:12] == b"WAVE")
        or (len(header) >= 12 and header[4:8] == b"ftyp")  # MP4 / M4A
        or header.startswith(b"ID3")
        or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    )
    if not is_supported:
        raise ValueError(
            "Unsupported answer audio. Record WebM, OGG, WAV, MP4/M4A, or MP3."
        )


def enrich_resume(resume):
    if not resume.extracted_text:
        return resume
    try:
        resume.structured_profile = normalize_resume(resume.extracted_text)
        resume.error_message = ""
    except Exception as exc:
        resume.structured_profile = {}
        resume.error_message = (
            "Text was extracted, but local AI resume normalization was unavailable: "
            f"{str(exc)[:160]}"
        )
    resume.information_graph = build_resume_information_graph(resume)
    resume.status = "parsed"
    resume.save(
        update_fields=(
            "structured_profile",
            "information_graph",
            "error_message",
            "status",
        )
    )
    return resume


def _create_question(
    session,
    sequence_number,
    previous_answer="",
    *,
    question_data=None,
):
    if question_data:
        data = question_data
    elif getattr(session, "mock_interview_id", None):
        generator = QuestionGenerator()
        previous_questions = list(
            session.questions.values_list("question_text", flat=True)
        )
        data = generator.generate_question(
            session.mock_interview,
            sequence_number,
            previous_questions=previous_questions,
            previous_answer=previous_answer,
        )
    else:
        data = generate_question(
            session,
            sequence_number,
            previous_answer,
        )
    question = InterviewQuestion.objects.create(
        session=session,
        sequence_number=sequence_number,
        question_text=data["question_text"],
        question_type=data["question_type"],
        source=data["source"],
        selection_reason=data["selection_reason"],
        rubric=data["rubric"],
        expected_concepts=data["expected_concepts"],
        model_name=data["model_name"],
        is_follow_up=bool(previous_answer),
    )
    try:
        synthesize_question(question)
    except TextToSpeechError:
        # Text remains authoritative. The UI reports that local TTS is unavailable
        # instead of pretending generated audio exists.
        pass
    return question


@transaction.atomic
def start_interview(session):
    session = InterviewSession.objects.select_for_update().get(pk=session.pk)
    has_question = session.questions.exists()

    # Browser retries, double-clicks, and a Back/Forward resubmission must not
    # destroy a valid running interview or generate a duplicate first question.
    if session.status == "in_progress":
        if session.started_at and has_question:
            return session
        raise InterviewStateError(
            "The running interview has incomplete state. Please start a new session."
        )

    # Recover sessions affected by the previous non-idempotent start handler:
    # it marked an already-started interview as failed on a repeated POST.
    if (
        session.status == "failed"
        and session.started_at
        and not session.completed_at
        and has_question
    ):
        session.status = "in_progress"
        session.error_message = ""
        session.save(update_fields=("status", "error_message", "updated_at"))
        return session

    can_retry_failed_start = (
        session.status == "failed"
        and not session.started_at
        and not session.completed_at
        and not has_question
    )
    if (
        session.status not in {"draft", "ready", "planning"}
        and not can_retry_failed_start
    ):
        raise InterviewStateError("This interview cannot be started again.")
    if not session.consented_at:
        raise InterviewStateError("Consent is required before starting.")
    session.status = "planning"
    session.error_message = ""
    session.save(update_fields=("status", "error_message", "updated_at"))
    if fast_pipeline_enabled():
        build_question_pool(session)
        _ensure_current_question_audio(session)
    elif not session.questions.exists():
        _create_question(session, 1)
    session.status = "in_progress"
    session.started_at = session.started_at or timezone.now()
    session.save(update_fields=("status", "started_at", "updated_at"))
    logger.info(
        "Interview %s started for student %s: %s (round=%s, questions=%s)",
        session.public_id,
        session.student_employee_id,
        session.role,
        session.interview_round,
        session.question_count,
    )
    return session


def current_question(session):
    answered_ids = StudentAnswer.objects.filter(
        question__session=session,
        submitted_at__isnull=False,
    ).values_list("question_id", flat=True)
    return (
        session.questions.filter(skipped=False)
        .exclude(id__in=answered_ids)
        .order_by("sequence_number")
        .first()
    )


def save_and_transcribe_answer(question, uploaded_audio):
    if question.skipped:
        raise InterviewStateError("A skipped question cannot accept an answer.")
    if StudentAnswer.objects.filter(
        question=question, submitted_at__isnull=False
    ).exists():
        raise InterviewStateError("This answer has already been submitted.")
    if AnswerEvaluation.objects.filter(answer__question=question).exists():
        raise InterviewStateError("This answer has already been submitted.")
    _validate_answer_audio(uploaded_audio)

    uploaded_audio.seek(0)
    digest = hashlib.sha256()
    for chunk in uploaded_audio.chunks():
        digest.update(chunk)
    uploaded_audio.seek(0)
    sha256 = digest.hexdigest()

    # A browser retry (or a double-clicked "Stop and transcribe") uploads the
    # same bytes again. Reuse the finished transcription instead of paying for a
    # second model inference, and never clobber a student's edited transcript.
    existing = StudentAnswer.objects.filter(question=question).first()
    if (
        existing is not None
        and existing.transcribed_at
        and existing.original_transcript
        and existing.audio_sha256 == sha256
    ):
        logger.info(
            "STT retry for answer %s on Q%s (session %s) reused the existing "
            "transcript for identical audio sha256=%s",
            existing.public_id,
            question.sequence_number,
            question.session_id,
            sha256[:12],
        )
        return existing

    logger.info(
        "STT upload accepted: session=%s question=Q%s bytes=%s content_type=%s "
        "sha256=%s",
        question.session.public_id,
        question.sequence_number,
        getattr(uploaded_audio, "size", "unknown"),
        getattr(uploaded_audio, "content_type", "unknown"),
        sha256[:12],
    )

    # Serialise concurrent uploads for the same question (duplicate tabs, a
    # retried request) on the answer row itself.
    try:
        with transaction.atomic():
            answer, _ = StudentAnswer.objects.select_for_update().get_or_create(
                question=question
            )
            previous_sha = answer.audio_sha256
            answer.audio_file = uploaded_audio
            answer.audio_sha256 = sha256
            if answer.transcribed_at and previous_sha and previous_sha != sha256:
                # New recording: the previous transcript no longer describes it.
                answer.original_transcript = ""
                answer.corrected_transcript = ""
                answer.transcribed_at = None
            answer.save()
    except Exception:
        logger.exception(
            "Could not persist answer audio for Q%s (session %s)",
            question.sequence_number,
            question.session.public_id,
        )
        raise

    answer.audio_file.open("rb")
    try:
        result = transcribe_audio(
            answer.audio_file,
            language_mode=question.session.language_mode,
        )
    except Exception:
        logger.exception(
            "STT failed for answer %s (Q%s, session %s, bytes=%s, content_type=%s). "
            "The audio is kept so the student can retry.",
            answer.public_id,
            question.sequence_number,
            question.session.public_id,
            getattr(uploaded_audio, "size", "unknown"),
            getattr(uploaded_audio, "content_type", "unknown"),
        )
        raise
    finally:
        answer.audio_file.close()
    answer.original_transcript = result["transcript"]
    answer.corrected_transcript = result["transcript"]
    answer.detected_language = result["detected_language"]
    answer.stt_confidence = result["confidence"]
    answer.word_timestamps = result["words"]
    answer.duration_seconds = result["duration_seconds"]
    answer.speech_metrics = calculate_speech_metrics(
        result["transcript"],
        result["duration_seconds"],
        result["words"],
    )
    answer.speech_metrics["transcript_quality"] = result.get(
        "quality",
        {"status": "needs_review", "issues": ["Quality was not measured."]},
    )
    answer.transcribed_at = timezone.now()
    answer.save()
    logger.info(
        "Answer %s transcribed for question Q%s in session %s: %d chars, %.1fs "
        "audio, lang=%s, conf=%.2f, quality=%s, model=%s",
        answer.public_id,
        question.sequence_number,
        question.session.public_id,
        len(result["transcript"]),
        result["duration_seconds"],
        result["detected_language"],
        result["confidence"] or 0,
        answer.speech_metrics["transcript_quality"].get("status"),
        result.get("model_name", ""),
    )
    return answer


@transaction.atomic
def submit_answer(answer, reviewed_transcript):
    answer = (
        StudentAnswer.objects.select_for_update()
        .select_related("question__session")
        .get(pk=answer.pk)
    )
    session = answer.question.session
    if session.status != "in_progress":
        raise InterviewStateError("The interview is not accepting answers.")
    if answer.submitted_at:
        raise InterviewStateError("This answer has already been submitted.")
    if AnswerEvaluation.objects.filter(answer=answer).exists():
        raise InterviewStateError("This answer has already been submitted.")
    reviewed = (reviewed_transcript or "").strip()
    if not reviewed:
        raise ValueError("Transcript cannot be empty.")
    answer.corrected_transcript = reviewed[:12000]
    answer.transcript_changed = reviewed != answer.original_transcript
    answer.submitted_at = timezone.now()
    answer.save(
        update_fields=(
            "corrected_transcript",
            "transcript_changed",
            "submitted_at",
        )
    )

    with timed_latency(
        "submit.total",
        session_id=session.public_id,
        question_id=answer.question.public_id,
        answer_id=answer.public_id,
    ):
        attempted_count = session.questions.filter(
            models_q_answered_or_skipped()
        ).distinct().count()
        completes_interview = attempted_count >= session.question_count

        if fast_pipeline_enabled():
            return _fast_submit_path(
                answer, session, reviewed, completes_interview
            )

        next_question_data = None
        next_sequence = session.questions.count() + 1

        document_id = ""
        if getattr(session, "mock_interview_id", None) and session.mock_interview.document_id:
            document_id = str(session.mock_interview.document_id)

        if getattr(session, "mock_interview_id", None):
            rag_evaluator = RAGEvaluator()
            evaluation_data = rag_evaluator.evaluate_answer(
                answer.question, answer, document_id=document_id
            )
            if not completes_interview:
                generator = QuestionGenerator()
                previous_questions = list(
                    session.questions.values_list("question_text", flat=True)
                )
                next_question_data = generator.generate_question(
                    session.mock_interview,
                    next_sequence,
                    previous_questions=previous_questions,
                    previous_answer=reviewed,
                )
        elif completes_interview:
            evaluation_data = evaluate_answer(answer.question, answer, document_id=document_id)
        else:
            evaluation_data, next_question_data = (
                evaluate_answer_and_generate_question(
                    answer.question,
                    answer,
                    next_sequence,
                    document_id=document_id,
                )
            )
        evaluation, _ = AnswerEvaluation.objects.update_or_create(
            answer=answer,
            defaults=evaluation_data,
        )

        if attempted_count >= session.question_count:
            logger.info(
                "All %d questions answered for session %s — completing interview",
                session.question_count, session.public_id,
            )
            prepare_interview_for_report(session)
            return evaluation, None

        next_question = _create_question(
            session,
            next_sequence,
            previous_answer=reviewed,
            question_data=next_question_data,
        )
        logger.info(
            "Answer %s evaluated for Q%s in session %s — score=%.2f — next=Q%s",
            answer.public_id,
            answer.question.sequence_number,
            session.public_id,
            float(evaluation.total_score),
            next_sequence,
        )
        return evaluation, next_question


def _ensure_current_question_audio(session):
    """Fast-path start: give the first served question its audio.

    The pool is bulk-created without audio, so unlike the legacy per-turn
    creation (which synthesized at creation), the current question is
    synthesized here (and cached) so the room page renders the audio the same
    way it did before. Failure keeps the text-only fallback.
    """
    question = (
        session.questions.filter(skipped=False)
        .exclude(answer__submitted_at__isnull=False)
        .order_by("sequence_number")
        .first()
    )
    if not question or question.audio_file:
        return
    try:
        with timed_latency(
            "tts.prewarm",
            session_id=session.public_id,
            question_id=question.public_id,
        ):
            synthesize_question(question)
    except TextToSpeechError:
        return


def _fast_submit_path(answer, session, reviewed, completes_interview):
    """Background-evaluation submit: return instantly with the next pool question.

    Saves the corrected transcript (authoritative for content evaluation), runs a
    deterministic quality probe for adaptivity (no LLM), marks the answer for the
    background worker, and returns the next pre-planned pool question with its
    audio ready. The detailed evaluation happens in the worker.
    """
    question = answer.question
    _profile, estimated = answer_quality(question, reviewed)
    answer.evaluation_status = "pending"
    answer.evaluation_error = ""
    answer.save(update_fields=("evaluation_status", "evaluation_error"))
    record_submit_state(session, question, estimated)

    if completes_interview:
        logger.info(
            "Answer %s is the final answer for session %s — finalization deferred to worker",
            answer.public_id, session.public_id,
        )
        record_latency(
            "submit.decision",
            session_id=session.public_id,
            question_id=question.public_id,
            answer_id=answer.public_id,
            notes=f"action=final est={estimated:.1f}",
        )
        return None, None

    next_question = next_pool_question(session)
    if next_question is None:
        return None, None

    with timed_latency(
        "submit.tts.synthesize",
        session_id=session.public_id,
        question_id=next_question.public_id,
        answer_id=answer.public_id,
    ):
        try:
            synthesize_question(next_question)
        except TextToSpeechError:
            pass

    record_latency(
        "submit.decision",
        session_id=session.public_id,
        question_id=question.public_id,
        answer_id=answer.public_id,
        notes=f"action=next next_seq={next_question.sequence_number} est={estimated:.1f}",
    )
    logger.info(
        "Answer %s queued for Q%s in session %s — fast path (est=%.1f), next=Q%s",
        answer.public_id,
        question.sequence_number,
        session.public_id,
        float(estimated),
        next_question.sequence_number,
    )
    return None, next_question


@transaction.atomic
def skip_question(question):
    question = InterviewQuestion.objects.select_for_update().get(pk=question.pk)
    session = question.session
    if session.status != "in_progress":
        raise InterviewStateError("The interview is not in progress.")
    question.skipped = True
    question.save(update_fields=("skipped",))
    attempted = session.questions.filter(
        models_q_answered_or_skipped()
    ).count()
    if fast_pipeline_enabled():
        if attempted >= session.question_count:
            return None
        next_question = next_pool_question(session)
        if next_question is not None:
            try:
                synthesize_question(next_question)
            except TextToSpeechError:
                pass
        return next_question
    if attempted >= session.question_count:
        prepare_interview_for_report(session)
        return None
    return _create_question(session, session.questions.count() + 1)


@transaction.atomic
def finish_attempt_fast(session):
    """Fast-pipeline end-of-interview handoff.

    Marks every unanswered pool question as skipped and lets the background
    worker finalize the interview once the submitted answers are evaluated.
    Requires at least one submitted answer, matching legacy semantics.
    """
    session = InterviewSession.objects.select_for_update().get(pk=session.pk)
    if session.status != "in_progress":
        raise InterviewStateError("The interview is not in progress.")
    submitted = session.questions.filter(
        answer__submitted_at__isnull=False
    ).count()
    if submitted == 0:
        raise InterviewStateError("At least one answer is required to finish.")
    remaining = session.questions.filter(
        skipped=False
    ).exclude(answer__submitted_at__isnull=False)
    remaining.update(skipped=True)
    logger.info(
        "Fast-pipeline finish for session %s — %d submitted, rest skipped; worker will finalize",
        session.public_id, submitted,
    )
    return session


def models_q_answered_or_skipped():
    from django.db.models import Q

    return Q(skipped=True) | Q(answer__submitted_at__isnull=False)


def ensure_report_evaluations(session):
    """Inline-evaluate submissions not yet evaluated by the background worker.

    Called by generate_report so report generation never depends on the worker
    being alive. Idempotent: answers already ``ready`` are skipped, and answers
    actively claimed by the worker (``evaluating``) are left alone — the claim
    logic in the worker guarantees mutual exclusion.
    """
    from mock_interview.services import interview_worker

    rows = (
        session.questions.filter(answer__submitted_at__isnull=False)
        .exclude(answer__evaluation_status="ready")
        .select_related("answer")
    )
    for row in rows:
        interview_worker._evaluate_one(row.answer)


@transaction.atomic
def prepare_interview_for_report(session):
    session = InterviewSession.objects.select_for_update().get(pk=session.pk)
    if session.status == "report_ready" and InterviewReport.objects.filter(
        session=session
    ).exists():
        return session
    evaluations = list(
        AnswerEvaluation.objects.filter(answer__question__session=session)
    )
    if not evaluations:
        raise InterviewStateError(
            "At least one evaluated answer is required to finish."
        )
    session.status = "completed"
    if session.completed_at is None:
        session.completed_at = timezone.now()
    session.error_message = ""
    session.save(
        update_fields=(
            "status",
            "completed_at",
            "error_message",
            "updated_at",
        )
    )
    InterviewAssignment.objects.filter(
        session=session,
        status__in=("assigned", "in_progress"),
    ).update(
        status="completed",
        completed_at=session.completed_at,
    )
    logger.info(
        "Session %s marked as completed with %d evaluations",
        session.public_id, len(evaluations),
    )
    return session


def finish_interview(session):
    with transaction.atomic():
        session = InterviewSession.objects.select_for_update().get(pk=session.pk)
        existing_report = InterviewReport.objects.filter(session=session).first()
        if session.status == "report_ready" and existing_report:
            return existing_report
        if session.status == "evaluating":
            raise InterviewStateError("The coaching report is already being generated.")
        evaluations = list(
            AnswerEvaluation.objects.filter(answer__question__session=session)
            .select_related("answer__question")
            .order_by("answer__question__sequence_number")
        )
        if not evaluations:
            raise InterviewStateError(
                "At least one evaluated answer is required to finish."
            )
        session.status = "evaluating"
        if session.completed_at is None:
            session.completed_at = timezone.now()
        session.error_message = ""
        session.save(
            update_fields=(
                "status",
                "completed_at",
                "error_message",
                "updated_at",
            )
        )
        InterviewAssignment.objects.filter(
            session=session,
            status__in=("assigned", "in_progress"),
        ).update(
            status="completed",
            completed_at=session.completed_at,
        )

    overall = sum(float(row.total_score) for row in evaluations) / len(evaluations)
    information_graph = build_session_information_graph(session, evaluations)
    evaluation_rows = [
        {
            "question": row.answer.question.question_text,
            "score": float(row.total_score),
            "dimension_scores": row.dimension_scores,
            "strengths": row.strengths,
            "improvement_actions": row.improvement_actions,
        }
        for row in evaluations
    ]
    logger.info(
        "finish_interview: session %s — %d evaluations, overall=%.2f, status=%s",
        session.public_id, len(evaluations), overall, session.status,
    )
    try:
        report_text = generate_report_text(
            session,
            evaluation_rows,
            overall,
            information_graph=information_graph.get("insights", {}),
        )
        # dimension_analysis must be removed before spreading into
        # InterviewReport.update_or_create — it is not a model field.
        # Always pop it (even when falsy / empty) to prevent FieldError.
        dimension_analysis = report_text.pop("dimension_analysis", None)
        if dimension_analysis:
            information_graph.setdefault("insights", {})["dimension_analysis"] = (
                dimension_analysis
            )
        logger.info(
            "Report text generated for session %s via model %s",
            session.public_id, report_text.get("model_name", "unknown"),
        )
    except Exception as exc:
        logger.error(
            "Report generation failed for session %s: %s",
            session.public_id, exc,
        )
        InterviewSession.objects.filter(pk=session.pk).update(
            status="failed",
            error_message=f"Report generation failed: {exc}"[:255],
            updated_at=timezone.now(),
        )
        raise
    round_lower = str(getattr(session, "interview_round", "")).lower()
    is_behavioural = "behavio" in round_lower or "hr" in round_lower
    is_technical = "technical" in round_lower or "mixed" in round_lower

    if is_behavioural:
        behavioural_dims = {
            "star_structure", "specific_evidence", "reflection",
            "relevance", "communication",
        }
        b_scores = []
        for row in evaluations:
            for dim, val in (row.dimension_scores or {}).items():
                if dim.lower() in behavioural_dims:
                    b_scores.append(float(val))
        technical = (
            round(sum(b_scores) / len(b_scores) * 10, 2)
            if b_scores else overall
        )
    else:
        tech_dims = {
            "technical_correctness", "completeness", "relevance",
            "structure", "practical_example",
        }
        t_scores = []
        for row in evaluations:
            for dim, val in (row.dimension_scores or {}).items():
                if dim.lower() in tech_dims:
                    t_scores.append(float(val))
        technical = (
            round(sum(t_scores) / len(t_scores) * 10, 2)
            if t_scores else overall
        )

    comm_dims = {"communication", "star_structure", "relevance"}
    communication_values = []
    for row in evaluations:
        for dim, val in (row.dimension_scores or {}).items():
            if dim.lower() in comm_dims:
                communication_values.append(float(val) * 10)
    if not communication_values:
        communication_values = [
            float(row.dimension_scores.get("communication", row.total_score / 10))
            * 10
            for row in evaluations
        ]
    communication = (
        round(sum(communication_values) / len(communication_values), 2)
        if communication_values
        else overall
    )
    try:
        with transaction.atomic():
            session = InterviewSession.objects.select_for_update().get(pk=session.pk)
            report, _ = InterviewReport.objects.update_or_create(
                session=session,
                defaults={
                    "overall_score": Decimal(str(round(overall, 2))),
                    "technical_score": Decimal(str(round(technical, 2))),
                    "communication_score": Decimal(str(round(communication, 2))),
                    "information_graph": information_graph,
                    "answer_coaching": report_text.get("answer_coaching") or [],
                    "communication_intelligence": report_text.get("communication_intelligence") or {},
                    "competency_breakdown": report_text.get("competency_breakdown") or [],
                    "fix_this_first": report_text.get("fix_this_first") or {},
                    "ready_for_next": report_text.get("ready_for_next") or {},
                    "seven_day_plan": report_text.get("seven_day_plan") or [],
                    **{k: v for k, v in report_text.items() if k not in ("answer_coaching", "communication_intelligence", "competency_breakdown", "fix_this_first", "ready_for_next", "seven_day_plan")},
                },
            )
            _generate_report_pdf(report)
            session.overall_score = report.overall_score
            session.status = "report_ready"
            session.error_message = ""
            session.save(
                update_fields=(
                    "overall_score",
                    "status",
                    "error_message",
                    "updated_at",
                )
            )
        logger.info(
            "InterviewReport %s saved for session %s → status=report_ready, "
            "overall=%.2f, technical=%.2f, communication=%.2f",
            report.pk, session.public_id,
            float(report.overall_score),
            float(report.technical_score),
            float(report.communication_score),
        )
    except Exception as exc:
        logger.error(
            "Report storage failed for session %s: %s",
            session.public_id, exc,
        )
        InterviewSession.objects.filter(pk=session.pk).update(
            status="failed",
            error_message=f"Report storage failed: {exc}"[:255],
            updated_at=timezone.now(),
        )
        raise
    return report


def _generate_report_pdf(report):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError:
        return
    import io

    session = report.session
    round_lower = str(getattr(session, "interview_round", "")).lower()
    is_behavioural = "behavio" in round_lower or "hr" in round_lower
    content_label = "Behavioural Quality" if is_behavioural else "Content"

    stream = io.BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=16, spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"], fontSize=10,
        textColor=colors.grey, spaceAfter=12,
    )
    heading_style = ParagraphStyle(
        "ReportHeading", parent=styles["Heading2"], fontSize=12,
        spaceBefore=14, spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "ReportBody", parent=styles["Normal"], fontSize=9, leading=13,
    )
    small_style = ParagraphStyle(
        "ReportSmall", parent=styles["Normal"], fontSize=8, leading=11,
        textColor=colors.Color(0.3, 0.3, 0.3),
    )

    story.append(Paragraph("AI Mock Interview Coaching Report", title_style))

    meta_lines = [
        f"<b>Student:</b> {session.student_name or session.student_employee_id}",
        f"<b>Role:</b> {_esc(session.role)}",
        f"<b>Round:</b> {_esc(session.interview_round)}",
        f"<b>Difficulty:</b> {_esc(session.difficulty)}",
        f"<b>Date:</b> {session.completed_at.strftime('%d %b %Y, %H:%M') if session.completed_at else session.created_at.strftime('%d %b %Y')}",
    ]
    if session.mock_interview:
        meta_lines.append(
            f"<b>Document:</b> {_esc(session.mock_interview.title)}"
        )
    story.append(Paragraph(" &nbsp;|&nbsp; ".join(meta_lines), subtitle_style))

    score_data = [
        ["Overall", f"{report.overall_score}/100"],
        [content_label, f"{report.technical_score}/100"],
        ["Communication", f"{report.communication_score}/100"],
    ]
    score_table = Table(score_data, colWidths=[2.2 * inch, 1.2 * inch])
    score_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.93, 0.95, 1.0)),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.8, 0.8, 0.8)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(score_table)
    story.append(Spacer(1, 12))

    story.append(Paragraph("Summary", heading_style))
    for line in _wrap_text(str(report.summary), 95):
        story.append(Paragraph(_esc(line), body_style))

    evaluations = list(
        AnswerEvaluation.objects.filter(answer__question__session=session)
        .select_related("answer__question")
        .order_by("answer__question__sequence_number")
    )
    if evaluations:
        story.append(Paragraph("Question Analysis", heading_style))
        for ev in evaluations:
            q = ev.answer.question
            story.append(
                Paragraph(
                    f"<b>Q{q.sequence_number}:</b> {_esc(q.question_text[:200])}",
                    body_style,
                )
            )
            story.append(
                Paragraph(
                    f"Score: {ev.total_score}/100 &nbsp;&nbsp; "
                    f"Round: {_esc(q.question_type)}",
                    small_style,
                )
            )
            dims = ev.dimension_scores or {}
            if dims:
                dim_parts = [
                    f"{k}: {v}/10" for k, v in dims.items()
                ]
                story.append(
                    Paragraph(
                        f"Dimensions: {' | '.join(dim_parts)}", small_style
                    )
                )
            if ev.strengths:
                story.append(
                    Paragraph(
                        f"<b>Strengths:</b> {_esc('; '.join(ev.strengths[:3]))}",
                        small_style,
                    )
                )
            if ev.improvement_actions:
                story.append(
                    Paragraph(
                        f"<b>Improvements:</b> {_esc('; '.join(ev.improvement_actions[:3]))}",
                        small_style,
                    )
                )
            story.append(Spacer(1, 6))

    story.append(Paragraph("Strengths", heading_style))
    for item in report.strengths:
        story.append(Paragraph(f"• {_esc(item)}", body_style))

    story.append(Paragraph("Improvement Areas", heading_style))
    for item in report.improvement_areas:
        story.append(Paragraph(f"• {_esc(item)}", body_style))

    if report.learning_plan:
        story.append(Paragraph("Learning Plan", heading_style))
        for i, item in enumerate(report.learning_plan, 1):
            story.append(Paragraph(f"{i}. {_esc(item)}", body_style))

    swot = report.swot_analysis or {}
    if any(swot.get(k) for k in ("strengths", "weaknesses", "opportunities", "threats")):
        story.append(Paragraph("SWOT Analysis", heading_style))
        swot_labels = [
            ("strengths", "Strengths"),
            ("weaknesses", "Weaknesses"),
            ("opportunities", "Opportunities"),
            ("threats", "Threats"),
        ]
        for key, label in swot_labels:
            items = swot.get(key) or []
            if not items:
                continue
            story.append(Paragraph(f"<b>{label}</b>", body_style))
            for item in items:
                story.append(Paragraph(f"• {_esc(item)}", body_style))
            story.append(Spacer(1, 4))

    dim_analysis = (
        report.information_graph or {}
    ).get("insights", {}).get("dimension_analysis", {})
    if dim_analysis:
        story.append(Paragraph("Dimension Analysis", heading_style))
        for dim, text in dim_analysis.items():
            story.append(
                Paragraph(f"<b>{_esc(dim)}:</b> {_esc(text)}", body_style)
            )

    doc.build(story)
    report.pdf_file.save(
        f"mock-interview-{report.session.public_id}.pdf",
        ContentFile(stream.getvalue()),
        save=True,
    )


def _esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap_text(text, width):
    words = text.split()
    lines = []
    current = []
    for word in words:
        if len(" ".join(current + [word])) > width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]
