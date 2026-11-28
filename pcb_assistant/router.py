"""One question entry point with local routing and bounded conversation context.

Only user messages supply case facts. Assistant replies and retrieved evidence
must never become observations about the current board.
"""
from __future__ import annotations

from dataclasses import replace
import os
import re

import pcb_assistant.catalog.chat as catalog_chat
import pcb_assistant.interfaces.agent_runtime as agent_runtime
import pcb_assistant.interfaces.gate as phase6_gate
import pcb_assistant.interfaces.managed as phase6_managed
from agent.gate import extract_facts
from agent.models import CaseInput
from agent.retriever import CatalogRetriever
from agent.workflow import diagnose
from pcb_assistant.knowledge import field_coverage, term_groups, is_knowledge_question
from pcb_assistant.materials.selection import is_selection_question
from pcb_assistant.tables.followup import is_table_scope_request


_KNOWLEDGE = re.compile(
    r"什么是|什麼是|是什么(?!原因)|是什麼(?!原因)|定义|定義|含义|含義|区别|區別|原理|机理|機理|"
    r"解释|解釋|资料|資料|文献|文獻|标准|標準|术语|術語|如何设置|怎么设置|如何选择|怎么选|测试方法|作用|"
    r"\b(?:what is|definition|difference|explain|standard)\b", re.IGNORECASE)
_OBSERVATION = re.compile(
    r"出现|出現|发生|發生|发现|發現|检测到|檢測到|压合后|壓合後|回流后|回流後|"
    r"板边|板邊|板中|孔区|孔區|本批|这批|這批|不良|切片|"
    r"\b(?:observed|found|after lamination|batch)\b", re.IGNORECASE)
_TROUBLESHOOT = re.compile(
    r"排查|诊断|診斷|怎么处理|怎麼處理|怎么解决|如何解决|改善|对策|對策|验证|驗證|原因|为什么|為什麼|"
    r"\b(?:diagnos\w*|troubleshoot\w*)\b", re.IGNORECASE)
_RESET = re.compile(r"另一个问题|另一個問題|新问题|新問題|换个问题|換個問題|换个话题|"
                    r"换一批|換一批|另一批|新批次|重新开始|重新開始|\bnew (?:case|topic|batch)\b",
                    re.IGNORECASE)
_FOLLOWUP = re.compile(r"补充|補充|更正|纠正|糾正|实际|實際|刚才|剛才|这个|這個|上述|"
                       r"这批|這批|继续|繼續|那|还有|還有|\b(?:also|actually|it|this)\b",
                       re.IGNORECASE)
_CASE_FIELDS = ("stage", "location", "material", "stackup", "equipment", "batch", "timing")
_DOC_ID = re.compile(r"\b(?:MAIL|CASE|KNOWLEDGE|ATTACHMENT|PUBLIC)_[0-9a-f]{24,32}\b")


def _local_operation(question):
    ids = list(dict.fromkeys(_DOC_ID.findall(question)))
    if len(ids) == 1:
        arguments = {"doc_id": ids[0]}
        if re.search(r"相似|类似|類似", question):
            return "pcb_similar_cases", arguments
        if re.search(r"关联|關聯|关系|關係|来源关系|來源關係", question):
            return "pcb_related", arguments
        return "pcb_get_document", {**arguments, "include_text": True,
                                     "include_attachments": "附件" in question}
    if re.search(r"知识库|知識庫|资料库|資料庫|本地库|本地庫|catalog", question, re.I) and re.search(
            r"多少|数量|數量|统计|統計|状态|狀態|健康|版本", question):
        return "pcb_stats", {}
    return None


def _facts(text):
    # A correction should not select the explicitly negated defect first.
    text = re.sub(r"(?:不是|并非|並非|没有|沒有)[^，,。；;]{0,15}[，,。；;]", "", text)
    return extract_facts(CaseInput.from_mapping({"question": text}))


