import io
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from mock_interview.models import ATSResumeCheck, ResumeDocument
from mock_interview.services.ats import analyze_resume, extract_jd_keywords


def _label_for(score):
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Strong"
    if score >= 50:
        return "Needs Improvement"
    return "Weak"
from mock_interview.services.ats_check_service import run_ats_check
from mock_interview.services.resume_generator import (
    build_docx,
    build_pdf,
    build_sections,
)


def make_pdf(text):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
    ]
    stream = b"BT /F1 24 Tf 100 700 Td (" + text.encode() + b") Tj ET"
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write((str(index) + " 0 obj ").encode() + obj + b" endobj\n")
    xref_pos = out.tell()
    out.write(("xref\n0 " + str(len(objects) + 1) + "\n").encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(("%010d 00000 n \n" % offset).encode())
    out.write(
        (
            "trailer\n<< /Size "
            + str(len(objects) + 1)
            + " /Root 1 0 R >>\nstartxref\n"
            + str(xref_pos)
            + "\n%%EOF"
        ).encode()
    )
    return out.getvalue()


GOOD_TEXT = (
    "Priya Ramesh\npriya.ramesh@example.com +91 98765 43210\n"
    "linkedin.com/in/priya-ramesh\nSUMMARY\nFinal-year B.Tech CSE student "
    "with strong Python and SQL skills.\nSKILLS\nPython, SQL, Java, Spring "
    "Boot, MySQL, Git, Docker\nEXPERIENCE\n- Developed a REST API for a "
    "hostel management system.\n- Reduced query time by 40% for 2,000+ "
    "users.\n- Automated CI builds, cutting release time by 30%.\n"
    "PROJECTS\n- Built a feedback classifier in Python and SQL.\n"
    "EDUCATION\nB.Tech CSE, RIT, 2026\nCERTIFICATIONS\nAWS Certified Cloud "
    "Practitioner\n"
)

GOOD_PROFILE = {
    "skills": ["Python", "SQL", "Java", "Spring Boot", "MySQL", "Git", "Docker"],
    "projects": ["Built a feedback classifier in Python and SQL."],
    "education": ["B.Tech CSE, RIT, 2026"],
    "experience": [
        "Developed a REST API for a hostel management system.",
        "Reduced query time by 40% for 2,000+ users.",
        "Automated CI builds, cutting release time by 30%.",
    ],
    "certifications": ["AWS Certified Cloud Practitioner"],
}


NO_EVIDENCE_TEXT = (
    "JUAN DELA CRUZ\njuan.delacruz@gmail.com | +63 917 123 4567\n"
    "123 Mabini Street, Quezon City\n\n"
    "PERSONAL PARTICULARS\nDate of Birth: 12 January 2002\nAge: 22\n"
    "Gender: Male\nCivil Status: Single\nReligion: Roman Catholic\n"
    "Passport No.: P1234567A\nSSS No.: 34-1234567-8\nTIN No.: 123-456-789-000\n\n"
    "EDUCATION DETAILS\nBS Information Technology, University of the "
    "Philippines, 2020 - 2024\n\n"
    "SKILLS\nAdobe Photoshop - 98%\nAdobe Illustrator - 85%\n"
    "Microsoft Word - 90%\nPython - 70%\n"
)
NO_EVIDENCE_PROFILE = {
    "skills": ["Adobe Photoshop", "Adobe Illustrator", "Microsoft Word", "Python"],
    "projects": [],
    "experience": [],
    "education": ["BS Information Technology, University of the Philippines, 2020-2024"],
    "certifications": [],
}


class AtsAnalysisTests(SimpleTestCase):
    def _resume(self, text=GOOD_TEXT, profile=None, name="Priya Ramesh"):
        return SimpleNamespace(
            public_id="resume-1",
            student_employee_id="22CS001",
            student_name=name,
            original_filename="resume.pdf",
            extracted_text=text,
            structured_profile=profile or GOOD_PROFILE,
        )

    def _no_evidence(self):
        return self._resume(NO_EVIDENCE_TEXT, NO_EVIDENCE_PROFILE, "Juan Dela Cruz")

    # ── nine-dimension architecture ──────────────────────────────────
    def test_dimensions_and_max_points(self):
        analysis = analyze_resume(self._resume())
        dims = analysis["dimensions"]
        self.assertEqual(
            set(dims),
            {
                "parseability", "structure", "job_relevance", "keyword_match",
                "experience_achievement", "quantification", "skills_alignment",
                "contact", "formatting",
            },
        )
        self.assertEqual(sum(d["max"] for d in dims.values()), 100)
        for d in dims.values():
            self.assertLessEqual(d["score"], d["max"])
            self.assertIn(
                d["status"],
                {"good", "needs_improvement", "critical", "not_evaluable"},
            )

    def test_good_resume_scores_high(self):
        analysis = analyze_resume(self._resume())
        self.assertGreaterEqual(analysis["score"], 70)
        self.assertEqual(analysis["dimensions"]["contact"]["status"], "good")
        self.assertEqual(
            analysis["dimensions"]["experience_achievement"]["status"], "good"
        )

    def test_analysis_is_deterministic(self):
        self.assertEqual(
            analyze_resume(self._resume()), analyze_resume(self._resume())
        )

    # ── absence is not weak evidence ─────────────────────────────────
    def test_no_bullets_makes_action_verbs_not_evaluable(self):
        analysis = self._no_evidence_analysis()
        self.assertEqual(analysis["action_verb_analysis"]["status"], "not_evaluable")
        messages = " ".join(i["message"] for i in analysis["issues"]).lower()
        self.assertNotIn("do not begin with a strong action verb", messages)
        self.assertIn(
            "no professional experience or achievement bullets detected",
            messages,
        )

    def test_no_bullets_makes_quantification_not_evaluable(self):
        analysis = self._no_evidence_analysis()
        self.assertEqual(
            analysis["dimensions"]["quantification"]["status"], "not_evaluable"
        )
        self.assertEqual(
            analysis["dimensions"]["job_relevance"]["status"], "not_evaluable"
        )
        self.assertEqual(
            analysis["dimensions"]["keyword_match"]["status"], "not_evaluable"
        )

    def _no_evidence_analysis(self):
        return analyze_resume(self._no_evidence())

    def test_personal_information_is_flagged_by_category_only(self):
        analysis = self._no_evidence_analysis()
        flags = analysis["personal_info_flags"]
        self.assertIn("Government identification numbers", flags)
        self.assertIn("Date of birth", flags)
        # The raw values must never be echoed back.
        blob = repr(analysis)
        self.assertNotIn("P1234567A", blob)
        self.assertNotIn("34-1234567-8", blob)

    def test_skill_percentages_are_not_quantification(self):
        analysis = self._no_evidence_analysis()
        joined = " ".join(
            f["message"] if isinstance(f, dict) else f
            for f in analysis["dimensions"]["skills_alignment"]["findings"]
        ).lower()
        self.assertIn("self-rated percentages", joined)

    def test_score_is_explained(self):
        analysis = analyze_resume(self._resume())
        self.assertTrue(analysis["score_explanation"])
        self.assertEqual(analysis["score_label"], _label_for(analysis["score"]))

    # ── job description mode ─────────────────────────────────────────
    def test_jd_keyword_match_reports_matched_and_missing(self):
        resume = self._resume()
        jd = (
            "Backend developer. Requirements: strong Python and Django, MySQL, "
            "Docker. Familiarity with Kubernetes and MongoDB."
        )
        analysis = analyze_resume(resume, jd)
        self.assertEqual(analysis["analysis_mode"], "resume_plus_jd")
        matched = {k.lower() for k in analysis["matched_keywords"]}
        missing = {k.lower() for k in analysis["missing_keywords"]}
        self.assertIn("python", matched)
        self.assertIn("docker", matched)
        self.assertTrue({"kubernetes", "mongodb"} & missing)
        self.assertLess(
            analysis["dimensions"]["keyword_match"]["score"],
            analysis["dimensions"]["keyword_match"]["max"],
        )

    def test_semantic_keyword_equivalence(self):
        resume = self._resume(
            GOOD_TEXT + "\nExperienced in Search Engine Optimization and REST APIs.\n",
            {**GOOD_PROFILE, "skills": GOOD_PROFILE["skills"]
             + ["Search Engine Optimization", "REST API"]},
        )
        analysis = analyze_resume(
            resume, "Requirements: SEO expertise and RESTful services."
        )
        matched = {k.lower() for k in analysis["matched_keywords"]}
        # "SEO" in the JD is satisfied by "Search Engine Optimization" in the CV,
        # and "RESTful" by "REST API".
        self.assertIn("seo", matched)
        self.assertIn("restful", matched)

    def test_no_jd_disables_job_dimensions(self):
        analysis = analyze_resume(self._resume(), "")
        self.assertFalse(analysis["jd_given"])
        self.assertEqual(analysis["analysis_mode"], "resume_only")
        self.assertEqual(
            analysis["dimensions"]["job_relevance"]["status"], "not_evaluable"
        )
        # not_evaluable dimensions are excluded from the denominator.
        self.assertGreater(analysis["score"], 0)

    def test_jd_keywords_bounded_and_shaped(self):
        jd = " ".join(f"skill{index}" for index in range(200))
        keywords = extract_jd_keywords(jd)
        self.assertLessEqual(len(keywords), 40)
        for entry in keywords:
            self.assertIn("term", entry)
            self.assertIn("weight", entry)

    def test_sample_resume_regression_findings(self):
        """Brief section 17: the no-evidence sample must be diagnosed honestly."""
        analysis = self._no_evidence_analysis()
        issue_text = " ".join(i["message"] for i in analysis["issues"]).lower()
        finding_text = " ".join(
            f for d in analysis["dimensions"].values() for f in d["findings"]
        ).lower()
        blob = issue_text + " " + finding_text

        # Present, honest findings.
        self.assertIn("no professional experience or achievement bullets", blob)
        self.assertIn("projects", blob)
        self.assertIn("self-rated percentages", blob)
        self.assertIn("personal information", issue_text)
        self.assertIn("no professional profile or portfolio link", blob)

        # Forbidden false positives.
        self.assertNotIn("do not begin with a strong action verb", blob)
        self.assertNotIn("poor quantification", blob)

        # Score is shown as-is, not inflated.
        self.assertLess(analysis["score"], 65)
        self.assertIn(analysis["score_label"], {"Weak", "Needs Improvement"})

    def test_issue_severities_use_controlled_vocabulary(self):
        analysis = self._no_evidence_analysis()
        for issue in analysis["issues"]:
            self.assertIn(
                issue["severity"], {"critical", "warning", "suggestion", "good"}
            )
        # CRITICAL is reserved for genuinely severe problems.
        criticals = [i for i in analysis["issues"] if i["severity"] == "critical"]
        self.assertLessEqual(len(criticals), 3)

    def test_recommendations_are_relevant_and_bounded(self):
        analysis = self._no_evidence_analysis()
        recs = analysis["recommendations"]
        self.assertLessEqual(len(recs), 6)
        self.assertTrue(recs)
        # No "start every bullet" advice when there are no bullets.
        self.assertFalse(
            any("start every bullet" in r.lower() for r in recs)
        )


class AtsResumeGeneratorTests(SimpleTestCase):
    def test_sections_are_ats_ordered(self):
        resume = SimpleNamespace(
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            student_name="Priya Ramesh",
        )
        sections = build_sections(resume)
        keys = [section["key"] for section in sections]
        self.assertEqual(keys[0], "contact")
        self.assertIn("skills", keys)
        expected_order = [
            key for key in
            ("summary", "skills", "projects", "experience", "education", "certifications")
            if key in keys
        ]
        self.assertEqual(
            [key for key in keys if key in expected_order],
            expected_order,
        )

    def test_generated_sections_preserve_uploaded_details(self):
        resume = SimpleNamespace(
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            student_name="Priya Ramesh",
        )
        sections = build_sections(resume)
        experience = next(s for s in sections if s["key"] == "experience")
        texts = [item["text"] for item in experience["items"]]
        self.assertEqual(
            texts,
            [
                "Developed a REST API for a hostel management system.",
                "Reduced query time by 40% for 2,000+ users.",
                "Automated CI builds, cutting release time by 30%.",
            ],
        )
        education = next(s for s in sections if s["key"] == "education")
        self.assertEqual(
            [item["text"] for item in education["items"]],
            ["B.Tech CSE, RIT, 2026"],
        )
        certifications = next(s for s in sections if s["key"] == "certifications")
        self.assertEqual(
            [item["text"] for item in certifications["items"]],
            ["AWS Certified Cloud Practitioner"],
        )

    def test_rewrite_map_is_never_applied_to_the_download(self):
        resume = SimpleNamespace(
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            student_name="Priya Ramesh",
        )
        sections = build_sections(resume)
        experience = next(s for s in sections if s["key"] == "experience")
        original = [item["text"] for item in experience["items"]]

        rewritten = build_sections(
            resume,
            rewrite_map={"experience": ["Optimized SQL queries, reducing runtime by 40%."]},
        )
        experience_after = next(s for s in rewritten if s["key"] == "experience")
        self.assertEqual(
            [item["text"] for item in experience_after["items"]],
            original,
        )

    def test_docx_magic_bytes(self):
        resume = SimpleNamespace(
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            student_name="Priya Ramesh",
        )
        data = build_docx(build_sections(resume))
        self.assertTrue(data[:2], b"PK")

    def test_pdf_magic_bytes(self):
        resume = SimpleNamespace(
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            student_name="Priya Ramesh",
        )
        data = build_pdf(build_sections(resume))
        self.assertTrue(data[:5], b"%PDF-")


def _student_request(employee_id="22CS001", username="Student One"):
    return SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_student=True,
        Employee_id=employee_id,
        username=username,
    )


