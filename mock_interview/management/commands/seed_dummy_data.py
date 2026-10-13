"""Populate the standalone Mock Interview site with rich dummy data.

Everything here is deterministic and offline (no LLM / Whisper / TTS / Qdrant
required). It creates:

* login users (student / faculty / HOD / mentor / superuser)
* departments, faculty profiles and the student roster
* a parsed resume, an ATS resume check (with generated DOCX+PDF)
* a faculty-uploaded teaching document (with chunks)
* a faculty-created interview + assignment
* two fully completed interviews with per-question evaluations and reports

Run:  python manage.py seed_dummy_data
"""

import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from faculty_management.models import general_information
from mock_interview.models import (
    AnswerEvaluation,
    ATSResumeCheck,
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
from user_accounts.models import Add_Department, Department, Role, StudentDetails

PASSWORD = "demo@1234"

RESUME_TEXT = """\
ARJUN KUMAR
arjun.kumar@example.com | +91 98765 43210 | linkedin.com/in/arjunkumar
Computer Science and Engineering student.

SUMMARY
Final year Computer Science student with hands-on experience building Django
web applications, REST APIs and relational databases.

SKILLS
Python, Django, JavaScript, PostgreSQL, REST APIs, Git, Data Structures,
HTML, CSS, Problem Solving

PROJECTS
Campus Placement Portal - Built a Django application with student and faculty
logins, role based dashboards and interview scheduling. Designed the database
schema, wrote the REST endpoints and added automated tests.
Expense Tracker API - Developed a REST API using Django REST Framework with
token authentication and PostgreSQL storage.

EDUCATION
B.E. Computer Science and Engineering, Ramco Institute of Technology (pursuing)

CERTIFICATIONS
- Python for Everybody (Coursera)
"""

STUDENT_EMPLOYEES = [
    ("1001", "Arjun Kumar"),
    ("1002", "Divya Sharma"),
    ("1003", "Karthik Raja"),
    ("1004", "Priya Nair"),
    ("1005", "Vignesh M"),
    ("1006", "Sneha Reddy"),
]

QUESTION_ROWS = [
    {
        "text": "Tell me about yourself and one project you are proud of.",
        "type": "behavioural",
        "topic": "Introduction",
        "concepts": ["background", "skills", "project ownership"],
        "rubric": {"content": 40, "structure": 20, "communication": 25, "clarity": 15},
        "scores": {"content": 8, "structure": 7, "communication": 7, "clarity": 8},
    },
    {
        "text": "Explain the request-response cycle in a Django application.",
        "type": "technical",
        "topic": "Web frameworks",
        "concepts": ["url routing", "views", "middleware", "templates"],
        "rubric": {"content": 45, "structure": 20, "communication": 20, "clarity": 15},
        "scores": {"content": 8, "structure": 8, "communication": 7, "clarity": 8},
    },
    {
        "text": "How would you design a database schema for a placement portal?",
        "type": "technical",
        "topic": "Databases",
        "concepts": ["entities", "relationships", "normalization", "indexes"],
        "rubric": {"content": 45, "structure": 20, "communication": 20, "clarity": 15},
        "scores": {"content": 7, "structure": 8, "communication": 7, "clarity": 7},
    },
    {
        "text": "Describe a time you resolved a difficult bug under a deadline.",
        "type": "behavioural",
        "topic": "Problem solving",
        "concepts": ["situation", "action", "result"],
        "rubric": {"content": 35, "structure": 25, "communication": 25, "clarity": 15},
        "scores": {"content": 8, "structure": 7, "communication": 7, "clarity": 8},
    },
    {
        "text": "What is the difference between a list and a tuple in Python?",
        "type": "technical",
        "topic": "Python",
        "concepts": ["mutability", "performance", "use cases"],
        "rubric": {"content": 40, "structure": 20, "communication": 25, "clarity": 15},
        "scores": {"content": 9, "structure": 7, "communication": 7, "clarity": 8},
    },
]

SUMMARY_TEXT = (
    "The candidate demonstrated solid technical fundamentals and clear "
    "communication across the interview. Django and database concepts were "
    "explained accurately with relevant project examples. Structured answers "
    "using the STAR format would further strengthen behavioural responses."
)

SWOT = {
    "strengths": [
        "Explains technical concepts accurately with project examples.",
        "Comfortable discussing Django and relational database design.",
        "Speaks clearly and stays on topic.",
    ],
    "weaknesses": [
        "Some answers lacked a clear situation-action-result structure.",
        "Depth on system design trade-offs could be improved.",
    ],
    "opportunities": [
        "Practice system design questions to strengthen architecture answers.",
        "Add quantified impact to project descriptions.",
    ],
    "threats": [
        "Slow recovery if an unfamiliar topic appears without structure.",
        "Relying on a single project story can weaken variety.",
    ],
}


class Command(BaseCommand):
    help = "Seed the standalone Mock Interview site with dummy data."

    def handle(self, *args, **options):
        with transaction.atomic():
            self._seed_reference()
        self.stdout.write(self.style.SUCCESS("Dummy data seeded successfully."))
        self._print_logins()

    # ── reference data ────────────────────────────────────────────────────
    def _seed_reference(self):
        self.cse = Add_Department.objects.get_or_create(
            Department_code="CSE",
            defaults={
                "Department": "Computer Science and Engineering",
                "is_academic": True,
                "is_active": True,
            },
        )[0]
        Add_Department.objects.get_or_create(
            Department_code="ECE",
            defaults={
                "Department": "Electronics and Communication Engineering",
                "is_academic": True,
                "is_active": True,
            },
        )

        self.department = Department.objects.get_or_create(
            Department_code="CSE",
            defaults={"Department": "Computer Science and Engineering"},
        )[0]

        self.roles = {
            name: Role.objects.get_or_create(role=name)[0]
            for name in ("student", "faculty", "hod", "mentor")
        }

        User = get_user_model()

        self.admin = self._ensure_user(
            User, "9999", "System Admin", self.roles["faculty"],
            is_staff=True, is_superuser=True,
        )
        self.faculty = self._ensure_user(
            User, "2001", "Prof. Meena R", self.roles["faculty"]
        )
        self.hod = self._ensure_user(
            User, "3001", "Dr. Suresh Kumar", self.roles["hod"]
        )
        self.mentor = self._ensure_user(
            User, "4001", "Prof. Anil Varghese", self.roles["mentor"]
        )
        self.students = {}
        for employee_id, name in STUDENT_EMPLOYEES:
            self.students[employee_id] = self._ensure_user(
                User, employee_id, name, self.roles["student"], is_student=True
            )

        self.faculty_profile = self._ensure_faculty_profile(
            self.faculty, "Prof. Meena R"
        )
        self.hod_profile = self._ensure_faculty_profile(self.hod, "Dr. Suresh Kumar")
        self.mentor_profile = self._ensure_faculty_profile(
            self.mentor, "Prof. Anil Varghese"
        )

        for employee_id, name in STUDENT_EMPLOYEES:
            StudentDetails.objects.get_or_create(
                reg_no=employee_id,
                defaults={
                    "name": name,
                    "department": self.cse,
                    "batch": "2022-2026",
                    "year": "4",
                    "section": "A",
                    "semester": "8",
                    "email": f"{employee_id}@example.com",
                    "is_active": True,
                    "mentor": self.mentor_profile,
                    "ca": self.faculty_profile,
                },
            )

        self._seed_documents()
        self._seed_ats()
        self.interview = self._seed_mock_interview()
        self._seed_assignment()
        self._seed_sessions()

    def _ensure_user(
        self, User, employee_id, username, role, *,
        is_student=False, is_staff=False, is_superuser=False,
    ):
        user = User.objects.filter(Employee_id=employee_id).first()
        if user is None:
            user = User(Employee_id=employee_id)
        user.username = username
        user.role = role
        user.Department = self.department
        user.is_student = is_student
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        user.is_active = True
        user.set_password(PASSWORD)
        user.save()
        return user

    def _ensure_faculty_profile(self, user, name):
        profile, _ = general_information.objects.get_or_create(
            faculty_id=int(user.Employee_id),
            defaults={
                "name": name,
                "department": self.cse,
                "email": f"{user.Employee_id}@example.com",
            },
        )
        return profile

    # ── resume + teaching document ────────────────────────────────────────
    def _seed_documents(self):
        from mock_interview.services.interview_service import enrich_resume

        if not ResumeDocument.objects.filter(student_employee_id="1001").exists():
            resume = ResumeDocument.objects.create(
                student_employee_id="1001",
                student_name="Arjun Kumar",
                file=ContentFile(RESUME_TEXT.encode("utf-8"), name="arjun_kumar.txt"),
                original_filename="arjun_kumar.txt",
                mime_type="text/plain",
                file_size=len(RESUME_TEXT),
                sha256="0" * 64,
                extracted_text=RESUME_TEXT,
                status="parsed",
            )
            enrich_resume(resume)
        self.resume = ResumeDocument.objects.filter(
            student_employee_id="1001"
        ).first()

        if not UploadedDocument.objects.filter(subject_code="CS8591").exists():
            text = (
                "CS8591 Computer Networks - Unit 3. The transport layer provides "
                "end-to-end communication between processes using TCP and UDP. "
                "TCP guarantees reliable, ordered delivery with flow control, "
                "congestion control and connection setup via the three-way "
                "handshake. UDP is connectionless and offers low-latency "
                "delivery without reliability guarantees."
            )
            document = UploadedDocument.objects.create(
                faculty_employee_id="2001",
                faculty_name="Prof. Meena R",
                file=ContentFile(text.encode("utf-8"), name="cs8591_unit3.txt"),
                original_filename="cs8591_unit3.txt",
                mime_type="text/plain",
                file_size=len(text),
                sha256="1" * 64,
                extracted_text=text,
                subject_code="CS8591",
                chapter="Unit 3 - Transport Layer",
                status="ready",
                chunk_count=2,
            )
            for index, content in enumerate(
                [
                    "The transport layer provides end-to-end communication "
                    "between processes using TCP and UDP.",
                    "TCP guarantees reliable, ordered delivery with flow "
                    "control, congestion control and the three-way handshake, "
                    "while UDP is connectionless and low latency.",
                ]
            ):
                DocumentChunk.objects.create(
                    document=document,
                    chunk_index=index,
                    content=content,
                    embedding_id=f"{document.id}-{index}",
                    metadata={
                        "document_id": str(document.id),
                        "subject_code": "CS8591",
                        "chunk_index": index,
                    },
                )

    def _seed_ats(self):
        from mock_interview.services.ats_check_service import run_ats_check

        if ATSResumeCheck.objects.filter(student_employee_id="1001").exists():
            return
        check = ATSResumeCheck.objects.create(
            student_employee_id="1001",
            student_name="Arjun Kumar",
            source_resume=self.resume,
            job_description=(
                "We are hiring a Python Django developer with experience in "
                "REST APIs, PostgreSQL and Git."
            ),
            status="analyzing",
        )
        try:
            run_ats_check(check)
        except Exception as exc:  # pragma: no cover - best effort demo data
            self.stderr.write(f"ATS check generation skipped: {exc}")

    def _seed_mock_interview(self):
        interview = MockInterview.objects.filter(title__startswith="Campus").first()
        if interview:
            return interview
        now = timezone.now()
        return MockInterview.objects.create(
            created_by="2001",
            created_by_name="Prof. Meena R",
            title="Campus Placement Mock - Computer Networks",
            description=(
                "Mock interview grounded in the CS8591 Computer Networks "
                "teaching document."
            ),
            subject_code="CS8591",
            target_department=self.cse,
            status="open",
            difficulty="mixed",
            start_time=now - dt.timedelta(days=1),
            end_time=now + dt.timedelta(days=14),
            question_count=5,
            duration_minutes=20,
        )

    def _seed_assignment(self):
        InterviewAssignment.objects.get_or_create(
            interview=self.interview,
            student_employee_id="1001",
            defaults={"student_name": "Arjun Kumar", "status": "assigned"},
        )

    # ── completed sessions ────────────────────────────────────────────────
    def _seed_sessions(self):
        if InterviewSession.objects.filter(
            student_employee_id="1001", status="report_ready"
        ).exists():
            return
        self._build_completed_session("1001", "Arjun Kumar", "Software Engineer")
        self._build_completed_session("1002", "Divya Sharma", "Data Analyst")

    def _build_completed_session(self, employee_id, name, role):
        from mock_interview.services.interview_service import _generate_report_pdf
        from mock_interview.speech.tts import synthesize_question

        now = timezone.now()
        session = InterviewSession.objects.create(
            student_employee_id=employee_id,
            student_name=name,
            resume=self.resume if employee_id == "1001" else None,
            mock_interview=self.interview,
            interview_type="role",
            role=role,
            company_name="Ramco Systems",
            interview_round="Technical",
            difficulty="Intermediate",
            target_skills=["Python", "Django", "Databases"],
            question_count=len(QUESTION_ROWS),
            duration_minutes=20,
            status="report_ready",
            consented_at=now - dt.timedelta(hours=2),
            consent_version="1.0",
            started_at=now - dt.timedelta(hours=1, minutes=45),
            completed_at=now - dt.timedelta(hours=1),
            overall_score=Decimal("78.00"),
        )

        per_question_scores = []
        for sequence, row in enumerate(QUESTION_ROWS, start=1):
            question = InterviewQuestion.objects.create(
                session=session,
                question_text=row["text"],
                question_type=row["type"],
                sequence_number=sequence,
                source="document" if sequence % 2 else "general",
                selection_reason="Targets a core interview competency.",
                topic=row["topic"],
                difficulty_level=2,
                rubric=row["rubric"],
                expected_concepts=row["concepts"],
                model_name="dummy",
            )
            try:
                synthesize_question(question)
            except Exception:
                pass

            transcript = (
                f"{row['text']} {self._answer_body(row['topic'])}"
            )
            answer = StudentAnswer.objects.create(
                question=question,
                audio_file=ContentFile(b"RIFF0000WAVE", name=f"ans-{sequence}.wav"),
                original_transcript=transcript,
                corrected_transcript=transcript,
                detected_language="en",
                stt_confidence=0.95,
                word_timestamps=[],
                speech_metrics={"words_per_minute": 130, "filler_count": 1},
                duration_seconds=45,
                audio_sha256="2" * 64,
                transcribed_at=now,
                submitted_at=now,
                evaluation_status="ready",
            )
            total = self._weighted_total(row["scores"], row["rubric"])
            AnswerEvaluation.objects.create(
                answer=answer,
                dimension_scores={key: float(value) for key, value in row["scores"].items()},
                total_score=Decimal(str(total)),
                evidence=["Referenced a relevant project."],
                strengths=["Clear explanation of the core concept."],
                missing_concepts=[],
                improvement_actions=["Add a concrete example and outcome."],
                improved_answer="",
                retrieved_chunks=[],
                model_name="dummy",
            )
            per_question_scores.append(total)

        overall = round(sum(per_question_scores) / len(per_question_scores), 2)
        InterviewSession.objects.filter(pk=session.pk).update(
            overall_score=Decimal(str(overall))
        )
        session.refresh_from_db()

        report = InterviewReport.objects.create(
            session=session,
            overall_score=Decimal(str(overall)),
            technical_score=Decimal(str(min(100, overall + 4))),
            communication_score=Decimal(str(max(0, overall - 6))),
            summary=SUMMARY_TEXT,
            strengths=[
                "Strong grasp of Django and database fundamentals.",
                "Communicates technical ideas clearly.",
                "Grounded project explanations with real examples.",
            ],
            improvement_areas=[
                "Structure behavioural answers with the STAR method.",
                "Deepen system design and trade-off reasoning.",
            ],
            learning_plan=[
                "Practice two system design questions per week.",
                "Rehearse STAR answers for common behavioural prompts.",
                "Add measurable impact to each project bullet.",
            ],
            swot_analysis=SWOT,
            model_name="dummy",
        )
        try:
            _generate_report_pdf(report)
            report.save()
        except Exception:
            pass

    def _weighted_total(self, scores, rubric):
        # rubric weights sum to 100, dimension scores are out of 10.
        total = sum(scores[key] * float(rubric[key]) for key in rubric) / 10.0
        return round(min(100.0, max(0.0, total)), 2)

    @staticmethod
    def _answer_body(topic):
        answers = {
            "Introduction": (
                "I come from a Computer Science background and I am proud of a "
                "placement portal I built with Django using role based "
                "dashboards and a relational database."
            ),
            "Web frameworks": (
                "The request goes through URL routing to a view, the view runs "
                "the business logic, and a template renders the response, with "
                "middleware handling cross cutting concerns."
            ),
            "Databases": (
                "I would model the entities, define relationships with foreign "
                "keys, normalize to remove redundancy and add indexes on the "
                "columns used most often in queries."
            ),
            "Problem solving": (
                "A deployment failed right before a demo, so I checked the "
                "logs, reproduced the issue locally, fixed the configuration "
                "and shipped a hotfix in time for the presentation."
            ),
            "Python": (
                "Lists are mutable while tuples are immutable, so tuples can be "
                "used as dictionary keys and are slightly faster, while lists "
                "are better when the collection changes over time."
            ),
        }
        return answers.get(topic, "I explained the concept with a concrete example.")

    def _print_logins(self):
        self.stdout.write("")
        self.stdout.write("Demo logins (password: %s):" % PASSWORD)
        self.stdout.write("  superuser : 9999")
        self.stdout.write("  student   : 1001  (Arjun Kumar)")
        self.stdout.write("  student   : 1002  (Divya Sharma)")
        self.stdout.write("  faculty   : 2001  (Prof. Meena R)")
        self.stdout.write("  hod       : 3001  (Dr. Suresh Kumar)")
        self.stdout.write("  mentor    : 4001  (Prof. Anil Varghese)")
