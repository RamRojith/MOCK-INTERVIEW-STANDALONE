import uuid

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


class ResumeDocument(models.Model):
    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("parsed", "Parsed"),
        ("failed", "Failed"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    student_employee_id = models.CharField(max_length=225, db_index=True)
    student_name = models.CharField(max_length=500, blank=True)
    file = models.FileField(upload_to="mock_interview/resumes/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    extracted_text = models.TextField(blank=True)
    structured_profile = models.JSONField(default=dict, blank=True)
    information_graph = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="uploaded",
    )
    error_message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"
        ordering = ("-created_at",)

    def __str__(self):
        student = self.student_name or self.student_employee_id
        return f"{student} - {self.original_filename}"

class InterviewSession(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("planning", "Planning"),
        ("ready", "Ready"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("evaluating", "Evaluating"),
        ("report_ready", "Report Ready"),
        ("failed", "Failed"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    student_employee_id = models.CharField(max_length=225, db_index=True)
    student_name = models.CharField(max_length=500, blank=True)
    resume = models.ForeignKey(
        ResumeDocument,
        related_name="interview_sessions",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    mock_interview = models.ForeignKey(
        "MockInterview",
        related_name="sessions",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    interview_type = models.CharField(max_length=30, default="role")
    role = models.CharField(max_length=150)
    company_name = models.CharField(max_length=150, blank=True)
    job_description = models.TextField(blank=True)
    interview_round = models.CharField(max_length=100)
    difficulty = models.CharField(max_length=50)
    target_skills = models.JSONField(default=list, blank=True)
    language_mode = models.CharField(max_length=20, default="en")
    interviewer_voice = models.CharField(max_length=50, default="af_heart")
    question_count = models.PositiveIntegerField(default=10)
    duration_minutes = models.PositiveIntegerField(default=20)
    interview_state = models.JSONField(default=dict, blank=True)
    report_generation = models.BooleanField(default=False)
    consented_at = models.DateTimeField(null=True, blank=True)
    consent_version = models.CharField(max_length=20, blank=True)
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default="draft"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    recording = models.FileField(
        upload_to="recordings/",
        null=True,
        blank=True
    )
    overall_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    error_message = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'mock_interview'
        ordering = ("-created_at",)

    def __str__(self):
        student = self.student_name or self.student_employee_id
        return f"{student} - {self.role} ({self.created_at.strftime('%Y-%m-%d')})"


class InterviewQuestion(models.Model):
    session = models.ForeignKey(
        InterviewSession,
        related_name="questions",
        on_delete=models.CASCADE
    )
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    question_text = models.TextField()
    question_type = models.CharField(max_length=50)
    sequence_number = models.PositiveIntegerField()
    audio_file = models.FileField(
        upload_to="tts/",
        null=True,
        blank=True
    )
    source = models.CharField(max_length=40, default="general")
    selection_reason = models.TextField(blank=True)
    topic = models.CharField(max_length=150, blank=True)
    difficulty_level = models.PositiveSmallIntegerField(null=True, blank=True)
    rubric = models.JSONField(default=dict, blank=True)
    expected_concepts = models.JSONField(default=list, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    is_follow_up = models.BooleanField(default=False)
    skipped = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'mock_interview'
        ordering = ("sequence_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("session", "sequence_number"),
                name="mock_interview_unique_question_sequence",
            ),
        ]

    def __str__(self):
        return f"Q{self.sequence_number} for Session {self.session.id}"


class StudentAnswer(models.Model):
    question = models.OneToOneField(
        InterviewQuestion,
        related_name="answer",
        on_delete=models.CASCADE
    )
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    audio_file = models.FileField(upload_to="mock_interview/answers/%Y/%m/")
    original_transcript = models.TextField(blank=True)
    corrected_transcript = models.TextField(blank=True)
    transcript_changed = models.BooleanField(default=False)
    detected_language = models.CharField(max_length=20, blank=True)
    stt_confidence = models.FloatField(null=True, blank=True)
    word_timestamps = models.JSONField(default=list, blank=True)
    speech_metrics = models.JSONField(default=dict, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    audio_sha256 = models.CharField(max_length=64, blank=True)
    transcribed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    EVALUATION_STATUS_CHOICES = [
        ("none", "Not submitted"),
        ("pending", "Queued"),
        ("evaluating", "Evaluating"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]

    evaluation_status = models.CharField(
        max_length=20,
        choices=EVALUATION_STATUS_CHOICES,
        default="none",
    )
    evaluation_attempts = models.PositiveSmallIntegerField(default=0)
    evaluation_error = models.CharField(max_length=500, blank=True)

    class Meta:
        app_label = 'mock_interview'

    def __str__(self):
        return f"Answer for {self.question}"


class AnswerEvaluation(models.Model):
    answer = models.OneToOneField(
        StudentAnswer,
        related_name="evaluation",
        on_delete=models.CASCADE,
    )
    dimension_scores = models.JSONField(default=dict)
    total_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    evidence = models.JSONField(default=list, blank=True)
    strengths = models.JSONField(default=list, blank=True)
    missing_concepts = models.JSONField(default=list, blank=True)
    improvement_actions = models.JSONField(default=list, blank=True)
    improved_answer = models.TextField(blank=True)
    retrieved_chunks = models.JSONField(default=list, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"

    def __str__(self):
        return f"Evaluation for {self.answer}"


class InterviewReport(models.Model):
    session = models.OneToOneField(
        InterviewSession,
        related_name="report_data",
        on_delete=models.CASCADE,
    )
    overall_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    technical_score = models.DecimalField(max_digits=5, decimal_places=2)
    communication_score = models.DecimalField(max_digits=5, decimal_places=2)
    summary = models.TextField()
    strengths = models.JSONField(default=list, blank=True)
    improvement_areas = models.JSONField(default=list, blank=True)
    learning_plan = models.JSONField(default=list, blank=True)
    # SWOT coaching grid: {"strengths": [...], "weaknesses": [...],
    # "opportunities": [...], "threats": [...]}. Derived from the validated
    # evaluation data in ai/interview_engine.generate_swot_analysis.
    swot_analysis = models.JSONField(default=dict, blank=True)
    information_graph = models.JSONField(default=dict, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    pdf_file = models.FileField(
        upload_to="mock_interview/reports/%Y/%m/",
        null=True,
        blank=True,
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"

    def __str__(self):
        return f"Report for {self.session}"


class ATSResumeCheck(models.Model):
    """A standalone ATS-friendliness check of a student's resume.

    Produced by deterministic rules in services/ats.py. The generated
    ATS-friendly resume is stored as DOCX and PDF so the student can
    download and edit it. Scores are always computed in Python; the LLM
    (when enabled) only improves bullet wording and can never change scores.
    """

    STATUS_CHOICES = [
        ("analyzing", "Analyzing"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]
    REWRITE_SOURCES = [
        ("deterministic", "Deterministic"),
        ("llm", "Local LLM"),
    ]
    CAREER_LEVELS = [
        ("auto", "Auto-detect from resume"),
        ("fresher", "Fresher"),
        ("intern", "Intern"),
        ("experienced", "Experienced"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    student_employee_id = models.CharField(max_length=225, db_index=True)
    student_name = models.CharField(max_length=500, blank=True)
    source_resume = models.ForeignKey(
        ResumeDocument,
        related_name="ats_checks",
        on_delete=models.CASCADE,
    )
    job_description = models.TextField(blank=True)
    target_role = models.CharField(max_length=200, blank=True)
    career_level = models.CharField(
        max_length=20,
        choices=CAREER_LEVELS,
        default="auto",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="analyzing",
    )
    error_message = models.CharField(max_length=500, blank=True)
    score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    score_label = models.CharField(max_length=30, blank=True)
    analysis_mode = models.CharField(max_length=20, default="resume_only")
    dimension_scores = models.JSONField(default=dict, blank=True)
    issues = models.JSONField(default=list, blank=True)
    matched_keywords = models.JSONField(default=list, blank=True)
    missing_keywords = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    score_explanation = models.JSONField(default=list, blank=True)
    personal_info_flags = models.JSONField(default=list, blank=True)
    action_verb_analysis = models.JSONField(default=dict, blank=True)
    rewrite_warnings = models.JSONField(default=list, blank=True)
    rewrite_source = models.CharField(
        max_length=20,
        choices=REWRITE_SOURCES,
        default="deterministic",
    )
    rules_version = models.CharField(max_length=50, default="ats_rules_v2")
    generated_docx = models.FileField(
        upload_to="mock_interview/ats/%Y/%m/",
        null=True,
        blank=True,
    )
    generated_pdf = models.FileField(
        upload_to="mock_interview/ats/%Y/%m/",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"
        ordering = ("-created_at",)

    def __str__(self):
        student = self.student_name or self.student_employee_id
        return f"ATS check for {student} - {self.source_resume.original_filename}"


class UploadedDocument(models.Model):
    """Faculty-uploaded teaching documents for RAG-grounded interviews."""

    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("processing", "Processing"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    faculty_employee_id = models.CharField(max_length=225, db_index=True)
    faculty_name = models.CharField(max_length=500, blank=True)
    file = models.FileField(upload_to="mock_interview/documents/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    extracted_text = models.TextField(blank=True)
    subject_code = models.CharField(max_length=100, db_index=True)
    chapter = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="uploaded",
    )
    chunk_count = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.subject_code} - {self.original_filename}"


class DocumentChunk(models.Model):
    """Individual chunks of faculty-uploaded documents stored in Qdrant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        UploadedDocument,
        related_name="chunks",
        on_delete=models.CASCADE,
    )
    chunk_index = models.PositiveIntegerField()
    content = models.TextField()
    page_number = models.PositiveIntegerField(null=True, blank=True)
    embedding_id = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"
        ordering = ("chunk_index",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "chunk_index"),
                name="mock_interview_unique_document_chunk",
            ),
        ]

    def __str__(self):
        return f"Chunk {self.chunk_index} of {self.document}"


class MockInterview(models.Model):
    """Faculty-created interview assignment backed by uploaded documents."""

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("open", "Open"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    DIFFICULTY_CHOICES = [
        ("easy", "Easy"),
        ("medium", "Medium"),
        ("hard", "Hard"),
        ("mixed", "Mixed"),
    ]

    MODE_CHOICES = [
        ("technical", "Technical"),
        ("behavioural", "Behavioural"),
        ("mixed", "Mixed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.CharField(max_length=225, db_index=True)
    created_by_name = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    subject_code = models.CharField(max_length=100, db_index=True)
    chapter = models.CharField(max_length=255, blank=True)
    document = models.ForeignKey(
        UploadedDocument,
        related_name="interviews",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    difficulty = models.CharField(
        max_length=20,
        choices=DIFFICULTY_CHOICES,
        default="medium",
    )
    interview_mode = models.CharField(
        max_length=20,
        choices=MODE_CHOICES,
        default="technical",
    )
    question_count = models.PositiveIntegerField(default=10)
    duration_minutes = models.PositiveIntegerField(default=20)
    language_mode = models.CharField(max_length=20, default="en")
    target_skills = models.JSONField(default=list, blank=True)
    target_batch = models.CharField(max_length=100, blank=True)
    target_section = models.CharField(max_length=100, blank=True)
    target_department = models.ForeignKey(
        "user_accounts.Add_Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
    )
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "mock_interview"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.title} ({self.subject_code})"


class InterviewAssignment(models.Model):
    """Tracks which students are assigned to which interviews."""

    STATUS_CHOICES = [
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("expired", "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interview = models.ForeignKey(
        MockInterview,
        related_name="assignments",
        on_delete=models.CASCADE,
    )
    student_employee_id = models.CharField(max_length=225, db_index=True)
    student_name = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="assigned",
    )
    session = models.ForeignKey(
        InterviewSession,
        related_name="assignment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "mock_interview"
        constraints = [
            models.UniqueConstraint(
                fields=("interview", "student_employee_id"),
                name="mock_interview_unique_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.student_employee_id} -> {self.interview.title}"
