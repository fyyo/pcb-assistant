"""Deterministic, human-readable rendering of a diagnosis result."""
from __future__ import annotations

from .source_policy import OCR_CAUTION, is_ocr_evidence


STRENGTH_LABELS = {
    "strong": "证据较多",
    "medium": "有部分证据",
    "weak": "证据较少",
    "none": "暂无直接证据",
}


def _evidence_line(item: dict) -> str:
    quote = " ".join(str(item.get("text") or "").split())
    if len(quote) > 160:
        quote = quote[:157] + "..."
    citation = (
        f"- [{item.get('citation_id')}] {item.get('source_tier')} 级 · "
        f"{item.get('title')}：{quote}"
    )
    if item.get("url"):
        citation += f" ({item['url']})"
    if is_ocr_evidence(item):
        citation += " · OCR 未核实"
    locator = item.get("locator") or {}
    if locator.get("value"):
        citation += f" · {locator.get('type', '位置')} {locator['value']}"
    elif locator.get("page") or locator.get("unit"):
        citation += f" · 页/单元 {locator.get('page') or locator.get('unit')}"
    provenance = item.get("provenance") or {}
    if provenance.get("document_revision"):
        citation += f" · 版本 {provenance['document_revision']}"
    if provenance.get("quality_notes"):
        citation += " · 来源有已知疑点，需查看结构化引用中的 quality_notes"
    return citation


def render_answer(result: dict) -> str:
    status = result.get("status")
    gate = result.get("gate") or {}
    lines = []
    if status == "refused":
        lines.append("这个问题不属于 PCB 压合工艺诊断范围，暂不给出工艺结论。")
        if gate.get("reason"):
            lines.append(f"原因：{gate['reason']}。")
        return "\n".join(lines)
    if status == "needs_clarification":
        lines.append(gate.get("clarifying_question") or "需要先确认缺陷类型。")
        lines.append("请补充缺陷现象、发生阶段、位置和材料/叠构信息。")
        return "\n".join(lines)

    hypotheses = result.get("hypotheses") or []
    lines.append("候选方向（结合引用证据与缺陷类别规则排序，需现场验证）：")
    if not hypotheses:
        lines.append("暂无可排序的候选方向。")
    for hypothesis in hypotheses[:3]:
        strength = STRENGTH_LABELS.get(hypothesis.get("evidence_strength"), "未知")
        lines.append(f"{hypothesis.get('rank')}. {hypothesis.get('cause_label')}（{strength}）")
        for item in (hypothesis.get("supporting_evidence") or [])[:2]:
            lines.append(_evidence_line(item))
        steps = hypothesis.get("verification_steps") or []
        if steps:
            lines.append(f"   建议验证：{steps[0]}")

    if status == "insufficient_evidence":
        lines.append("")
        if result.get("citations"):
            lines.append("当前证据强度不足，以上仅为待验证的排查方向。")
        else:
            lines.append("当前 Catalog 中没有找到可直接引用的证据，以上仅为待验证的排查方向。")

    retrieval = result.get("retrieval") or {}
    if retrieval.get("excluded_background_evidence"):
        lines.extend(["", "部分检索资料研究 CFRP（碳纤维增强复合材料）修补，仅作背景，未用于 PCB 原因评分或直接引用。"])
    uncovered = (retrieval.get('context_coverage') or {}).get('uncovered') or []
    if uncovered:
        lines.extend(["", "资料适用性缺口：当前引用未直接覆盖“" + '、'.join(uncovered)
                      + "”，需结合该条件另行核对，不能视为已解决。"])
    if retrieval.get("external_queries_sent"):
        lines.append("")
        if retrieval.get("provider_errors"):
            lines.append("部分公开检索请求失败，本轮已保留可用证据；可稍后重试。")
        lines.append("公开检索摘录未经独立验证，需结合原网页和现场情况核实。")

    if any(is_ocr_evidence(item) for item in result.get("citations") or []):
        lines.extend(["", OCR_CAUTION])

    missing = result.get("missing_information") or []
    if missing:
        lines.append("")
        lines.append("需要补充：")
        for item in missing[:6]:
            lines.append(f"- {item.get('prompt')}")

    lines.append("")
    lines.append("边界：这是证据辅助的候选诊断，不替代切片、现场参数核对和工程师确认。")
    return "\n".join(lines)
