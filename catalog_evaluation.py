"""Read-only quality and latency evaluation for similar-case recommendations.

The evaluation uses the production ranking path and reports aggregate counts
only.  It never emits titles, source prose, or per-document recommendations.
The overlap rates are internal signal-consistency checks, not human relevance
labels or engineering correctness judgments.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import random
import statistics
import time

from catalog_common import CatalogError, connect_readonly
from catalog_insights import similar_cases, warm_insights


_DEFAULT_SAMPLE_SIZE = 200
_DEFAULT_LIMIT = 10
_DEFAULT_SEED = 20261022


def _validate_integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CatalogError("invalid_evaluation_parameters")


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _case_document_ids(directory: Path):
    with closing(connect_readonly(directory)) as connection:
        return [row[0] for row in connection.execute(
            "SELECT doc_id FROM documents WHERE type='CASE' ORDER BY doc_id")]


def evaluate_similar_cases(directory: Path, *, sample_size=_DEFAULT_SAMPLE_SIZE,
                           limit=_DEFAULT_LIMIT, seed=_DEFAULT_SEED) -> dict:
    """Measure recommendation signal consistency and request latency.

    A deterministic sample of retained CASE documents is evaluated through the
    same ``similar_cases`` function used by the local retrieval layer.  Results contain aggregate
    metrics only.  ``same_source`` recommendations are counted separately from
    semantic overlap because they represent the CASE/KNOWLEDGE view of one
    retained source record rather than an independent related case.
    """
    _validate_integer("sample_size", sample_size, 1, 1000)
    _validate_integer("limit", limit, 1, 200)
    _validate_integer("seed", seed, 0, 2**32 - 1)
    directory = Path(directory).resolve()

    warmup_started = time.perf_counter()
    warm_counts = warm_insights(directory)
    warmup_ms = round((time.perf_counter() - warmup_started) * 1000, 3)
    case_ids = _case_document_ids(directory)
    if not case_ids:
        raise CatalogError("no_cases_for_evaluation")
    evaluated_count = min(sample_size, len(case_ids))
    sampled_ids = sorted(random.Random(seed).sample(case_ids, evaluated_count))

    latencies = []
    slots = 0
    same_source_slots = 0
    semantic_slots = 0
    topic_overlap_slots = 0
    term_overlap_slots = 0
    strong_overlap_slots = 0
    queries_with_results = 0
    queries_with_semantic = 0
    queries_with_strong = 0
    top1_topic = 0
    top1_term = 0
    top1_strong = 0
    first_semantic_ranks = []
    distinct_source_ratios = []
    self_recommendations = 0
    duplicate_result_ids = 0
    score_explanation_failures = 0
    contract_failures = 0

    evaluation_started = time.perf_counter()
    for source_id in sampled_ids:
        request_started = time.perf_counter()
        result = similar_cases(directory, source_id, limit=limit, offset=0)
        latencies.append(round((time.perf_counter() - request_started) * 1000, 3))

        if not (result.get("derived_recommendation") is True
                and result.get("not_source_evidence") is True
                and result.get("read_only") is True
                and result.get("writes_catalog") is False):
            contract_failures += 1

        results = result.get("results", [])
        if results:
            queries_with_results += 1
        result_ids = [item.get("doc_id") for item in results]
        if len(result_ids) != len(set(result_ids)):
            duplicate_result_ids += 1
        if source_id in result_ids:
            self_recommendations += 1

        distinct_sources = {(item.get("source_type"), item.get("source_id"))
                            for item in results}
        if results:
            distinct_source_ratios.append(len(distinct_sources) / len(results))

        non_same = []
        for rank, item in enumerate(results, start=1):
            slots += 1
            components = item.get("score_components", {})
            if item.get("score") != sum(components.values()):
                score_explanation_failures += 1
            if item.get("same_source") is True:
                same_source_slots += 1
                continue
            non_same.append((rank, item))
            semantic_slots += 1
            has_topic = bool(item.get("shared_tags"))
            has_term = bool(item.get("shared_terms"))
            topic_overlap_slots += int(has_topic)
            term_overlap_slots += int(has_term)
            strong_overlap_slots += int(has_topic and has_term)

        if non_same:
            queries_with_semantic += 1
            first_semantic_ranks.append(non_same[0][0])
            top = non_same[0][1]
            has_topic = bool(top.get("shared_tags"))
            has_term = bool(top.get("shared_terms"))
            top1_topic += int(has_topic)
            top1_term += int(has_term)
            top1_strong += int(has_topic and has_term)
            if has_topic and has_term:
                queries_with_strong += 1

    evaluation_ms = round((time.perf_counter() - evaluation_started) * 1000, 3)
    invariant_failures = (self_recommendations + duplicate_result_ids
                          + score_explanation_failures + contract_failures)
    return {
        "status": "completed",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evaluation_kind": "internal_signal_consistency_not_human_relevance",
        "sample": {
            "requested": sample_size,
            "evaluated": evaluated_count,
            "eligible_cases": len(case_ids),
            "limit": limit,
            "seed": seed,
            "selection": "deterministic_random_case_documents",
        },
        "warmup": {
            "elapsed_ms": warmup_ms,
            "relationship_count": warm_counts["relationship_count"],
            "case_profile_count": warm_counts["case_count"],
        },
        "coverage": {
            "queries_with_results": queries_with_results,
            "queries_with_semantic_recommendation": queries_with_semantic,
            "queries_with_strong_overlap": queries_with_strong,
            "query_result_rate": _rate(queries_with_results, evaluated_count),
            "semantic_coverage_rate": _rate(queries_with_semantic, evaluated_count),
            "strong_coverage_rate": _rate(queries_with_strong, evaluated_count),
            "mean_first_semantic_rank": (round(statistics.fmean(first_semantic_ranks), 3)
                                         if first_semantic_ranks else None),
        },
        "signal_consistency": {
            "slots_returned": slots,
            "semantic_slots": semantic_slots,
            "topic_overlap_slots": topic_overlap_slots,
            "term_overlap_slots": term_overlap_slots,
            "strong_overlap_slots": strong_overlap_slots,
            "topic_overlap_rate": _rate(topic_overlap_slots, semantic_slots),
            "term_overlap_rate": _rate(term_overlap_slots, semantic_slots),
            "strong_overlap_rate": _rate(strong_overlap_slots, semantic_slots),
            "top1_topic_overlap_rate": _rate(top1_topic, queries_with_semantic),
            "top1_term_overlap_rate": _rate(top1_term, queries_with_semantic),
            "top1_strong_overlap_rate": _rate(top1_strong, queries_with_semantic),
        },
        "source_identity": {
            "same_source_slots": same_source_slots,
            "same_source_slot_rate": _rate(same_source_slots, slots),
            "mean_distinct_source_ratio": (round(statistics.fmean(distinct_source_ratios), 4)
                                           if distinct_source_ratios else None),
        },
        "latency_ms": {
            "requests": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
            "total_evaluation": evaluation_ms,
        },
        "invariants": {
            "self_recommendations": self_recommendations,
            "queries_with_duplicate_result_ids": duplicate_result_ids,
            "score_explanation_failures": score_explanation_failures,
            "contract_failures": contract_failures,
            "failures": invariant_failures,
        },
        "limitations": [
            "Overlap rates reuse the ranking signals and therefore measure consistency, not human relevance.",
            "No engineering correctness or independent expert judgment is inferred from these metrics.",
            "Latency is a local single-process sample and is not a concurrency benchmark.",
        ],
        "read_only": True,
        "catalog_writes": False,
        "contains_source_prose": False,
        "derived_recommendation": True,
        "not_source_evidence": True,
    }


_GATE_THRESHOLDS = {
    "min_query_result_rate": 1.0,
    "min_semantic_coverage_rate": 0.95,
    "min_strong_coverage_rate": 0.95,
    "min_top1_strong_overlap_rate": 0.95,
    "max_invariant_failures": 0,
    "max_p95_ms": 500.0,
}


def evaluate_quality_gate(directory: Path, *, sample_size=_DEFAULT_SAMPLE_SIZE,
                          limit=_DEFAULT_LIMIT, seed=_DEFAULT_SEED) -> dict:
    """Run the automated recommendation quality gate.

    This gate is deliberately model-independent and read-only.  It does not
    read or write human review labels and it does not claim human relevance or
    engineering correctness.
    """
    evaluation = evaluate_similar_cases(
        directory, sample_size=sample_size, limit=limit, seed=seed)
    checks = {
        "query_result_rate": evaluation["coverage"]["query_result_rate"],
        "semantic_coverage_rate": evaluation["coverage"]["semantic_coverage_rate"],
        "strong_coverage_rate": evaluation["coverage"]["strong_coverage_rate"],
        "top1_strong_overlap_rate": evaluation["signal_consistency"]["top1_strong_overlap_rate"],
        "invariant_failures": evaluation["invariants"]["failures"],
        "p95_ms": evaluation["latency_ms"]["p95"],
    }
    failures = []

    def require_min(name, value, threshold):
        if value is None or value < threshold:
            failures.append({"check": name, "value": value,
                             "threshold": threshold, "rule": "minimum"})

    def require_max(name, value, threshold):
        if value is None or value > threshold:
            failures.append({"check": name, "value": value,
                             "threshold": threshold, "rule": "maximum"})

    require_min("query_result_rate", checks["query_result_rate"],
                _GATE_THRESHOLDS["min_query_result_rate"])
    require_min("semantic_coverage_rate", checks["semantic_coverage_rate"],
                _GATE_THRESHOLDS["min_semantic_coverage_rate"])
    require_min("strong_coverage_rate", checks["strong_coverage_rate"],
                _GATE_THRESHOLDS["min_strong_coverage_rate"])
    require_min("top1_strong_overlap_rate", checks["top1_strong_overlap_rate"],
                _GATE_THRESHOLDS["min_top1_strong_overlap_rate"])
    require_max("invariant_failures", checks["invariant_failures"],
                _GATE_THRESHOLDS["max_invariant_failures"])
    require_max("p95_ms", checks["p95_ms"], _GATE_THRESHOLDS["max_p95_ms"])

    return {
        "status": "passed" if not failures else "failed",
        "checked_at": evaluation["checked_at"],
        "evaluation_kind": "automated_proxy_not_human_validation",
        "acceptance_scope": "internal_recommendation_quality_gate_only",
        "human_confirmation_required": False,
        "human_labels_used": False,
        "review_queue_required": False,
        "thresholds": dict(_GATE_THRESHOLDS),
        "checks": checks,
        "failures": failures,
        "evaluation": evaluation,
        "limitations": [
            "This gate measures internal recommendation signal consistency and latency; it does not establish human relevance or engineering correctness.",
            "No human labels are read or written.",
            "Passing this gate is not an expert acceptance or production qualification.",
        ],
        "read_only": True,
        "review_data_writes": False,
        "catalog_writes": False,
        "contains_source_prose": False,
        "contains_document_level_results": False,
        "labels_generated_by_model": False,
        "training_allowed": False,
        "published": False,
        "external_mail_transmission": False,
        "derived_recommendation": True,
        "not_source_evidence": True,
    }


__all__ = ["evaluate_similar_cases", "evaluate_quality_gate"]