"""Background worker for the fast interview pipeline.

Moves detailed answer evaluation, RAG retrieval, speech-metric scoring, pool
audio pre-warming, session finalization and report generation off the
student-facing request path. Auto-starts as a daemon thread in the web process
(MOCK_INTERVIEW["INTERVIEW_WORKER_AUTO_START"]) or runs standalone via:

    python manage.py run_interview_worker

Idempotency: each answer is only evaluated once per claim. Claims flip
evaluation_status atomically (pending/evaluating/failed with backoff), and
AnswerEvaluation is created with ``update_or_create`` so a crash + stale-reclaim
(INTERVIEW_EVAL_STALE_SECONDS) can never double-count a score.
"""

import logging
import sys
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mock_interview.ai.interview_engine import evaluate_answer
from mock_interview.models import (
    AnswerEvaluation,
    InterviewQuestion,
    InterviewSession,
    StudentAnswer,
)
from mock_interview.services import interview_service
from mock_interview.services.evaluator import RAGEvaluator
from mock_interview.services.interview_planner import fast_pipeline_enabled
from mock_interview.services.telemetry import record_latency, timed_latency
from mock_interview.speech.tts import TextToSpeechError, synthesize_question

logger = logging.getLogger(__name__)

_worker_started = False
_worker_lock = threading.Lock()


def _cfg(key, default):
    return settings.MOCK_INTERVIEW.get(key, default)


def max_eval_attempts():
    return int(_cfg("INTERVIEW_EVAL_MAX_ATTEMPTS", 3))


def stale_before():
    return timezone.now() - timedelta(
        seconds=float(_cfg("INTERVIEW_EVAL_STALE_SECONDS", 600))
    )


def _pending_answers(limit=8, *, reclaim_stale=True):
    query = Q(evaluation_status="pending")
    query |= Q(
        evaluation_status="failed",
        evaluation_attempts__lt=max_eval_attempts(),
    )
    if reclaim_stale:
        query |= Q(
            evaluation_status="evaluating",
            submitted_at__lt=stale_before(),
            evaluation_attempts__gt=0,
        )
    return list(
        StudentAnswer.objects.filter(query, submitted_at__isnull=False)
        .select_related("question__session")
        .order_by("submitted_at")[:limit]
    )


def _claim(answer, *, reclaim_stale=True):
    """Atomically claim one answer for evaluation; returns row or None."""
    row = (
        StudentAnswer.objects.select_for_update()
        .select_related("question__session")
        .get(pk=answer.pk)
    )
    if row.evaluation_status == "ready":
        return None
    if row.evaluation_status == "pending":
        pass
    elif (
        row.evaluation_status == "failed"
        and row.evaluation_attempts < max_eval_attempts()
    ):
        pass
    elif (
        reclaim_stale
        and row.evaluation_status == "evaluating"
        and row.submitted_at
        and row.submitted_at < stale_before()
        and row.evaluation_attempts > 0
    ):
        pass
    else:
        return None
    row.evaluation_status = "evaluating"
    row.evaluation_attempts = (row.evaluation_attempts or 0) + 1
    row.save(update_fields=("evaluation_status", "evaluation_attempts"))
    return row


def _evaluate_one(answer, *, reclaim_stale=True):
    """Claim and evaluate one answer inside its own transaction.

    The atomic wrapper is required in production: the claim uses
    select_for_update(), which Django rejects when the connection is in
    autocommit (outside a transaction). One answer = one transaction, so a
    crash never leaves a half-committed evaluation and the worker keeps
    making progress on the remaining answers.
    """
    claimed = None
    try:
        with transaction.atomic():
            claimed = _claim(answer, reclaim_stale=reclaim_stale)
            if claimed is None:
                return
            question = claimed.question
            session = claimed.question.session
            try:
                document_id = ""
                if (
                    getattr(session, "mock_interview_id", None)
                    and getattr(session.mock_interview, "document_id", None)
                ):
                    document_id = str(session.mock_interview.document_id)
                with timed_latency(
                    "evaluation.detailed",
                    session_id=session.public_id,
                    question_id=question.public_id,
                    answer_id=claimed.public_id,
                ):
                    if document_id:
                        evaluation_data = RAGEvaluator().evaluate_answer(
                            question, claimed, document_id=document_id
                        )
                    else:
                        evaluation_data = evaluate_answer(
                            question, claimed, document_id=""
                        )
                AnswerEvaluation.objects.update_or_create(
                    answer=claimed, defaults=evaluation_data
                )
                claimed.evaluation_status = "ready"
                claimed.evaluation_error = ""
                claimed.save(
                    update_fields=("evaluation_status", "evaluation_error")
                )
                record_latency(
                    "evaluation.store",
                    session_id=session.public_id,
                    question_id=question.public_id,
                    answer_id=claimed.public_id,
                    notes="status=ready",
                )
            except Exception as exc:  # noqa: BLE001 - worker must keep moving
                attempts = claimed.evaluation_attempts or 0
                claimed.evaluation_error = str(exc)[:500]
                claimed.evaluation_status = (
                    "failed" if attempts >= max_eval_attempts() else "pending"
                )
                claimed.save(
                    update_fields=("evaluation_status", "evaluation_error")
                )
                record_latency(
                    "evaluation.failed",
                    session_id=session.public_id,
                    question_id=question.public_id,
                    answer_id=claimed.public_id,
                    notes=f"attempts={attempts} error={str(exc)[:200]}",
                )
                logger.warning(
                    "Evaluation failed for answer %s (attempt %s/%s): %s",
                    claimed.public_id,
                    attempts,
                    max_eval_attempts(),
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not claim/process answer %s: %s",
            getattr(answer, "public_id", "?"),
            exc,
        )


