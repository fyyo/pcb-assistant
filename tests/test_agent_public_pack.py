"""Public-only pack provenance, integrity, offline diagnosis and job checks."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_common import CatalogError

from agent.cli import main
from agent.evaluation import evaluate_file
from agent import jobs
from agent.public_pack import (
    PublicPackRetriever, _fingerprint, build_pack, load_pack,
)
from agent.retriever import source_tier
from agent.source_policy import public_source_tier


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def archives(root):
    for version in ("v1", "v2"):
        base = root / "data" / ("public_lamination_" + version)
        (base / "raw").mkdir(parents=True)
        manifest, rows = [], []
        for source_id, split, kind in (("ipc_flow", "train", "test_method"),
                                      ("vendor", "train", "technical_guide"),
                                      ("heldout", "evaluation", "technical_guide")):
            raw_text = version + source_id
            (base / "raw" / (source_id + ".pdf")).write_text(raw_text, encoding="utf-8")
            manifest.append({
                "id": source_id, "title": "IPC standard title" if kind == "test_method" else "Material guide",
                "kind": kind, "publisher": "IPC / Global Electronics Association" if kind == "test_method" else "Vendor",
                "url": "https://www.electronics.org/method.pdf" if kind == "test_method" else "https://example.test/guide.pdf",
                "split": split, "sha256": hash_text(raw_text), "raw_path": f"raw/{source_id}.pdf",
                "content_status": "usable_text", "rights_status": "local_reference_only",
                "document_revision": "Retrieved historical edition", "revision_status": "not asserted current",
            })
            for number in range(1, 4):
                text = f"Unit {number}. Vacuum pressure loss causes voids during PCB lamination."
                rows.append({
                    "source_id": source_id, "split": split, "text": text,
                    "page" if version == "v1" else "unit": number, "sha256": hash_text(text),
                    "locator_type": "pdf_page", "locator": str(number),
                })
        manifest.append({"id": "blank", "split": "train", "content_status": "unusable_empty_document"})
        write_json(base / "manifest.json", manifest)
        write_json(base / "quality_findings.json", [{"source": "ipc_flow", "issue": "Unit ambiguity: check original."}])
        filename = "pages.jsonl" if version == "v1" else "units.jsonl"
        (base / filename).write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class AgentPublicPackTests(unittest.TestCase):
    def test_build_verifies_sources_keeps_provenance_and_excludes_heldout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            original = {path: path.read_bytes() for path in (root / "data").rglob("*") if path.is_file()}
            output = root / "pack.json"
            summary = build_pack(root=root, output=output)
            pack = load_pack(output)
            self.assertEqual(summary["source_count"], 4)
            self.assertEqual(summary["unit_count"], 12)
            self.assertEqual(summary["source_tier_counts"], {"A": 2, "B": 2})
            self.assertEqual(summary["skipped"]["evaluation_sources"], 2)
            self.assertEqual(summary["skipped"]["unusable_sources"], 2)
            self.assertNotIn("heldout", output.read_text(encoding="utf-8"))
            self.assertTrue(all(source["split"] == "train" for source in pack["sources"]))
            source = pack["sources"][0]
            self.assertEqual(source["document_revision"], "Retrieved historical edition")
            self.assertEqual(source["rights_status"], "local_reference_only")
            self.assertIn("Unit ambiguity", source["quality_notes"][0])
            self.assertNotIn("raw_path", source)
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)
            second = root / "second.json"
            build_pack(root=root, output=second)
            self.assertEqual(output.read_bytes(), second.read_bytes())

    def test_raw_and_unit_tampering_are_rejected_before_output(self):
        for kind in ("raw", "text"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archives(root)
                base = root / "data/public_lamination_v1"
                if kind == "raw":
                    (base / "raw/ipc_flow.pdf").write_text("changed", encoding="utf-8")
                else:
                    path = base / "pages.jsonl"
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    rows[0]["text"] = "changed"
                    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                output = root / "pack.json"
                with self.assertRaisesRegex(CatalogError, "hash_mismatch"):
                    build_pack(root=root, output=output)
                self.assertFalse(output.exists())

    def test_output_cannot_overwrite_pack_or_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            build_pack(root=root, output=output)
            with self.assertRaisesRegex(CatalogError, "public_pack_output_exists"):
                build_pack(root=root, output=output)
            with self.assertRaisesRegex(CatalogError, "public_pack_output_inside_source"):
                build_pack(root=root, output=root / "data/public_lamination_v1/new.json")

    def test_pack_tampering_and_invalid_structure_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            build_pack(root=root, output=output)
            original = load_pack(output)
            changed = json.loads(json.dumps(original))
            changed["units"][0]["text"] = "changed"
            write_json(output, changed)
            with self.assertRaisesRegex(CatalogError, "public_pack_fingerprint_mismatch"):
                load_pack(output)
            for field, value in (("split", "evaluation"), ("source_tier", "C"), ("url", "file:///private")):
                changed = json.loads(json.dumps(original))
                changed["sources"][0][field] = value
                changed["fingerprint"] = _fingerprint(changed)
                write_json(output, changed)
                with self.assertRaises(CatalogError):
                    load_pack(output)
            changed = json.loads(json.dumps(original))
            changed["units"][0]["sha256"] = "0" * 64
            changed["fingerprint"] = _fingerprint(changed)
            write_json(output, changed)
            with self.assertRaisesRegex(CatalogError, "invalid_public_pack_unit"):
                load_pack(output)

    def test_search_preserves_literal_windows_and_limits_each_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            build_pack(root=root, output=output)
            retriever = PublicPackRetriever(output)
            hits = retriever.search("空洞 真空 vacuum pressure", limit=20)
            self.assertEqual(len(hits), 8)
            for hit in hits:
                unit = next(unit for unit in retriever.pack["units"] if unit["unit_id"] == hit["doc_id"])
                locator = hit["locator"]
                self.assertEqual(hit["text"], unit["text"][locator["start"]:locator["end"]])
                self.assertEqual(locator["sha256"], hash_text(hit["text"]))
            self.assertEqual(hits, retriever.search("空洞 真空 vacuum pressure", limit=20))
            self.assertEqual(retriever.search("没有任何相关词"), [])

    def test_cli_offline_diagnosis_and_unlabeled_evaluation_need_no_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            build_pack(root=root, output=output)
            with patch("agent.workflow.CatalogRetriever", side_effect=AssertionError("catalog must not open")):
                with patch("agent.web_search.build_opener", side_effect=AssertionError("network must not open")):
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        code = main(["diagnose", "--question", "压合后空洞", "--public-pack", str(output), "--json"])
                    self.assertEqual(code, 0)
                    result = json.loads(buffer.getvalue())
                    self.assertEqual(result["retrieval"]["providers_used"], ["local_public_pack"])
                    self.assertTrue(result["citations"])
                    self.assertIn("版本 Retrieved historical edition", result["answer"])
                    self.assertTrue(any(item["provenance"]["quality_notes"] for item in result["citations"]))
                    self.assertFalse(result["boundaries"]["external_services"])
                    suite = root / "unlabeled.jsonl"
                    suite.write_text(json.dumps({"case_id": "offline-1", "question": "压合后空洞"}), encoding="utf-8")
                    report = evaluate_file(suite, public_pack=output, check_determinism=True)
            self.assertEqual(report["summary"]["citation_integrity_pass_rate"], 1)
            self.assertEqual(report["summary"]["determinism_pass_rate"], 1)
            self.assertIsNone(report["summary"]["cause_top_1_accuracy"])
            self.assertEqual(report["settings"]["public_pack_fingerprint"], load_pack(output)["fingerprint"])

    def test_background_worker_uses_selected_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            build_pack(root=root, output=output)
            suite = root / "suite.jsonl"
            suite.write_text(json.dumps({"case_id": "offline-1", "question": "压合后空洞"}), encoding="utf-8")
            with patch("agent.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                started = jobs.start_evaluation(suite, catalog=root / "no-catalog", public_pack=output,
                                                check_determinism=True, root=root / "jobs")
            with patch("agent.workflow.CatalogRetriever", side_effect=AssertionError("catalog must not open")):
                self.assertEqual(jobs.run_worker(started["job_id"], root=root / "jobs"), 0)
            record, report = jobs.get_result(started["job_id"], root=root / "jobs")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(report["settings"]["local_provider"], "local_public_pack")

    def test_cli_build_verify_and_missing_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            output = root / "pack.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["public-pack", "build", "--root", str(root), "--output", str(output)]), 0)
                self.assertEqual(main(["public-pack", "verify", "--pack", str(output)]), 0)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(["public-pack", "verify", "--pack", str(root / "missing.json")]), 2)
            self.assertIn("public_pack_unavailable", stderr.getvalue())

    def test_title_tokens_cannot_promote_source_grade(self):
        self.assertEqual(source_tier({"type": "PUBLIC", "title": "IPC Standard"}), "B")
        self.assertEqual(public_source_tier({"kind": "technical_guide", "publisher": "Vendor",
                                            "title": "IPC Standard", "url": "https://example.test"}), "B")
        official = {"kind": "test_method", "publisher": "IPC / Global Electronics Association",
                    "url": "https://www.electronics.org/method.pdf"}
        self.assertEqual(public_source_tier(official), "A")
        self.assertEqual(source_tier({"type": "KNOWLEDGE", "source_type": "public_reference",
                                      "public_source": official}), "A")
        self.assertEqual(public_source_tier({**official, "url": "https://electronics.org.attacker.test"}), "B")


if __name__ == "__main__":
    unittest.main()
