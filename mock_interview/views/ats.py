"""Standalone ATS resume checker: upload, analyze, and download the rewrite."""

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from mock_interview.models import ATSResumeCheck, ResumeDocument
from mock_interview.services.access import (
    student_identity,
    student_required,
)
from mock_interview.services.ats_check_service import run_ats_check
from mock_interview.services.interview_service import enrich_resume
from mock_interview.services.resume_parser import (
    ResumeValidationError,
    extract_resume_text,
    file_sha256,
    validate_resume,
)


def _owned_check(request, public_id):
    return get_object_or_404(
        ATSResumeCheck.objects.select_related("source_resume"),
        public_id=public_id,
        student_employee_id=student_identity(request.user)["student_employee_id"],
    )


def _bounded_job_description(value):
    return (value or "").strip()[:8000]


@student_required
def ats_checker(request):
    """Landing page for the ATS Resume Checker (GET) and upload entry (POST)."""
    identity = student_identity(request.user)
    if request.method == "POST":
        uploaded = request.FILES.get("resume")
        if not uploaded:
            return render(
                request,
                "mock_interview/ats_checker.html",
                {"error": "Choose a PDF or DOCX resume to check."},
                status=400,
            )
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
        except ResumeValidationError as exc:
            return render(
                request,
                "mock_interview/ats_checker.html",
                {"error": str(exc)},
                status=400,
            )
        try:
            enrich_resume(resume)
        except Exception:
            # The ATS checker works from extracted text and the parsed shape;
            # an LLM normalization failure must not block the check.
            pass
        ats_check = ATSResumeCheck.objects.create(
            **identity,
            source_resume=resume,
            job_description=_bounded_job_description(
                request.POST.get("job_description")
            ),
        )
        try:
            run_ats_check(ats_check)
        except Exception as exc:
            ats_check.status = "failed"
            ats_check.error_message = str(exc)[:500]
            ats_check.save()
            return render(
                request,
                "mock_interview/ats_checker.html",
                {"error": f"Analysis failed. Please try again. Detail: {exc}"},
                status=500,
            )
        return redirect("mock_interview:ats_report", public_id=ats_check.public_id)

    return render(
        request,
        "mock_interview/ats_checker.html",
        {"existing_checks": ATSResumeCheck.objects.filter(
            student_employee_id=identity["student_employee_id"],
        ).select_related("source_resume")[:10]},
    )


@student_required
def ats_report(request, public_id):
    check = _owned_check(request, public_id)
    return render(
        request,
        "mock_interview/ats_report.html",
        {"check": check},
    )


@student_required
@require_GET
def ats_check_detail(request, public_id):
    check = _owned_check(request, public_id)
    return JsonResponse(
        {
            "ok": True,
            "public_id": str(check.public_id),
            "status": check.status,
            "error_message": check.error_message,
            "score": float(check.score),
            "score_label": check.score_label,
            "analysis_mode": check.analysis_mode,
            "dimension_scores": check.dimension_scores,
            "issues": check.issues,
            "matched_keywords": check.matched_keywords,
            "missing_keywords": check.missing_keywords,
            "recommendations": check.recommendations,
            "score_explanation": check.score_explanation,
            "personal_info_flags": check.personal_info_flags,
            "action_verb_analysis": check.action_verb_analysis,
            "rewrite_warnings": check.rewrite_warnings,
            "rewrite_source": check.rewrite_source,
            "docx_url": (
                reverse(
                    "mock_interview:ats_download_docx",
                    args=[check.public_id],
                )
                if check.generated_docx
                else None
            ),
            "pdf_url": (
                reverse("mock_interview:ats_download_pdf", args=[check.public_id])
                if check.generated_pdf
                else None
            ),
            "created_at": check.created_at.isoformat(),
        }
    )


def _download(request, public_id, field, content_type, extension):
    check = _owned_check(request, public_id)
    if check.status != "ready":
        return JsonResponse(
            {"ok": False, "error": "The checked resume is not ready."},
            status=404,
        )
    stored = getattr(check, field)
    if not stored:
        return JsonResponse(
            {"ok": False, "error": f"{extension.upper()} resume unavailable."},
            status=404,
        )
    name = f"ats_resume_{check.public_id.hex[:8]}.{extension}"
    return FileResponse(
        stored.open("rb"),
        content_type=content_type,
        as_attachment=True,
        filename=name,
    )


@student_required
@require_GET
def ats_download_docx(request, public_id):
    return _download(
        request,
        public_id,
        "generated_docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    )


@student_required
@require_GET
def ats_download_pdf(request, public_id):
    return _download(
        request,
        public_id,
        "generated_pdf",
        "application/pdf",
        "pdf",
    )