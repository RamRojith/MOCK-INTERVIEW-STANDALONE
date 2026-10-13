import json
from pathlib import Path
from types import SimpleNamespace

from mock_interview.ai.interview_engine import evaluate_answer

from .metrics import compare_predictions


def load_cases(path):
    cases = []
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            case = json.loads(text)
            case.setdefault("id", f"{source.stem}:{line_number}")
            _validate_case(case, line_number)
            cases.append(case)
    return cases


def run_benchmark(
    cases,
    *,
    evaluator=evaluate_answer,
    total_tolerance=10.0,
    dimension_tolerance=1.0,
):
    predictions = [
        evaluate_case(case, evaluator=evaluator)
        for case in cases
    ]
    return {
        "summary": compare_predictions(
            predictions,
            total_tolerance=total_tolerance,
            dimension_tolerance=dimension_tolerance,
        ),
        "predictions": predictions,
    }


def evaluate_case(case, *, evaluator=evaluate_answer):
    try:
        question = _question_from_case(case)
        answer = _answer_from_case(case)
        prediction = evaluator(question, answer)
        return {
            "id": case["id"],
            "status": "ok",
            "question": case["question"],
            "expert_total_score": _safe_float(case["expert_total_score"]),
            "predicted_total_score": _safe_float(prediction.get("total_score")),
            "expert_dimension_scores": case.get("expert_dimension_scores", {}),
            "predicted_dimension_scores": prediction.get("dimension_scores", {}),
            "model_name": prediction.get("model_name", ""),
        }
    except Exception as exc:
        return {
            "id": case.get("id"),
            "status": "failed",
            "question": case.get("question", ""),
            "expert_total_score": _safe_float(case.get("expert_total_score")),
            "predicted_total_score": 0.0,
            "expert_dimension_scores": case.get("expert_dimension_scores", {}),
            "predicted_dimension_scores": {},
            "error": str(exc)[:500],
        }


def _question_from_case(case):
    return SimpleNamespace(
        question_text=case["question"],
        rubric=_rubric_from_case(case),
        expected_concepts=case.get("expected_concepts", []),
    )


def _answer_from_case(case):
    transcript = case["transcript"]
    return SimpleNamespace(
        corrected_transcript=transcript,
        original_transcript=transcript,
        speech_metrics=case.get("speech_metrics", {}),
    )


def _rubric_from_case(case):
    rubric = case.get("rubric")
    if isinstance(rubric, dict) and rubric:
        return rubric
    dimensions = list((case.get("expert_dimension_scores") or {}).keys())
    if not dimensions:
        return {"answer_quality": 100}
    weight = 100 / len(dimensions)
    return {dimension: weight for dimension in dimensions}


def _validate_case(case, line_number):
    required = ("question", "transcript", "expert_total_score")
    missing = [field for field in required if field not in case]
    if missing:
        raise ValueError(
            f"Benchmark case line {line_number} is missing: {', '.join(missing)}"
        )
    if not isinstance(case.get("expert_dimension_scores", {}), dict):
        raise ValueError(
            f"Benchmark case line {line_number} has invalid expert_dimension_scores."
        )


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
