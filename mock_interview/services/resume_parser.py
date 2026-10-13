import hashlib
import io
import re
from pathlib import Path

from django.conf import settings
from pypdf import PdfReader


class ResumeValidationError(ValueError):
    pass


def _normalized_filename(name):
    return Path(name or "resume").name[:255]


def validate_resume(uploaded_file):
    max_bytes = int(
        settings.MOCK_INTERVIEW.get("MAX_RESUME_BYTES", 5 * 1024 * 1024)
    )
    filename = _normalized_filename(uploaded_file.name)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ResumeValidationError("Only PDF and DOCX resumes are supported.")
    if uploaded_file.size <= 0 or uploaded_file.size > max_bytes:
        raise ResumeValidationError("Resume must be between 1 byte and 5 MB.")

    header = uploaded_file.read(8)
    uploaded_file.seek(0)
    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise ResumeValidationError("The uploaded file is not a valid PDF.")
    if suffix == ".docx" and not header.startswith(b"PK"):
        raise ResumeValidationError("The uploaded file is not a valid DOCX file.")
    return filename


def file_sha256(uploaded_file):
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def _extract_pdf(raw):
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ResumeValidationError("Password-protected PDFs are not supported.")
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(raw):
    try:
        from docx import Document
    except ImportError as exc:
        raise ResumeValidationError(
            "DOCX parsing requires the open-source python-docx package."
        ) from exc
    document = Document(io.BytesIO(raw))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def extract_resume_text(uploaded_file):
    filename = _normalized_filename(uploaded_file.name)
    raw = uploaded_file.read()
    uploaded_file.seek(0)
    if filename.lower().endswith(".pdf"):
        text = _extract_pdf(raw)
    else:
        text = _extract_docx(raw)
    text = re.sub(r"\x00", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 40:
        raise ResumeValidationError(
            "The resume contains too little extractable text."
        )
    return text[:50000]
