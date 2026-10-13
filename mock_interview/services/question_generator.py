import json
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from mock_interview.ai.ollama_client import LocalModelError, chat_json, configured_model
from mock_interview.ai.schemas import QUESTION_SCHEMA
from mock_interview.rag.retriever import DocumentRetriever

logger = logging.getLogger(__name__)


DEFAULT_TECHNICAL_RUBRIC = {
    "technical_correctness": 40,
    "completeness": 20,
    "relevance": 15,
    "structure": 15,
    "practical_example": 10,
}

DEFAULT_BEHAVIOURAL_RUBRIC = {
    "relevance": 20,
    "star_structure": 30,
    "specific_evidence": 20,
    "reflection": 15,
    "communication": 15,
}

FORBIDDEN_OUTPUT_PATTERNS = [
    "generate questions about",
    "ask questions related to",
    "create interview questions",
    "based on the uploaded document",
    "focus on",
    "use the following topics",
    "the uploaded document contains",
    "the document context",
    "here are some questions",
    "here is a question",
    "suggested questions",
    "interview questions about",
    "topics from the document",
]


def _is_forbidden_output(text: str) -> bool:
    """Return True if the text contains any phrase that should never appear as a question."""
    normalized = text.strip().lower()
    for phrase in FORBIDDEN_OUTPUT_PATTERNS:
        if phrase in normalized:
            logger.warning("Rejected question text containing forbidden phrase '%s': %.120s", phrase, text)
            return True
    return False


def _extract_document_topics(text: str, max_topics: int = 20) -> list[str]:
    """Extract likely headings and key topic lines from document text."""
    if not text or not text.strip():
        return []
    topics = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        is_heading = (
            re.match(r"^[A-Z][A-Za-z\s\-/:]{2,100}$", line)
            or re.match(r"^\d+[\.\)]\s+[A-Z]", line)
            or re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z]+){1,5}$", line)
            or re.match(r"^(Chapter|Section|Module|Unit|Topic|Lesson)\s+\d+", line, re.IGNORECASE)
            or (len(line) < 100 and line[0].isupper() and line[-1] not in ".!?" and not line.endswith(":"))
        )
        if is_heading and line not in topics:
            topics.append(line)
    if not topics:
        words = re.findall(r"[A-Z][a-z]{2,}(?:\s+[a-z]{2,}){0,3}", text)
        seen = set()
        for w in words:
            w = w.strip()
            if w and w not in seen and len(w) > 5:
                seen.add(w)
                topics.append(w)
    return topics[:max_topics]


