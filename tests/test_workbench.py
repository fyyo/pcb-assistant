"""Offline synthetic tests for the multi-turn Agent workbench and Case workspace."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from pcb_assistant.catalog.common import CatalogError
import pcb_assistant.interfaces.workbench as workbench
from tests.phase6_test_fixtures import SyntheticCatalogTestCase


class WorkbenchTests(SyntheticCatalogTestCase):
    def test_load_local_agent_env_merges_profile_with_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing, loaded = workbench.load_local_agent_env(
                Path(tmp) / "missing.json", base_env={})
            self.assertFalse(loaded)
            self.assertEqual(missing, {})

            profile = Path(tmp) / "agent_profile.json"
            profile.write_text(json.dumps({
                "PCB_ALLOW_CLOUD_EGRESS": True,
                "PCB_EGRESS_SCOPE": "sanitized_catalog",
                "PCB_AGENT_LOOP": True,
                "PCB_LLM_PROVIDER": "openai_chat_completions",
                "PCB_LLM_BASE_URL": "https://api.example.com/v1",
                "PCB_LLM_MODEL": "profile-model",
                "PCB_LLM_API_KEY_ENV": "CUSTOM_KEY",
                "api_key": "profile-secret-value",
                "IGNORED_KEY": "must_not_load",
            }), encoding="utf-8")
            env, loaded = workbench.load_local_agent_env(
                profile,
                base_env={"PCB_LLM_MODEL": "env-override-model"},
            )
            self.assertTrue(loaded)
            self.assertEqual(env["PCB_ALLOW_CLOUD_EGRESS"], "true")
            self.assertEqual(env["PCB_EGRESS_SCOPE"], "sanitized_catalog")
            self.assertEqual(env["PCB_LLM_MODEL"], "env-override-model")
            self.assertEqual(env["CUSTOM_KEY"], "profile-secret-value")
            self.assertNotIn("IGNORED_KEY", env)

    def test_case_workspace_persists_turns_notes_and_exports_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_dir = Path(tmp) / "cases"
            ws = workbench.CaseWorkspace(cases_dir, catalog_directory=self.catalog)

            with self.assertRaises(CatalogError):
                workbench.CaseWorkspace(self.catalog / "cases", catalog_directory=self.catalog)
            with self.assertRaises(CatalogError):
                ws.open_or_create("../escape")

            case_data = ws.open_or_create("WS_TEST_01")
            outcome = workbench._ask_once(
                "压合后板边出现白斑，真空曲线正常，怎么排查？",
                [],
                mode="local",
                catalog_directory=self.catalog,
                env={},
            )
            ws.append_turn(case_data, "压合后板边出现白斑，真空曲线正常，怎么排查？", outcome)
            ws.append_note(case_data, "已复测真空曲线正常，切片待出。")

            reopened = ws.open_or_create("WS_TEST_01")
            self.assertEqual(len(reopened["turns"]), 1)
            self.assertEqual(len(reopened["history"]), 2)
            self.assertEqual(len(reopened["notes"]), 1)
            listed = ws.list_cases()
            self.assertEqual(listed[0]["case_id"], "WS_TEST_01")

            report_path = Path(tmp) / "draft.md"
            receipt = workbench.export_case_markdown(
                reopened, report_path, catalog_directory=self.catalog)
            self.assertEqual(receipt["status"], "exported")
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("WS_TEST_01", text)
            self.assertIn("已复测真空曲线正常", text)
            self.assertIn("不可直接作为放行或生产工艺参数", text)

            with self.assertRaises(CatalogError):
                workbench.export_case_markdown(
                    reopened, self.catalog / "bad.md", catalog_directory=self.catalog)

    def test_mcp_client_config_has_no_secrets_and_matches_toolset(self):
        cfg = workbench.mcp_client_config(self.catalog, toolset="agent")
        server = cfg["mcpServers"]["pcb-assistant"]
        self.assertIn("--toolset", server["args"])
        self.assertIn("agent", server["args"])
        self.assertIn("mcpo", cfg["mcpo_command_for_open_webui"])
        self.assertEqual(server["env"]["DEEPSEEK_API_KEY"], "<YOUR_API_KEY>")

    def test_workbench_cli_one_shot_local_mode_and_case_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_dir = Path(tmp) / "cases"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = workbench.main([
                    "--catalog", str(self.catalog),
                    "--mode", "local",
                    "--case", "CLI_CASE_01",
                    "--cases-dir", str(cases_dir),
                    "--json",
                    "什么是白斑？",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "answered")
            self.assertEqual(payload["data_egress"], "none")
            saved = json.loads((cases_dir / "CLI_CASE_01.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["turns"]), 1)


if __name__ == "__main__":
    unittest.main()
