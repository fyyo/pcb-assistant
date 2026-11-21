"""Export and import real-case calibration packets for the lamination agent."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from urllib.parse import quote

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG

from .gate import MISSING_FACT_PROMPTS, STAGE_TERMS, extract_facts, gate_case
from .models import MAX_QUESTION_CHARS, CaseInput, canonical_json
from .taxonomy import (
    CAUSES,
    DEFECT_MODES,
    cause_prior,
    causes_for,
    detect_defects,
    resolve_defect,
    term_present,
)


CALIBRATION_SCHEMA = "lamination-agent-calibration-v1"
REVIEW_SCHEMA = "lamination-agent-calibration-review-v1"
DEFAULT_SOURCE = Path("data/knowledge_v1/cases.jsonl")
DEFAULT_EXPORT_PATH = Path(".private/agent_calibration/review.jsonl")
DEFAULT_IMPORT_PATH = Path(".private/agent_calibration/suite.jsonl")
DEFAULT_UNLABELED_PATH = Path(".private/agent_calibration/unlabeled.jsonl")
MAX_LIMIT = 100
MAX_PREFILL_CAUSES = 5
MAX_SOURCE_SUBJECT_CHARS = 300
MAX_REFERENCE_CHARS = 2000
MAX_FOCUSED_QUESTION_CHARS = 800
SAFE_OUTPUT_ROOTS = (".private", "data", ".tmp_work")
CASE_FIELDS = (
    "case_id",
    "question",
    "stage",
    "location",
    "material",
    "stackup",
    "equipment",
    "parameters",
    "batch",
    "timing",
    "language",
)
REVIEW_STATUSES = ("pending", "complete", "rejected")
GATE_STATUS_VALUES = ("accepted", "needs_clarification", "refused")
SENTENCE_SPLIT = re.compile(r"(?<=[。；;！？!?])|\n+")
ADMIN_MARKERS = (
    "會議", "会议", "代辦", "代办", "請於", "请于", "填寫", "填写",
    "路徑", "路径", "通知", "培訓", "培训", "領料", "领料", "簽核", "签核",
    "報價", "报价", "交期", "出貨", "出货", "訂單", "订单", "主管",
    "名單", "名单", "表格", "附件", "請假", "请假", "出差", "報銷", "报销",
)
OCCURRENCE_MARKERS = (
    "出現", "出现", "發生", "发生", "異常", "异常", "不良", "問題", "问题",
    "issue", "NG", "客訴", "客诉", "反饋", "反馈", "發現", "发现", "缺陷",
    "報廢", "报废", "超標", "超标", "惡化", "恶化", "fail", "failure", "defect",
)
CAUSAL_MARKERS = (
    "原因", "導致", "导致", "因為", "因为", "由於", "由于", "改善",
    "對策", "对策", "調整", "调整", "降低", "避免", "控制", "驗證", "验证",
    "切片", "root cause", "cause",
)
ACTUAL_MARKERS = (
    "發現", "发现", "出現", "出现", "發生", "发生", "不良", "報廢", "报废",
    "客訴", "客诉", "反饋", "反馈", "NG", "異常", "异常", "問題", "问题",
    "爆板", "缺陷",
)
RISK_MARKERS = (
    "預防", "预防", "防止", "避免", "風險", "风险", "請示", "请示",
    "計劃", "计划", "評估", "评估", "模擬", "模拟", "確認是否", "确认是否",
    "是否會", "是否会", "若無問題", "若无问题", "暫無異常", "暂无异常",
    "無異常", "无异常", "測試中", "测试中", "對策", "对策",
)
NEGATION_MARKERS = (
    "是否", "嗎", "吗", "?", "？", "請確認", "请确认", "確認是否", "确认是否",
    "會不會", "会不会", "能否", "可否", "若", "如果", "假如", "假設", "假设",
)
CONTEXT_MARKERS = (
    "FR-4", "FR4", "PP", "prepreg", "半固化片", "層", "层", "oz", "mil", "mm",
    "真空", "壓機", "压机", "壓合", "压合", "溫度", "温度", "壓力", "压力",
)


def _clip(value: str, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _field_texts(fields: Mapping[str, Any], name: str) -> list[str]:
    values = fields.get(name) or []
    if not isinstance(values, (list, tuple)):
        return []
    parts = []
    for item in values:
        text = item.get("text") if isinstance(item, Mapping) else item
        if isinstance(text, str) and text.strip():
            parts.append(" ".join(text.split()))
    return parts


def _join(parts) -> str:
    return "；".join(dict.fromkeys(part for part in parts if part))


def _term_count(text: str, term: str) -> int:
    lowered = text.casefold()
    term = str(term or "").casefold()
    if not term:
        return 0
    if term.isascii() and term.isalnum() and len(term) <= 8:
        return len(re.findall(
            r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lowered
        ))
    return lowered.count(term)


def _primary_defect(text: str) -> str | None:
    detected = detect_defects(text)
    if not detected:
        return None
    order = {defect_id: index for index, defect_id in enumerate(DEFECT_MODES)}
    scored = []
    for defect_id in detected:
        mode = DEFECT_MODES[defect_id]
        aliases = {defect_id, *mode.aliases, *str(mode.label).split("/")}
        hits = sum(_term_count(text, alias) for alias in aliases if alias)
        scored.append((hits, -order[defect_id], defect_id))
    return max(scored)[2]


def _is_lamination(text: str, topics: list[str]) -> bool:
    lowered = text.casefold()
    normalized_topics = {str(topic).casefold() for topic in topics}
    if normalized_topics & {"压合", "壓合", "层压", "層壓", "lamination"}:
        return True
    return any(term_present(lowered, term) for term in STAGE_TERMS["lamination"])


def _sentences(fields: Mapping[str, Any]) -> list[str]:
    parts = []
    for name in ("phenomenon", "materials", "conditions", "limitations"):
        for text in _field_texts(fields, name):
            for sentence in SENTENCE_SPLIT.split(text):
                sentence = " ".join(str(sentence or "").split())
                if len(sentence) >= 4:
                    parts.append(sentence)
    return list(dict.fromkeys(parts))


def _defect_aliases(defect_id: str) -> set[str]:
    mode = DEFECT_MODES[defect_id]
    return {defect_id, *mode.aliases, *str(mode.label).split("/")}


def _sentence_score(sentence: str, defect_id: str) -> int:
    aliases = _defect_aliases(defect_id)
    score = 6 if any(alias and _term_count(sentence, alias) for alias in aliases) else 0
    lowered = sentence.casefold()
    if any(term_present(lowered, term) for term in STAGE_TERMS["lamination"]):
        score += 2
    score += 2 * sum(1 for marker in OCCURRENCE_MARKERS if marker.casefold() in lowered)
    score += sum(1 for marker in CONTEXT_MARKERS if marker.casefold() in lowered)
    score -= 4 * sum(1 for marker in ADMIN_MARKERS if marker.casefold() in lowered)
    if 20 <= len(sentence) <= 300:
        score += 1
    elif len(sentence) > 600:
        score -= 2
    return score


def _build_question(
    fields: Mapping[str, Any], defect_id: str, reference: Mapping[str, Any]
) -> str:
    blocked = " ".join(
        text for name in ("analysis", "actions") for text in reference.get(name) or []
    ).casefold()
    scored = []
    for index, sentence in enumerate(_sentences(fields)):
        lowered = sentence.casefold()
        if blocked and lowered in blocked:
            continue
        scored.append((_sentence_score(sentence, defect_id), index, sentence))
    if not scored:
        return ""
    aliases = _defect_aliases(defect_id)
    defect_sentences = [
        item for item in scored
        if any(alias and _term_count(item[2], alias) for alias in aliases)
    ]
    context_sentences = [
        item for item in scored
        if item not in defect_sentences and item[0] > 0
    ]
    defect_sentences.sort(key=lambda item: (-item[0], item[1]))
    context_sentences.sort(key=lambda item: (-item[0], item[1]))
    selected = defect_sentences[:2] + context_sentences[:2]
    selected.sort(key=lambda item: item[1])
    question = _join(item[2] for item in selected)
    if not question:
        return ""
    lowered = question.casefold()
    if not any(term_present(lowered, term) for term in STAGE_TERMS["lamination"]):
        question = "压合后 " + question
    return _clip(question, MAX_FOCUSED_QUESTION_CHARS)


def _actual_occurrence_hits(question: str, defect_id: str) -> int:
    aliases = _defect_aliases(defect_id)
    hits = 0
    for sentence in SENTENCE_SPLIT.split(question):
        if not any(alias and _term_count(sentence, alias) for alias in aliases):
            continue
        lowered = sentence.casefold()
        if any(marker.casefold() in lowered for marker in NEGATION_MARKERS + RISK_MARKERS):
            continue
        if any(marker.casefold() in lowered for marker in ACTUAL_MARKERS):
            hits += 1
    return hits


def _question_quality(
    question: str, subject: str, reference_text: str, defect_id: str
) -> dict[str, Any]:
    lowered = f"{question} {subject}".casefold()
    occurrence_hits = sum(
        1 for marker in OCCURRENCE_MARKERS if marker.casefold() in lowered
    )
    admin_hits = sum(1 for marker in ADMIN_MARKERS if marker.casefold() in lowered)
    risk_hits = sum(1 for marker in RISK_MARKERS if marker.casefold() in lowered)
    causal_hits = sum(
        1 for marker in CAUSAL_MARKERS
        if marker.casefold() in reference_text.casefold()
    )
    actual_hits = _actual_occurrence_hits(question, defect_id)
    score = (
        4 * actual_hits + 2 * occurrence_hits + 2 * causal_hits
        - 3 * risk_hits - 2 * admin_hits
    )
    if len(question) > 500:
        score -= (len(question) - 500) // 100
    if score >= 10 and actual_hits > 0:
        hint = "high"
    elif score >= 5 and actual_hits > 0:
        hint = "medium"
    else:
        hint = "low"
    return {
        "score": score,
        "actual_hits": actual_hits,
        "occurrence_hits": occurrence_hits,
        "causal_hits": causal_hits,
        "admin_hits": admin_hits,
        "risk_hits": risk_hits,
        "question_chars": len(question),
        "review_hint": hint,
    }


def _reference(fields: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        name: [
            _clip(text, MAX_REFERENCE_CHARS)
            for text in _field_texts(fields, name)
        ]
        for name in ("analysis", "actions", "verification")
    }


def _reference_text(reference: Mapping[str, Any]) -> str:
    return _join(
        text
        for name in ("analysis", "actions", "verification")
        for text in reference.get(name) or []
    )


def _case_id(source_id: str, subject: str, text: str) -> str:
    basis = source_id or f"{subject}\n{text}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"REAL_{digest}"


def _prefill_causes(defect_id: str, reference_text: str) -> list[str]:
    scored = []
    for cause in causes_for(defect_id):
        hits = sum(_term_count(reference_text, keyword) for keyword in cause.keywords)
        if hits:
            scored.append((
                hits,
                cause_prior(defect_id, cause.cause_id),
                cause.order,
                cause.cause_id,
            ))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return [item[3] for item in scored[:MAX_PREFILL_CAUSES]]


def _build_candidate(record: Mapping[str, Any], *, include_without_reference: bool) -> dict | None:
    fields = record.get("fields")
    if not isinstance(fields, Mapping):
        return None
    topics = [str(topic) for topic in record.get("topics") or []]
    all_text = _join(
        text
        for name in fields
        for text in _field_texts(fields, name)
    )
    subject = str(record.get("subject") or "")
    detection_text = f"{subject} {all_text}".strip()
    if not _is_lamination(detection_text, topics):
        return None
    defect_id = _primary_defect(detection_text)
    if not defect_id:
        return None
    reference = _reference(fields)
    reference_text = _reference_text(reference)
    if not reference_text and not include_without_reference:
        return None
    question = _build_question(fields, defect_id, reference)
    if not question or defect_id not in detect_defects(question):
        return None

    prefill_case = CaseInput.from_mapping({
        "question": question,
        "defect": defect_id,
        "stage": "lamination",
    })
    facts = extract_facts(prefill_case)
    gate = gate_case(prefill_case, facts)
    candidate_causes = _prefill_causes(defect_id, reference_text)
    missing_facts = list(gate.get("missing_facts") or [])
    source_id = str(record.get("source_id") or "")
    case_id = _case_id(source_id, subject, all_text)
    content_hash = hashlib.sha256(
        f"{subject}\n{all_text}".encode("utf-8")
    ).hexdigest()
    facts_summary = {
        key: facts.get(key)
        for key in ("stage", "location", "material", "stackup", "equipment", "parameters")
        if facts.get(key) not in (None, "", {})
    }
    return {
        "schema": REVIEW_SCHEMA,
        "case_id": case_id,
        "source": {
            "source_id": source_id,
            "doc_id": None,
            "doc_ids": [],
            "subject": _clip(subject, MAX_SOURCE_SUBJECT_CHARS),
            "topics": topics,
            "evidence_status": str(record.get("evidence_status") or ""),
            "independently_verified": bool(record.get("independently_verified")),
            "manual_review_required": True,
            "local_use_allowed": bool(record.get("local_use_allowed", True)),
            "content_sha256": content_hash,
        },
        "case": {
            "case_id": case_id,
            "question": question,
        },
        "reference": reference,
        "quality": _question_quality(question, subject, reference_text, defect_id),
        "prefill": {
            "gate_status": "accepted",
            "defect": defect_id,
            "acceptable_causes": candidate_causes,
            "forbidden_causes": [],
            "required_missing_facts": missing_facts,
            "require_citations": False,
            "facts": facts_summary,
        },
        "review": {
            "status": "pending",
            "reviewer": "",
            "reviewed_at": "",
            "gate_status": "accepted",
            "defect": defect_id,
            "acceptable_causes": candidate_causes,
            "forbidden_causes": [],
            "required_missing_facts": missing_facts,
            "require_citations": False,
            "notes": "",
        },
        "tags": ["real_case", defect_id],
    }


def _load_json_documents(path: Path | str, *, kind: str) -> list[Any]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError(f"calibration_{kind}_not_found") from exc
    stripped = text.strip()
    if not stripped:
        raise CatalogError(f"calibration_{kind}_empty")
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
                raise CatalogError(
                    f"invalid_calibration_{kind}_json:{line_number}"
                ) from exc
    else:
        if isinstance(parsed, Mapping) and "cases" in parsed:
            entries = parsed["cases"]
        elif isinstance(parsed, Mapping):
            entries = [parsed]
        else:
            entries = parsed
    if not isinstance(entries, list) or not entries:
        raise CatalogError(f"calibration_{kind}_empty")
    return entries


def collect_candidates(
    source: Path | str = DEFAULT_SOURCE,
    *,
    include_without_reference: bool = False,
) -> list[dict]:
    records = _load_json_documents(source, kind="source")
    candidates = []
    seen = set()
    seen_content = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            candidate = _build_candidate(
                record, include_without_reference=include_without_reference
            )
        except CatalogError:
            continue
        if not candidate:
            continue
        content_hash = candidate["source"]["content_sha256"]
        if candidate["case_id"] in seen or content_hash in seen_content:
            continue
        seen.add(candidate["case_id"])
        seen_content.add(content_hash)
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["case_id"])
    return candidates


def select_candidates(candidates: list[dict], limit: int) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise CatalogError("invalid_calibration_limit")
    groups = defaultdict(list)
    for candidate in candidates:
        groups[candidate["review"]["defect"]].append(candidate)
    for group in groups.values():
        group.sort(key=lambda item: (
            -int(item.get("quality", {}).get("score", 0)),
            -int(item.get("quality", {}).get("occurrence_hits", 0)),
            int(item.get("quality", {}).get("admin_hits", 0)),
            int(item.get("quality", {}).get("question_chars", 0)),
            item["case_id"],
        ))
    order = [defect_id for defect_id in DEFECT_MODES if groups.get(defect_id)]
    selected = []
    first_cap = max(1, min(3, limit // max(1, len(order))))
    for defect_id in order:
        group = groups[defect_id]
        preferred = [
            item for item in group
            if item.get("quality", {}).get("actual_hits", 0) > 0
            and item.get("quality", {}).get("review_hint") in {"high", "medium"}
        ]
        chosen = preferred[:first_cap]
        if len(chosen) < first_cap:
            for item in group:
                if item not in chosen:
                    chosen.append(item)
                if len(chosen) >= first_cap:
                    break
        for item in chosen:
            selected.append(item)
            group.remove(item)
        if len(selected) >= limit:
            return selected[:limit]
    while len(selected) < limit:
        progressed = False
        for defect_id in order:
            if not groups[defect_id]:
                continue
            selected.append(groups[defect_id].pop(0))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _lookup_doc_ids(catalog: Path | str, source_ids: list[str]) -> dict[str, list[str]]:
    if not source_ids:
        return {}
    database = Path(catalog)
    if database.is_dir():
        database = database / "catalog.sqlite3"
    if not database.exists():
        return {}
    uri = "file:" + quote(str(database.resolve()).replace("\\", "/")) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"select source_id, doc_id from documents where source_id in ({placeholders})",
            tuple(source_ids),
        ).fetchall()
    finally:
        connection.close()
    mapping = defaultdict(list)
    for source_id, doc_id in rows:
        mapping[source_id].append(doc_id)
    return mapping


def _safe_output(path: Path | str, overwrite: bool) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()
    roots = [(Path.cwd() / root).resolve() for root in SAFE_OUTPUT_ROOTS]
    roots.append(Path(tempfile.gettempdir()).resolve())
    if not any(target == root or root in target.parents for root in roots):
        raise CatalogError("unsafe_calibration_output")
    if target.exists() and not overwrite:
        raise CatalogError("calibration_output_exists")
    return target


def _write_jsonl(entries: list[Mapping[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(canonical_json(entry) + "\n")
    os.replace(temporary, target)


def export_reviews(
    *,
    source: Path | str = DEFAULT_SOURCE,
    catalog: Path | str = DEFAULT_CATALOG,
    limit: int = 30,
    output: Path | str = DEFAULT_EXPORT_PATH,
    overwrite: bool = False,
    include_without_reference: bool = False,
) -> dict[str, Any]:
    candidates = collect_candidates(
        source, include_without_reference=include_without_reference
    )
    if not candidates:
        raise CatalogError("no_calibration_candidates")
    selected = select_candidates(candidates, limit)
    mapping = _lookup_doc_ids(
        catalog,
        [item["source"]["source_id"] for item in selected if item["source"]["source_id"]],
    )
    for item in selected:
        doc_ids = mapping.get(item["source"]["source_id"], [])
        item["source"]["doc_id"] = doc_ids[0] if doc_ids else None
        item["source"]["doc_ids"] = list(doc_ids)
    target = _safe_output(output, overwrite)
    _write_jsonl(selected, target)
    return {
        "schema": CALIBRATION_SCHEMA,
        "action": "export",
        "candidate_count": len(candidates),
        "exported_count": len(selected),
        "defect_counts": dict(Counter(item["review"]["defect"] for item in selected)),
        "output": str(target),
    }


def export_unlabeled(
    *,
    source: Path | str = DEFAULT_SOURCE,
    limit: int = 30,
    output: Path | str = DEFAULT_UNLABELED_PATH,
    overwrite: bool = False,
    include_without_reference: bool = False,
) -> dict[str, Any]:
    """Export observation inputs directly, without review or expected labels."""
    candidates = collect_candidates(
        source, include_without_reference=include_without_reference
    )
    if not candidates:
        raise CatalogError("no_calibration_candidates")
    selected = select_candidates(candidates, limit)
    entries = [
        {
            "case_id": item["case_id"],
            "case": _observation_payload(item["case_id"], item["case"]),
            "tags": ["real_case", "unlabeled"],
        }
        for item in selected
    ]
    target = _safe_output(output, overwrite)
    _write_jsonl(entries, target)
    return {
        "schema": CALIBRATION_SCHEMA,
        "action": "export_unlabeled",
        "candidate_count": len(candidates),
        "exported_count": len(entries),
        "labeled_case_count": 0,
        "defect_counts": dict(Counter(
            str(extract_facts(CaseInput.from_mapping(item["case"])).get("defect"))
            for item in entries
        )),
        "output": str(target),
    }


def _review_envelope(entry: Any, index: int) -> tuple[str, str, Mapping, Mapping]:
    if not isinstance(entry, Mapping):
        raise CatalogError(f"invalid_calibration_review:{index}")
    schema = entry.get("schema")
    if schema is not None and schema != REVIEW_SCHEMA:
        raise CatalogError("invalid_calibration_review_schema")
    case_id = entry.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise CatalogError(f"review_case_id_required:{index}")
    case_id = case_id.strip()
    case = entry.get("case")
    if not isinstance(case, Mapping):
        raise CatalogError(f"review_case_required:{case_id}")
    review = entry.get("review")
    if not isinstance(review, Mapping):
        raise CatalogError(f"review_block_required:{case_id}")
    status = review.get("status")
    if status not in REVIEW_STATUSES:
        raise CatalogError(f"invalid_review_status:{case_id}")
    return case_id, status, case, review


def _cause_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CatalogError(f"invalid_review_{name}")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"invalid_review_{name}")
        cause_id = item.strip()
        if cause_id not in CAUSES:
            raise CatalogError(f"unknown_review_cause:{cause_id}")
        cleaned.append(cause_id)
    return list(dict.fromkeys(cleaned))


def _missing_fact_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CatalogError("invalid_review_required_missing_facts")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError("invalid_review_required_missing_facts")
        code = item.strip()
        if code not in MISSING_FACT_PROMPTS:
            raise CatalogError(f"unknown_review_missing_fact:{code}")
        cleaned.append(code)
    return list(dict.fromkeys(cleaned))


def _observation_payload(case_id: str, case: Mapping) -> dict[str, Any]:
    unknown = sorted(set(case) - set(CASE_FIELDS))
    if unknown:
        raise CatalogError(f"review_case_field_not_allowed:{unknown[0]}")
    case_payload = {key: case.get(key) for key in CASE_FIELDS if key in case}
    if case_payload.get("case_id") and case_payload["case_id"] != case_id:
        raise CatalogError(f"eval_case_id_mismatch:{case_id}")
    case_payload["case_id"] = case_id
    CaseInput.from_mapping(case_payload)
    return case_payload


def _build_suite_entry(case_id: str, case: Mapping, review: Mapping) -> dict[str, Any]:
    case_input = CaseInput.from_mapping(_observation_payload(case_id, case))

    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise CatalogError(f"review_reviewer_required:{case_id}")
    if len(reviewer.strip()) > 100:
        raise CatalogError(f"review_reviewer_too_long:{case_id}")
    reviewed_at = review.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise CatalogError(f"review_reviewed_at_required:{case_id}")
    try:
        datetime.fromisoformat(reviewed_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError(f"invalid_reviewed_at:{case_id}") from exc

    gate_status = review.get("gate_status")
    if gate_status not in GATE_STATUS_VALUES:
        raise CatalogError(f"invalid_review_gate_status:{case_id}")
    defect_value = review.get("defect")
    if defect_value in (None, ""):
        defect_id = None
    else:
        defect_id = resolve_defect(str(defect_value))
        if not defect_id:
            raise CatalogError(f"unknown_review_defect:{defect_value}")
    acceptable = _cause_list(review.get("acceptable_causes"), "acceptable_causes")
    forbidden = _cause_list(review.get("forbidden_causes"), "forbidden_causes")
    conflict = set(acceptable) & set(forbidden)
    if conflict:
        raise CatalogError(f"conflicting_review_cause:{sorted(conflict)[0]}")
    if defect_id and acceptable:
        allowed = {cause.cause_id for cause in causes_for(defect_id)}
        outside = sorted(set(acceptable) - allowed)
        if outside:
            raise CatalogError(f"review_cause_not_for_defect:{outside[0]}")
    required_missing = _missing_fact_list(review.get("required_missing_facts"))
    require_citations = review.get("require_citations", False)
    if type(require_citations) is not bool:
        raise CatalogError(f"invalid_review_require_citations:{case_id}")

    expected = {"gate_status": gate_status, "defect": defect_id}
    if acceptable:
        expected["acceptable_causes"] = acceptable
    if forbidden:
        expected["forbidden_causes"] = forbidden
    if required_missing:
        expected["required_missing_facts"] = required_missing
    if require_citations:
        expected["require_citations"] = True
    tags = ["real_case"]
    if defect_id:
        tags.append(defect_id)
    return {
        "case_id": case_id,
        "case": case_input.to_dict(),
        "expected": expected,
        "tags": tags,
    }


def import_reviews(
    *,
    review: Path | str,
    output: Path | str = DEFAULT_IMPORT_PATH,
    overwrite: bool = False,
) -> dict[str, Any]:
    entries = _load_json_documents(review, kind="review")
    skipped = Counter()
    imported = []
    seen = set()
    for index, entry in enumerate(entries):
        case_id, status, case, review_block = _review_envelope(entry, index)
        if status != "complete":
            skipped[status] += 1
            continue
        if case_id in seen:
            raise CatalogError(f"duplicate_review_case_id:{case_id}")
        seen.add(case_id)
        imported.append(_build_suite_entry(case_id, case, review_block))
    if not imported:
        raise CatalogError("no_complete_reviews")
    target = _safe_output(output, overwrite)
    _write_jsonl(imported, target)
    return {
        "schema": CALIBRATION_SCHEMA,
        "action": "import",
        "review_count": len(entries),
        "imported_count": len(imported),
        "skipped_pending": skipped.get("pending", 0),
        "skipped_rejected": skipped.get("rejected", 0),
        "defect_counts": dict(Counter(item["expected"]["defect"] for item in imported)),
        "output": str(target),
    }


def render_export_summary(summary: Mapping[str, Any]) -> str:
    defects = summary.get("defect_counts") or {}
    distribution = "、".join(f"{key}:{value}" for key, value in sorted(defects.items()))
    if summary.get("action") == "export_unlabeled":
        return "\n".join([
            "真实案例无标签导出",
            f"候选 {summary.get('candidate_count', 0)} 条，导出 {summary.get('exported_count', 0)} 条",
            f"识别的缺陷分布：{distribution or 'n/a'}",
            f"评测套件：{summary.get('output', '')}",
            "下一步：直接运行 evaluate --cases <评测套件>，可加 --background。",
            "仅包含观察输入，无期望标签；准确率为 n/a。",
        ])
    return "\n".join([
        "真实案例校准导出",
        f"候选 {summary.get('candidate_count', 0)} 条，导出 {summary.get('exported_count', 0)} 条",
        f"缺陷分布：{distribution or 'n/a'}",
        f"复核文件：{summary.get('output', '')}",
        "无标签评测：可直接 evaluate --cases <复核文件>，或用 calibrate export --unlabeled 导出独立套件。",
        "可选标注：编辑 review.status=complete，填写 reviewer/reviewed_at，再运行 calibrate import。",
        "边界：预填标签只是建议；reference 仅供人工查看，不会进入 Agent 输入或评测套件。",
    ])


def render_import_summary(summary: Mapping[str, Any]) -> str:
    return "\n".join([
        "真实案例校准导入",
        (
            f"复核 {summary.get('review_count', 0)} 条，导入 {summary.get('imported_count', 0)} 条，"
            f"跳过 pending {summary.get('skipped_pending', 0)} 条、"
            f"rejected {summary.get('skipped_rejected', 0)} 条"
        ),
        f"评测套件：{summary.get('output', '')}",
        "下一步：运行 agent.cli evaluate 指向该套件。",
        "边界：只有 complete 且字段完整的复核记录会进入评测。",
    ])
