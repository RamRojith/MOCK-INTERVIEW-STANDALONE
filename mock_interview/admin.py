from django.contrib import admin
from .models import (
    ATSResumeCheck,
    AnswerEvaluation,
    DocumentChunk,
    InterviewAssignment,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    MockInterview,
    ResumeDocument,
    StudentAnswer,
    UploadedDocument,
)


@admin.register(ResumeDocument)
class ResumeDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "student_employee_id",
        "student_name",
        "original_filename",
        "status",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("student_employee_id", "student_name", "original_filename")
    readonly_fields = (
        "public_id",
        "sha256",
        "extracted_text",
        "structured_profile",
        "information_graph",
    )

@admin.register(InterviewSession)
class InterviewSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "student_employee_id",
        "student_name",
        "role",
        "status",
        "overall_score",
        "created_at",
    )
    list_filter = ("status", "role", "interview_round")
    search_fields = ("student_employee_id", "student_name", "role")
    readonly_fields = ("public_id", "consented_at", "started_at", "completed_at")

@admin.register(InterviewQuestion)
class InterviewQuestionAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'sequence_number', 'question_type')
    list_filter = ('question_type', 'is_follow_up')
    readonly_fields = ("public_id", "rubric", "expected_concepts")

@admin.register(StudentAnswer)
class StudentAnswerAdmin(admin.ModelAdmin):
    list_display = ('id', 'question', 'duration_seconds', 'submitted_at')
    readonly_fields = (
        "public_id",
        "audio_sha256",
        "original_transcript",
        "word_timestamps",
        "speech_metrics",
    )


@admin.register(AnswerEvaluation)
class AnswerEvaluationAdmin(admin.ModelAdmin):
    list_display = ("id", "answer", "total_score", "model_name", "created_at")
    readonly_fields = (
        "dimension_scores",
        "evidence",
        "strengths",
        "missing_concepts",
        "improvement_actions",
    )


@admin.register(InterviewReport)
class InterviewReportAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "overall_score", "generated_at")
    readonly_fields = (
        "overall_score",
        "technical_score",
        "communication_score",
        "summary",
        "strengths",
        "improvement_areas",
        "learning_plan",
        "swot_analysis",
        "information_graph",
    )


@admin.register(ATSResumeCheck)
class ATSResumeCheckAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "student_employee_id",
        "student_name",
        "source_resume",
        "target_role",
        "career_level",
        "score",
        "rewrite_source",
        "status",
        "created_at",
    )
    list_filter = (
        "status",
        "career_level",
        "rewrite_source",
        "created_at",
    )
    search_fields = ("student_employee_id", "student_name", "target_role")
    readonly_fields = (
        "public_id",
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
        "rules_version",
    )


@admin.register(UploadedDocument)
class UploadedDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "faculty_employee_id",
        "subject_code",
        "chapter",
        "original_filename",
        "status",
        "chunk_count",
        "created_at",
    )
    list_filter = ("status", "subject_code", "created_at")
    search_fields = (
        "faculty_employee_id",
        "subject_code",
        "original_filename",
    )
    readonly_fields = ("sha256", "extracted_text", "chunk_count")


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "chunk_index", "embedding_id")
    list_filter = ("document",)
    readonly_fields = ("content", "metadata")


@admin.register(MockInterview)
class MockInterviewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "created_by",
        "subject_code",
        "difficulty",
        "status",
        "start_time",
        "end_time",
        "created_at",
    )
    list_filter = ("status", "difficulty", "interview_mode", "subject_code")
    search_fields = ("title", "created_by", "subject_code")
    readonly_fields = ("public_id",)


@admin.register(InterviewAssignment)
class InterviewAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "interview",
        "student_employee_id",
        "status",
        "assigned_at",
    )
    list_filter = ("status",)
    search_fields = ("student_employee_id",)
