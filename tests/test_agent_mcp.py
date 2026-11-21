"""The legacy module launches the same single-entry MCP service."""
import io
import json
import unittest
from unittest.mock import patch

from agent import mcp
import phase6_mcp as unified


class LaminationMcpTests(unittest.TestCase):
    def test_legacy_launch_uses_the_same_protocol_implementation(self):
        self.assertIs(mcp.dispatch, unified.dispatch)
        self.assertIs(mcp.serve, unified.serve)
        self.assertIs(mcp.main, unified.main)
        self.assertEqual([item["name"] for item in mcp.list_tools()], ["pcb_answer"])

    def test_call_returns_the_unified_read_only_envelope(self):
        response = mcp.dispatch({"jsonrpc": "2.0", "id": "diag-1", "method": "tools/call",
            "params": {"name": "pcb_answer", "arguments": {"question": "压合后有问题，怎么解决？"}}})
        result = response["result"]
        envelope = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertEqual(envelope["contract_version"], "pcb-agent-v1")
        self.assertEqual(envelope["status"], "needs_clarification")
        self.assertTrue(envelope["read_only"])
        self.assertFalse(envelope["external_services"])
        self.assertEqual(envelope["data_egress"], "none")

    def test_invalid_arguments_stop_before_the_assistant(self):
        with patch("pcb_assistant.answer") as assistant:
            response = mcp.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "pcb_answer", "arguments": {"question": "", "path": "secret"}}})
        self.assertTrue(response["result"]["isError"])
        assistant.assert_not_called()

    def test_serve_handles_json_lines(self):
        source = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        target = io.StringIO()
        mcp.serve(source, target)
        self.assertEqual(json.loads(target.getvalue())["result"], {})


if __name__ == "__main__":
    unittest.main()
