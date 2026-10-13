"""Generate an ATS-friendly resume as editable DOCX and printable PDF.

The downloaded resume is always a faithful, deterministic rebuild of the
student's own uploaded resume:

* Every detail is carried over verbatim from the extracted text and re-sorted
  under standard ATS-friendly headings (Contact, Objective/Summary, Skills,
  Projects, Experience/Internships, Education, Certifications, ...).
* Nothing is invented, rephrased or dropped. Bullets are only re-indented --
  the wording is the student's own.
* LLM bullet rewrites never touch the download, so the file is always a
  true copy of what the student uploaded, in the clean single-column layout
  described in the resume format guide.

The one deliberate exception: "Personal particulars" / "References" blocks
(date of birth, gender, IDs, ...) are excluded because they hurt ATS parsing,
exactly as the checker's own recommendations advise.
"""

import io
import re

from django.core.files.base import ContentFile

from mock_interview.services.ats import (
    _BULLET_RE,
    _EMAIL_RE,
    _GITHUB_RE,
    _LINKEDIN_RE,
    _PHONE_RE,
    _detect_sections,
    _lines,
    _normalize_heading,
)

# Section order matches the standard ATS-friendly layout. "experience" also
# absorbs the "internships" content into one Experience / Internships block.
SECTION_ORDER = (
    ("summary", "SUMMARY"),
    ("skills", "SKILLS"),
    ("projects", "PROJECTS"),
    ("experience", "EXPERIENCE / INTERNSHIPS"),
    ("education", "EDUCATION"),
    ("certifications", "CERTIFICATIONS"),
)

EXTRA_SECTIONS = (
    ("achievements", "ACHIEVEMENTS"),
    ("activities", "ACTIVITIES"),
    ("coursework", "COURSEWORK"),
    ("publications", "PUBLICATIONS"),
    ("languages", "LANGUAGES"),
    ("interests", "INTERESTS"),
)

# Canonical sections deliberately excluded from the ATS-friendly rebuild.
_SKIPPED_SECTIONS = frozenset(("personal", "references"))

_OBJECTIVE_HEADINGS = frozenset(
    (
        "objective", "career objective", "professional objective",
        "job objective", "objective statement",
    )
)

_CITY_RE = re.compile(
    r"\b[A-Z][A-Za-z]+(?: [A-Za-z]+)?,\s*(?:[A-Z]{2}|[A-Z][A-Za-z]+)\b"
)


# ── small helpers ─────────────────────────────────────────────────────────
def _profile(profile):
    return profile if isinstance(profile, dict) else {}


def _clean_lines(lines):
    """Trim, drop empties/separators and remove exact duplicates in order."""
    seen = set()
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"[-=_*#~•·…]{3,}", line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _strip_marker(line):
    removed = _BULLET_RE.sub("", line).strip()
    return removed or line


def _item(line, is_bullet=None):
    marked = bool(_BULLET_RE.match(line))
    kind = "bullet" if (is_bullet if is_bullet is not None else marked) else "plain"
    return {"text": _strip_marker(line), "kind": kind}


def _profile_lines(profile, key):
    out = []
    for item in profile.get(key) or []:
        out.extend(_lines(str(item)))
    return out


def _detect_name(resume, lines):
    name = (getattr(resume, "student_name", "") or "").strip()
    if name:
        return name
    for line in lines[:6]:
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        words = line.split()
        if 1 <= len(words) <= 5 and line[:1].isalpha():
            return line.rstrip(".,;") or "Your Name"
    return "Your Name"


def _detect_location(lines):
    for line in lines[:15]:
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        match = _CITY_RE.search(line)
        if match and any(char.islower() for char in match.group(0)):
            return match.group(0).strip().strip(":,")
    return ""


def _build_header_from(name, text, lines):
    """Build the header line: location | phone | email | linkedin | github."""
    head = "\n".join(lines[:10])
    fields = []
    location = _detect_location(lines)
    if location:
        fields.append(location)
    phone = _PHONE_RE.search(head) or _PHONE_RE.search(text)
    if phone:
        fields.append(phone.group(0).strip())
    email = _EMAIL_RE.search(text)
    if email:
        fields.append(email.group(0).strip())
    linkedin = _LINKEDIN_RE.search(text)
    if linkedin:
        fields.append(linkedin.group(0).strip())
    github = _GITHUB_RE.search(text)
    if github:
        fields.append(github.group(0).strip())
    return name, "  |  ".join(fields)


