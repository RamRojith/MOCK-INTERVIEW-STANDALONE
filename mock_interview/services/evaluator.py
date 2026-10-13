import json
import logging
import re
from decimal import Decimal

from mock_interview.ai.ollama_client import LocalModelError, chat_json, configured_model
from mock_interview.ai.schemas import LIVE_EVALUATION_SCHEMA
from mock_interview.rag.retriever import DocumentRetriever

logger = logging.getLogger(__name__)

EVALUATION_CALIBRATION_GUIDE = (
    "Use strict score bands. 0-3 means missing, irrelevant, or unsafe to score. "
    "4-5 means vague or mostly generic with little evidence. 6-7 means partially "
    "correct but missing depth, expected concepts, or measurable outcome. 8-9 "
    "means specific, relevant, technically clear, demonstrates understanding of "
    "document concepts using semantic understanding (exact wording from the "
    "document is not required — paraphrasing and equivalent explanations count), "
    "and supported by transcript evidence. 10 means exceptional and complete — "
    "shows deep understanding of document concepts with correct additional "
    "knowledge that enhances the answer. Do not give scores above 6 for answers "
    "that mainly say a project was good, useful, or used a model without "
    "explaining the problem, implementation, and outcome."
)

VAGUE_PHRASES = (
    "many things",
    "it was good",
    "project was good",
    "very useful",
    "helps users",
    "used database and model",
    "gives output",
    "learned many things",
)

NON_ANSWER_PHRASES = (
    "i don't know",
    "i dont know",
    "don't know",
    "dont know",
    "didn't know",
    "didnt know",
    "i don't remember",
    "i dont remember",
    "i can't answer",
    "i cant answer",
    "i cannot answer",
    "i don't understand",
    "i dont understand",
    "i have no idea",
    "i don't have any idea",
    "i dont have any idea",
    "not sure",
    "no idea",
    "haven't studied",
    "havent studied",
    "didn't study",
    "didnt study",
    "didn't learn",
    "didnt learn",
    "never studied",
    "not studied",
    "don't have any idea",
    "dont have any idea",
)

_QUESTION_STOP_WORDS = frozenset(
    (
        "the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "with",
        "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
        "have", "has", "had", "this", "that", "these", "those", "it", "its",
        "we", "you", "they", "i", "me", "my", "your", "what", "why", "how",
        "when", "where", "which", "who", "not", "no", "about", "as", "at", "by",
        "from", "can", "could", "would", "should", "will", "into", "their",
        "them", "there", "here", "tell", "explain", "describe", "answer",
        "please", "question", "someone", "something", "thing", "things",
    )
)


