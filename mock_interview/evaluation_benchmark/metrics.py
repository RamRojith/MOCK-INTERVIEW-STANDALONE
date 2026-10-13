from collections import defaultdict


DEFAULT_TOTAL_TOLERANCE = 10.0
DEFAULT_DIMENSION_TOLERANCE = 1.0


def compare_predictions(
    predictions,
    *,
    total_tolerance=DEFAULT_TOTAL_TOLERANCE,
    dimension_tolerance=DEFAULT_DIMENSION_TOLERANCE,
):
    rows = [row for row in predictions if row.get("status") == "ok"]
    failed = [row for row in predictions if row.get("status") != "ok"]
    total_errors = []
    over_scored = []
    under_scored = []
    dimension_errors = defaultdict(list)

    for row in rows:
        expert_total = _safe_float(row.get("expert_total_score"))
        predicted_total = _safe_float(row.get("predicted_total_score"))
        error = round(predicted_total - expert_total, 2)
        absolute_error = abs(error)
        total_errors.append(absolute_error)

        scored_case = {
            "id": row.get("id"),
            "question": row.get("question", "")[:160],
            "expert_total_score": expert_total,
            "predicted_total_score": predicted_total,
            "error": error,
            "absolute_error": round(absolute_error, 2),
        }
        if error > total_tolerance:
            over_scored.append(scored_case)
        elif error < -total_tolerance:
            under_scored.append(scored_case)

        expert_dimensions = row.get("expert_dimension_scores") or {}
        predicted_dimensions = row.get("predicted_dimension_scores") or {}
        for dimension, expert_score in expert_dimensions.items():
            if dimension not in predicted_dimensions:
                continue
            dimension_errors[dimension].append(
                abs(
                    _safe_float(predicted_dimensions[dimension])
                    - _safe_float(expert_score)
                )
            )

    return {
        "case_count": len(predictions),
        "successful_cases": len(rows),
        "failed_cases": len(failed),
        "mean_absolute_error": _mean(total_errors),
        "within_tolerance_accuracy": _within_rate(total_errors, total_tolerance),
        "total_tolerance": total_tolerance,
        "dimension_tolerance": dimension_tolerance,
        "dimension_metrics": {
            dimension: {
                "case_count": len(errors),
                "mean_absolute_error": _mean(errors),
                "within_tolerance_accuracy": _within_rate(
                    errors,
                    dimension_tolerance,
                ),
            }
            for dimension, errors in sorted(dimension_errors.items())
        },
        "over_scored_cases": sorted(
            over_scored,
            key=lambda item: item["absolute_error"],
            reverse=True,
        )[:10],
        "under_scored_cases": sorted(
            under_scored,
            key=lambda item: item["absolute_error"],
            reverse=True,
        )[:10],
        "failed_case_ids": [row.get("id") for row in failed],
    }


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mean(values):
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _within_rate(errors, tolerance):
    if not errors:
        return 0.0
    within_count = sum(1 for error in errors if error <= tolerance)
    return round(within_count * 100 / len(errors), 2)
