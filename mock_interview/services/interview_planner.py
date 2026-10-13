"""Pre-planned question pool for the fast interview pipeline.

The full question set for an interview is written ahead of time as ``pool``
rows (source="plan"), so the submit -> next-question path picks an already-
existing question instead of waiting on an LLM. The pool is created at
``start_interview`` (one batched Qwen call, or a deterministic fallback when the
local model is unavailable) and produced in a progressive difficulty ramp so the
first unanswered question is always the next adaptive step.
"""

import logging

from django.conf import settings

from mock_interview.ai.interview_engine import (
    _answer_quality_profile,
    _score_cap_for_profile,
    normalize_resume,
)
from mock_interview.ai.ollama_client import (
    LocalModelError,
    chat_json,
    configured_model,
)
from mock_interview.models import InterviewQuestion
from mock_interview.services.telemetry import record_latency, timed_latency

logger = logging.getLogger(__name__)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 5},
                    "type": {
                        "type": "string",
                        "enum": ["technical", "behavioural", "situational"],
                    },
                    "topic": {"type": "string"},
                    "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
                    "rubric": {"type": "object"},
                    "expected_concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "selection_reason": {"type": "string"},
                },
                "required": [
                    "text",
                    "type",
                    "topic",
                    "difficulty",
                    "expected_concepts",
                    "selection_reason",
                ],
            },
        }
    },
    "required": ["questions"],
}


def fast_pipeline_enabled():
    return bool(settings.MOCK_INTERVIEW.get("FAST_INTERVIEW_PIPELINE", True))


def answer_quality(question, transcript):
    """Deterministic evidence-supported quality estimate (used for adaptivity).

    Runs in a few milliseconds with no LLM: same signal source the detailed
    evaluation calibrates against (_answer_quality_profile / _score_cap_for_profile).
    Returns (profile, estimated_score_0_10) where the estimate is the maximum the
    detailed evaluation may award.
    """
    profile = _answer_quality_profile(
        transcript,
        question.expected_concepts,
        getattr(question, "question_text", ""),
    )
    return profile, float(_score_cap_for_profile(profile))


def _clean_skills(session):
    skills = list(session.target_skills or [])
    if not skills and session.resume_id:
        profile = session.resume.structured_profile or {}
        skills = profile.get("skills", []) or profile.get("technical_skills", [])
    if not skills and getattr(session, "mock_interview_id", None):
        skills = list(session.mock_interview.target_skills or [])
    return [str(item).strip() for item in skills if str(item).strip()][:8]


def _plan_context(session):
    """Compact, context-window-friendly planning context for one session."""
    if getattr(session, "mock_interview_id", None):
        mock = session.mock_interview
        context = {
            "interview_type": "faculty_document",
            "title": mock.title,
            "subject_code": mock.subject_code,
            "chapter": mock.chapter,
            "description": (mock.description or "")[:1200],
            "difficulty": mock.difficulty,
            "mode": mock.interview_mode,
            "target_skills": list(mock.target_skills or []),
            "language_mode": session.language_mode,
            "question_count": int(session.question_count),
        }
        if mock.document_id and mock.document.extracted_text:
            context["document_excerpt"] = mock.document.extracted_text[:2500]
        return context

    resume_text = ""
    structured = {}
    if session.resume_id:
        resume_text = (session.resume.extracted_text or "")[:1800]
        try:
            structured = normalize_resume(session.resume.extracted_text or "")
        except Exception:
            structured = session.resume.structured_profile or {}
    return {
        "interview_type": "role_based",
        "role": session.role,
        "company_name": session.company_name,
        "job_description": (session.job_description or "")[:1500],
        "difficulty": session.difficulty,
        "round": session.interview_round,
        "target_skills": _clean_skills(session),
        "language_mode": session.language_mode,
        "question_count": int(session.question_count),
        "resume_excerpt": resume_text,
        "structured_profile_excerpt": {
            key: structured.get(key)
            for key in ("headline", "summary", "skills", "project_highlights")
            if key in structured
        },
    }


_PLAN_SYSTEM_PROMPT = (
    "You are an expert interview coach generating a complete mock-interview "
    "question set for a college placement mock interview. Write original, "
    "specific questions. Order them in a clear progression: start broad and "
    "easy (difficulty 1-2), then probe deeper (difficulty 3), then finish with "
    "challenging questions that require connecting concepts (difficulty 4-5). "
    "For behavioural/situational items keep them tied to engineering, project "
    "work, and campus-to-workplace scenarios. Each question must include a "
    "selection_reason explaining why it is asked at that stage."
)


def _normalize_plan_item(item, seq):
    return {
        "question_text": str(item.get("text") or "").strip(),
        "question_type": str(
            item.get("type") or item.get("question_type") or "technical"
        ).lower(),
        "topic": str(item.get("topic") or "").strip(),
        "difficulty": int(item.get("difficulty") or 0) or _ramp_difficulty(seq),
        "rubric": dict(item.get("rubric") or {}),
        "expected_concepts": list(item.get("expected_concepts") or []),
        "selection_reason": str(item.get("selection_reason") or "").strip(),
    }


