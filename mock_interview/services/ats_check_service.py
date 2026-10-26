"""Orchestrates an ATS resume check: analyze -> tailor -> generate files."""
import logging
from decimal import Decimal

from django.db import transaction

from mock_interview.ai import ats_rewriter
from mock_interview.services.ats import analyze_resume
from mock_interview.services.resume_generator import (
    ResumeGenerationError,
    generate_resume_files,
)
from mock_interview.services.resume_tailoring import (
    detect_career_level,
    infer_target_role,
)

logger = logging.getLogger(__name__)

_PERSISTED_FIELDS = (
    "status",
    "error_message",
    "score",
    "score_label",
    "analysis_mode",
    "dimension_scores",
    "issues",
    "matched_keywords",
    "missing_keywords",
    "recommendations",
    "score_explanation",
    "personal_info_flags",
    "action_verb_analysis",
    "rewrite_warnings",
    "rewrite_source",
    "rules_version",
    "target_role",
    "career_level",
)


@transaction.atomic
def run_ats_check(ats_check):
    """Analyze a source resume and persist the ATSResumeCheck result.

    The score and issues are deterministic (services/ats.py).  The generated
    DOCX/PDF starts from the uploaded resume's own evidence and follows the
    reference ATS hierarchy.  An optional local-LLM wording pass is accepted
    only if it passes the evidence guard; it can never change the score.
    """
    resume = ats_check.source_resume
    analysis = analyze_resume(resume, ats_check.job_description)

    ats_check.score = Decimal(str(round(analysis["score"], 2)))
    ats_check.score_label = analysis["score_label"]
    ats_check.analysis_mode = analysis["analysis_mode"]
    ats_check.dimension_scores = analysis["dimensions"]
    ats_check.issues = analysis["issues"]
    ats_check.matched_keywords = analysis["matched_keywords"]
    ats_check.missing_keywords = analysis["missing_keywords"]
    ats_check.recommendations = analysis["recommendations"]
    ats_check.score_explanation = analysis["score_explanation"]
    ats_check.personal_info_flags = analysis["personal_info_flags"]
    ats_check.action_verb_analysis = analysis["action_verb_analysis"]
    ats_check.rules_version = analysis["rules_version"]

    target_role = infer_target_role(
        ats_check.job_description,
        ats_check.target_role,
        resume.extracted_text,
    )
    career_level = detect_career_level(resume, ats_check.career_level)
    ats_check.target_role = target_role or ats_check.target_role
    if (
        ats_check.career_level == "auto"
        and career_level in {"fresher", "intern", "experienced"}
    ):
        ats_check.career_level = career_level
    try:
        rewrite_map, rewrite_warnings = ats_rewriter.rewrite_resume_bullets(
            resume,
            ats_check.job_description,
            missing_keywords=analysis["missing_keywords"],
            target_role=target_role,
            career_level=career_level,
        )
    except Exception:
        logger.exception("Optional ATS wording pass failed for %s", ats_check.public_id)
        rewrite_map = None
        rewrite_warnings = [
            "The optional wording pass was unavailable; the resume kept the "
            "uploaded wording."
        ]

    ats_check.rewrite_warnings = list(rewrite_warnings or [])
    ats_check.rewrite_source = "deterministic"
    try:
        generate_resume_files(
            ats_check,
            rewrite_map=rewrite_map,
            warnings=ats_check.rewrite_warnings,
            target_role=target_role,
            career_level=career_level,
            job_description=ats_check.job_description,
            matched_keywords=analysis["matched_keywords"],
        )
    except ResumeGenerationError as exc:
        ats_check.status = "failed"
        ats_check.error_message = str(exc)[:500]
        ats_check.save(update_fields=_PERSISTED_FIELDS)
        raise
    ats_check.status = "ready"
    ats_check.error_message = ""
    ats_check.save()
    logger.info(
        "ATS check %s ready for %s (score=%.2f, mode=%s, rewrite=%s)",
        ats_check.public_id,
        ats_check.student_employee_id,
        float(ats_check.score),
        ats_check.analysis_mode,
        ats_check.rewrite_source,
    )
    return ats_check