def build_header(resume, lines):
    """Return (name, contact_line) matching the sample layout's header block."""
    text = str(getattr(resume, "extracted_text", "") or "")
    return _build_header_from(_detect_name(resume, lines), text, lines)


def extract_contact(text):
    """Backward-compatible summarised contact line (phone | email | link)."""
    lines = _lines(text)
    name = ""
    for line in lines[:6]:
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        words = line.split()
        if 1 <= len(words) <= 5 and line[:1].isalpha():
            name = line.rstrip(".,;")
            break
    _, contact = _build_header_from(name, text, lines)
    return contact


_SENSITIVE_SUMMARY_RE = re.compile(
    r"\b(date of birth|d\.?o\.?b|passport|aadhaar|aadhar|marital status|"
    r"gender|father'?s name|mother'?s name|nationality|religion|"
    r"\bage\b\s*[:\-]?\s*\d)\b",
    re.IGNORECASE,
)


def first_summary(text):
    """First non-heading paragraph used as a summary fallback.

    Skips contact lines and personal-information lines so the generated
    resume never surfaces sensitive details.
    """
    lines = [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip()
    ]
    for line in lines[:10]:
        if len(line) < 60:
            continue
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        if _SENSITIVE_SUMMARY_RE.search(line):
            continue
        return line[:600]
    sentences = re.split(r"(?<=[.!?])\s+", text or "")
    candidate = " ".join(sentences[:2])[:600]
    if _SENSITIVE_SUMMARY_RE.search(candidate):
        return ""
    return candidate


def _fallback_summary(text):
    summary = first_summary(text)
    return [summary] if summary else []


def _has_objective_heading(lines):
    for line in lines:
        if len(line) > 46:
            continue
        norm = _normalize_heading(line)
        if norm in _OBJECTIVE_HEADINGS:
            return True
    return False


def build_sections(resume, rewrite_map=None):
    """Return ordered [{key, title, items}] sections for the ATS layout.

    ``rewrite_map`` is accepted only for API compatibility and is deliberately
    NOT applied: the download must always reproduce the student's own
    uploaded details exactly.
    """
    text = str(getattr(resume, "extracted_text", "") or "")
    profile = _profile(getattr(resume, "structured_profile", {}))
    lines = _lines(text)
    present, spans = _detect_sections(lines)

    name, contact = build_header(resume, lines)
    sections = [{
        "key": "contact",
        "is_contact": True,
        "name": name,
        "contact": contact,
    }]

    # Objective / Summary.
    summary_lines = spans.get("summary") or _fallback_summary(text)
    if summary_lines:
        title = "OBJECTIVE" if _has_objective_heading(lines) else "SUMMARY"
        sections.append({
            "key": "summary",
            "title": title,
            "items": [_item(line, is_bullet=False) for line in _clean_lines(summary_lines)],
        })

    # Skills.
    skills = spans.get("skills") or _profile_lines(profile, "skills")
    if skills:
        sections.append({
            "key": "skills",
            "title": "SKILLS",
            "items": [_item(line, is_bullet=True) for line in _clean_lines(skills)],
        })

    # Projects.
    projects = spans.get("projects") or _profile_lines(profile, "projects")
    if projects:
        sections.append({
            "key": "projects",
            "title": "PROJECTS",
            "items": [_item(line) for line in _clean_lines(projects)],
        })

    # Experience + Internships merged into one block.
    exp_lines = list(spans.get("experience", []))
    intern_lines = list(spans.get("internships", []))
    if not exp_lines and not intern_lines:
        exp_lines = _profile_lines(profile, "experience")
    combined = _clean_lines(exp_lines + intern_lines)
    if combined:
        if "experience" in present and "internships" in present:
            title = "EXPERIENCE / INTERNSHIPS"
        elif "internships" in present:
            title = "INTERNSHIPS"
        else:
            title = "EXPERIENCE"
        sections.append({
            "key": "experience",
            "title": title,
            "items": [_item(line) for line in combined],
        })

    # Education.
    education = spans.get("education") or _profile_lines(profile, "education")
    if education:
        sections.append({
            "key": "education",
            "title": "EDUCATION",
            "items": [_item(line, is_bullet=True) for line in _clean_lines(education)],
        })

    # Certifications.
    certifications = spans.get("certifications") or _profile_lines(
        profile, "certifications"
    )
    if certifications:
        sections.append({
            "key": "certifications",
            "title": "CERTIFICATIONS",
            "items": [_item(line, is_bullet=True) for line in _clean_lines(certifications)],
        })

    # Remaining labelled sections are carried over verbatim so nothing from
    # the uploaded resume is lost (achievements, activities, coursework,
    # publications, languages, interests).
    for key, title in EXTRA_SECTIONS:
        content = _clean_lines(spans.get(key, []))
        if content:
            sections.append({
                "key": key,
                "title": title,
                "items": [_item(line) for line in content],
            })
    return sections


