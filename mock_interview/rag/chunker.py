import re
import logging
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class TextChunk:
    """A single chunk of text with metadata."""

    content: str
    chunk_index: int
    page_number: int | None = None
    start_char: int = 0
    end_char: int = 0
    metadata: dict = field(default_factory=dict)


class DocumentChunker:
    """Semantic chunking for educational documents."""

    def __init__(self):
        config = settings.RAG_CONFIG
        self._chunk_size = config["CHUNK_SIZE"]
        self._chunk_overlap = config["CHUNK_OVERLAP"]

    def chunk_text(
        self,
        text: str,
        document_id: str,
        subject_code: str = "",
        chapter: str = "",
        faculty_id: str = "",
    ) -> list[TextChunk]:
        """Split text into semantic chunks with overlap."""
        if not text or not text.strip():
            return []

        cleaned = self._clean_text(text)
        paragraphs = self._split_paragraphs(cleaned)

        chunks = []
        current_chunk = ""
        chunk_index = 0
        char_offset = 0

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            if len(current_chunk) + len(paragraph) + 1 <= self._chunk_size:
                current_chunk = (
                    f"{current_chunk}\n{paragraph}".strip()
                    if current_chunk
                    else paragraph
                )
            else:
                if current_chunk:
                    chunks.append(
                        self._make_chunk(
                            current_chunk,
                            chunk_index,
                            document_id,
                            subject_code,
                            chapter,
                            faculty_id,
                            char_offset,
                        )
                    )
                    chunk_index += 1
                    char_offset += len(current_chunk) + 1
                    overlap_text = self._get_overlap_tail(current_chunk)
                    current_chunk = (
                        f"{overlap_text}\n{paragraph}".strip()
                        if overlap_text
                        else paragraph
                    )
                else:
                    if len(paragraph) > self._chunk_size:
                        sub_chunks = self._force_split(
                            paragraph, chunk_index, document_id,
                            subject_code, chapter, faculty_id, char_offset
                        )
                        chunks.extend(sub_chunks)
                        chunk_index += len(sub_chunks)
                        char_offset += len(paragraph) + 1
                        current_chunk = ""
                    else:
                        current_chunk = paragraph

        if current_chunk:
            chunks.append(
                self._make_chunk(
                    current_chunk,
                    chunk_index,
                    document_id,
                    subject_code,
                    chapter,
                    faculty_id,
                    char_offset,
                )
            )

        return chunks

    def _clean_text(self, text: str) -> str:
        """Normalize whitespace and remove noise."""
        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _split_paragraphs(self, text: str) -> list[str]:
        """Split text into paragraphs."""
        return re.split(r"\n\s*\n", text)

    def _get_overlap_tail(self, text: str) -> str:
        """Get the last N characters for overlap."""
        if self._chunk_overlap <= 0:
            return ""
        words = text.split()
        overlap_words = []
        char_count = 0
        for word in reversed(words):
            char_count += len(word) + 1
            if char_count > self._chunk_overlap:
                break
            overlap_words.insert(0, word)
        return " ".join(overlap_words)

    def _make_chunk(
        self,
        content: str,
        index: int,
        document_id: str,
        subject_code: str,
        chapter: str,
        faculty_id: str,
        char_offset: int,
    ) -> TextChunk:
        return TextChunk(
            content=content,
            chunk_index=index,
            start_char=char_offset,
            end_char=char_offset + len(content),
            metadata={
                "document_id": document_id,
                "subject_code": subject_code,
                "chapter": chapter,
                "faculty_id": faculty_id,
            },
        )

    def _force_split(
        self,
        text: str,
        start_index: int,
        document_id: str,
        subject_code: str,
        chapter: str,
        faculty_id: str,
        char_offset: int,
    ) -> list[TextChunk]:
        """Force-split a long paragraph into fixed-size chunks."""
        chunks = []
        words = text.split()
        current = ""
        idx = start_index
        offset = char_offset

        for word in words:
            if len(current) + len(word) + 1 > self._chunk_size:
                if current:
                    chunks.append(
                        self._make_chunk(
                            current, idx, document_id,
                            subject_code, chapter, faculty_id, offset
                        )
                    )
                    idx += 1
                    offset += len(current) + 1
                    overlap = self._get_overlap_tail(current)
                    current = f"{overlap} {word}".strip() if overlap else word
                else:
                    chunks.append(
                        self._make_chunk(
                            word, idx, document_id,
                            subject_code, chapter, faculty_id, offset
                        )
                    )
                    idx += 1
                    offset += len(word) + 1
                    current = ""
            else:
                current = f"{current} {word}".strip() if current else word

        if current:
            chunks.append(
                self._make_chunk(
                    current, idx, document_id,
                    subject_code, chapter, faculty_id, offset
                )
            )

        return chunks
