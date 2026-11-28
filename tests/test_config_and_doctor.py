"""Unit tests for unified config precedence, multi-profile store, probe, and doctor."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

import pcb_assistant.catalog.cli as catalog_cli
import pcb_assistant.config as runtime_config
import pcb_assistant.interfaces.mcp as phase6_mcp
from tests.phase6_test_fixtures import SyntheticCatalogTestCase


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class UnifiedConfigAndDoctorTests(SyntheticCatalogTestCase):
    def test_precedence_base_env_over_dotenv_over_named_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dotenv = root / ".env"
            dotenv.write_text(
                "# comment\n"
                "PCB_LLM_MODEL=dotenv-model\n"
                "export PCB_EGRESS_SCOPE='sanitized_catalog'\n"
                "CUSTOM_GW_KEY=\"dotenv-key-value\"\n"
                "PCB_LLM_API_KEY_ENV=CUSTOM_GW_KEY\n"
                "UNRELATED_SECRET=ignored\n",
                encoding="utf-8",
            )
            profile = root / "agent_profile.json"
            runtime_config.save_profile_settings(profile, {
                "profile_name": "gw1",
                "label": "网关一",
                "PCB_ALLOW_CLOUD_EGRESS": True,
                "PCB_EGRESS_SCOPE": "public_only",
                "PCB_LLM_PROVIDER": "openai_chat_completions",
                "PCB_LLM_BASE_URL": "https://gw1.example.com/v1",
                "PCB_LLM_MODEL": "gw1-model",
                "PCB_LLM_API_KEY_ENV": "CUSTOM_GW_KEY",
                "api_key": "profile-key-value",
            })
            runtime_config.save_profile_settings(profile, {
                "profile_name": "ollama",
                "label": "本机 Ollama",
                "PCB_ALLOW_CLOUD_EGRESS": True,
                "PCB_ALLOW_LOCALHOST_HTTP": True,
                "PCB_LLM_PROVIDER": "openai_chat_completions",
                "PCB_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                "PCB_LLM_MODEL": "qwen2.5:14b",
                "set_active": False,
            })

            view = runtime_config.safe_profile_view(profile, base_env={})
            self.assertEqual(view["active_profile"], "gw1")
            self.assertEqual(len(view["profiles"]), 2)
            self.assertNotIn("profile-key-value", json.dumps(view))

            # Precedence check: base_env > .env > profile
            merged, loaded = runtime_config.load_runtime_env(
                profile,
                dotenv_path=dotenv,
                base_env={"PCB_EGRESS_SCOPE": "public_only"},
            )
            self.assertTrue(loaded)
            self.assertEqual(merged["PCB_EGRESS_SCOPE"], "public_only")
            self.assertEqual(merged["PCB_LLM_MODEL"], "dotenv-model")
            self.assertEqual(merged["CUSTOM_GW_KEY"], "dotenv-key-value")
            self.assertEqual(merged["PCB_LLM_BASE_URL"], "https://gw1.example.com/v1")
            self.assertNotIn("UNRELATED_SECRET", merged)

            # Activate second profile and delete first profile
            switched = runtime_config.save_profile_settings(
                profile, {"action": "activate", "profile_name": "ollama"}
            )
            self.assertEqual(switched["active_profile"], "ollama")
            self.assertTrue(switched["PCB_ALLOW_LOCALHOST_HTTP"])

            after_del = runtime_config.save_profile_settings(
                profile, {"action": "delete", "profile_name": "ollama"}
            )
            self.assertEqual(after_del["active_profile"], "gw1")

    def test_probe_llm_connection_validates_and_classifies_http_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "agent_profile.json"
            missing = runtime_config.probe_llm_connection(
                profile,
                overrides={"PCB_LLM_BASE_URL": "https://api.example.com/v1", "PCB_LLM_MODEL": "m1"},
            )
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["error_code"], "missing_api_key")

            http_blocked = runtime_config.probe_llm_connection(
                profile,
                overrides={
                    "PCB_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                    "PCB_LLM_MODEL": "qwen2.5",
                    "PCB_ALLOW_LOCALHOST_HTTP": False,
                },
            )
            self.assertFalse(http_blocked["ok"])
            self.assertEqual(http_blocked["error_code"], "invalid_base_url")

            ok_res = runtime_config.probe_llm_connection(
                profile,
                overrides={
                    "PCB_LLM_BASE_URL": "https://api.example.com/v1",
                    "PCB_LLM_MODEL": "demo-chat",
                    "api_key": "sk-secret-probe-key-123456",
                },
                opener=lambda _req, timeout=10: _FakeHTTPResponse({"model": "demo-chat-01"}),
            )
            self.assertTrue(ok_res["ok"])
            self.assertEqual(ok_res["model"], "demo-chat-01")
            self.assertNotIn("sk-secret-probe-key-123456", json.dumps(ok_res))

            def _raise_404(_req, timeout=10):
                raise HTTPError("https://api.example.com/chat/completions", 404, "Not Found", {}, None)

            err_404 = runtime_config.probe_llm_connection(
                profile,
                overrides={
                    "PCB_LLM_BASE_URL": "https://api.example.com",
                    "PCB_LLM_MODEL": "demo-chat",
                    "api_key": "sk-secret-probe-key-123456",
                },
                opener=_raise_404,
            )
            self.assertFalse(err_404["ok"])
            self.assertEqual(err_404["error_code"], "endpoint_or_model_not_found")
            self.assertIn("/v1", err_404["message"])

    def test_doctor_and_mcp_cli_profile_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "agent_profile.json"
            runtime_config.save_profile_settings(profile, {
                "profile_name": "prod",
                "PCB_ALLOW_CLOUD_EGRESS": True,
                "PCB_LLM_BASE_URL": "https://api.example.com/v1",
                "PCB_LLM_MODEL": "prod-model",
                "PCB_LLM_API_KEY_ENV": "PROD_TEST_KEY",
                "api_key": "sk-prod-secret-value-999999",
            })
            report = runtime_config.run_doctor(
                catalog_directory=self.catalog,
                profile_path=profile,
                dotenv_path=Path(tmp) / ".env",
            )
            self.assertIn(report["status"], ("ok", "warn"))
            by_name = {c["name"]: c for c in report["checks"]}
            self.assertEqual(by_name["catalog_database"]["status"], "ok")
            self.assertEqual(by_name["llm_profile"]["status"], "ok")
            self.assertNotIn("sk-prod-secret-value-999999", json.dumps(report))

            # Verify MCP CLI accepts --agent-config and --profile cleanly without leaking env after exit
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = phase6_mcp.main([
                    "--catalog", str(self.catalog),
                    "--agent-config", str(profile),
                    "--profile", "prod",
                    "--self-test",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "ok")

            # Verify CLI doctor subcommand JSON output
            doc_buf = io.StringIO()
            with unittest.mock.patch("sys.argv", [
                "knowledge_catalog.py", "--catalog", str(self.catalog),
                "doctor", "--json", "--agent-config", str(profile),
            ]), redirect_stdout(doc_buf):
                doc_code = catalog_cli.main()
            self.assertEqual(doc_code, 0)
            self.assertEqual(json.loads(doc_buf.getvalue())["schema"], "pcb-doctor-v1")


if __name__ == "__main__":
    unittest.main()