def _ramp_difficulty(seq):
    return min(5, max(1, (int(seq) + 1) // 2))


def _deterministic_pool_question(session, seq):
    skills = _clean_skills(session)
    difficulty = _ramp_difficulty(seq)
    if getattr(session, "mock_interview_id", None):
        mock = session.mock_interview
        subject = f"{mock.subject_code}" + (f" - {mock.chapter}" if mock.chapter else "")
        topic = skills[(seq - 1) % len(skills)] if skills else f"{subject} concept"
        text = (
            f"Explain the core concept behind {topic} in the context of "
            f"{mock.title} and give a practical example."
        )
        selection_reason = (
            f"Tier {difficulty} pool question on {topic or subject} "
            "with a practical-example probe."
        )
    else:
        topic = skills[(seq - 1) % len(skills)] if skills else session.role
        text = (
            f"Walk me through how you applied {topic} in a real project. "
            "Explain the approach, the outcome, and what you would do differently."
        )
        selection_reason = (
            f"Tier {difficulty} pool question probing {topic} with an "
            "approach/outcome/retrospective structure."
        )
    return {
        "question_text": text,
        "question_type": "technical",
        "topic": topic,
        "difficulty": difficulty,
        "rubric": {
            "technical_correctness": 100,
            "communication": 100,
            "relevance": 100,
        },
        "expected_concepts": [topic] if len(topic) <= 60 else [session.role],
        "selection_reason": selection_reason,
    }


def _request_plan(session):
    """Try one batched Qwen call; fall back to deterministic questions."""
    try:
        import json as _json

        context_text = _json.dumps(_plan_context(session), ensure_ascii=False)
        messages = [
            {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Generate the complete question set for this interview as JSON:"
                    "\n\n"
                    + context_text
                ),
            },
        ]
        result = chat_json(
            messages,
            PLAN_SCHEMA,
            temperature=0.7,
            context_tokens=4096,
            max_output_tokens=2048,
        )
        items = result.get("questions") or []
        if not isinstance(items, list) or not items:
            raise LocalModelError("Planner returned no questions.")
        return [_normalize_plan_item(item, i) for i, item in enumerate(items, 1)]
    except LocalModelError as exc:
        logger.warning(
            "Question-pool planning fell back to deterministic questions: %s. "
            "Session %s",
            exc,
            getattr(session, "public_id", ""),
        )
        return None


def _planner_items(session, count):
    plan = _request_plan(session)
    if plan:
        if len(plan) >= count:
            return plan[:count]
        return plan + [
            _deterministic_pool_question(session, seq)
            for seq in range(len(plan) + 1, count + 1)
        ]
    return [_deterministic_pool_question(session, seq) for seq in range(1, count + 1)]


def build_question_pool(session, *, force=False):
    """Create or fill the session's pre-planned question set (idempotent)."""
    with timed_latency("planning.pool.build", session_id=session.public_id):
        count = max(1, int(session.question_count or 1))
        existing = {question.sequence_number: question for question in session.questions.all()}
        missing = [
            seq for seq in range(1, count + 1) if seq not in existing
        ]
        if not missing:
            return sorted(existing.values(), key=lambda question: question.sequence_number)

        items = _planner_items(session, count)
        if len(items) < count:
            items += [
                _deterministic_pool_question(session, seq)
                for seq in range(len(items) + 1, count + 1)
            ]
        items = items[:count]

        model_name = ""
        try:
            model_name = configured_model()
        except Exception:
            model_name = "deterministic-plan"

        created = [
            InterviewQuestion(
                session=session,
                sequence_number=seq,
                question_text=str(item["question_text"])[:1500],
                question_type=str(item["question_type"])[:50],
                source="plan",
                topic=str(item["topic"])[:150],
                difficulty_level=item["difficulty"],
                selection_reason=str(item["selection_reason"])[:2000],
                rubric=item.get("rubric") or {},
                expected_concepts=item.get("expected_concepts") or [],
                model_name=model_name,
            )
            for seq, item in zip(missing, (items[seq - 1] for seq in missing))
        ]
        InterviewQuestion.objects.bulk_create(created)

        state = dict(session.interview_state or {})
        state["pool"] = {"built": True, "size": count, "model": model_name}
        session.interview_state = state
        session.save(update_fields=("interview_state", "updated_at"))
        logger.info(
            "Built %d-question pool for session %s (%s)",
            count,
            session.public_id,
            model_name,
        )
        return list(session.questions.order_by("sequence_number"))


def next_pool_question(session):
    """Lowest-sequence unanswered, unskipped pool question."""
    return (
        session.questions.filter(skipped=False)
        .exclude(answer__submitted_at__isnull=False)
        .order_by("sequence_number")
        .first()
    )


def record_submit_state(session, question, estimated_score):
    """Compact non-PII mastery history used for adaptivity and telemetry."""
    state = dict(session.interview_state or {})
    history = list(state.get("history") or [])
    history.append(
        {
            "question": question.sequence_number,
            "topic": question.topic or "",
            "difficulty": question.difficulty_level or 0,
            "estimated": round(float(estimated_score or 0.0), 1),
        }
    )
    history = history[-30:]
    state["history"] = history
    state["last"] = {
        "question": question.sequence_number,
        "estimated": round(float(estimated_score or 0.0), 1),
    }
    session.interview_state = state
    session.save(update_fields=("interview_state", "updated_at"))
    record_latency(
        "submit.adaptive.probe",
        session_id=session.public_id,
        question_id=question.public_id,
        duration_ms=0.0,
        notes=f"est={state['last']['estimated']}",
    )
    return state