"""Offline export contracts: failure isolation, source gates and revision identity."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.data.export_dify import attachment_rows, incremental_rows, write_bundle


class ExportTests(unittest.TestCase):
    def test_duplicate_aborts_without_publishing_and_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "bundle"
            with self.assertRaises(ValueError):
                write_bundle(out, [("same", "first", {}), ("same", "second", {})])
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])
            write_bundle(out, [("same", "first", {})])
            before = (out / "manifest.jsonl").read_bytes()
            with self.assertRaises(FileExistsError):
                write_bundle(out, [("same", "replacement", {})])
            self.assertEqual(before, (out / "manifest.jsonl").read_bytes())

    def test_stable_names_and_metadata_changes_trigger_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for name, metadata in [("one", {"verified": False}), ("two", {"verified": True})]:
                write_bundle(root / name, [("案例/1", "正文", metadata)])
                entry = json.loads((root / name / "manifest.jsonl").read_text())
                raw = (root / name / entry["path"]).read_bytes()
                self.assertNotIn(b"\r\n", raw)
                self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
                entries.append(entry)
            self.assertEqual(entries[0]["path"], entries[1]["path"])
            self.assertNotEqual(entries[0]["sync_sha256"], entries[1]["sync_sha256"])

    def test_attachment_gates_and_missing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "data/attachments_v2"
            base.mkdir(parents=True)
            docs = [{"document_id": "a", "status": "processed"},
                    {"document_id": "b", "status": "parse_failed"}]
            candidates = [
                {"id": "1", "document_id": "a", "text": "allowed", "training_allowed": True},
                {"id": "2", "document_id": "a", "text": "excluded", "training_allowed": False},
                {"id": "3", "document_id": "b", "text": "failed", "training_allowed": True},
            ]
            (base / "documents.jsonl").write_text("\n".join(map(json.dumps, docs)))
            source = base / "engineering_candidates.jsonl"
            source.write_text("\n".join(map(json.dumps, candidates)))
            self.assertEqual([r[0] for r in attachment_rows(root)], ["attachment_1"])
            source.write_text(json.dumps({"id": "4", "document_id": "missing", "text": "orphan"}))
            with self.assertRaises(ValueError):
                list(attachment_rows(root))

    def test_latest_revision_supersedes_history_including_ineligible_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "data/continuous/records"
            records.mkdir(parents=True)
            row = {"id": "old", "group_id": "case_a", "created_at": 1,
                   "eligible": True, "samples": [{"text": "old conclusion"}]}
            (records / "old.json").write_text(json.dumps(row))
            initial = list(incremental_rows(root))
            row.update(id="new", created_at=2, samples=[{"text": "corrected conclusion"}])
            (records / "new.json").write_text(json.dumps(row))
            updated = list(incremental_rows(root))
            self.assertEqual(len(updated), 1)
            self.assertEqual(initial[0][0], updated[0][0])
            self.assertIn("corrected conclusion", updated[0][1])
            row["eligible"] = False
            (records / "new.json").write_text(json.dumps(row))
            self.assertEqual(list(incremental_rows(root)), [])


if __name__ == "__main__":
    unittest.main()
