import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from user_accounts.models import Add_Department, StudentDetails

from mock_interview.models import (
    AnswerEvaluation,
    InterviewAssignment,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    MockInterview,
    UploadedDocument,
)
from mock_interview.services.access import (
    is_hod_user,
    is_mock_interview_faculty_manager,
    is_student_user,
)
from mock_interview.services.document_service import DocumentService, DocumentServiceError
from mock_interview.services.hod_reports_service import HODDepartmentError, hod_add_department
from mock_interview.services.scheduler import InterviewScheduler, SchedulerError

logger = logging.getLogger(__name__)


def _is_faculty_user(user):
    """Check if the user can open the mock-interview faculty workspace.

    HOD users are allowed here, with department-sensitive operations scoped
    to their mapped department.
    """
    return is_mock_interview_faculty_manager(user)


def _faculty_employee_id(user):
    """Get the employee ID for faculty users."""
    return str(getattr(user, "Employee_id", "") or "").strip()


def _faculty_name(user):
    return str(getattr(user, "username", "") or "").strip()

def _hod_department_scope(user):
    """Return the HOD's academic department, or None for non-HOD/superuser."""
    if not is_hod_user(user) or getattr(user, "is_superuser", False):
        return None
    return hod_add_department(user)


def faculty_required(view_func):
    """Decorator to restrict views to faculty users."""
    from functools import wraps

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return redirect("login_view")
        if not _is_faculty_user(request.user):
            return JsonResponse(
                {"error": "Faculty access required."}, status=403
            )
        return view_func(request, *args, **kwargs)

    return wrapped


# ── Document Management ──────────────────────────────────────────────


@login_required
@faculty_required
def faculty_dashboard(request):
    """Faculty dashboard showing their documents and interviews."""
    employee_id = _faculty_employee_id(request.user)
    documents = UploadedDocument.objects.filter(
        faculty_employee_id=employee_id
    )
    interviews = MockInterview.objects.filter(created_by=employee_id)

    return render(request, "mock_interview/faculty_dashboard.html", {
        "documents": documents,
        "interviews": interviews,
        "user": request.user,
    })


@login_required
@faculty_required
def upload_document_page(request):
    """Page for faculty to upload teaching documents."""
    return render(request, "mock_interview/upload_document.html", {
        "user": request.user,
    })


@login_required
@faculty_required
@require_POST
def upload_document_api(request):
    """API endpoint for document upload and processing."""
    uploaded_file = request.FILES.get("document")
    if not uploaded_file:
        return JsonResponse({"error": "No document provided."}, status=400)

    subject_code = request.POST.get("subject_code", "").strip()
    chapter = request.POST.get("chapter", "").strip()

    if not subject_code:
        return JsonResponse({"error": "Subject code is required."}, status=400)

    employee_id = _faculty_employee_id(request.user)
    faculty_name = _faculty_name(request.user)

    try:
        service = DocumentService()
        document = service.process_document(
            uploaded_file=uploaded_file,
            faculty_employee_id=employee_id,
            faculty_name=faculty_name,
            subject_code=subject_code,
            chapter=chapter,
        )
        return JsonResponse({
            "id": str(document.id),
            "filename": document.original_filename,
            "status": document.status,
            "chunk_count": document.chunk_count,
            "subject_code": document.subject_code,
            "chapter": document.chapter,
        })
    except DocumentServiceError as exc:
        error_msg = str(exc)
        logger.warning("Document upload failed for user %s: %s", employee_id, error_msg)
        return JsonResponse({"error": error_msg}, status=400)
    except Exception as exc:
        logger.exception("Document upload failed unexpectedly for user %s", employee_id)
        return JsonResponse(
            {"error": "Upload failed due to an internal error. Please try again or contact support."},
            status=500,
        )


@login_required
@faculty_required
@require_POST
def delete_document_api(request, document_id):
    """Delete a faculty-uploaded document."""
    document = get_object_or_404(
        UploadedDocument,
        id=document_id,
        faculty_employee_id=_faculty_employee_id(request.user),
    )
    try:
        service = DocumentService()
        service.delete_document(document)
        return JsonResponse({"status": "deleted"})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


