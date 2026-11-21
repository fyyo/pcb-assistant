"""Offline regression and calibration evaluation for the lamination agent."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG

from .gate import MISSING_FACT_PROMPTS
from .models import AGENT_SCHEMA, CaseInput, canonical_json
from .taxonomy import CAUSES, DEFECT_MODES
from .workflow import diagnose


EVAL_SCHEMA = "lamination-agent-eval-v1"
MAX_CASES = 200
DEFAULT_TOP_K = 3
STATUS_VALUES = ("diagnosed", "insufficient_evidence", "needs_clarification", "refused")
GATE_STATUS_VALUES = ("accepted", "needs_clarification", "refused")
ALLOWED_EXPECTED_FIELDS = {
    "gate_status",
    "status",
    "defect",
    "acceptable_causes",
    "forbidden_causes",
    "required_missing_facts",
    "require_citations",
}


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    case: CaseInput
    expected: dict[str, Any]
    tags: tuple[str, ...] = ()

    @property
    def labeled(self) -> bool:
        return bool(self.expected)


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise CatalogError(f"invalid_{name}")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"invalid_{name}")
        cleaned.append(item.strip())
    return tuple(dict.fromkeys(cleaned))


def _validate_expected(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CatalogError("invalid_eval_expected")
    unknown = sorted(set(value) - ALLOWED_EXPECTED_FIELDS)
    if unknown:
        raise CatalogError(f"unknown_expected_field:{unknown[0]}")
    expected: dict[str, Any] = {}
    if "gate_status" in value:
        gate_status = value["gate_status"]
        if gate_status not in GATE_STATUS_VALUES:
            raise CatalogError("invalid_expected_gate_status")
        expected["gate_status"] = gate_status
    if "status" in value:
        status = value["status"]
        if status not in STATUS_VALUES:
            raise CatalogError("invalid_expected_status")
        expected["status"] = status
    if "defect" in value:
        defect = value["defect"]
        if defect is not None and defect not in DEFECT_MODES:
            raise CatalogError(f"unknown_expected_defect:{defect}")
        expected["defect"] = defect
    acceptable = _string_list(value.get("acceptable_causes"), "acceptable_causes")
    forbidden = _string_list(value.get("forbidden_causes"), "forbidden_causes")
    for cause_id in (*acceptable, *forbidden):
        if cause_id not in CAUSES:
            raise CatalogError(f"unknown_expected_cause:{cause_id}")
    conflict = set(acceptable) & set(forbidden)
    if conflict:
        raise CatalogError(f"conflicting_expected_cause:{sorted(conflict)[0]}")
    if "acceptable_causes" in value:
        if not acceptable:
            raise CatalogError("acceptable_causes_empty")
        expected["acceptable_causes"] = acceptable
    if forbidden:
        expected["forbidden_causes"] = forbidden
    required_missing = _string_list(
        value.get("required_missing_facts"), "required_missing_facts"
    )
    for code in required_missing:
        if code not in MISSING_FACT_PROMPTS:
            raise CatalogError(f"unknown_missing_fact:{code}")
    if required_missing:
        expected["required_missing_facts"] = required_missing
    if "require_citations" in value:
        require_citations = value["require_citations"]
        if type(require_citations) is not bool:
            raise CatalogError("invalid_require_citations")
        if require_citations:
            expected["require_citations"] = True
    return expected


def _normalize_entry(entry: Any, index: int) -> EvalCase:
    if not isinstance(entry, Mapping):
        raise CatalogError(f"invalid_eval_case:{index}")
    case_id = entry.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise CatalogError(f"eval_case_id_required:{index}")
    case_id = case_id.strip()
    if len(case_id) > 120:
        raise CatalogError(f"eval_case_id_too_long:{index}")
    payload = entry.get("case")
    if payload is None:
        payload = {
            key: value for key, value in entry.items()
            if key not in {"expected", "tags", "review_notes"}
        }
    elif not isinstance(payload, Mapping):
        raise CatalogError(f"invalid_eval_case_payload:{case_id}")
    if not isinstance(payload, Mapping):
        raise CatalogError(f"invalid_eval_case_payload:{case_id}")
    payload = dict(payload)
    embedded_id = payload.get("case_id")
    if embedded_id and embedded_id != case_id:
        raise CatalogError(f"eval_case_id_mismatch:{case_id}")
    payload["case_id"] = case_id
    case = CaseInput.from_mapping(payload)
    tags = _string_list(entry.get("tags"), "eval_case_tags")
    expected = _validate_expected(entry.get("expected"))
    return EvalCase(case_id=case_id, case=case, expected=expected, tags=tags)


def load_suite(path: Path | str) -> list[EvalCase]:
    """Load a JSON array or JSONL suite and validate every label before running."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError("eval_cases_not_found") from exc
    entries: Any
    stripped = text.strip()
    if not stripped:
        raise CatalogError("eval_cases_empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        entries = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CatalogError(f"invalid_eval_cases_json:{line_number}") from exc
    else:
        if isinstance(parsed, Mapping) and "cases" in parsed:
            entries = parsed["cases"]
        elif isinstance(parsed, Mapping):
            entries = [parsed]
        else:
            entries = parsed
        if not isinstance(entries, list):
            raise CatalogError("invalid_eval_cases_json")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("eval_cases_empty")
    if len(entries) > MAX_CASES:
        raise CatalogError("too_many_eval_cases")
    suite = [_normalize_entry(entry, index) for index, entry in enumerate(entries)]
    seen = set()
    for item in suite:
        if item.case_id in seen:
            raise CatalogError(f"duplicate_eval_case_id:{item.case_id}")
        seen.add(item.case_id)
    return suite


def _rate(hits: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(hits / total, 4)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _checks_for(result: Mapping[str, Any], expected: Mapping[str, Any], top_k: int) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    gate = result.get("gate") or {}
    hypotheses = result.get("hypotheses") or []
    top_causes = [
        item.get("cause_id") for item in hypotheses[:top_k] if item.get("cause_id")
    ]
    if "gate_status" in expected:
        checks["gate_status"] = gate.get("status") == expected["gate_status"]
    if "status" in expected:
        checks["status"] = result.get("status") == expected["status"]
    if "defect" in expected:
        checks["defect"] = result.get("defect") == expected["defect"]
    if "acceptable_causes" in expected:
        acceptable = set(expected["acceptable_causes"])
        checks["cause_top_1"] = bool(top_causes and top_causes[0] in acceptable)
        checks["cause_top_k"] = bool(set(top_causes) & acceptable)
    if "forbidden_causes" in expected:
        checks["forbidden_causes_absent"] = not (
            set(top_causes) & set(expected["forbidden_causes"])
        )
    if "required_missing_facts" in expected:
        actual_missing = {
            item.get("code") for item in result.get("missing_information") or []
        }
        checks["required_missing_facts"] = set(
            expected["required_missing_facts"]
        ) <= actual_missing
    if expected.get("require_citations"):
        checks["citations_present"] = bool(result.get("citations"))
    return checks


def _quality_for(result: Mapping[str, Any]) -> dict[str, Any]:
    citations = result.get("citations") or []
    citation_ids = [item.get("citation_id") for item in citations]
    valid_citations = all(
        isinstance(item.get("citation_id"), str)
        and str(item["citation_id"]).startswith("EXC_")
        and bool(item.get("text"))
        for item in citations
    )
    hypotheses = result.get("hypotheses") or []
    ranks = [item.get("rank") for item in hypotheses]
    support_ids = [
        evidence.get("citation_id")
        for hypothesis in hypotheses
        for evidence in hypothesis.get("supporting_evidence") or []
    ]
    support_counts = Counter(item for item in support_ids if item)
    return {
        "citations_valid": valid_citations,
        "duplicate_citation_ids": len(citation_ids) != len(set(citation_ids)),
        "rank_sequence_ok": ranks == list(range(1, len(hypotheses) + 1)),
        "unknown_citation_ids": sorted(set(support_ids) - set(citation_ids)),
        "shared_citations": sorted(
            citation_id for citation_id, count in support_counts.items() if count > 1
        ),
        "hypothesis_causes_valid": all(
            item.get("cause_id") in CAUSES for item in hypotheses
        ),
    }


def _result_summary(result: Mapping[str, Any], top_k: int) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "gate_status": (result.get("gate") or {}).get("status"),
        "defect": result.get("defect"),
        "top_causes": [
            item.get("cause_id") for item in (result.get("hypotheses") or [])[:top_k]
        ],
        "citation_count": len(result.get("citations") or []),
        "missing_facts": [
            item.get("code") for item in result.get("missing_information") or []
        ],
    }


def evaluate_suite(
    suite: list[EvalCase],
    *,
    catalog: Path = DEFAULT_CATALOG,
    top_k: int = DEFAULT_TOP_K,
    check_determinism: bool = False,
    include_answers: bool = False,
    retriever_factory: Callable[[CaseInput], Any] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    public_pack: Path | str | None = None,
) -> dict[str, Any]:
    """Run a validated suite and return aggregate metrics plus per-case checks."""
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise CatalogError("invalid_top_k")
    if not isinstance(suite, list) or not suite:
        raise CatalogError("eval_cases_empty")
    pack_retriever = None
    if public_pack is not None:
        if retriever_factory is not None:
            raise CatalogError("public_pack_retriever_conflict")
        from .public_pack import PublicPackRetriever
        pack_retriever = PublicPackRetriever(public_pack)
        retriever_factory = lambda case: pack_retriever

    case_reports = []
    latencies = []
    for index, item in enumerate(suite, start=1):
        retriever = retriever_factory(item.case) if retriever_factory else None
        started = time.perf_counter()
        result = diagnose(item.case, catalog=catalog, retriever=retriever)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        latencies.append(latency_ms)
        deterministic = None
        if check_determinism:
            retriever = retriever_factory(item.case) if retriever_factory else None
            repeated = diagnose(item.case, catalog=catalog, retriever=retriever)
            deterministic = repeated.get("result_fingerprint") == result.get("result_fingerprint")
        checks = _checks_for(result, item.expected, top_k)
        quality = _quality_for(result)
        passed = None
        if item.labeled:
            passed = all(checks.values()) if checks else False
            if deterministic is False:
                passed = False
        case_report = {
            "case_id": item.case_id,
            "tags": list(item.tags),
            "labeled": item.labeled,
            "expected": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in item.expected.items()
            },
            "actual": _result_summary(result, top_k),
            "checks": checks,
            "passed": passed,
            "latency_ms": latency_ms,
            "deterministic": deterministic,
            "quality": quality,
        }
        if include_answers:
            case_report["answer"] = result.get("answer") or ""
        case_reports.append(case_report)
        if progress_callback:
            progress_callback(index, len(suite), item.case_id)

    labeled = [item for item in case_reports if item["labeled"]]
    gate_labeled = [item for item in labeled if "gate_status" in item["expected"]]
    status_labeled = [item for item in labeled if "status" in item["expected"]]
    defect_labeled = [item for item in labeled if "defect" in item["expected"]]
    cause_labeled = [item for item in labeled if "acceptable_causes" in item["expected"]]
    forbidden_labeled = [item for item in labeled if "forbidden_causes" in item["expected"]]
    missing_labeled = [item for item in labeled if "required_missing_facts" in item["expected"]]
    citation_labeled = [item for item in labeled if item["expected"].get("require_citations")]
    required_missing_total = sum(
        len(item["expected"]["required_missing_facts"]) for item in missing_labeled
    )
    missing_hits = 0
    if required_missing_total:
        for item in missing_labeled:
            actual = set(item["actual"]["missing_facts"])
            missing_hits += len(set(item["expected"]["required_missing_facts"]) & actual)
    quality_passed = sum(
        1 for item in case_reports
        if item["quality"]["citations_valid"]
        and not item["quality"]["duplicate_citation_ids"]
        and item["quality"]["rank_sequence_ok"]
        and not item["quality"]["unknown_citation_ids"]
        and item["quality"]["hypothesis_causes_valid"]
    )
    summary = {
        "case_count": len(case_reports),
        "labeled_case_count": len(labeled),
        "label_coverage": _rate(len(labeled), len(case_reports)),
        "passed_case_count": sum(1 for item in labeled if item["passed"]),
        "pass_rate": _rate(sum(1 for item in labeled if item["passed"]), len(labeled)),
        "gate_status_accuracy": _rate(
            sum(1 for item in gate_labeled if item["checks"].get("gate_status")),
            len(gate_labeled),
        ),
        "status_accuracy": _rate(
            sum(1 for item in status_labeled if item["checks"].get("status")),
            len(status_labeled),
        ),
        "defect_accuracy": _rate(
            sum(1 for item in defect_labeled if item["checks"].get("defect")),
            len(defect_labeled),
        ),
        "cause_top_1_accuracy": _rate(
            sum(1 for item in cause_labeled if item["checks"].get("cause_top_1")),
            len(cause_labeled),
        ),
        "cause_top_k_accuracy": _rate(
            sum(1 for item in cause_labeled if item["checks"].get("cause_top_k")),
            len(cause_labeled),
        ),
        "forbidden_cause_violations": sum(
            1 for item in forbidden_labeled
            if not item["checks"].get("forbidden_causes_absent")
        ),
        "missing_fact_recall": _rate(missing_hits, required_missing_total),
        "citation_requirement_pass_rate": _rate(
            sum(1 for item in citation_labeled if item["checks"].get("citations_present")),
            len(citation_labeled),
        ),
        "citation_integrity_pass_rate": _rate(quality_passed, len(case_reports)),
        "cases_with_shared_citations": sum(
            1 for item in case_reports if item["quality"]["shared_citations"]
        ),
        "determinism_pass_rate": _rate(
            sum(1 for item in case_reports if item["deterministic"] is True),
            sum(1 for item in case_reports if item["deterministic"] is not None),
        ),
        "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "latency_ms_p95": _percentile(latencies, 0.95),
        "latency_ms_max": round(max(latencies), 3) if latencies else None,
        "status_counts": dict(Counter(item["actual"]["status"] for item in case_reports)),
        "defect_counts": dict(Counter(
            str(item["actual"]["defect"]) for item in case_reports
        )),
        "top_cause_counts": dict(Counter(
            item["actual"]["top_causes"][0]
            for item in case_reports if item["actual"]["top_causes"]
        )),
        "top_cause_counts_by_defect": {
            defect: dict(Counter(
                item["actual"]["top_causes"][0]
                for item in case_reports
                if str(item["actual"]["defect"]) == defect
                and item["actual"]["top_causes"]
            ))
            for defect in sorted({str(item["actual"]["defect"]) for item in case_reports})
        },
        "cases_with_citations": sum(
            1 for item in case_reports if item["actual"]["citation_count"] > 0
        ),
        "cases_without_hypotheses": sum(
            1 for item in case_reports if not item["actual"]["top_causes"]
        ),
        "failed_case_ids": [
            item["case_id"] for item in labeled if not item["passed"]
        ],
    }
    return {
        "schema": EVAL_SCHEMA,
        "agent_schema": AGENT_SCHEMA,
        "settings": {
            "top_k": top_k,
            "check_determinism": check_determinism,
            "include_answers": include_answers,
            **({"local_provider": "local_public_pack",
                "public_pack_fingerprint": pack_retriever.pack["fingerprint"]}
               if pack_retriever is not None else {}),
        },
        "summary": summary,
        "cases": case_reports,
        "boundaries": {
            "read_only": True,
            "external_services": False,
            "generated_by_model": False,
            "expert_gold": False,
            "quality_claim_ready": False,
            "human_review_required": True,
        },
    }


