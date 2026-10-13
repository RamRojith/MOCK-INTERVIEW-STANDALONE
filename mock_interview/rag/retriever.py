import logging
from typing import Optional

from django.conf import settings

from .vectorstore import QdrantVectorStore
from .embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class DocumentRetriever:
    """Retrieves relevant document chunks for question generation and evaluation."""

    def __init__(self):
        config = settings.RAG_CONFIG
        self._top_k = config["TOP_K_CHUNKS"]
        self._vectorstore = None
        self._embeddings = None

    def _ensure_services(self):
        """Lazily initialize Qdrant and embedding service."""
        if self._vectorstore is None:
            self._vectorstore = QdrantVectorStore()
        if self._embeddings is None:
            self._embeddings = EmbeddingService()
        return True

    def retrieve_for_question_generation(
        self,
        query: str,
        subject_code: str = "",
        chapter: str = "",
        faculty_id: str = "",
        document_id: str = "",
        top_k: Optional[int] = None,
    ) -> list[dict]:
        """Retrieve chunks relevant to generating interview questions."""
        metadata_filters = {}
        if subject_code:
            metadata_filters["subject_code"] = subject_code
        if chapter:
            metadata_filters["chapter"] = chapter
        if document_id:
            metadata_filters["document_id"] = document_id
        if faculty_id:
            metadata_filters["faculty_id"] = faculty_id

        try:
            self._ensure_services()
            query_vector = self._embeddings.embed_query(query)
            results = self._vectorstore.search(
                query_vector=query_vector,
                top_k=top_k or self._top_k,
                metadata_filters=metadata_filters,
            )
            return results
        except Exception as exc:
            logger.error("Failed to retrieve chunks for question generation: %s", exc)
            return []

    def retrieve_for_evaluation(
        self,
        question: str,
        expected_concepts: list[str],
        subject_code: str = "",
        chapter: str = "",
        document_id: str = "",
        top_k: Optional[int] = None,
    ) -> list[dict]:
        """Retrieve chunks relevant to evaluating a student answer."""
        combined_query = f"{question} {' '.join(expected_concepts)}"
        metadata_filters = {}
        if subject_code:
            metadata_filters["subject_code"] = subject_code
        if chapter:
            metadata_filters["chapter"] = chapter
        if document_id:
            metadata_filters["document_id"] = document_id

        try:
            self._ensure_services()
            query_vector = self._embeddings.embed_query(combined_query)
            results = self._vectorstore.search(
                query_vector=query_vector,
                top_k=top_k or self._top_k,
                metadata_filters=metadata_filters,
            )
            return results
        except Exception as exc:
            logger.error("Failed to retrieve chunks for evaluation: %s", exc)
            return []

    def build_context_string(self, chunks: list[dict]) -> str:
        """Build a context string from retrieved chunks for LLM prompts."""
        if not chunks:
            return ""
        parts = []
        for i, chunk in enumerate(chunks, 1):
            payload = chunk.get("payload", {})
            content = payload.get("content", "")
            if content:
                parts.append(f"[Chunk {i}] {content}")
        return "\n\n".join(parts)

    def health(self) -> bool:
        """Check if retrieval pipeline is functional."""
        try:
            self._ensure_services()
            return self._vectorstore.health() and self._embeddings.health()
        except Exception:
            return False
