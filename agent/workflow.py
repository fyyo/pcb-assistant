"""Deterministic lamination diagnosis workflow over catalog evidence."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG
from pcb_assistant.knowledge import query_context_groups

from .composer import render_answer
from .gate import (
    MISSING_FACT_PROMPTS,
    extract_facts,
    gate_case,
    missing_fact_codes,
)
from .models import (
    AGENT_FINGERPRINT_ALGORITHM,
    AGENT_SCHEMA,
    CaseInput,
    fingerprint_result,
)
from .providers import EvidenceProvider
from .retriever import CatalogRetriever
from .source_policy import diagnostic_background_scope
from .taxonomy import CAUSES, DEFECT_MODES, cause_prior, causes_for, term_present


MAX_QUERY_CHARS = 320
MAX_EVIDENCE_PER_CAUSE = 3
MAX_EVIDENCE_TEXT_CHARS = 600
PRIOR_WEIGHT = 10.0
CONTRADICTION_PENALTY = 2.5
NUMERIC_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:°C|℃|MPa|mPa|bar|Bar|Pa|kPa|mbar|Torr|min|分钟|分鐘|小时|小時|h|秒|s|%)",
    re.IGNORECASE,
)
QUESTION_MARKERS = (
    "是否", "多少", "是什么", "如何", "怎么", "哪些", "吗", "呢", "?",
    "？", "有没有", "能不能", "会不会",
)
CAUSAL_MARKERS = (
    "造成", "导致", "引起", "因为", "由於", "由于", "原因", "产生", "產生",
    "形成", "改善", "降低", "增加", "避免", "控制", "建议", "建議", "应",
    "應", "because", "cause", "due to", "result", "lead to", "contribute",
)

QUERY_LABELS = {
    "board_edge": "板边",
    "board_center": "板中",
    "hole_area": "孔区",
    "large_area": "大面积",
    "local": "局部",
    "bga_area": "BGA",
    "panel_edge": "拼板边",
    "lamination": "压合",
    "drilling": "钻孔",
    "plating": "沉铜电镀",
    "etching": "蚀刻",
    "soldermask": "阻焊",
    "reflow": "回流焊",
    "incoming": "来料",
    "storage": "库存",
    "microsection": "切片",
}

BOUNDARIES = {
    "read_only": True,
    "external_services": False,
    "generated_by_model": False,
    "source_text_is_untrusted_data": True,
    "citations_required_for_evidence": True,
    "engineering_conclusion_supported": False,
    "human_confirmation_required": True,
}


def _validate_options(max_evidence_per_cause: int, search_limit: int) -> None:
    if type(max_evidence_per_cause) is not int or not 1 <= max_evidence_per_cause <= 10:
        raise CatalogError("invalid_max_evidence_per_cause")
    if type(search_limit) is not int or not 1 <= search_limit <= 20:
        raise CatalogError("invalid_search_limit")


def _query_for_cause(mode, cause, facts: dict) -> str:
    # Preserve explicit product conditions ahead of the generic cause keywords.
    context = facts.get("product_context") or []
    parts = [*context, mode.label, *cause.keywords]
    for key in ("location", "stage", "material", "stackup", "equipment"):
        value = facts.get(key)
        if value:
            parts.append(str(QUERY_LABELS.get(value, value)))
    for name, value in (facts.get("parameters") or {}).items():
        parts.append(f"{name} {value}")
    query = " ".join(dict.fromkeys(part for part in parts if part))
    return query[:MAX_QUERY_CHARS]


def _external_query_for_cause(mode, cause) -> str:
    """Exclude raw case text and user-supplied fields from outbound queries."""
    parts = ["PCB lamination", mode.label, cause.label, *cause.keywords[:4]]
    return " ".join(dict.fromkeys(part for part in parts if part))[:MAX_QUERY_CHARS]


def _quoted_text(item: dict) -> str:
    text = " ".join(str(item.get("text") or "").split())
    if len(text) > MAX_EVIDENCE_TEXT_CHARS:
        text = text[:MAX_EVIDENCE_TEXT_CHARS - 3] + "..."
    return text


def _citation_item(item: dict) -> dict:
    citation = {
        "citation_id": item.get("citation_id"),
        "doc_id": item.get("doc_id"),
        "title": item.get("title") or "",
        "source_tier": item.get("source_tier") or "C",
        "source_type": item.get("source_type") or "",
        "url": item.get("url"),
        "provider": item.get("provider"),
        "evidence_status": item.get("evidence_status") or "",
        "text": _quoted_text(item),
        "retrieval_score": round(float(item.get("retrieval_score") or 0.0), 6),
    }
    for field in ("locator", "provenance", "ocr_values_independently_verified"):
        if field in item:
            citation[field] = item[field]
    return citation


def _is_assertive(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return False
    if normalized.rstrip().endswith(("?", "？")):
        return False
    return not any(marker.casefold() in normalized.casefold() for marker in QUESTION_MARKERS)


def _causal_hits(text: str) -> int:
    lowered = str(text or "").casefold()
    return sum(1 for marker in CAUSAL_MARKERS if marker.casefold() in lowered)


def _evidence_cause_hits(item: dict, cause) -> int:
    # Only the quoted body can support a cause. Titles and retrieval metadata
    # locate documents but must not supply words absent from the cited excerpt.
    text = _quoted_text(item).casefold()
    if cause.evidence_context and not any(
            term_present(text, marker) for marker in cause.evidence_context):
        return 0
    return sum(1 for keyword in cause.keywords if term_present(text, keyword))


def _score_items(items: list[dict], cause, mode) -> dict:
    if not items:
        return {"score": 0.0, "strength": "none", "source_count": 0, "keyword_hits": 0}
    cause_hits = 0
    defect_hits = 0
    causal_hits = 0
    for item in items:
        text = _quoted_text(item).casefold()
        cause_hits += _evidence_cause_hits(item, cause)
        # Count support for this defect once per excerpt. Other defects and
        # repeated aliases must not inflate its relevance score.
        defect_hits += int(any(term_present(text, alias) for alias in mode.aliases))
        causal_hits += _causal_hits(text)
    source_ids = {
        item.get("source_id") or item.get("doc_id") or item.get("citation_id")
        for item in items
    }
    tier_weight = max(int(item.get("tier_weight") or 1) for item in items)
    score = (
        tier_weight
        + min(len(source_ids), 3) * 0.50
        + min(cause_hits, 4) * 0.50
        + min(defect_hits, 3) * 0.75
        + min(causal_hits, 3) * 0.50
        + 0.20 * len(items)
    )
    if (tier_weight >= 2 and len(source_ids) >= 2 and causal_hits >= 2 and cause_hits >= 2
            and score >= 5.5):
        strength = "strong"
    elif score >= 3.0 and (causal_hits >= 1 or cause_hits >= 2):
        strength = "medium"
    else:
        strength = "weak"
    return {
        "score": round(score, 3),
        "strength": strength,
        "source_count": len(source_ids),
        "keyword_hits": cause_hits,
        "defect_hits": defect_hits,
        "causal_hits": causal_hits,
    }


def _missing_information(facts: dict, hypotheses: list[dict], gate: dict) -> list[dict]:
    codes = list(gate.get("missing_facts") or [])
    for hypothesis in hypotheses[:3]:
        cause = CAUSES[hypothesis["cause_id"]]
        codes.extend(missing_fact_codes(facts, cause.required_facts))
    ordered = list(dict.fromkeys(codes))
    return [
        {"code": code, "prompt": MISSING_FACT_PROMPTS.get(code, code)}
        for code in ordered
    ]


def _numeric_claims(question: str) -> list[dict]:
    return [
        {"text": match.group(0), "used_in_recommendation": False, "source": None}
        for match in NUMERIC_PATTERN.finditer(question or "")
    ]


def _finalize(result: dict) -> dict:
    result["fingerprint_algorithm"] = AGENT_FINGERPRINT_ALGORITHM
    result["result_fingerprint"] = fingerprint_result(result)
    return result


def diagnose(case, *, catalog: Path = DEFAULT_CATALOG, retriever=None,
             max_evidence_per_cause: int = MAX_EVIDENCE_PER_CAUSE,
             search_limit: int = 6,
             external_search: EvidenceProvider | None = None,
             allow_external_search: bool = False) -> dict:
    """Return a deterministic, evidence-backed candidate diagnosis.

    The function never writes to the catalog and never calls a model. It ranks
    candidate causes from the controlled taxonomy, attaches literal catalog
    excerpts, and reports missing information and verification steps.
    """
    if isinstance(case, Mapping):
        case = CaseInput.from_mapping(case)
    if not isinstance(case, CaseInput):
        raise CatalogError("invalid_case")
    _validate_options(max_evidence_per_cause, search_limit)
    if type(allow_external_search) is not bool:
        raise CatalogError("invalid_external_search_opt_in")
    if allow_external_search and external_search is None:
        raise CatalogError("external_search_provider_required")

    facts = extract_facts(case)
    gate = gate_case(case, facts)
    result = {
        "schema": AGENT_SCHEMA,
        "status": "needs_clarification",
        "case_id": case.case_id or None,
        "question": case.question,
        "defect": facts.get("defect"),
        "defect_label": DEFECT_MODES[facts["defect"]].label if facts.get("defect") else None,
        "facts": {
            key: facts.get(key) for key in (
                "stage", "location", "material", "stackup", "equipment",
                "parameters", "batch", "timing", "product_context",
                "normal_observations",
            ) if facts.get(key) not in (None, "", {}, [])
        },
        "gate": gate,
        "hypotheses": [],
        "missing_information": [],
        "next_questions": [],
        "verification_steps": [],
        "citations": [],
        "numeric_claims": _numeric_claims(case.question),
        "safety": {
            "numeric_parameters_cited": True,
            "numeric_parameter_policy": "no_uncited_numeric_recommendation",
            "human_confirmation_required": True,
        },
        "limitations": [
            "候选原因来自受控分类和检索证据，不是已确认根因。",
            "邮件和附件文本属于未独立验证来源，需要现场数据和切片确认。",
        ],
        "boundaries": dict(BOUNDARIES),
        "retrieval": {
            "providers_used": [],
            "external_search_enabled": False,
            "external_queries_sent": 0,
            "provider_errors": [],
        },
    }

    if gate["status"] != "accepted":
        result["status"] = "refused" if gate["status"] == "refused" else "needs_clarification"
        result["answer"] = render_answer(result)
        return _finalize(result)

    defect_id = facts["defect"]
    mode = DEFECT_MODES[defect_id]
    active_retriever = retriever or CatalogRetriever(catalog, limit=search_limit)
    result["retrieval"]["providers_used"].append(
        getattr(active_retriever, "provider_name", "local_catalog")
    )
    if getattr(active_retriever, "provider_name", None) == "local_public_pack":
        result["retrieval"]["public_pack_fingerprint"] = active_retriever.pack["fingerprint"]
    if allow_external_search:
        provider_name = getattr(external_search, "provider_name", "external_search")
        result["retrieval"].update({
            "external_search_enabled": True,
            "external_provider": provider_name,
        })
        result["boundaries"]["external_services"] = True
        result["boundaries"]["data_egress"] = "defect_and_cause_terms_only"
        result["limitations"].append(
            "網頁搜索結果未經獨立驗證；僅傳送缺陷與原因詞，不傳送原始問題或案例欄位。"
        )
    hypotheses = []
    citations = {}
    background = {}

    def diagnostic_items(items):
        eligible = []
        for item in items:
            scope = diagnostic_background_scope(item)
            if scope is None:
                eligible.append(item)
            else:
                # Record the exclusion without turning its excerpt into a
                # direct citation or making its numbers available to rendering.
                background.setdefault(item.get("citation_id"), {
                    "citation_id": item.get("citation_id"),
                    "doc_id": item.get("doc_id"),
                    "title": item.get("title") or "",
                    "url": item.get("url"),
                    **scope,
                })
        return eligible

    for cause in causes_for(defect_id):
        query = _query_for_cause(mode, cause, facts)
        try:
            items = active_retriever.search(query, limit=search_limit) or []
        except CatalogError:
            items = []
        items = [item for item in diagnostic_items(items) if _evidence_cause_hits(item, cause)]
        assertive_items = [item for item in items if _is_assertive(_quoted_text(item))]
        if not assertive_items and allow_external_search:
            external_query = _external_query_for_cause(mode, cause)
            result["retrieval"]["external_queries_sent"] += 1
            try:
                external_items = external_search.search(external_query, limit=search_limit)
                if external_items:
                    result["retrieval"]["providers_used"].append(
                        getattr(external_search, "provider_name", "external_search")
                    )
                external_items = [
                    item for item in diagnostic_items(external_items)
                    if _evidence_cause_hits(item, cause)
                    and _is_assertive(_quoted_text(item))
                ]
                assertive_items.extend(external_items)
            except CatalogError:
                result["retrieval"]["provider_errors"].append("external_search_failed")
        assertive_items = assertive_items[:max_evidence_per_cause]
        score = _score_items(assertive_items, cause, mode)
        supporting = [_citation_item(item) for item in assertive_items]
        for item in supporting:
            citations.setdefault(item["citation_id"], item)
        contradicting = [
            dict(item) for item in (facts.get("normal_observations") or [])
            if item.get("cause_id") == cause.cause_id
        ]
        penalty = CONTRADICTION_PENALTY if contradicting else 0.0
        hypotheses.append({
            "rank": 0,
            "cause_id": cause.cause_id,
            "cause_label": cause.label,
            "category": cause.category,
            "evidence_strength": score["strength"],
            "evidence_score": score["score"],
            "cause_prior": cause_prior(defect_id, cause.cause_id),
            "priority_score": round(
                max(0.0, score["score"] + PRIOR_WEIGHT * cause_prior(defect_id, cause.cause_id) - penalty), 3
            ),
            "source_count": score["source_count"],
            "supporting_evidence": supporting,
            "contradicting_evidence": contradicting,
            "verification_steps": list(cause.verification_steps),
        })

    hypotheses.sort(key=lambda item: (
        -item["priority_score"], CAUSES[item["cause_id"]].order, item["cause_id"]
    ))
    for rank, hypothesis in enumerate(hypotheses, start=1):
        hypothesis["rank"] = rank

    missing = _missing_information(facts, hypotheses, gate)
    next_questions = [item["prompt"] for item in missing]
    if not next_questions:
        next_questions = list(mode.checklist[:3])
    top = hypotheses[0] if hypotheses else None
    verification_steps = []
    if top:
        verification_steps.extend(top["verification_steps"])
    verification_steps.extend(mode.checklist)
    verification_steps = list(dict.fromkeys(verification_steps))[:8]

    has_support = any(
        hypothesis["evidence_strength"] in {"strong", "medium"}
        for hypothesis in hypotheses
    )
    result.update({
        "status": "diagnosed" if has_support else "insufficient_evidence",
        "hypotheses": hypotheses,
        "missing_information": missing,
        "next_questions": next_questions,
        "verification_steps": verification_steps,
        "citations": list(citations.values()),
    })
    requested_context = query_context_groups(' '.join(facts.get('product_context') or []))
    if requested_context:
        cited_text = ' '.join(item['text'] for item in citations.values()).casefold()
        result['retrieval']['context_coverage'] = {
            'basis': 'cited_body_literal_aliases',
            'requested': [group[0] for group in requested_context],
            'uncovered': [group[0] for group in requested_context
                          if not any(alias in cited_text for alias in group)],
        }
    result["retrieval"]["providers_used"] = list(dict.fromkeys(
        result["retrieval"]["providers_used"]
    ))
    if background:
        result["retrieval"]["excluded_background_evidence"] = list(background.values())
    result["answer"] = render_answer(result)
    return _finalize(result)
