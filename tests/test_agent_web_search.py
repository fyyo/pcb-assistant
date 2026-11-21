"""Checks for opt-in public search, failure fallback and HTTP transport."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from pcb_assistant.catalog.common import CatalogError

from agent.cli import main
from agent.models import fingerprint_result
from agent.providers import ExternalSearchAdapter
from agent.web_search import SearXNGSearchAdapter
from agent.workflow import diagnose
from tests.test_agent_workflow import FakeRetriever, evidence_item


def response_payload():
    return {"results": [{
        "title": "Vacuum guidance",
        "content": "Vacuum pressure loss can cause voids during PCB lamination.",
        "url": "https://example.test/vacuum",
        "score": 0.9,
    }]}


@contextmanager
def local_search_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            body = json.dumps(response_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AgentWebSearchTests(unittest.TestCase):
    def test_default_cli_never_connects_even_with_endpoint_configured(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"PCB_AGENT_SEARXNG_URL": "http://127.0.0.1:9999"}):
            with patch("agent.web_search.build_opener") as opener:
                with patch("agent.retriever.CatalogRetriever.search", return_value=[]):
                    with redirect_stdout(buffer):
                        code = main(["diagnose", "--question", "压合后空洞", "--json"])
        self.assertEqual(code, 0)
        opener.assert_not_called()
        result = json.loads(buffer.getvalue())
        self.assertFalse(result["retrieval"]["external_search_enabled"])
        self.assertEqual(result["retrieval"]["external_queries_sent"], 0)
        self.assertFalse(result["boundaries"]["external_services"])

    def test_local_evidence_prevents_public_requests(self):
        provider = SearXNGSearchAdapter("http://127.0.0.1:9999")
        text = "真空压力、受潮水分、树脂流动流胶、树脂量铜厚和异物污染会造成空洞。"
        retriever = FakeRetriever((("空洞", [evidence_item("EXC_local", text)]),))
        with patch("agent.web_search.build_opener") as opener:
            result = diagnose(
                {"question": "压合后空洞"}, retriever=retriever,
                external_search=provider, allow_external_search=True,
            )
        opener.assert_not_called()
        self.assertEqual(result["retrieval"]["external_queries_sent"], 0)
        self.assertTrue(result["citations"])

    def test_real_http_cli_round_trip_sends_only_taxonomy_and_preserves_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "case.json"
            case.write_text(json.dumps({
                "question": "压合后空洞，客户 SECRET-CUSTOMER。",
                "batch": "SECRET-BATCH", "material": "SECRET-MATERIAL",
                "stackup": "SECRET-STACKUP", "equipment": "SECRET-MACHINE",
                "parameters": {"vacuum": "SECRET-PARAMETER"},
                "notes": "SECRET-NOTE",
            }, ensure_ascii=False), encoding="utf-8")
            with local_search_server() as (endpoint, calls):
                buffer = io.StringIO()
                with patch.dict(os.environ, {"PCB_AGENT_SEARXNG_URL": endpoint}):
                    with patch("agent.retriever.CatalogRetriever.search", return_value=[]):
                        with redirect_stdout(buffer):
                            code = main([
                                "diagnose", "--input", str(case),
                                "--external-search", "searxng", "--json",
                            ])
        self.assertEqual(code, 0)
        result = json.loads(buffer.getvalue())
        self.assertTrue(calls)
        self.assertNotIn("SECRET", " ".join(calls))
        self.assertEqual(len(calls), result["retrieval"]["external_queries_sent"])
        for request_path in calls:
            parsed = urlsplit(request_path)
            self.assertEqual(parsed.path, "/search")
            query = parse_qs(parsed.query)
            self.assertEqual(set(query), {"q", "format"})
            self.assertEqual(query["format"], ["json"])
            self.assertIn("PCB lamination", query["q"][0])
        self.assertIn("searxng", result["retrieval"]["providers_used"])
        self.assertEqual(result["retrieval"]["provider_errors"], [])
        self.assertEqual(result["boundaries"]["data_egress"], "defect_and_cause_terms_only")
        self.assertFalse(result["boundaries"]["engineering_conclusion_supported"])
        self.assertTrue(result["citations"])
        citation = result["citations"][0]
        self.assertTrue(citation["citation_id"].startswith("EXC_WEB_"))
        self.assertEqual(citation["source_tier"], "C")
        self.assertEqual(citation["evidence_status"], "unverified_external")
        self.assertEqual(citation["url"], "https://example.test/vacuum")
        self.assertIn(citation["url"], result["answer"])
        self.assertIn("未经独立验证", result["answer"])
        self.assertEqual(result["result_fingerprint"], fingerprint_result(result))

    def test_endpoint_and_timeout_validation_does_not_connect(self):
        with patch("agent.web_search.build_opener") as opener:
            for endpoint in (
                "", "file:///tmp/search", "https://", "https://user:password@example.test",
                "https://example.test/?key=SECRET", "https://example.test/#fragment",
                "http://[broken", "http://example.test:invalid", "https://example.test/a\nb",
            ):
                with self.subTest(endpoint=endpoint):
                    with self.assertRaisesRegex(CatalogError, "invalid_searxng_url"):
                        SearXNGSearchAdapter(endpoint)
            for timeout in (True, 0, -1, 31, float("nan"), float("inf"), "5"):
                with self.subTest(timeout=timeout):
                    with self.assertRaisesRegex(CatalogError, "invalid_search_timeout"):
                        SearXNGSearchAdapter("https://example.test", timeout=timeout)
        opener.assert_not_called()
        self.assertEqual(SearXNGSearchAdapter("https://example.test/prefix/").endpoint,
                         "https://example.test/prefix/search")
        self.assertEqual(SearXNGSearchAdapter("https://example.test/prefix/search/").endpoint,
                         "https://example.test/prefix/search")

    def test_cli_reports_missing_configuration_without_running_diagnosis(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with patch("agent.cli.diagnose") as diagnosis:
                with redirect_stderr(buffer):
                    code = main(["diagnose", "--question", "压合后空洞", "--external-search", "searxng"])
        self.assertEqual(code, 2)
        diagnosis.assert_not_called()
        self.assertIn("searxng_url_required", buffer.getvalue())

    def test_timeout_http_errors_and_bad_json_have_stable_errors(self):
        provider = SearXNGSearchAdapter("https://example.test", timeout=1.5)
        for failure in (
            TimeoutError("SECRET-TIMEOUT"), URLError("SECRET-URL"),
            HTTPError("https://SECRET.test", 403, "SECRET-HTTP", {}, None),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch("agent.web_search.build_opener") as opener:
                    opener.return_value.open.side_effect = failure
                    with self.assertRaises(CatalogError) as raised:
                        provider.search("PCB lamination vacuum")
                    self.assertEqual(str(raised.exception), "external_search_failed")
        for raw in (b"not json", b'[]', b'{"results": {}}', b'{}'):
            with self.subTest(raw=raw):
                with patch("agent.web_search.build_opener") as opener:
                    opener.return_value.open.return_value.__enter__.return_value.read.return_value = raw
                    with self.assertRaisesRegex(CatalogError, "external_search_failed"):
                        provider.search("PCB lamination vacuum")

    def test_response_size_is_bounded_and_timeout_is_used(self):
        with patch("agent.web_search.MAX_RESPONSE_BYTES", 20):
            with patch("agent.web_search.build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.return_value = b"x" * 21
                provider = SearXNGSearchAdapter("https://example.test", timeout=2)
                with self.assertRaisesRegex(CatalogError, "external_search_failed"):
                    provider.search("PCB lamination vacuum")
                response.read.assert_called_once_with(21)
                self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 2)

    def test_provider_failure_keeps_local_evidence_and_explains_fallback(self):
        provider = SearXNGSearchAdapter("https://example.test")
        local = FakeRetriever((("受潮", [evidence_item("EXC_local", "PP 受潮会造成白斑。")]),))
        with patch("agent.web_search.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("SECRET-ENDPOINT")
            result = diagnose(
                {"question": "压合后白斑"}, retriever=local,
                external_search=provider, allow_external_search=True,
            )
        self.assertIn("EXC_local", {item["citation_id"] for item in result["citations"]})
        self.assertIn("external_search_failed", result["retrieval"]["provider_errors"])
        self.assertIn("公开检索请求失败", result["answer"])
        self.assertNotIn("SECRET-ENDPOINT", json.dumps(result))

    def test_malformed_result_urls_do_not_discard_valid_results(self):
        urls = ["http://[broken", "http://user:password@example.test", "javascript:alert(1)",
                "https://example.test/a\nb", "https://example.test:invalid", "https://example.test/valid"]
        provider = ExternalSearchAdapter(lambda query, limit: [
            {"title": "Vacuum", "snippet": "Vacuum pressure loss causes voids.", "url": url}
            for url in urls
        ])
        results = provider.search("PCB lamination", limit=10)
        self.assertEqual([item["url"] for item in results], [urls[-1]])

    def test_redirect_is_not_followed(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            provider = SearXNGSearchAdapter(f"http://127.0.0.1:{server.server_port}")
            with self.assertRaisesRegex(CatalogError, "external_search_failed"):
                provider.search("PCB lamination vacuum")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("/redirect-target", calls)


if __name__ == "__main__":
    unittest.main()