def _attached_messages(request):
    request.session = SessionStore()
    messages = FallbackStorage(request)
    setattr(request, "_messages", messages)
    return messages


class AtsRunCheckTests(TestCase):
    def _resume(self, text=GOOD_TEXT):
        return ResumeDocument.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            file=SimpleUploadedFile(
                "resume.pdf",
                make_pdf(text),
                content_type="application/pdf",
            ),
            original_filename="resume.pdf",
            sha256="a" * 64,
            extracted_text=text,
            structured_profile=GOOD_PROFILE,
            status="parsed",
        )

    def _check(self):
        return ATSResumeCheck.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            source_resume=self._resume(),
            job_description="Python developer with MySQL and Docker.",
        )

    @patch(
        "mock_interview.ai.ats_rewriter.rewrite_resume_bullets",
        return_value=(
            {"experience": ["Optimized SQL queries, reducing runtime by 40%."]},
            [],
        ),
    )
    def test_run_ats_check_persists_files_and_score(self, _rewrite):
        check = self._check()
        run_ats_check(check)
        check.refresh_from_db()

        self.assertEqual(check.status, "ready")
        # The download is always the student's own wording, never an LLM pass.
        self.assertEqual(str(check.rewrite_source), "deterministic")
        self.assertEqual(check.rewrite_warnings, [])
        self.assertGreater(float(check.score), 0)
        self.assertEqual(check.analysis_mode, "resume_plus_jd")
        matched = {k.lower() for k in check.matched_keywords}
        self.assertIn("python", matched)
        self.assertTrue(check.score_label)
        self.assertTrue(check.generated_docx)
        self.assertTrue(check.generated_pdf)
        self.assertIn("keyword_match", check.dimension_scores)
        self.assertIn("score", check.dimension_scores["keyword_match"])

    def test_generated_docx_contains_exact_uploaded_details(self):
        check = self._check()
        run_ats_check(check)
        check.refresh_from_db()

        from docx import Document
        doc = Document(check.generated_docx.path)
        text = "\n".join(paragraph.text for paragraph in doc.paragraphs)

        for detail in GOOD_PROFILE["experience"] + GOOD_PROFILE["education"] \
                + GOOD_PROFILE["certifications"]:
            self.assertIn(detail, text)
        self.assertIn("Python, SQL, Java, Spring Boot, MySQL, Git, Docker", text)
        self.assertIn("Priya Ramesh", text)
        # The LLM rewrite output (patched above) must never leak into the file.
        self.assertNotIn("Optimized SQL queries", text)

    @patch(
        "mock_interview.ai.ats_rewriter.rewrite_resume_bullets",
        return_value=None,
    )
    def test_run_ats_check_falls_back_to_deterministic(self, _rewrite):
        check = self._check()
        run_ats_check(check)
        check.refresh_from_db()

        self.assertEqual(check.status, "ready")
        self.assertEqual(str(check.rewrite_source), "deterministic")
        self.assertTrue(check.generated_docx)

    def test_run_ats_check_with_empty_context_still_scores(self):
        resume = ResumeDocument.objects.create(
            student_employee_id="22CS001",
            student_name="",
            file=SimpleUploadedFile("resume.pdf", make_pdf(""), content_type="application/pdf"),
            original_filename="resume.pdf",
            sha256="b" * 64,
            extracted_text="Just a name.",
            structured_profile={},
            status="parsed",
        )
        check = ATSResumeCheck.objects.create(
            student_employee_id="22CS001",
            source_resume=resume,
        )
        run_ats_check(check)
        check.refresh_from_db()
        self.assertEqual(check.status, "ready")
        self.assertLess(float(check.score), 40)


