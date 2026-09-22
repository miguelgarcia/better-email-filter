from __future__ import annotations

from collections import Counter

from .domain import CATEGORIES, AppError, Message, grouped


def uncontaminated(holdout, snapshots):
    """Exclude any evaluation group connected to training from ANY compared version."""
    records = [dict(r, role="evaluation") for r in holdout]
    for snapshot in snapshots:
        records.extend(dict(r, role="train") for r in snapshot["train"])
    safe = []
    for group in grouped(records):
        if not any(r["role"] == "train" for r in group):
            safe.extend(r for r in group if r["role"] == "evaluation")
    return safe


def metrics(results):
    matrix = {actual: {predicted: 0 for predicted in CATEGORIES} for actual in CATEGORIES}
    errors = review = important_low = input_tokens = output_tokens = 0
    support = Counter(r["actual"] for r in results)
    for result in results:
        if "error" in result:
            errors += 1
            continue
        matrix[result["actual"]][result["category"]] += 1
        review += result["review"]
        important_low += (
            result["actual"] == "important"
            and result["category"] != "important"
            and not result["review"]
        )
        input_tokens += result["input_tokens"]
        output_tokens += result["output_tokens"]
    precision = {}
    recall = {}
    for category in CATEGORIES:
        predicted = sum(matrix[c][category] for c in CATEGORIES)
        precision[category] = matrix[category][category] / predicted if predicted else None
        recall[category] = (
            matrix[category][category] / support[category] if support[category] else None
        )
    n = len(results)
    return {
        "sample_size": n,
        "support": dict(support),
        "confusion_matrix": matrix,
        "precision": precision,
        "recall_including_errors": recall,
        "review_count": review,
        "review_rate": review / n if n else None,
        "errors": errors,
        "important_labeled_low_without_review": important_low,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def evaluate(classifier, snapshots: dict, holdout: list[dict], limit=200):
    examples = uncontaminated(holdout, list(snapshots.values()))[:limit]
    if not examples:
        raise AppError(
            "No uncontaminated held-out examples yet. Confirm more messages, then learn."
        )
    reports = {}
    for vid, snapshot in snapshots.items():
        results = []
        for e in examples:
            result = {
                "id": e["message_id"],
                "actual": e["category"],
                "language": e.get("language", "unknown"),
            }
            try:
                decision = classifier.classify(
                    Message(
                        e["message_id"],
                        e["thread_id"],
                        e["sender"],
                        e["subject"],
                        body_excerpt=e.get("body_excerpt", ""),
                        body_truncated=bool(e.get("body_truncated", False)),
                        body_status=e.get("body_status", "not_fetched"),
                    ),
                    snapshot,
                )
                result.update(
                    category=decision.category,
                    review=decision.review,
                    model=decision.model,
                    input_tokens=decision.input_tokens,
                    output_tokens=decision.output_tokens,
                )
            except AppError:
                result["error"] = "classification_failed"
            results.append(result)
        reports[vid] = {
            "metrics": metrics(results),
            "results": results,
            "by_language": {
                language: metrics([r for r in results if r["language"] == language])
                for language in sorted({r["language"] for r in results})
            },
        }
    comparison = {}
    if len(reports) == 2:
        candidate, baseline = list(reports)
        prior = {r["id"]: r for r in reports[baseline]["results"]}
        regressions, improvements = [], []
        for r in reports[candidate]["results"]:
            old = prior[r["id"]]
            correct_now = r.get("category") == r["actual"]
            correct_before = old.get("category") == old["actual"]
            if correct_before and not correct_now:
                regressions.append(r["id"])
            elif correct_now and not correct_before:
                improvements.append(r["id"])
        comparison = {
            "candidate": candidate,
            "baseline": baseline,
            "regressions": regressions,
            "improvements": improvements,
        }
    return {
        "eligible": len(examples),
        "excluded_or_over_limit": len(holdout) - len(examples),
        "versions": reports,
        "comparison": comparison,
    }