def _case_context(question, history):
    """Accumulate one case; explicit new cases and topic changes end inheritance."""
    state = {}
    active = False
    inherited = False
    texts = [item["content"] for item in history if item["role"] == "user"] + [question]
    for index, text in enumerate(texts):
        facts = _facts(text)
        defect = facts.get("defect")
        observation = bool(_OBSERVATION.search(text))
        troubleshooting = bool(_TROUBLESHOOT.search(text))
        knowledge = (bool(_KNOWLEDGE.search(text)) or is_knowledge_question(text)) and not troubleshooting
        reset = bool(_RESET.search(text))
        if reset or knowledge or (defect and state.get("defect") and defect != state["defect"]):
            state, active = {}, False
        if knowledge:
            continue
        starts_case = ((defect and (observation or troubleshooting or len(text.strip()) <= 12))
                       or (facts.get("domain_hits") and observation and not knowledge))
        facts_present = any(facts.get(key) for key in _CASE_FIELDS) or facts.get("parameters")
        follows_case = active and not reset and (
            bool(_FOLLOWUP.search(text)) or troubleshooting or bool(facts_present))
        if not starts_case and not follows_case:
            state, active = {}, False
            continue
        if index == len(texts) - 1:
            inherited = bool(state)
        for key in _CASE_FIELDS:
            if facts.get(key):
                state[key] = facts[key]
        if facts.get("parameters"):
            state.setdefault("parameters", {}).update(facts["parameters"])
        if facts.get('product_context'):
            state['notes'] = ' '.join(dict.fromkeys(
                (state.get('notes') or '').split() + facts['product_context']))
        if defect:
            state["defect"] = defect
        active = True
    return ({"question": question, **state} if active else None), inherited


def _annotate(outcome, route, *, context_used=False, agent_loop=False):
    result = dict(outcome.result)
    workflow = {"route": route, "context_used": context_used,
                "routing": "local_rules_v1"}
    if agent_loop:
        workflow["agent_loop"] = True
    result["workflow"] = workflow
    return replace(outcome, result=catalog_chat._finalize_result(result))


def _agent_loop_enabled(arguments, environ=None):
    env = os.environ if environ is None else environ
    return (arguments.get("mode") == "managed"
            and env.get("PCB_AGENT_LOOP", "false").strip().lower() == "true")


def _emit(on_event, payload):
    if callable(on_event):
        try:
            on_event(payload)
        except Exception:
            pass