class RAGEvaluator:
    """Evaluates student answers against document-grounded rubrics."""

    def __init__(self):
        self._retriever = DocumentRetriever()

    def evaluate_answer(self, question, answer, document_id: str = "") -> dict:
        """Evaluate a student answer using RAG-retrieved document context."""
        transcript = answer.corrected_transcript or answer.original_transcript
        rubric = self._normalize_rubric(question.rubric)

        chunks = self._retriever.retrieve_for_evaluation(
            question=question.question_text,
            expected_concepts=question.expected_concepts or [],
            subject_code=(
                question.session.mock_interview.subject_code
                if question.session.mock_interview
                else ""
            ),
            chapter=(
                question.session.mock_interview.chapter
                if question.session.mock_interview
                else ""
            ),
            document_id=document_id,
        )
        context = self._retriever.build_context_string(chunks)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are evaluating a college student's answer to an interview "
                    "question based on a specific study document. "
                    "CRITICAL: Use ONLY the provided document context as the reference "
                    "for correctness. The question was generated FROM this document, "
                    "so evaluate the answer against the document content only. "
                    "Use semantic understanding to compare the student's response "
                    "with the document content — do NOT use simple keyword matching. "
                    "The following are all valid and should be scored positively: "
                    "(1) correct explanations that paraphrase or rephrase document "
                    "content using different wording, (2) additional relevant knowledge "
                    "that enhances the answer without contradicting the document, "
                    "(3) correct conceptual understanding expressed in the student's "
                    "own words. Each dimension score must be 0 to 10. "
                    + EVALUATION_CALIBRATION_GUIDE
                    + " An answer that says 'I don't know', admits the topic "
                    "was not studied, or is otherwise a refusal to answer must "
                    "receive 0 or 1 in every dimension. An answer that does not "
                    "address the question at all must score 0 in the relevance "
                    "dimension and no more than 2 in every other dimension. "
                    "Never inflate a non-answer or off-topic answer out of "
                    "sympathy. "
                    " Cite brief transcript evidence. "
                    "Do not infer personality, emotion, honesty, or employability."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "document_context": context[:3000],
                        "question": question.question_text,
                        "rubric": rubric,
                        "expected_concepts": question.expected_concepts,
                        "transcript": transcript,
                        "speech_metrics": answer.speech_metrics,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        try:
            result = chat_json(
                messages,
                LIVE_EVALUATION_SCHEMA,
                temperature=0.1,
                context_tokens=3072,
                max_output_tokens=384,
            )
            model_name = configured_model()
        except LocalModelError:
            result = self._deterministic_evaluation(transcript, rubric, question)
            model_name = "deterministic-fallback"

        scores = self._bounded_scores(
            result.get("dimension_scores"), rubric
        )
        scores = self._calibrate_scores(
            transcript,
            question.expected_concepts,
            scores,
            question.question_text,
        )

        return {
            "dimension_scores": scores,
            "total_score": Decimal(str(self._weighted_total(scores, rubric))),
            "evidence": self._clean_list(result.get("evidence"), limit=8),
            "strengths": self._clean_list(result.get("strengths"), limit=8),
            "missing_concepts": self._clean_list(
                result.get("missing_concepts"), limit=8
            ),
            "improvement_actions": self._clean_list(
                result.get("improvement_actions"), limit=8
            ),
            "improved_answer": str(result.get("improved_answer", ""))[:4000],
            "retrieved_chunks": [c["id"] for c in chunks],
            "model_name": model_name,
        }

    def _normalize_rubric(self, rubric) -> dict:
        if not isinstance(rubric, dict) or not rubric:
            return {
                "technical_correctness": 40,
                "completeness": 20,
                "relevance": 15,
                "structure": 15,
                "practical_example": 10,
            }
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
            return {
                "technical_correctness": 40,
                "completeness": 20,
                "relevance": 15,
                "structure": 15,
                "practical_example": 10,
            }
        return {k: round(v * 100 / total, 2) for k, v in cleaned.items()}

    def _bounded_scores(self, raw_scores, rubric) -> dict:
        result = {}
        raw_scores = raw_scores if isinstance(raw_scores, dict) else {}
        for dimension in rubric:
            try:
                value = float(raw_scores.get(dimension, 0))
            except (TypeError, ValueError):
                value = 0
            result[dimension] = round(min(10, max(0, value)), 2)
        return result

    def _weighted_total(self, scores: dict, rubric: dict) -> float:
        return round(
            sum(scores[key] * float(rubric[key]) for key in rubric) / 10, 2
        )

    def _calibrate_scores(
        self, transcript, expected_concepts, scores, question_text=""
    ) -> dict:
        """Cap scores for non-answers, off-topic, vague, or short answers."""
        text = str(transcript or "").strip()
        words = text.split()
        word_count = len(words)

        normalized = text.lower()
        normalized_words = set(re.findall(r"[a-z0-9]+", normalized))
        vague_hits = sum(1 for p in VAGUE_PHRASES if p in normalized)
        is_non_answer = any(
            phrase in normalized for phrase in NON_ANSWER_PHRASES
        )

        matched = 0
        for concept in (expected_concepts or []):
            concept_words = set(re.findall(r"[a-z0-9]+", str(concept).lower()))
            if not concept_words:
                continue
            overlap = concept_words & normalized_words
            if len(overlap) >= max(1, len(concept_words) * 0.5):
                matched += 1
        coverage = matched / len(expected_concepts) if expected_concepts else 1.0

        question_words = re.findall(
            r"[a-z0-9]+", str(question_text or "").lower()
        )
        content_question_words = [
            word for word in question_words if word not in _QUESTION_STOP_WORDS
        ]
        if content_question_words:
            question_coverage = round(
                len(set(content_question_words) & normalized_words)
                / len(set(content_question_words)),
                3,
            )
        else:
            question_coverage = 1.0

        cap = 10.0
        if word_count == 0:
            cap = 0.0
        elif is_non_answer:
            cap = 1.0 if word_count < 25 else 3.0
        elif coverage == 0 and question_coverage < 0.25:
            cap = 2.0
        elif word_count < 12:
            cap = 3.0
        elif coverage < 0.25:
            cap = 4.0
        elif word_count < 25:
            cap = 5.0
        elif vague_hits >= 3:
            cap = 5.0
        elif vague_hits >= 2 and coverage < 0.4:
            cap = 5.5
        elif coverage < 0.4:
            cap = 5.5

        if cap >= 10.0:
            return scores

        calibrated = {}
        for dim, score in scores.items():
            dim_lower = dim.lower()
            dim_cap = min(6.0, cap + 0.5) if dim_lower in {
                "relevance", "communication", "structure"
            } else cap
            calibrated[dim] = round(min(float(score), dim_cap), 2)
        return calibrated

    def _deterministic_evaluation(self, transcript, rubric, question) -> dict:
        word_count = len(transcript.split()) if transcript else 0
        base = min(7.0, max(1.0, word_count / 12))
        scores = {key: round(base, 2) for key in rubric}
        return {
            "dimension_scores": scores,
            "evidence": [transcript[:240]] if transcript else [],
            "strengths": ["Answer was submitted."] if transcript else [],
            "missing_concepts": (question.expected_concepts or [])[:5],
            "improvement_actions": [
                "Add specific technical details and a practical example."
            ],
            "improved_answer": "",
        }

    def _clean_list(self, value, limit=12) -> list:
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:limit]:
            text = str(item).strip()
            if text and text not in result:
                result.append(text[:500])
        return result
