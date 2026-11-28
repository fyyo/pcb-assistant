"""Offline unit tests for the local Web workbench UI, REST API, and /mcp HTTP endpoint."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pcb_assistant.catalog.common import CatalogError
import pcb_assistant.interfaces.web_workbench as web_workbench
from tests.phase6_test_fixtures import SyntheticCatalogTestCase


def _http_json(url: str, *, method: str = "GET", body: dict | None = None,
               headers: dict | None = None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    req = Request(url, data=data, headers=req_headers, method=method)
    with urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class WebWorkbenchTests(SyntheticCatalogTestCase):
    def _start_server(self, tmpdir: Path, *, toolset="agent", api_token=None):
        server = web_workbench.create_server(
            host="127.0.0.1",
            port=0,
            catalog_directory=self.catalog,
            cases_dir=tmpdir / "cases",
            profile_path=tmpdir / "agent_profile.json",
            toolset=toolset,
            api_token=api_token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        return server, f"http://{host}:{port}"

    def test_html_health_profile_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, base = self._start_server(Path(tmp))
            try:
                with urlopen(base + "/", timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("PCB 压合排查工作台", html)

                status, health = _http_json(base + "/api/health")
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["catalog_version"], self.catalog_version)
                self.assertEqual(health["toolset"], "agent")

                status, saved = _http_json(
                    base + "/api/profile",
                    method="POST",
                    body={
                        "PCB_ALLOW_CLOUD_EGRESS": True,
                        "PCB_EGRESS_SCOPE": "sanitized_catalog",
                        "PCB_LLM_PROVIDER": "openai_chat_completions",
                        "PCB_LLM_BASE_URL": "https://api.example.com/v1",
                        "PCB_LLM_MODEL": "demo-model",
                        "PCB_LLM_API_KEY_ENV": "DEMO_KEY",
                        "api_key": "sk-super-secret-value-1234567890",
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(saved["has_api_key"])
                self.assertNotIn("sk-super-secret-value-1234567890", json.dumps(saved))

                # Updating model without resending api_key must preserve saved api_key.
                status, updated = _http_json(
                    base + "/api/profile",
                    method="POST",
                    body={"PCB_LLM_MODEL": "demo-model-v2"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(updated["PCB_LLM_MODEL"], "demo-model-v2")
                self.assertTrue(updated["has_api_key"])

                # Multi-profile creation, switching, doctor, mcp-config, and single-instance probe.
                status, created_p2 = _http_json(
                    base + "/api/profile",
                    method="POST",
                    body={
                        "profile_name": "local_ollama",
                        "label": "Ollama Local",
                        "PCB_ALLOW_CLOUD_EGRESS": True,
                        "PCB_ALLOW_LOCALHOST_HTTP": True,
                        "PCB_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                        "PCB_LLM_MODEL": "qwen2.5:14b",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(created_p2["active_profile"], "local_ollama")
                self.assertEqual(len(created_p2["profiles"]), 2)

                status, view_def = _http_json(base + "/api/profile?name=default")
                self.assertEqual(status, 200)
                self.assertEqual(view_def["PCB_LLM_MODEL"], "demo-model-v2")

                status, probe_res = _http_json(
                    base + "/api/profile/test",
                    method="POST",
                    body={"PCB_LLM_BASE_URL": "", "PCB_LLM_MODEL": ""},
                )
                self.assertEqual(status, 200)
                self.assertFalse(probe_res["ok"])

                status, doc_res = _http_json(base + "/api/doctor")
                self.assertEqual(status, 200)
                self.assertEqual(doc_res["schema"], "pcb-doctor-v1")

                status, mcp_cfg = _http_json(base + "/api/mcp-config")
                self.assertEqual(status, 200)
                self.assertIn("recommended_mcpServers_using_local_profile", mcp_cfg)
                self.assertIn("streamable_http_mcp", mcp_cfg)

                host, port = server.server_address[:2]
                self.assertTrue(web_workbench._probe_existing_workbench(host, port))
                reuse_code = web_workbench.main([
                    "--host", host,
                    "--port", str(port),
                    "--reuse-running",
                ])
                self.assertEqual(reuse_code, 0)
            finally:
                server.shutdown()
                server.server_close()

    def test_case_lifecycle_answer_notes_and_markdown_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, base = self._start_server(Path(tmp))
            try:
                status, created = _http_json(
                    base + "/api/cases", method="POST", body={"case_id": "WEB_CASE_01"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(created["case_id"], "WEB_CASE_01")

                status, ans = _http_json(
                    base + "/api/answer",
                    method="POST",
                    body={
                        "question": "压合后板边出现白斑，真空曲线正常，怎么排查？",
                        "mode": "local",
                        "case_id": "WEB_CASE_01",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(ans["status"], "answered")
                self.assertEqual(ans["data_egress"], "none")
                self.assertIn("已记录的正常观测", ans["result"]["answer"])

                status, note = _http_json(
                    base + "/api/cases/WEB_CASE_01/notes",
                    method="POST",
                    body={"text": "切片确认层间有微小白点"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(note["text"], "切片确认层间有微小白点")

                with urlopen(base + "/api/cases/WEB_CASE_01/report", timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    md = resp.read().decode("utf-8")
                    self.assertIn("WEB_CASE_01", md)
                    self.assertIn("切片确认层间有微小白点", md)

                status, cleared = _http_json(
                    base + "/api/cases/WEB_CASE_01/clear", method="POST", body={}
                )
                self.assertEqual(status, 200)
                self.assertEqual(cleared["status"], "cleared")
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_streamable_http_endpoint_supports_tools_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, base = self._start_server(Path(tmp), toolset="agent")
            try:
                status, meta = _http_json(base + "/mcp")
                self.assertEqual(status, 200)
                self.assertEqual(meta["transport"], "streamable-http")

                status, init = _http_json(
                    base + "/mcp",
                    method="POST",
                    body={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(init["result"]["serverInfo"]["name"], "pcb-knowledge")

                status, listed = _http_json(
                    base + "/mcp",
                    method="POST",
                    body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(listed["result"]["tools"]), 8)

                status, called = _http_json(
                    base + "/mcp",
                    method="POST",
                    body={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "pcb_stats", "arguments": {}},
                    },
                )
                self.assertEqual(status, 200)
                self.assertFalse(called["result"]["isError"])
                self.assertEqual(called["result"]["structuredContent"]["status"], "ok")

                status, _ = _http_json(
                    base + "/mcp",
                    method="POST",
                    body={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                )
                self.assertEqual(status, 202)
            finally:
                server.shutdown()
                server.server_close()

    def test_non_loopback_requires_token_and_bearer_auth_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError):
                web_workbench.create_server(
                    host="0.0.0.0",
                    port=0,
                    catalog_directory=self.catalog,
                    cases_dir=Path(tmp) / "cases",
                    profile_path=Path(tmp) / "agent_profile.json",
                    api_token="",
                )
            server, base = self._start_server(Path(tmp), api_token="secret-bearer-token")
            try:
                with self.assertRaises(HTTPError) as caught:
                    _http_json(base + "/api/health")
                self.assertEqual(caught.exception.code, 401)
                status, health = _http_json(
                    base + "/api/health",
                    headers={"Authorization": "Bearer secret-bearer-token"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ok")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
