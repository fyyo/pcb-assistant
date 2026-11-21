"""Deterministic, read-only evidence agent over the unified catalog.

The agent orchestrates the existing catalog evidence API. It does not call a
model, synthesize an engineering conclusion, or write to the catalog.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from catalog_common import CatalogError, DEFAULT_CATALOG
from catalog_common import json_text as _canonical_json
from catalog_evidence import (
    MAX_EVIDENCE_LIMIT,
    MAX_EVIDENCE_OFFSET,
    evidence,
)
from pcb_knowledge import (
    coverage_label,
    field_coverage,
    group_coverage,
    retrieval_query,
    term_groups,
)


AGENT_SCHEMA = "catalog-agent-v1"
AGENT_FINGERPRINT_ALGORITHM = "sha256-canonical-json-v1"
MAX_AGENT_QUESTION_CHARS = 1000
MAX_AGENT_SUMMARY_ITEMS = 20
ANSWER_MODE = "extractive_only"
BOUNDARIES = {
    "read_only": True,
    "generated_by_model": False,
    "human_confirmation_required": False,
    "source_text_is_untrusted_data": True,
    "retrieval_only": True,
    "engineering_conclusion_supported": False,
}


def fingerprint_agent_result(result):
    """Return the deterministic SHA-256 fingerprint of an agent response."""
    if not isinstance(result, dict):
        raise CatalogError("invalid_agent_result")
    payload = {
        key: value for key, value in result.items()
        if key not in {"fingerprint_algorithm", "result_fingerprint"}
    }
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_agent_result") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalize_result(result):
    result["fingerprint_algorithm"] = AGENT_FINGERPRINT_ALGORITHM
    result["result_fingerprint"] = fingerprint_agent_result(result)
    return result


def _validate_question(question):
    if (not isinstance(question, str) or not question.strip()
            or len(question) > MAX_AGENT_QUESTION_CHARS):
        raise CatalogError("invalid_agent_question")


def _validate_page(limit, offset, max_sources):
    if (type(limit) is not int or not 1 <= limit <= MAX_EVIDENCE_LIMIT
            or type(offset) is not int or not 0 <= offset <= MAX_EVIDENCE_OFFSET):
        raise CatalogError("invalid_agent_pagination")
    if max_sources is not None and (
            type(max_sources) is not int
            or not 1 <= max_sources <= MAX_EVIDENCE_LIMIT):
        raise CatalogError("invalid_agent_max_sources")


def _evidence_groups(evidence_result):
    groups = []
    for row in evidence_result.get("results", []):
        groups.append({
            "source_id": row["source_id"],
            "doc_id": row["doc_id"],
            "type": row["type"],
            "title": row["title"],
            "source_type": row["source_type"],
            "source_version": row["source_version"],
            "source_path": row["source_path"],
            "content_status": row["content_status"],
            "evidence_status": row["evidence_status"],
            "independently_verified": row["independently_verified"],
            "citation": row["citation"],
            "representations": row["representations"],
            "excerpt_text_variant": row["excerpt_text_variant"],
            "excerpt_text_sha256": row["excerpt_text_sha256"],
            "excerpts": row["excerpts"],
        })
    return groups


def _extractive_summary(evidence_result):
    items = []
    for row in evidence_result.get("results", []):
        for excerpt in row.get("excerpts", []):
            if len(items) >= MAX_AGENT_SUMMARY_ITEMS:
                return items, True
            items.append({
                "text": excerpt["text"],
                "citations": [excerpt["excerpt_id"]],
                "source_id": row["source_id"],
                "doc_id": row["doc_id"],
                "offset_unit": excerpt["offset_unit"],
                "start": excerpt["start"],
                "end": excerpt["end"],
                "line_start": excerpt["line_start"],
                "line_end": excerpt["line_end"],
                "sha256": excerpt["sha256"],
            })
    return items, False


def _missing_information(evidence_result):
    status = evidence_result.get("status")
    if status == "no_evidence":
        return ["no_matching_catalog_records"]
    if status == "page_out_of_range":
        return ["requested_page_outside_bounded_candidate_window"]

    missing = []
    if not evidence_result.get("sources_with_excerpts", 0):
        missing.append("no_literal_excerpts_in_retrieved_sources")
    if evidence_result.get("candidate_window_truncated"):
        missing.append("bounded_candidate_window_truncated")
    if evidence_result.get("has_more_in_window"):
        missing.append("more_sources_available_in_bounded_window")
    return missing


def _evidence_relevance(evidence_result, groups):
    """Label how much of the question wording the returned page covers."""
    used_groups = [
        tuple(group)
        for group in evidence_result.get("ranking", {}).get("term_groups", [])
    ]
    texts = [
        excerpt["text"]
        for group in groups
        for excerpt in group.get("excerpts", [])
    ]
    covered = group_coverage(used_groups, texts)
    return {
        "basis": "query_term_group_coverage",
        "term_groups": len(used_groups),
        "covered_groups": covered,
        "label": coverage_label(covered, len(used_groups), has_hits=bool(texts)),
    }


def _retrieval_metadata(evidence_result, *, retrieval_text, summary_count,
                        summary_omitted_count):
    metadata = {
        "queries": [retrieval_text],
        "limit": evidence_result["limit"],
        "offset": evidence_result["offset"],
        "evidence_schema": evidence_result["schema"],
        "evidence_result_fingerprint": evidence_result["result_fingerprint"],
        "candidate_window_limit": evidence_result["candidate_window_limit"],
        "candidate_representations": evidence_result["candidate_representations"],
        "candidate_window_truncated": evidence_result["candidate_window_truncated"],
        "source_count_in_window": evidence_result["source_count_in_window"],
        "returned": evidence_result["returned"],
        "next_offset": evidence_result["next_offset"],
        "has_more_in_window": evidence_result["has_more_in_window"],
        "pagination_scope": evidence_result["pagination_scope"],
        "excerpt_count": evidence_result["excerpt_count"],
        "sources_with_excerpts": evidence_result["sources_with_excerpts"],
        "summary_item_count": summary_count,
        "summary_omitted_count": summary_omitted_count,
    }
    if "ranking" in evidence_result:
        metadata["ranking"] = dict(evidence_result["ranking"])
    if 'deduplication' in evidence_result:
        metadata['deduplication'] = dict(evidence_result['deduplication'])
    if 'candidate_pools' in evidence_result:
        metadata['candidate_pools'] = dict(evidence_result['candidate_pools'])
    if 'table_followup_scope' in evidence_result:
        metadata['table_followup_scope'] = dict(evidence_result['table_followup_scope'])
    return metadata


def _field_coverage(groups):
    """Field coverage of the same excerpts this agent response shows."""
    return field_coverage([
        group.get("title", "") + "\n" + excerpt["text"]
        for group in groups
        for excerpt in group.get("excerpts", ())
    ])


def ask_agent(directory: Path = DEFAULT_CATALOG, question: str = "", *,
              doc_type=None, source_type=None, tags=(), limit=10, offset=0,
              max_sources=None, source_version=None, content_status=None,
              ranking_terms=(), table_scope=None) -> dict:
    """Return a deterministic extractive answer over catalog evidence.

    ``summary`` contains exact excerpt text only. Every item points to an
    ``EXC_...`` citation that is also present in ``evidence_groups``. This
    function does not infer root causes, recommendations, or engineering
    correctness.
    """
    _validate_question(question)
    _validate_page(limit, offset, max_sources)
    effective_limit = min(limit, max_sources) if max_sources is not None else limit
    question = question.strip()
    retrieval_text = retrieval_query(question)
    effective_ranking_terms = ranking_terms if ranking_terms else term_groups(question)

    evidence_result = evidence(
        directory, question, doc_type=doc_type, source_type=source_type,
        tags=tags, limit=effective_limit, offset=offset,
        source_version=source_version, content_status=content_status,
        ranking_terms=effective_ranking_terms, **({'table_scope':table_scope} if table_scope else {}))
    groups = _evidence_groups(evidence_result)
    summary, summary_truncated = _extractive_summary(evidence_result)
    total_excerpts = evidence_result["excerpt_count"]
    omitted = max(0, total_excerpts - len(summary))

    result = {
        "schema": AGENT_SCHEMA,
        "status": evidence_result["status"],
        "question": question,
        "retrieval_query": retrieval_text,
        "answer_mode": ANSWER_MODE,
        "summary": summary,
        "evidence_groups": groups,
        "missing_information": _missing_information(evidence_result),
        "evidence_relevance": _evidence_relevance(evidence_result, groups),
        "evidence_field_coverage": _field_coverage(groups),
        "retrieval": _retrieval_metadata(
            evidence_result, retrieval_text=retrieval_text,
            summary_count=len(summary), summary_omitted_count=omitted),
        "summary_truncated": summary_truncated,
        "boundaries": dict(BOUNDARIES),
    }
    result.update(BOUNDARIES)
    return _finalize_result(result)


def export_agent(result, destination, *, overwrite=False, catalog_directory=None):
    """Write one verified agent response to a user-selected local JSON file."""
    if not isinstance(result, dict):
        raise CatalogError("invalid_agent_result")
    if type(overwrite) is not bool:
        raise CatalogError("invalid_agent_export_overwrite")
    computed = fingerprint_agent_result(result)
    if (result.get("fingerprint_algorithm") != AGENT_FINGERPRINT_ALGORITHM
            or result.get("result_fingerprint") != computed):
        raise CatalogError("invalid_agent_fingerprint")

    try:
        target = Path(destination).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError) as exc:
        raise CatalogError("invalid_agent_export_path") from exc
    if target.suffix.lower() != ".json":
        raise CatalogError("agent_export_requires_json")
    if catalog_directory is not None:
        catalog_root = Path(catalog_directory).expanduser().resolve(strict=False)
        if target == catalog_root or target.is_relative_to(catalog_root):
            raise CatalogError("agent_export_inside_catalog_forbidden")
    if not target.parent.is_dir():
        raise CatalogError("agent_export_parent_missing")

    try:
        payload = (_canonical_json(result, pretty=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_agent_result") from exc
    try:
        with target.open("wb" if overwrite else "xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CatalogError("agent_export_exists") from exc
    except OSError as exc:
        raise CatalogError("agent_export_failed") from exc

    return {
        "status": "exported",
        "path": str(target),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "fingerprint_algorithm": AGENT_FINGERPRINT_ALGORITHM,
        "result_fingerprint": computed,
        "read_only": True,
        "human_confirmation_required": False,
        "generated_by_model": False,
        "retrieval_only": True,
        "engineering_conclusion_supported": False,
        "source_text_is_untrusted_data": True,
    }