class AtsCheckerViewTests(TestCase):
    def setUp(self):
        self.url = reverse("mock_interview:ats_checker")
        self.factory = RequestFactory()

    @patch(
        "mock_interview.ai.ats_rewriter.rewrite_resume_bullets",
        return_value=None,
    )
    @patch(
        "mock_interview.services.interview_service.normalize_resume",
        return_value=GOOD_PROFILE,
    )
    def test_post_creates_check_and_redirects(self, _normalize, _rewrite):
        uploaded = SimpleUploadedFile(
            "resume.pdf",
            make_pdf(GOOD_TEXT),
            content_type="application/pdf",
        )
        request = self.factory.post(
            self.url,
            {"resume": uploaded, "job_description": "Python developer with SQL."},
        )
        request.user = _student_request()
        _attached_messages(request)

        response = self._run_view(request)
        self.assertEqual(response.status_code, 302)
        check = ATSResumeCheck.objects.get()
        self.assertIn(str(check.public_id), response.url)
        self.assertEqual(check.status, "ready")
        self.assertEqual(str(check.rewrite_source), "deterministic")
        self.assertTrue(check.generated_docx)

    def _run_view(self, request):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.count.return_value = 0
        with patch("user_accounts.models.USER.objects.using", return_value=mock_qs):
            from mock_interview.views.ats import ats_checker
            return ats_checker(request)

    def test_post_invalid_file_returns_error_page(self):
        uploaded = SimpleUploadedFile("resume.pdf", b"not-a-pdf", content_type="application/pdf")
        request = self.factory.post(self.url, {"resume": uploaded})
        request.user = _student_request()

        response = self._run_view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ATSResumeCheck.objects.count(), 0)

    def test_get_renders_checker_page(self):
        request = self.factory.get(self.url)
        request.user = _student_request()
        response = self._run_view(request)
        self.assertEqual(response.status_code, 200)


class AtsOwnershipTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _setup_check(self):
        resume = ResumeDocument.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            file=SimpleUploadedFile("resume.pdf", make_pdf(GOOD_TEXT), content_type="application/pdf"),
            original_filename="resume.pdf",
            sha256="c" * 64,
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            status="parsed",
        )
        return ATSResumeCheck.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            source_resume=resume,
            status="ready",
            score=75,
        )

    @patch(
        "mock_interview.ai.ats_rewriter.rewrite_resume_bullets",
        return_value=None,
    )
    def test_other_student_cannot_access_check(self, _rewrite):
        check = self._setup_check()
        run_ats_check(check)
        request = self.factory.get(
            reverse("mock_interview:ats_check_detail", args=[check.public_id])
        )
        request.user = _student_request(employee_id="22CS002", username="Other")

        from mock_interview.views.ats import ats_check_detail
        with self.assertRaises(Exception):
            ats_check_detail(request, check.public_id)


class AtsDownloadTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _ready_check(self):
        resume = ResumeDocument.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            file=SimpleUploadedFile("resume.pdf", make_pdf(GOOD_TEXT), content_type="application/pdf"),
            original_filename="resume.pdf",
            sha256="d" * 64,
            extracted_text=GOOD_TEXT,
            structured_profile=GOOD_PROFILE,
            status="parsed",
        )
        check = ATSResumeCheck.objects.create(
            student_employee_id="22CS001",
            student_name="Priya Ramesh",
            source_resume=resume,
            status="ready",
            score=75,
        )
        with patch(
            "mock_interview.ai.ats_rewriter.rewrite_resume_bullets",
            return_value=None,
        ):
            run_ats_check(check)
        return check

    def test_download_docx_returns_file(self):
        check = self._ready_check()
        request = self.factory.get(
            reverse("mock_interview:ats_download_docx", args=[check.public_id])
        )
        request.user = _student_request()

        from mock_interview.views.ats import ats_download_docx
        response = ats_download_docx(request, check.public_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_download_pdf_returns_file(self):
        check = self._ready_check()
        request = self.factory.get(
            reverse("mock_interview:ats_download_pdf", args=[check.public_id])
        )
        request.user = _student_request()

        from mock_interview.views.ats import ats_download_pdf
        response = ats_download_pdf(request, check.public_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")