class QuestionGenerator:
    """RAG-grounded question generation for faculty-created interviews."""

    def __init__(self):
        self._retriever = DocumentRetriever()

    def generate_question(
        self,
        interview,
        sequence_number: int,
        previous_questions: list[str] = None,
        previous_answer: str = "",
    ) -> dict:
        """Generate a question grounded in retrieved document chunks."""
        previous_questions = previous_questions or []

        document_topics = self._get_document_topics(interview)

        query = self._build_retrieval_query(interview, sequence_number, previous_answer, document_topics)
        chunks = []
        try:
            chunks = self._retriever.retrieve_for_question_generation(
                query=query,
                subject_code=interview.subject_code,
                chapter=interview.chapter,
                document_id=str(interview.document_id) if interview.document_id else "",
            )
        except Exception as exc:
            logger.warning("Chunk retrieval failed for interview %s: %s", interview.id, exc)

        context = self._retriever.build_context_string(chunks)

        question_data = self._call_llm(
            interview=interview,
            context=context,
            document_topics=document_topics,
            sequence_number=sequence_number,
            previous_questions=previous_questions,
            previous_answer=previous_answer,
        )

        question_data["source_chunk_ids"] = [
            c["id"] for c in chunks
        ]
        question_data["retrieved_context_preview"] = context[:500]

        if self._questions_are_similar(
            question_data["question_text"],
            previous_questions,
        ):
            question_data = self._fallback_question(
                interview, sequence_number, previous_questions, document_topics
            )

        return question_data

    def _get_document_topics(self, interview) -> list[str]:
        """Extract key topics from the linked document."""
        if not interview.document_id:
            return []
        try:
            doc = interview.document
            if doc and doc.extracted_text:
                return _extract_document_topics(doc.extracted_text)
        except interview.document.RelatedObjectDoesNotExist:
            logger.warning("Linked document not found for interview %s", interview.id)
        except Exception as exc:
            logger.warning("Could not extract document topics: %s", exc)
        return []

    def _build_retrieval_query(
        self, interview, sequence_number: int,
        previous_answer: str = "",
        document_topics: Optional[list[str]] = None,
    ) -> str:
        """Build a semantic retrieval query for chunk retrieval.

        Uses document topics and target concepts so the embedding model
        can find semantically relevant chunks within the document.
        """
        concepts = []
        if document_topics:
            idx = (sequence_number - 1) % len(document_topics)
            topic = document_topics[idx]
            concepts.append(topic)
        if interview.target_skills:
            concepts.extend(interview.target_skills[:2])
        if interview.chapter:
            concepts.append(interview.chapter)

        if not concepts:
            concepts.append(interview.subject_code)

        query_templates = [
            f"Explain the core concepts and principles of {' '.join(concepts[:3])}",
            f"What are the key topics, definitions, and important ideas in {' '.join(concepts[:3])}",
            f"Describe practical applications, examples, and real-world use cases of {' '.join(concepts[:2])}",
            f"What are the important techniques, methods, and approaches covered in {' '.join(concepts[:2])}",
            f"Explain how things work, common problems, and solutions related to {' '.join(concepts[:2])}",
        ]
        idx = (sequence_number - 1) % len(query_templates)
        query = query_templates[idx]

        if previous_answer:
            query = f"{query} related to: {previous_answer[:300]}"

        return query

    def _call_llm(
        self,
        interview,
        context: str,
        document_topics: Optional[list[str]] = None,
        sequence_number: int = 1,
        previous_questions: Optional[list[str]] = None,
        previous_answer: str = "",
    ) -> dict:
        """Call the LLM to generate a question from retrieved context."""
        previous_questions = previous_questions or []
        rubric = (
            DEFAULT_BEHAVIOURAL_RUBRIC
            if interview.interview_mode == "behavioural"
            else DEFAULT_TECHNICAL_RUBRIC
        )

        context_stripped = context[:4000].strip()
        has_context = len(context_stripped) > 50

        # When no document context was retrieved, do not call the LLM at all.
        # Small models tend to echo meta-instructions (e.g. "generate questions
        # about X") instead of producing an actual question.  Use the
        # deterministic fallback which is document-grounded and safe.
        if not has_context:
            return self._fallback_question(
                interview, sequence_number, previous_questions, document_topics,
            )

        system_prompt = (
            "You are a senior technical interviewer for college students. "
            "Ask exactly ONE concise interview question based ONLY on the document context below. "
            "Never output instructions, commentary, or explanations about what you are doing. "
            "Output only the question itself. "
            "Create the scoring rubric before the student answers. "
            "Rubric values are percentage weights that must total 100."
        )

        user_data = {
            "document_context": context_stripped,
            "difficulty": interview.difficulty,
            "interview_mode": interview.interview_mode,
            "question_number": sequence_number,
            "total_questions": interview.question_count,
            "questions_already_asked": previous_questions,
            "previous_answer_for_follow_up": previous_answer[:2000],
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
        ]

        try:
            result = chat_json(
                messages,
                QUESTION_SCHEMA,
                temperature=0.35,
                max_output_tokens=512,
            )
            question_data = self._parse_question(result, configured_model(), rubric)
            if _is_forbidden_output(question_data["question_text"]):
                return self._fallback_question(
                    interview, sequence_number, previous_questions, document_topics,
                )
            return question_data
        except LocalModelError:
            return self._fallback_question(
                interview, sequence_number, previous_questions, document_topics
            )

    def _parse_question(self, result: dict, model_name: str, rubric: dict) -> dict:
        """Parse and validate LLM output into a question dict."""
        question_text = str(result.get("question_text", "")).strip()
        if not question_text:
            raise LocalModelError("Question text was empty.")

        raw_rubric = result.get("rubric", rubric)
        normalized_rubric = self._normalize_rubric(raw_rubric, rubric)

        return {
            "question_text": question_text[:2000],
            "question_type": str(result.get("question_type", "technical"))[:50],
            "source": "rag_document",
            "selection_reason": str(result.get("selection_reason", ""))[:1000],
            "expected_concepts": self._clean_list(
                result.get("expected_concepts"), limit=12
            ),
            "rubric": normalized_rubric,
            "model_name": model_name,
        }

    def _normalize_rubric(self, rubric, fallback: dict) -> dict:
        """Normalize rubric to sum to 100."""
        if not isinstance(rubric, dict):
            return fallback.copy()
        cleaned = {}
        for key, value in rubric.items():
            try:
                score = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
            if key and score:
                cleaned[str(key)[:50]] = score
        total = sum(cleaned.values())
        if not cleaned or total <= 0:
            return fallback.copy()
        return {k: round(v * 100 / total, 2) for k, v in cleaned.items()}

    def _clean_list(self, value, limit=12) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:limit]:
            text = str(item).strip()
            if text and text not in result:
                result.append(text[:500])
        return result

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

    def _questions_are_similar(self, candidate: str, previous: list[str]) -> bool:
        normalized = self._normalize_text(candidate)
        if not normalized:
            return True
        return any(
            SequenceMatcher(None, normalized, self._normalize_text(p)).ratio() >= 0.82
            for p in previous
        )

    def _fallback_question(
        self, interview, sequence_number: int,
        previous_questions: Optional[list[str]] = None,
        document_topics: Optional[list[str]] = None,
    ) -> dict:
        """Deterministic fallback grounded in document topics."""
        previous_questions = previous_questions or []
        document_topics = document_topics or self._get_document_topics(interview)
        mode = interview.interview_mode

        if document_topics:
            idx = (sequence_number - 1) % len(document_topics)
            topic = document_topics[idx]
            for offset in range(len(document_topics)):
                candidate_topic = document_topics[(idx + offset) % len(document_topics)]
                question = f"Explain the concept of {candidate_topic} as described in the study material."
                if not self._questions_are_similar(question, previous_questions):
                    topic = candidate_topic
                    break
            texts = [
                f"Explain the concept of {topic} as described in the study material.",
                f"What is {topic} and why is it important?",
                f"Describe the key characteristics of {topic}.",
                f"How does {topic} work? Explain with an example.",
                f"What are the main components or steps involved in {topic}?",
            ]
            idx2 = (sequence_number - 1) % len(texts)
            question_text = texts[idx2]
            expected_concepts = [topic]
            source = "document_topic_fallback"
            selection_reason = f"Fallback question based on document topic: {topic}"
        else:
            question_text = (
                "Describe a key concept from the study material you have prepared for this topic."
            )
            expected_concepts = []
            source = "fallback_question_bank"
            selection_reason = "Generic fallback used when no document topics were found."

        rubric = (
            DEFAULT_BEHAVIOURAL_RUBRIC if mode == "behavioural" else DEFAULT_TECHNICAL_RUBRIC
        )

        return {
            "question_text": question_text,
            "question_type": mode,
            "source": source,
            "selection_reason": selection_reason,
            "expected_concepts": expected_concepts,
            "rubric": rubric,
            "model_name": "deterministic-fallback",
            "source_chunk_ids": [],
            "retrieved_context_preview": "",
        }