# ── Interview Management ─────────────────────────────────────────────


@login_required
@faculty_required
def create_interview_page(request):
    """Page for faculty to create a mock interview."""
    if request.method == "POST":
        try:
            interview, auto_assigned = _create_interview(request.user, request.POST)
        except (SchedulerError, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            suffix = (
                f" {auto_assigned} student(s) auto-assigned."
                if auto_assigned
                else ""
            )
            messages.success(request, f"Interview created successfully.{suffix}")
            return redirect(
                "mock_interview:interview_detail_page",
                interview_id=interview.id,
            )

    employee_id = _faculty_employee_id(request.user)
    documents = UploadedDocument.objects.filter(
        faculty_employee_id=employee_id,
        status="ready",
    )
    try:
        hod_department = _hod_department_scope(request.user)
    except HODDepartmentError as exc:
        messages.error(request, str(exc))
        return redirect("mock_interview:faculty_dashboard")

    departments = (
        Add_Department.objects.filter(pk=hod_department.pk)
        if hod_department is not None
        else Add_Department.objects.filter(is_active=True)
    )
    return render(request, "mock_interview/create_interview.html", {
        "documents": documents,
        "departments": departments,
        "hod_department": hod_department,
        "user": request.user,
    })


def _bounded_int(value, *, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _target_skills(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _create_interview(user, data):
    employee_id = _faculty_employee_id(user)
    title = str(data.get("title", "") or "").strip()
    subject_code = str(data.get("subject_code", "") or "").strip()
    document_id = str(data.get("document_id", "") or "").strip()
    difficulty = str(data.get("difficulty", "medium") or "medium").strip()
    interview_mode = str(
        data.get("interview_mode", "technical") or "technical"
    ).strip()
    language_mode = str(data.get("language_mode", "en") or "en").strip()
    department_id = str(data.get("department_id", "") or "").strip()
    target_batch = str(data.get("target_batch", "") or "").strip()
    target_section = str(data.get("target_section", "") or "").strip()
    target_skills = _target_skills(data.get("target_skills", []))
    start_time = str(data.get("start_time", "") or "").strip()
    end_time = str(data.get("end_time", "") or "").strip()

    if not title or not subject_code:
        raise ValueError("Title and subject code are required.")
    if difficulty not in dict(MockInterview.DIFFICULTY_CHOICES):
        raise ValueError("Invalid difficulty.")
    if interview_mode not in dict(MockInterview.MODE_CHOICES):
        raise ValueError("Invalid interview mode.")
    if language_mode not in {"en", "ta"}:
        raise ValueError("Invalid language mode.")

    document = None
    if document_id:
        document = UploadedDocument.objects.filter(
            id=document_id,
            faculty_employee_id=employee_id,
        ).first()
        if not document:
            raise ValueError("Invalid source document.")

    hod_department = None
    try:
        hod_department = _hod_department_scope(user)
    except HODDepartmentError as exc:
        raise ValueError(str(exc))

    target_department = None
    if hod_department is not None:
        if department_id and str(department_id) != str(hod_department.pk):
            raise ValueError("HOD access is limited to your mapped department.")
        target_department = hod_department
    elif department_id:
        target_department = Add_Department.objects.filter(
            id=department_id,
            is_active=True,
        ).first()
        if not target_department:
            raise ValueError("Invalid department.")

    interview_status = "draft"
    interview_start = None
    interview_end = None
    if start_time or end_time:
        if not start_time or not end_time:
            raise ValueError("Both start and end time are required to schedule an interview.")
        from django.utils.dateparse import parse_datetime
        start_dt = parse_datetime(start_time)
        end_dt = parse_datetime(end_time)
        if start_dt is None or end_dt is None:
            raise ValueError("Invalid schedule time.")
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        scheduler = InterviewScheduler()
        scheduler.validate_interview_times(start_dt, end_dt)
        interview_start = start_dt
        interview_end = end_dt
        interview_status = "scheduled"

    question_count = _bounded_int(
        data.get("question_count"),
        default=10,
        minimum=3,
        maximum=30,
    )
    duration_minutes = _bounded_int(
        data.get("duration_minutes"),
        default=20,
        minimum=5,
        maximum=120,
    )

    with transaction.atomic():
        interview = MockInterview.objects.create(
            created_by=employee_id,
            created_by_name=_faculty_name(user),
            title=title,
            subject_code=subject_code,
            chapter=str(data.get("chapter", "") or "").strip(),
            document=document,
            difficulty=difficulty,
            interview_mode=interview_mode,
            question_count=question_count,
            duration_minutes=duration_minutes,
            language_mode=language_mode,
            target_skills=target_skills,
            target_batch=target_batch,
            target_section=target_section,
            target_department=target_department,
            start_time=interview_start,
            end_time=interview_end,
            status=interview_status,
        )

        auto_assigned = 0
        filters = {"is_active": True}
        if target_department:
            filters["department"] = target_department
        if target_batch:
            filters["batch"] = target_batch
        if target_section:
            filters["section"] = target_section

        students = (
            StudentDetails.objects.filter(**filters)
            .exclude(reg_no__isnull=True)
            .exclude(reg_no="")
            .values_list("reg_no", "name")
        )
        assignments = []
        seen_ids = set()
        for reg_no, student_name in students:
            sid = (reg_no or "").strip()
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            assignments.append(
                InterviewAssignment(
                    interview=interview,
                    student_employee_id=sid,
                    student_name=(student_name or "")[:500],
                )
            )

        if assignments:
            InterviewAssignment.objects.bulk_create(
                assignments,
                batch_size=500,
                ignore_conflicts=True,
            )
            auto_assigned = interview.assignments.count()

    return interview, auto_assigned


@login_required
@faculty_required
@require_POST
def create_interview_api(request):
    """API endpoint to create a mock interview."""
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    try:
        interview, auto_assigned = _create_interview(request.user, data)
    except SchedulerError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({
        "id": str(interview.id),
        "public_id": str(interview.public_id),
        "title": interview.title,
        "status": interview.status,
        "start_time": interview.start_time.isoformat() if interview.start_time else None,
        "end_time": interview.end_time.isoformat() if interview.end_time else None,
        "auto_assigned": auto_assigned,
        "detail_url": reverse("mock_interview:interview_detail_page", args=[interview.id]),
        "dashboard_url": reverse("mock_interview:faculty_dashboard"),
    })


@login_required
@faculty_required
@require_POST
def schedule_interview_api(request, interview_id):
    """Schedule an interview with start and end times."""
    interview = get_object_or_404(
        MockInterview,
        id=interview_id,
        created_by=_faculty_employee_id(request.user),
    )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    start_time = data.get("start_time")
    end_time = data.get("end_time")

    if not start_time or not end_time:
        return JsonResponse(
            {"error": "Start and end times are required."}, status=400
        )

    try:
        from django.utils.dateparse import parse_datetime

        start_dt = parse_datetime(start_time)
        end_dt = parse_datetime(end_time)

        if start_dt is None or end_dt is None:
            return JsonResponse({"error": "Invalid datetime format."}, status=400)

        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)

        scheduler = InterviewScheduler()
        scheduler.validate_interview_times(start_dt, end_dt)

        interview.start_time = start_dt
        interview.end_time = end_dt
        interview.status = "scheduled"
        interview.save(update_fields=("start_time", "end_time", "status", "updated_at"))

        return JsonResponse({
            "status": "scheduled",
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
        })
    except SchedulerError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@login_required
@faculty_required
@require_POST
def assign_interview_api(request, interview_id):
    """Assign students to an interview with department/batch/section validation."""
    interview = get_object_or_404(
        MockInterview,
        id=interview_id,
        created_by=_faculty_employee_id(request.user),
    )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    student_ids = data.get("student_ids", [])
    if not student_ids:
        return JsonResponse(
            {"error": "At least one student ID is required."}, status=400
        )

    results = []
    created = 0
    for raw_sid in student_ids:
        sid = str(raw_sid).strip()
        if not sid:
            continue

        # Step 1: Verify student exists
        student = StudentDetails.objects.filter(reg_no=sid).first()
        if not student:
            results.append({
                "student_id": sid,
                "status": "not_found",
                "message": "Student not found. Please enter a valid register number.",
            })
            continue

        # Step 2: Verify student is active
        if not student.is_active:
            results.append({
                "student_id": sid,
                "status": "inactive",
                "message": "This student is inactive and cannot be assigned.",
            })
            continue

        # Step 3: Verify student belongs to the interview's configured department
        if interview.target_department_id and student.department_id != interview.target_department_id:
            results.append({
                "student_id": sid,
                "status": "invalid",
                "message": "Cannot assign student. The selected student does not belong to the configured Department, Batch, or Section for this interview.",
            })
            continue

        # Step 4: Verify student belongs to the interview's configured batch
        if interview.target_batch and (student.batch or "") != interview.target_batch:
            results.append({
                "student_id": sid,
                "status": "invalid",
                "message": "Cannot assign student. The selected student does not belong to the configured Department, Batch, or Section for this interview.",
            })
            continue

        # Step 5: Verify student belongs to the interview's configured section
        if interview.target_section and (student.section or "") != interview.target_section:
            results.append({
                "student_id": sid,
                "status": "invalid",
                "message": "Cannot assign student. The selected student does not belong to the configured Department, Batch, or Section for this interview.",
            })
            continue

        # Step 6: Check for duplicate assignment (only after eligibility passes)
        existing = InterviewAssignment.objects.filter(
            interview=interview, student_employee_id=sid
        ).first()
        if existing:
            results.append({
                "student_id": sid,
                "status": "duplicate",
                "message": "This student has already been assigned to this interview.",
            })
            continue

        # Step 7: All validations passed — create assignment
        InterviewAssignment.objects.create(
            interview=interview,
            student_employee_id=sid,
            student_name=(student.name or "")[:500],
        )
        created += 1
        results.append({
            "student_id": sid,
            "status": "assigned",
            "message": "Assigned successfully.",
        })

    return JsonResponse({
        "assigned": created,
        "total": interview.assignments.count(),
        "results": results,
    })


@login_required
@faculty_required
@require_POST
def delete_interview_api(request, interview_id):
    """Delete a mock interview.

    Deletion is allowed only when the interview's scheduled end time has
    passed.  Student statuses (assigned, in_progress, completed, report
    generated) do NOT block deletion.

    All related records including sessions, questions, answers,
    evaluations, reports, and assignments are removed inside a single
    transaction.
    """
    employee_id = _faculty_employee_id(request.user)
    logger.info(
        "Delete interview request — user=%s interview_id=%s",
        employee_id, interview_id,
    )

    interview = get_object_or_404(
        MockInterview,
        id=interview_id,
        created_by=employee_id,
    )
    logger.info(
        "Authenticated faculty=%s interview_id=%s title=%s end_time=%s",
        employee_id, interview_id, interview.title, interview.end_time,
    )

    try:
        now = timezone.now()
        logger.info(
            "Schedule validation — end_time=%s current_time=%s",
            interview.end_time, now,
        )

        # ── Schedule check ──────────────────────────────────────────
        if interview.end_time is not None and now < interview.end_time:
            logger.warning(
                "Delete blocked — interview %s is still active (end=%s, now=%s)",
                interview_id, interview.end_time, now,
            )
            return JsonResponse({
                "error": (
                    "This interview is currently active and cannot be "
                    "deleted until the scheduled end time."
                ),
            }, status=409)

        # ── Perform deletion ────────────────────────────────────────
        with transaction.atomic():
            # Collect session IDs for logging before deletion.
            session_ids = list(
                interview.sessions.values_list("id", flat=True),
            )

            # Delete all InterviewSessions first.
            # This cascades:
            #   InterviewSession → InterviewQuestion (CASCADE)
            #       → StudentAnswer (CASCADE) → AnswerEvaluation (CASCADE)
            #   InterviewSession → InterviewReport (CASCADE)
            interview.sessions.all().delete()

            # Delete the MockInterview itself.
            # This cascades:
            #   MockInterview → InterviewAssignment (CASCADE)
            interview.delete()

            logger.info(
                "Interview deleted — user=%s interview_id=%s title=%s "
                "deleted_sessions=%d",
                employee_id, interview_id, interview.title,
                len(session_ids),
            )

        return JsonResponse({"status": "deleted"})

    except Exception:
        logger.exception(
            "Failed to delete interview %s for user %s",
            interview_id, employee_id,
        )
        return JsonResponse(
            {"error": "Unable to delete the interview. Please try again later."},
            status=500,
        )


@login_required
@faculty_required
def interview_detail_page(request, interview_id):
    """View interview details and assigned students."""
    interview = get_object_or_404(
        MockInterview,
        id=interview_id,
        created_by=_faculty_employee_id(request.user),
    )
    assignments = interview.assignments.all()
    scheduler = InterviewScheduler()
    status_info = scheduler.get_time_info(interview)

    return render(request, "mock_interview/interview_detail.html", {
        "interview": interview,
        "assignments": assignments,
        "status_info": status_info,
        "user": request.user,
    })


# ── AJAX Endpoints for Dependent Dropdowns ─────────────────────────


@login_required
@faculty_required
@require_GET
def get_batches_api(request):
    """Return distinct batches for a given department."""
    department_id = request.GET.get("department_id")
    try:
        hod_department = _hod_department_scope(request.user)
    except HODDepartmentError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if hod_department is not None:
        if department_id and str(department_id) != str(hod_department.pk):
            return JsonResponse({"error": "HOD access is limited to your mapped department."}, status=403)
        department_id = hod_department.pk
    if not department_id:
        return JsonResponse({"batches": []})
    batches = (
        StudentDetails.objects.filter(
            department_id=department_id, is_active=True
        )
        .values_list("batch", flat=True)
        .distinct()
        .order_by("batch")
    )
    return JsonResponse({
        "batches": [b for b in batches if b],
    })


@login_required
@faculty_required
@require_GET
def get_sections_api(request):
    """Return distinct sections for a given department and batch."""
    department_id = request.GET.get("department_id")
    batch = request.GET.get("batch", "")
    try:
        hod_department = _hod_department_scope(request.user)
    except HODDepartmentError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if hod_department is not None:
        if department_id and str(department_id) != str(hod_department.pk):
            return JsonResponse({"error": "HOD access is limited to your mapped department."}, status=403)
        department_id = hod_department.pk
    if not department_id or not batch:
        return JsonResponse({"sections": []})
    sections = (
        StudentDetails.objects.filter(
            department_id=department_id, batch=batch, is_active=True
        )
        .values_list("section", flat=True)
        .distinct()
        .order_by("section")
    )
    return JsonResponse({
        "sections": [s for s in sections if s],
    })


# ── Student Interview Endpoints ──────────────────────────────────────


@login_required
def student_interview_dashboard(request):
    """Student dashboard showing assigned interviews."""
    if not is_student_user(request.user):
        return JsonResponse({"error": "Student access required."}, status=403)

    employee_id = str(request.user.Employee_id).strip()
    assignments = InterviewAssignment.objects.filter(
        student_employee_id=employee_id,
    ).select_related("interview")

    scheduler = InterviewScheduler()
    interview_data = []
    for assignment in assignments:
        interview = assignment.interview
        can_start, message = scheduler.can_student_start(interview)
        interview_data.append({
            "assignment": assignment,
            "interview": interview,
            "can_start": can_start,
            "message": message,
            "status_info": scheduler.get_time_info(interview),
        })

    return render(request, "mock_interview/student_dashboard.html", {
        "interview_data": interview_data,
        "user": request.user,
    })


@login_required
@require_GET
def interview_status_api(request, interview_id):
    """Get current interview status for a student."""
    if not is_student_user(request.user):
        return JsonResponse({"error": "Student access required."}, status=403)

    interview = get_object_or_404(MockInterview, public_id=interview_id)
    employee_id = str(request.user.Employee_id).strip()

    assignment = InterviewAssignment.objects.filter(
        interview=interview,
        student_employee_id=employee_id,
    ).first()

    if not assignment:
        return JsonResponse(
            {"error": "You are not assigned to this interview."}, status=403
        )

    scheduler = InterviewScheduler()
    can_start, message = scheduler.can_student_start(interview)

    return JsonResponse({
        "can_start": can_start,
        "message": message,
        "status_info": scheduler.get_time_info(interview),
        "assignment_status": assignment.status,
    })


@login_required
def start_assigned_interview(request, interview_id):
    """Create an InterviewSession from an assigned MockInterview and redirect to device check."""
    if not is_student_user(request.user):
        return JsonResponse({"error": "Student access required."}, status=403)

    interview = get_object_or_404(MockInterview, public_id=interview_id)
    employee_id = str(request.user.Employee_id).strip()

    assignment = InterviewAssignment.objects.filter(
        interview=interview,
        student_employee_id=employee_id,
    ).first()

    if not assignment:
        return JsonResponse(
            {"error": "You are not assigned to this interview."}, status=403
        )

    scheduler = InterviewScheduler()
    can_start, message = scheduler.can_student_start(interview)
    if not can_start:
        return JsonResponse({"error": message}, status=403)

    existing_session = assignment.session
    if existing_session and existing_session.status in ("draft", "in_progress"):
        return redirect(
            "mock_interview:device_check", public_id=existing_session.public_id
        )

    difficulty_map = {
        "easy": "Beginner",
        "medium": "Intermediate",
        "hard": "Advanced",
    }
    mode_map = {
        "technical": "Technical",
        "hr": "HR",
        "behavioral": "Behavioural",
        "mixed": "Mixed",
    }

    session = InterviewSession.objects.create(
        student_employee_id=employee_id,
        student_name=str(getattr(request.user, "username", "") or "").strip()[:500],
        mock_interview=interview,
        interview_type="role",
        role=interview.title[:150],
        interview_round=mode_map.get(interview.interview_mode, "Mixed")[:100],
        difficulty=difficulty_map.get(interview.difficulty, "Intermediate")[:50],
        target_skills=interview.target_skills or [],
        language_mode=interview.language_mode,
        question_count=min(interview.question_count, 15),
        duration_minutes=min(interview.duration_minutes, 45),
    )

    assignment.session = session
    assignment.status = "in_progress"
    assignment.started_at = timezone.now()
    assignment.save(update_fields=("session", "status", "started_at"))

    return redirect("mock_interview:device_check", public_id=session.public_id)


@login_required
@faculty_required
def interview_performance_page(request, interview_id):
    """View student performance results for an assigned interview."""
    employee_id = _faculty_employee_id(request.user)
    interview = get_object_or_404(MockInterview, id=interview_id, created_by=employee_id)
    assignments = InterviewAssignment.objects.filter(
        interview=interview,
    ).select_related("session").order_by("-started_at")

    student_results = []
    for assignment in assignments:
        session = assignment.session
        session_status = session.status if session else None
        is_completed = assignment.status == "completed" or session_status in (
            "completed", "evaluating", "report_ready",
        )
        result = {
            "assignment": assignment,
            "student_id": assignment.student_employee_id,
            "student_name": assignment.student_name or assignment.student_employee_id,
            "status": "completed" if is_completed else assignment.status,
            "started_at": assignment.started_at,
            "completed_at": assignment.completed_at or (
                session.completed_at if session else None
            ),
            "score": None,
            "report_url": None,
        }
        if session:
            try:
                report = InterviewReport.objects.get(session=session)
                result["score"] = report.overall_score
                result["technical_score"] = report.technical_score
                result["communication_score"] = report.communication_score
                result["report_url"] = session.public_id
            except InterviewReport.DoesNotExist:
                if session.overall_score is not None:
                    result["score"] = session.overall_score
        student_results.append(result)

    scored = [r for r in student_results if r["score"] is not None]
    avg_score = (
        sum(float(r["score"]) for r in scored) / len(scored) if scored else None
    )

    return render(request, "mock_interview/interview_performance.html", {
        "interview": interview,
        "student_results": student_results,
        "avg_score": avg_score,
        "total_assigned": len(student_results),
        "total_completed": len(scored),
        "user": request.user,
    })


@login_required
@faculty_required
def student_report_detail(request, interview_id, session_public_id):
    """Faculty view of a specific student's interview report."""
    employee_id = _faculty_employee_id(request.user)
    interview = get_object_or_404(MockInterview, id=interview_id, created_by=employee_id)
    session = get_object_or_404(
        InterviewSession,
        public_id=session_public_id,
        mock_interview=interview,
    )
    assignment = InterviewAssignment.objects.filter(
        interview=interview,
        session=session,
    ).first()

    report_data = InterviewReport.objects.filter(session=session).first()
    if not report_data:
        return render(request, "mock_interview/faculty_report_detail.html", {
            "interview": interview,
            "session": session,
            "assignment": assignment,
            "report": None,
            "questions": [],
            "user": request.user,
        })

    evaluations = (
        session.questions.filter(answer__evaluation__isnull=False)
        .select_related("answer__evaluation")
        .order_by("sequence_number")
    )

    return render(request, "mock_interview/faculty_report_detail.html", {
        "interview": interview,
        "session": session,
        "assignment": assignment,
        "report": report_data,
        "questions": evaluations,
        "user": request.user,
    })


@login_required
@faculty_required
@require_GET
def faculty_report_pdf(request, interview_id, session_public_id):
    """Download a student's interview report PDF (faculty-only)."""
    employee_id = _faculty_employee_id(request.user)
    logger.info(
        "Faculty PDF download requested — user=%s role=faculty interview_id=%s session_public_id=%s",
        employee_id, interview_id, session_public_id,
    )
    interview = get_object_or_404(
        MockInterview,
        id=interview_id,
        created_by=employee_id,
    )
    session = get_object_or_404(
        InterviewSession,
        public_id=session_public_id,
        mock_interview=interview,
    )
    report_data = InterviewReport.objects.filter(session=session).first()
    if not report_data:
        logger.warning(
            "Faculty PDF download failed — report not found for session %s",
            session_public_id,
        )
        return JsonResponse(
            {"error": "Report not found for this session."}, status=404
        )
    if not report_data.pdf_file:
        logger.warning(
            "Faculty PDF download failed — PDF file missing for report %s",
            report_data.pk,
        )
        return JsonResponse(
            {"error": "PDF report is unavailable."}, status=404
        )
    logger.info(
        "Faculty PDF download success — user=%s session=%s file=%s",
        employee_id, session_public_id, report_data.pdf_file.name,
    )
    return FileResponse(
        report_data.pdf_file.open("rb"),
        content_type="application/pdf",
        as_attachment=True,
        filename=f"mock-interview-{session.public_id}.pdf",
    )


@login_required
@faculty_required
@require_GET
def runtime_status_api(request):
    """Check system readiness for interviews."""
    from mock_interview.ai.ollama_client import health as ollama_health

    status = {
        "ollama": ollama_health(),
        "rag": False,
    }

    try:
        from mock_interview.rag.vectorstore import QdrantVectorStore
        vs = QdrantVectorStore()
        status["rag"] = vs.health()
    except Exception:
        status["rag"] = False

    all_ok = all(status.values())
    return JsonResponse({"ready": all_ok, "components": status})

