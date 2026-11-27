"""Offline synthetic tests for the bounded sanitized-egress Agent runtime."""
from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import json
import unittest
from unittest import mock

import pcb_assistant.catalog.chat as catalog_chat
import pcb_assistant.interfaces.agent_runtime as agent_runtime
import pcb_assistant.interfaces.managed as managed
import pcb_assistant.router as router
from tests.phase6_test_fixtures import MAIL_ID, SyntheticManagedCatalogTestCase


CHAT_ENV = {
    "PCB_ALLOW_CLOUD_EGRESS": "true",
    "PCB_EGRESS_SCOPE": "sanitized_catalog",
    "PCB_AGENT_LOOP": "true",
    "PCB_LLM_PROVIDER": "openai_chat_completions",
    "PCB_LLM_BASE_URL": "https://api.example.com/v1",
    "PCB_LLM_MODEL": "deepseek-chat",
    "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY": "test-secret-agent-key-must-not-leak",
}


class AllowAll:
    def allow(self):
        return True


def _structured_json(citation_id, *, summary="依据检索摘录，白斑需结合材料与压合记录核对。"):
    return json.dumps({
        "status": "answered",
        "assessment": "hypothesis",
        "summary": summary,
        "summary_citation_ids": [citation_id],
        "possible_causes": [{
            "cause": "PP 受潮或固化条件异常需要结合现场记录核对",
            "citation_ids": [citation_id],
        }],
        "checks": [{
            "step": "复核本批材料烘烤与压合曲线记录",
            "purpose": "确认现场参数是否与历史案例线索一致",
            "citation_ids": [citation_id],
        }],
        "missing_information": ["材料批次与切片结果"],
        "limitations": ["摘录仅供排查参考，不作为放行依据"],
    }, ensure_ascii=False)