def _item_text(item):
    return item["text"] if isinstance(item, dict) else str(item)


def _item_kind(item):
    return item.get("kind") if isinstance(item, dict) else "bullet"


def build_docx(sections):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    document = Document()
    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)

    for section in sections:
        if section.get("is_contact"):
            name_paragraph = document.add_paragraph()
            name_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            name_run = name_paragraph.add_run(section.get("name") or "Resume")
            name_run.bold = True
            name_run.font.size = Pt(16)
            contact = section.get("contact") or ""
            if contact:
                contact_paragraph = document.add_paragraph()
                contact_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                contact_run = contact_paragraph.add_run(contact)
                contact_run.font.size = Pt(10)
                contact_run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            continue
        heading = document.add_heading(section["title"], level=2)
        for run in heading.runs:
            run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        for item in section.get("items", []):
            item_text = _item_text(item)
            if _item_kind(item) == "bullet":
                bullet = document.add_paragraph(style="List Bullet")
                bullet.add_run(item_text)
            else:
                paragraph = document.add_paragraph()
                paragraph.add_run(item_text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def build_pdf(sections):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
    )

    def escape(value):
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    stream = io.BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        topMargin=0.6 * inch,
        bottomMargin=0.7 * inch,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
    )
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle(
        "ATSName", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=16, leading=19, alignment=TA_CENTER,
        textColor=colors.HexColor("#1a1a1a"), spaceAfter=1,
    )
    contact_style = ParagraphStyle(
        "ATSContact", parent=styles["Normal"], fontName="Helvetica",
        fontSize=9.5, leading=13, alignment=TA_CENTER,
        textColor=colors.HexColor("#333333"), spaceAfter=5,
    )
    heading_style = ParagraphStyle(
        "ATSHeading", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=11.5, leading=14, textColor=colors.HexColor("#1F3A5F"),
        spaceBefore=9, spaceAfter=3,
    )
    body_style = ParagraphStyle(
        "ATSBody", parent=styles["Normal"], fontName="Helvetica",
        fontSize=9.5, leading=13, spaceAfter=2,
    )
    bullet_style = ParagraphStyle(
        "ATSBullet", parent=body_style, leftIndent=14, spaceAfter=1.5,
    )

    story = []
    for section in sections:
        if section.get("is_contact"):
            story.append(Paragraph(
                escape(section.get("name") or "Resume"), name_style
            ))
            if section.get("contact"):
                story.append(Paragraph(escape(section["contact"]), contact_style))
            story.append(HRFlowable(
                width="100%", thickness=1.1,
                color=colors.HexColor("#1F3A5F"), spaceAfter=2,
            ))
            continue
        story.append(Paragraph(escape(section["title"]), heading_style))
        for item in section.get("items", []):
            item_text = escape(_item_text(item))
            if _item_kind(item) == "bullet":
                story.append(Paragraph(f"&bull;&nbsp;{item_text}", bullet_style))
            else:
                story.append(Paragraph(item_text, body_style))
    doc.build(story)
    return stream.getvalue()


class ResumeGenerationError(RuntimeError):
    pass


def _safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "resume")).strip("_")
    return (cleaned or "resume")[:80]


def generate_resume_files(ats_check, rewrite_map=None):
    """Build DOCX + PDF from an ATSResumeCheck and save them on the model.

    ``rewrite_map`` is retained for API compatibility only; the generated
    files always reproduce the uploaded resume's own details verbatim.
    """
    try:
        sections = build_sections(ats_check.source_resume)
        docx_bytes = build_docx(sections)
        pdf_bytes = build_pdf(sections)
    except ImportError as exc:
        raise ResumeGenerationError(
            "Resume generation requires python-docx and reportlab."
        ) from exc
    base = (
        f"ats_resume_{_safe_filename(ats_check.student_name)}"
        f"_{ats_check.public_id.hex[:8]}"
    )
    docx_name = f"{base}.docx"
    pdf_name = f"{base}.pdf"
    try:
        ats_check.generated_docx.save(docx_name, ContentFile(docx_bytes), save=False)
        ats_check.generated_pdf.save(pdf_name, ContentFile(pdf_bytes), save=False)
    except Exception as exc:
        raise ResumeGenerationError(str(exc)[:200]) from exc
    return ats_check