def evaluate_file(
    cases_path: Path | str,
    *,
    catalog: Path = DEFAULT_CATALOG,
    top_k: int = DEFAULT_TOP_K,
    check_determinism: bool = False,
    include_answers: bool = False,
    retriever_factory: Callable[[CaseInput], Any] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    public_pack: Path | str | None = None,
) -> dict[str, Any]:
    return evaluate_suite(
        load_suite(cases_path),
        catalog=catalog,
        top_k=top_k,
        check_determinism=check_determinism,
        include_answers=include_answers,
        retriever_factory=retriever_factory,
        progress_callback=progress_callback,
        public_pack=public_pack,
    )


def write_report(report: Mapping[str, Any], path: Path | str) -> Path:
    """Write a report atomically so an interrupted run cannot leave partial JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(canonical_json(report, pretty=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and 0 <= value <= 1:
        return f"{value:.2%}"
    return str(value)


def render_summary(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "压合诊断 Agent 评测",
        (
            f"用例 {summary.get('case_count', 0)} 条，"
            f"有期望 {summary.get('labeled_case_count', 0)} 条，"
            f"通过率 {_metric(summary.get('pass_rate'))}"
        ),
        f"门禁状态准确率：{_metric(summary.get('gate_status_accuracy'))}",
        f"缺陷识别准确率：{_metric(summary.get('defect_accuracy'))}",
        (
            f"候选原因 Top-1 / Top-K 命中率："
            f"{_metric(summary.get('cause_top_1_accuracy'))} / "
            f"{_metric(summary.get('cause_top_k_accuracy'))}"
        ),
        f"缺失信息召回率：{_metric(summary.get('missing_fact_recall'))}",
        f"引用完整性：{_metric(summary.get('citation_integrity_pass_rate'))}",
        f"重复运行指纹一致性：{_metric(summary.get('determinism_pass_rate'))}",
        (
            f"平均耗时 {summary.get('latency_ms_avg', 'n/a')} ms，"
            f"p95 {summary.get('latency_ms_p95', 'n/a')} ms"
        ),
    ]
    if summary.get("top_cause_counts"):
        lines.append("首位候选分布：" + "、".join(
            f"{cause}:{count}" for cause, count in sorted(summary["top_cause_counts"].items())
        ))
    failures = summary.get("failed_case_ids") or []
    if failures:
        lines.append("未通过：" + "、".join(failures[:10]))
    lines.append("边界：本地只读评测；未建立专家 gold，不能对外声明准确率或发布就绪。")
    return "\n".join(lines)