class ScriptedToolProvider:
    """Offline mock Chat Completions provider executing scripted tool calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_messages(self, messages, *, tools=None, tool_choice=None,
                          response_format=None, deadline=None):
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
            "response_format": response_format,
        })
        handler = self.script.pop(0)
        return handler(messages, tools)


class AgentRuntimeTests(SyntheticManagedCatalogTestCase):
    def _local_fallback(self, question="压合后板边出现白斑，怎么排查？"):
        return managed.answer(
            {"question": question, "mode": "evidence_only"},
            "REQ_local_base",
            catalog_directory=self.catalog,
        )

    def test_native_agent_multi_step_search_and_attachment_trace(self):
        def step1(_messages, _tools):
            return {
                "message": {"role": "assistant", "content": ""},
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search_evidence",
                        "arguments": json.dumps({"query": "白斑 压合", "limit": 3}),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
            }

        def step2(_messages, _tools):
            return {
                "message": {"role": "assistant", "content": ""},
                "content": "",
                "tool_calls": [{
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "get_related_attachments",
                        "arguments": json.dumps({"doc_id": MAIL_ID, "query": "白斑"}),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 60, "output_tokens": 25, "total_tokens": 85},
            }

        def step3(messages, _tools):
            tool_payload = json.loads(messages[-1]["content"])
            citation = tool_payload["excerpts"][0]["excerpt_id"]
            return {
                "message": {"role": "assistant", "content": _structured_json(citation)},
                "content": _structured_json(citation),
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {"input_tokens": 90, "output_tokens": 40, "total_tokens": 130},
            }

        provider = ScriptedToolProvider([step1, step2, step3])
        log = StringIO()
        with redirect_stderr(log):
            outcome = agent_runtime.run_agent(
                {"question": "压合后板边出现白斑，怎么排查？", "mode": "managed"},
                "REQ_agent_multi",
                fallback_outcome=self._local_fallback(),
                route="knowledge",
                catalog_directory=self.catalog,
                environ=CHAT_ENV,
                provider_factory=lambda _cfg: provider,
                rate_limiter=AllowAll(),
            )
        self.assertEqual(outcome.status, "answered")
        self.assertTrue(outcome.external_services)
        self.assertEqual(outcome.data_egress, "sanitized_evidence")
        self.assertEqual(outcome.result["answer_mode"], "managed_agent")
        self.assertEqual(outcome.result["citation_validation"]["status"], "passed")
        self.assertEqual(outcome.result["model"]["usage"]["total_tokens"], 285)
        self.assertEqual(
            [step["tool"] for step in outcome.result["agent_trace"]],
            ["search_evidence", "get_related_attachments"],
        )
        self.assertEqual(
            outcome.result["result_fingerprint"],
            catalog_chat.fingerprint_chat_result(outcome.result),
        )
        self.assertNotIn(CHAT_ENV["DEEPSEEK_API_KEY"], log.getvalue())

    def test_router_dispatches_diagnosis_to_agent_when_opt_in(self):
        def step1(_messages, _tools):
            return {
                "message": {"role": "assistant", "content": ""},
                "content": "",
                "tool_calls": [{
                    "id": "call_diag",
                    "type": "function",
                    "function": {
                        "name": "inspect_diagnosis",
                        "arguments": json.dumps({
                            "question": "8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？"
                        }),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 40, "output_tokens": 15, "total_tokens": 55},
            }

        def step2(messages, _tools):
            tool_payload = json.loads(messages[-1]["content"])
            hyp = next(h for h in tool_payload["hypotheses"]
                       if h["cause_id"] == "vacuum_pressure_loss")
            self.assertTrue(hyp["contradicting_observations"])
            citation = tool_payload["excerpts"][0]["excerpt_id"]
            return {
                "message": {"role": "assistant", "content": _structured_json(citation)},
                "content": _structured_json(citation),
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {"input_tokens": 80, "output_tokens": 30, "total_tokens": 110},
            }

        provider = ScriptedToolProvider([step1, step2])
        with mock.patch.dict("os.environ", CHAT_ENV, clear=False), \
                mock.patch.object(agent_runtime.managed, "create_provider", return_value=provider), \
                mock.patch.object(agent_runtime.managed, "_RATE_LIMITER", AllowAll()), \
                redirect_stderr(StringIO()):
            outcome = router.answer(
                {
                    "question": "8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？",
                    "mode": "managed",
                },
                "REQ_router_agent",
                catalog_directory=self.catalog,
            )
        self.assertEqual(outcome.status, "answered")
        self.assertEqual(outcome.result["workflow"]["route"], "diagnosis")
        self.assertTrue(outcome.result["workflow"]["agent_loop"])
        self.assertEqual(outcome.result["answer_mode"], "managed_agent")
        self.assertEqual(outcome.result["citation_validation"]["status"], "passed")

    def test_agent_falls_back_on_forged_citation_or_uncited_number(self):
        for kind, summary, forged in (
            ("invalid_citation", "依据资料核对。", True),
            ("invalid_schema", "建议升温到 260℃ 进行固化。", False),
        ):
            with self.subTest(kind=kind):
                def step1(_messages, _tools):
                    return {
                        "message": {"role": "assistant", "content": ""},
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_evidence",
                                "arguments": json.dumps({"query": "白斑"}),
                            },
                        }],
                        "finish_reason": "tool_calls",
                        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    }

                def step2(messages, _tools, *, summary_text=summary, use_forged=forged):
                    payload = json.loads(messages[-1]["content"])
                    citation = ("EXC_ffffffffffffffffffffffff"
                                if use_forged else payload["excerpts"][0]["excerpt_id"])
                    content = _structured_json(citation, summary=summary_text)
                    return {
                        "message": {"role": "assistant", "content": content},
                        "content": content,
                        "tool_calls": [],
                        "finish_reason": "stop",
                        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    }

                provider = ScriptedToolProvider([step1, step2])
                with redirect_stderr(StringIO()):
                    outcome = agent_runtime.run_agent(
                        {"question": "压合后出现白斑，怎么排查？", "mode": "managed"},
                        "REQ_agent_bad_" + kind,
                        fallback_outcome=self._local_fallback(),
                        catalog_directory=self.catalog,
                        environ=CHAT_ENV,
                        provider_factory=lambda _cfg: provider,
                        rate_limiter=AllowAll(),
                    )
                self.assertEqual(outcome.status, "fallback")
                self.assertEqual(outcome.result["model"]["fallback_reason"], kind)
                self.assertFalse(outcome.result["generated_by_model"])

    def test_agent_enforces_max_steps_and_forces_final_answer(self):
        def tool_step(_messages, _tools):
            return {
                "message": {"role": "assistant", "content": ""},
                "content": "",
                "tool_calls": [{
                    "id": "call_loop",
                    "type": "function",
                    "function": {
                        "name": "search_evidence",
                        "arguments": json.dumps({"query": "白斑", "limit": 2}),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        def final_step(messages, tools):
            self.assertIsNone(tools)
            tool_msg = next(m for m in reversed(messages) if m["role"] == "tool")
            citation = json.loads(tool_msg["content"])["excerpts"][0]["excerpt_id"]
            content = _structured_json(citation)
            return {
                "message": {"role": "assistant", "content": content},
                "content": content,
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        provider = ScriptedToolProvider([tool_step, final_step])
        env = {**CHAT_ENV, "PCB_AGENT_MAX_STEPS": "2"}
        with redirect_stderr(StringIO()):
            outcome = agent_runtime.run_agent(
                {"question": "压合后出现白斑，怎么排查？", "mode": "managed"},
                "REQ_agent_cap",
                fallback_outcome=self._local_fallback(),
                catalog_directory=self.catalog,
                environ=env,
                provider_factory=lambda _cfg: provider,
                rate_limiter=AllowAll(),
            )
        self.assertEqual(outcome.status, "answered")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(outcome.result["model"]["tool_calls"], 1)

    def test_agent_blocks_when_privacy_models_missing_without_calling_provider(self):
        provider = mock.Mock()
        with mock.patch.object(managed, "_SANITIZER_COMPONENTS", None), \
                mock.patch("spacy.util.get_installed_models", return_value=[]), \
                redirect_stderr(StringIO()):
            outcome = agent_runtime.run_agent(
                {"question": "压合后出现白斑，怎么排查？", "mode": "managed"},
                "REQ_agent_noprivacy",
                fallback_outcome=self._local_fallback(),
                catalog_directory=self.catalog,
                environ=CHAT_ENV,
                provider_factory=provider,
                rate_limiter=AllowAll(),
            )
        self.assertEqual(outcome.status, "fallback")
        self.assertEqual(outcome.result["model"]["fallback_reason"], "policy_blocked")
        self.assertFalse(outcome.external_services)
        self.assertEqual(outcome.data_egress, "none")
        provider.assert_not_called()

    def test_agent_degrades_gracefully_when_pydantic_ai_not_installed(self):
        def step1(_messages, _tools):
            return {
                "message": {"role": "assistant", "content": ""},
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search_evidence",
                        "arguments": json.dumps({"query": "白斑"}),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": {},
            }

        def step2(messages, _tools):
            citation = json.loads(messages[-1]["content"])["excerpts"][0]["excerpt_id"]
            content = _structured_json(citation)
            return {
                "message": {"role": "assistant", "content": content},
                "content": content,
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {},
            }

        provider = ScriptedToolProvider([step1, step2])
        env = {**CHAT_ENV, "PCB_AGENT_BACKEND": "pydantic_ai"}
        with mock.patch.object(agent_runtime, "_run_pydantic_ai_loop", side_effect=ImportError("no pydantic_ai")), \
                redirect_stderr(StringIO()):
            outcome = agent_runtime.run_agent(
                {"question": "压合后出现白斑，怎么排查？", "mode": "managed"},
                "REQ_agent_degrade",
                fallback_outcome=self._local_fallback(),
                catalog_directory=self.catalog,
                environ=env,
                provider_factory=lambda _cfg: provider,
                rate_limiter=AllowAll(),
            )
        self.assertEqual(outcome.status, "answered")
        self.assertEqual(outcome.result["model"]["agent_backend"], "native")

    def test_public_only_scope_restricts_toolbox_to_public_sources(self):
        guard = managed.EgressGuard(scope="public_only")
        toolbox = agent_runtime.AgentToolbox(self.catalog, guard)
        mail_result = toolbox.get_related_attachments(MAIL_ID, query="白斑")
        self.assertEqual(mail_result["excerpts"], [])
        pub_result = toolbox.search_evidence("白斑", source="mail")
        self.assertTrue(all(item["source_type"] == "public_reference"
                            for item in pub_result["excerpts"]))


if __name__ == "__main__":
    unittest.main()