def answer(arguments, request_id, *, catalog_directory=None, operation_runner=None, on_event=None):
    """Select and execute the workflow; callers never select a business tool."""
    question = arguments["question"].strip()
    history = list(arguments.get("history") or ())[-8:]
    _emit(on_event, {"type": "step", "stage": "triage", "message": "识别问题范围与多轮上下文…"})
    context = "\n".join(item["content"] for item in history if item["role"] == "user")
    triage = phase6_gate.classify(question, context=context)
    if triage.status != phase6_gate.IN_SCOPE:
        record = phase6_managed._gate_record(
            question, history, triage, arguments.get("mode", "evidence_only"))
        return _annotate(phase6_managed.ManagedOutcome(record, triage.status), "clarification"
                         if triage.status == "needs_clarification" else "request_gate")

    filters_requested = any(arguments.get(key) for key in (
        'doc_type', 'source', 'tags', 'version', 'content_status'))
    table_followup=is_table_scope_request(question,history)
    operation = _local_operation(question) if not filters_requested and not table_followup else None
    if operation and operation_runner is not None:
        name, inputs = operation
        response = operation_runner(name, inputs)
        envelope = response['structuredContent']
        record = phase6_managed._gate_record(question, history, triage, 'evidence_only')
        record.update({
            'status': envelope['status'],
            'answer': response['content'][0]['text'],
            'answer_mode': 'local_record',
            'operation': name,
            'operation_result': envelope['result'],
            'evidence_status': 'local_record',
            'retrieval': {'skipped': False, 'basis': 'local_operation'},
            'citation_validation': {'status': 'not_applicable', 'basis': 'local_record_display'},
        })
        return _annotate(phase6_managed.ManagedOutcome(
            record, envelope['status'], errors=tuple(envelope['errors'])), 'local_record')

    case, inherited = (None,True) if table_followup else _case_context(question, history)
    if case is None:
        knowledge_args = {**arguments, "question": question}
        if not table_followup and (_RESET.search(question) or _KNOWLEDGE.search(question) or is_knowledge_question(question)):
            knowledge_args["history"] = []
        _emit(on_event, {"type": "step", "stage": "local_retrieval", "message": "检索本机只读 Catalog 相关证据摘录…"})
        if _agent_loop_enabled(arguments):
            local_args = {**knowledge_args, "mode": "evidence_only"}
            local_outcome = phase6_managed.answer(
                local_args, request_id, catalog_directory=catalog_directory)
            outcome = agent_runtime.run_agent(
                knowledge_args,
                request_id,
                fallback_outcome=local_outcome,
                route="knowledge",
                catalog_directory=catalog_directory,
                on_event=on_event,
            )
            return _annotate(outcome, "knowledge", context_used=table_followup, agent_loop=True)
        return _annotate(phase6_managed.answer(
            knowledge_args, request_id, catalog_directory=catalog_directory), "knowledge", context_used=table_followup)

    current_facts = _facts(question)
    if current_facts.get('secondary_defects'):
        triage = phase6_gate.Triage(
            phase6_gate.NEEDS_CLARIFICATION, 'multiple_defect_modes',
            clarifying_questions=(
                '本轮描述了多种缺陷，请说明主要失效现象，以及它们是否出现在同一位置或同一批板。',))
        record = phase6_managed._gate_record(question, history, triage, 'evidence_only')
        return _annotate(phase6_managed.ManagedOutcome(record, triage.status), 'clarification')

    _emit(on_event, {"type": "step", "stage": "local_diagnosis", "message": "运行压合诊断规则：提取板材事实、排除正常观测并评估候选原因…"})
    limit = min(arguments.get("limit", 6), arguments.get("max_sources", 6))
    filters = {key: arguments[key] for key in (
        "doc_type", "source", "tags", "version", "content_status") if arguments.get(key)}
    retriever = CatalogRetriever(catalog_directory or catalog_chat.DEFAULT_CATALOG,
                                 limit=limit, filters=filters)
    diagnosis = diagnose(case, retriever=retriever, search_limit=limit)
    citations = [{**item, "excerpt_id": item["citation_id"]}
                 for item in diagnosis.get("citations", [])]
    status = {"diagnosed": "answered", "insufficient_evidence": "no_evidence",
              "needs_clarification": "needs_clarification", "refused": "no_evidence"}[
                  diagnosis["status"]]
    record = phase6_managed._gate_record(question, history, triage, "evidence_only")
    record.update({
        "status": status,
        "answer": diagnosis["answer"],
        "answer_mode": "lamination_diagnosis",
        "diagnosis": diagnosis,
        "citations": citations,
        "sources": [{key: item.get(key) for key in (
            "doc_id", "title", "source_type", "source_tier", "evidence_status")}
                    for item in citations],
        "evidence_status": diagnosis["status"],
        "evidence_field_coverage": field_coverage([item["text"] for item in citations]),
        "evidence_relevance": catalog_chat._evidence_relevance(
            term_groups(question), [{"text": item["text"]} for item in citations]),
        "retrieval_ranking": {"basis": "evidence_plus_defect_rules"},
        "missing_information": diagnosis.get("missing_information", []),
        "clarifying_questions": diagnosis.get("next_questions", []),
        "retrieval": diagnosis["retrieval"],
        "citation_validation": catalog_chat._citation_validation(diagnosis["answer"], citations),
        "human_confirmation_required": True,
    })
    record["boundaries"]["human_confirmation_required"] = True
    record["model"].update({"status": "disabled", "mode": "evidence_only"})
    warnings = ()
    if arguments.get("mode") == "managed" and not _agent_loop_enabled(arguments):
        warnings = ("缺陷诊断由本地证据流程完成，本轮未调用托管模型。",)
    local_outcome = phase6_managed.ManagedOutcome(record, status, warnings=warnings)
    if _agent_loop_enabled(arguments):
        outcome = agent_runtime.run_agent(
            {**arguments, "question": question},
            request_id,
            fallback_outcome=local_outcome,
            route="diagnosis",
            case_context=case,
            catalog_directory=catalog_directory,
            on_event=on_event,
        )
        return _annotate(outcome, "diagnosis", context_used=inherited, agent_loop=True)
    return _annotate(local_outcome, "diagnosis", context_used=inherited)
