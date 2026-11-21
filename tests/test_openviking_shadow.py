"""Isolated checks for the OpenViking shadow-retrieval adapter."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from pcb_assistant.catalog.build import build
from pcb_assistant.catalog.common import connect_readonly, digest_file
from scripts.experimental.openviking_shadow import (
    DEFAULT_TARGET_URI,
    ShadowError,
    _post_json,
    export_shadow_corpus,
    find_shadow,
    load_shadow_index,
    main,
    verify_shadow_export,
)
from .test_catalog import fixture


class OpenVikingShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-openviking-shadow-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.catalog = cls.root / "data/phase3_catalog_test"
        with redirect_stdout(io.StringIO()):
            build(cls.root, cls.catalog)

    def setUp(self):
        self.output = self.root / "shadow" / self._testMethodName

    def export_public(self, **kwargs):
        return export_shadow_corpus(self.catalog, self.output, **kwargs)

    def manifest(self):
        path = self.output / "manifest.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def body_for(self, doc_id):
        with closing(connect_readonly(self.catalog)) as connection:
            row = connection.execute(
                """SELECT COALESCE(c.normalized_text,c.original_text)
                   FROM documents d JOIN contents c USING(content_id)
                   WHERE d.doc_id=?""",
                (doc_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_public_export_is_read_only_and_only_public_sources(self):
        catalog_hash = digest_file(self.catalog / "catalog.sqlite3")
        summary = self.export_public(limit=20)
        self.assertEqual(digest_file(self.catalog / "catalog.sqlite3"), catalog_hash)
        self.assertEqual(summary["status"], "completed")
        self.assertGreater(summary["documents"], 0)
        self.assertEqual(summary["documents"], summary["written_documents"])
        self.assertFalse(summary["boundaries"]["catalog_modified"])
        self.assertFalse(summary["boundaries"]["openviking_contacted"])
        self.assertFalse(summary["boundaries"]["contains_internal_material"])

        rows = self.manifest()
        self.assertEqual(len(rows), summary["documents"])
        self.assertTrue(all(row["source_type"] == "public_reference" for row in rows))
        self.assertTrue(all(row["content_status"] == "ready" for row in rows))
        self.assertTrue(all((self.output / row["relative_path"]).is_file() for row in rows))
        self.assertTrue(all(row["uri"].startswith(DEFAULT_TARGET_URI + "/") for row in rows))
        self.assertNotIn("body_text", (self.output / "manifest.jsonl").read_text(encoding="utf-8"))

    def test_export_does_not_copy_body_text_into_manifest(self):
        summary = self.export_public(limit=1)
        self.assertEqual(summary["documents"], 1)
        row = self.manifest()[0]
        body = self.body_for(row["doc_id"])
        manifest_text = (self.output / "manifest.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(body, manifest_text)
        file_text = (self.output / row["relative_path"]).read_text(encoding="utf-8")
        self.assertIn(body.rstrip(), file_text)
        self.assertEqual(
            hashlib.sha256(file_text.encode("utf-8")).hexdigest(),
            row["shadow_file_sha256"],
        )

    def test_internal_scope_requires_explicit_allow(self):
        with self.assertRaises(ShadowError) as raised:
            export_shadow_corpus(
                self.catalog, self.output, scope="internal", limit=5,
            )
        self.assertEqual(str(raised.exception), "internal_scope_requires_explicit_allow")
        self.assertFalse(self.output.exists())

    def test_internal_export_includes_internal_sources_when_allowed(self):
        summary = export_shadow_corpus(
            self.catalog,
            self.output,
            scope="internal",
            limit=100,
            allow_internal=True,
        )
        self.assertTrue(summary["boundaries"]["contains_internal_material"])
        self.assertTrue(summary["boundaries"]["requires_data_egress_review"])
        source_types = {row["source_type"] for row in self.manifest()}
        self.assertIn("mail", source_types)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir(parents=True)
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ShadowError) as raised:
            self.export_public(limit=5)
        self.assertEqual(str(raised.exception), "output_directory_not_empty")
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_output_inside_catalog_is_rejected(self):
        with self.assertRaises(ShadowError) as raised:
            export_shadow_corpus(
                self.catalog, self.catalog / "shadow", limit=1,
            )
        self.assertEqual(str(raised.exception), "output_must_be_outside_catalog")

    def test_output_inside_protected_data_root_is_rejected(self):
        with self.assertRaises(ShadowError) as raised:
            export_shadow_corpus(
                self.catalog, self.catalog.parent / "shadow", limit=1,
            )
        self.assertEqual(str(raised.exception), "output_must_be_outside_catalog")

    def test_verify_passes_and_detects_tampering(self):
        self.export_public(limit=1)
        verified = verify_shadow_export(self.catalog, self.output)
        self.assertEqual(verified["status"], "passed")
        self.assertEqual(verified["verified"], 1)
        row = self.manifest()[0]
        path = self.output / row["relative_path"]
        path.write_text("tampered\n", encoding="utf-8")
        failed = verify_shadow_export(self.catalog, self.output)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("shadow_file_hash_mismatch",
                      {error["code"] for error in failed["errors"]})

    def test_find_maps_hit_and_does_not_return_generated_text(self):
        self.export_public(limit=5)
        row = self.manifest()[0]
        calls = {}

        def fake_request(url, payload, headers, timeout):
            calls.update({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
            return {
                "status": "ok",
                "result": {
                    "resources": [{
                        "uri": row["uri"],
                        "score": 0.87,
                        "level": 2,
                        "abstract": "SECRET GENERATED SUMMARY",
                        "content": "SECRET SOURCE TEXT",
                    }],
                    "total": 1,
                },
            }

        result = find_shadow(
            self.catalog,
            self.output,
            "压合 真空",
            api_key="test-key",
            request_json=fake_request,
        )
        self.assertEqual(result["status"], "mapped")
        self.assertEqual(result["mapped"], 1)
        self.assertEqual(result["results"][0]["doc_id"], row["doc_id"])
        self.assertNotIn("abstract", result["results"][0])
        self.assertNotIn("content", result["results"][0])
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
        self.assertEqual(calls["url"], "http://127.0.0.1:1933/api/v1/search/find")
        self.assertEqual(calls["headers"]["X-API-Key"], "test-key")
        self.assertEqual(calls["payload"]["level"], [2])
        self.assertFalse(calls["payload"]["read_content"])
        self.assertEqual(calls["payload"]["target_uri"], result["target_uri"])
        self.assertEqual(result["boundaries"]["request_transport"], "local_http")
        self.assertEqual(result["boundaries"]["data_egress"], "not_verified")
        self.assertNotIn("test-key", json.dumps(result, ensure_ascii=False))

    def test_find_accepts_direct_sdk_response_shape(self):
        self.export_public(limit=1)
        row = self.manifest()[0]

        def fake_request(url, payload, headers, timeout):
            return {"resources": [{"uri": row["uri"], "score": 0.4, "level": 2}], "total": 1}

        result = find_shadow(
            self.catalog, self.output, "query", request_json=fake_request,
        )
        self.assertEqual(result["status"], "mapped")
        self.assertEqual(result["results"][0]["doc_id"], row["doc_id"])

    def test_find_unmapped_hit_is_safe(self):
        self.export_public(limit=1)

        def fake_request(url, payload, headers, timeout):
            return {
                "status": "ok",
                "result": {
                    "resources": [{
                        "uri": "viking://resources/other/KNOWLEDGE_" + "0" * 24 + ".md",
                        "score": 0.9,
                        "level": 2,
                        "abstract": "SECRET",
                    }],
                    "total": 1,
                },
            }

        result = find_shadow(
            self.catalog, self.output, "query", request_json=fake_request,
        )
        self.assertEqual(result["status"], "no_matches")
        self.assertEqual(result["unmapped"], 1)
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))

    def test_find_rejects_manifest_doc_id_outside_shadow_uri_scope(self):
        self.export_public(limit=1)
        row = self.manifest()[0]

        def fake_request(url, payload, headers, timeout):
            return {
                "status": "ok",
                "result": {
                    "resources": [{
                        "uri": "viking://resources/other/" + row["doc_id"] + ".md",
                        "score": 0.9,
                        "level": 2,
                    }],
                    "total": 1,
                },
            }

        result = find_shadow(
            self.catalog, self.output, "query", request_json=fake_request,
        )
        self.assertEqual(result["status"], "no_matches")
        self.assertEqual(result["unmapped"], 1)
        self.assertEqual(result["unmapped_hits"][0]["reason"], "uri_outside_shadow_scope")

    def test_remote_endpoint_requires_explicit_allow(self):
        self.export_public(limit=1)
        with patch("scripts.experimental.openviking_shadow._post_json") as transport:
            with self.assertRaises(ShadowError) as raised:
                find_shadow(
                    self.catalog,
                    self.output,
                    "query",
                    endpoint="https://example.com:1933",
                )
        self.assertEqual(str(raised.exception), "remote_endpoint_requires_explicit_allow")
        transport.assert_not_called()

    def test_remote_endpoint_is_flagged_when_explicitly_allowed(self):
        self.export_public(limit=1)

        def fake_request(url, payload, headers, timeout):
            return {"status": "ok", "result": {"resources": [], "total": 0}}

        result = find_shadow(
            self.catalog,
            self.output,
            "query",
            endpoint="https://example.com:1933",
            allow_remote=True,
            request_json=fake_request,
        )
        self.assertEqual(result["endpoint_scope"], "remote")
        self.assertEqual(result["boundaries"]["request_transport"], "remote_http")
        self.assertEqual(result["boundaries"]["data_egress"], "not_verified")

    def test_post_json_builds_expected_request(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"status":"ok","result":{"resources":[],"total":0}}'

        with patch("scripts.experimental.openviking_shadow.urllib.request.urlopen",
                   return_value=FakeResponse()) as opened:
            value = _post_json(
                "http://127.0.0.1:1933/api/v1/search/find",
                {"query": "分层", "read_content": False},
                {"Content-Type": "application/json", "X-API-Key": "test-key"},
                2.0,
            )
        request = opened.call_args.args[0]
        self.assertEqual(value["status"], "ok")
        self.assertEqual(request.full_url, "http://127.0.0.1:1933/api/v1/search/find")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["query"], "分层")

    def test_post_json_maps_http_errors_to_fixed_code(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:1933/api/v1/search/find",
            401,
            "unauthorized",
            hdrs=None,
            fp=None,
        )
        with patch("scripts.experimental.openviking_shadow.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ShadowError) as raised:
                _post_json(
                    "http://127.0.0.1:1933/api/v1/search/find",
                    {"query": "x"},
                    {"Content-Type": "application/json"},
                    2.0,
                )
        self.assertEqual(str(raised.exception), "openviking_http_401")

    def test_manifest_path_traversal_is_rejected(self):
        self.export_public(limit=1)
        manifest_path = self.output / "manifest.jsonl"
        row = self.manifest()[0]
        row["relative_path"] = "../escape.md"
        manifest_path.write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        with self.assertRaises(ShadowError) as raised:
            load_shadow_index(self.output)
        self.assertEqual(str(raised.exception), "invalid_shadow_manifest")

    def test_cli_export_and_verify(self):
        with redirect_stdout(io.StringIO()) as captured:
            code = main([
                "export",
                "--catalog", str(self.catalog),
                "--output", str(self.output),
                "--limit", "1",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["status"], "completed")
        with redirect_stdout(io.StringIO()) as captured:
            code = main([
                "verify",
                "--catalog", str(self.catalog),
                "--shadow-dir", str(self.output),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(captured.getvalue())["status"], "passed")

    def test_cli_dry_run_does_not_require_output_or_write(self):
        with redirect_stdout(io.StringIO()) as captured:
            code = main([
                "export",
                "--catalog", str(self.catalog),
                "--dry-run",
                "--limit", "1",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(captured.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["written_documents"], 0)
        self.assertFalse((Path.cwd() / "openviking-shadow").exists())

    def test_cli_error_is_fixed_code_without_traceback(self):
        with redirect_stdout(io.StringIO()) as captured:
            code = main([
                "export",
                "--catalog", str(self.catalog),
                "--output", str(self.output),
                "--scope", "internal",
            ])
        self.assertEqual(code, 2)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], "internal_scope_requires_explicit_allow")
        self.assertNotIn("Traceback", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