def process_evaluations(limit=8, *, reclaim_stale=True):
    answers = _pending_answers(limit, reclaim_stale=reclaim_stale)
    for answer in answers:
        _evaluate_one(answer, reclaim_stale=reclaim_stale)
    return len(answers)


def _pool_questions_needing_audio(limit=6):
    rows = []
    sessions = InterviewSession.objects.filter(status="in_progress")
    for session in sessions:
        state = session.interview_state or {}
        if not state.get("pool", {}).get("built"):
            continue
        pending = (
            session.questions.filter(skipped=False)
            .exclude(answer__submitted_at__isnull=False)
            .order_by("sequence_number")
        )
        for question in pending:
            if not question.audio_file:
                rows.append(question)
                if len(rows) >= limit:
                    return rows
        if len(rows) >= limit:
            return rows
    return rows


def warm_pool_audio(limit=6):
    if not fast_pipeline_enabled():
        return 0
    synthesized = 0
    for question in _pool_questions_needing_audio(limit):
        try:
            with timed_latency(
                "tts.prewarm",
                session_id=question.session.public_id,
                question_id=question.public_id,
            ):
                synthesize_question(question)
            synthesized += 1
        except TextToSpeechError:
            continue
    return synthesized


def _all_pool_questions_attempted(session):
    state = session.interview_state or {}
    if not state.get("pool", {}).get("built"):
        return False
    questions = session.questions.all()
    return not any(
        not question.skipped
        and not StudentAnswer.objects.filter(
            question=question, submitted_at__isnull=False
        ).exists()
        for question in questions
    )


def _pool_evaluations_ready(session):
    answers = StudentAnswer.objects.filter(
        question__session=session, submitted_at__isnull=False
    )
    return answers.exists() and not any(
        item.evaluation_status != "ready" for item in answers
    )


def finalize_completed_sessions():
    """Promote fully-evaluated, fully-attempted fast sessions to completed."""
    finalized = 0
    for session in InterviewSession.objects.filter(status="in_progress"):
        if not _all_pool_questions_attempted(session):
            continue
        if not _pool_evaluations_ready(session):
            continue
        try:
            with timed_latency(
                "completion.finalize",
                session_id=session.public_id,
            ):
                interview_service.prepare_interview_for_report(session)
            finalized += 1
            logger.info("Finalized session %s after background evaluation.", session.public_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not finalize session %s: %s", session.public_id, exc)
    return finalized


def process_report_requests():
    """Generate coaching reports for sessions queued by generate_report."""
    produced = 0
    queued = list(
        InterviewSession.objects.filter(
            status="evaluating", report_generation=True
        )
    )
    for session in queued:
        try:
            with transaction.atomic():
                claimed = (
                    InterviewSession.objects.select_for_update().get(pk=session.pk)
                )
                if claimed.status != "evaluating" or not claimed.report_generation:
                    continue
                claimed.status = "completed"
                claimed.error_message = ""
                claimed.save(
                    update_fields=("status", "error_message", "updated_at")
                )
            with timed_latency(
                "report.generate",
                session_id=session.public_id,
            ):
                interview_service.finish_interview(claimed)
            claimed.refresh_from_db()
            if claimed.status == "report_ready":
                InterviewSession.objects.filter(pk=claimed.pk).update(
                    report_generation=False
                )
            produced += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Report generation failed for %s: %s", session.public_id, exc)
    return produced


def process_pending_tasks_once(
    *,
    evaluations=8,
    prewarm=6,
    reclaim_stale=True,
):
    warm_pool_audio(prewarm)
    process_evaluations(evaluations, reclaim_stale=reclaim_stale)
    finalize_completed_sessions()
    process_report_requests()


def worker_loop(interval=None, stop_event=None, *, evaluations=8, prewarm=6):
    interval = interval or float(_cfg("INTERVIEW_WORKER_INTERVAL", 2.0))
    while stop_event is None or not stop_event.is_set():
        started = time.perf_counter()
        try:
            process_pending_tasks_once(evaluations=evaluations, prewarm=prewarm)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Interview worker round failed: %s", exc)
        elapsed = time.perf_counter() - started
        wait = max(0.0, interval - elapsed)
        if stop_event is not None:
            stop_event.wait(wait)
        else:
            time.sleep(wait)


def _skip_management_commands(argv):
    if len(argv) < 2:
        return False
    return argv[1] in {
        "seed_dummy_data",
        "makemigrations",
        "migrate",
        "test",
        "shell",
        "check",
        "collectstatic",
        "loaddata",
        "dumpdata",
        "createsuperuser",
    }


def ensure_auto_worker():
    """Start the daemon worker from AppConfig.ready (once per process)."""
    global _worker_started
    if _worker_started:
        return
    if not fast_pipeline_enabled():
        return
    if not _cfg("INTERVIEW_WORKER_AUTO_START", True):
        return
    if _skip_management_commands(sys.argv):
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=worker_loop,
            name="mock-interview-worker",
            daemon=True,
        )
        thread.start()
        _worker_started = True
        logger.info("Mock-interview background worker started (daemon).")