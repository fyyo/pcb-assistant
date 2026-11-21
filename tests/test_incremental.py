"""Incremental source-version and derivative-catalog checks."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from pcb_assistant.catalog.build import build, source_snapshots
from pcb_assistant.catalog.common import CatalogError, digest_file, digest_text, stable_id, validate_version_metadata
from pcb_assistant.catalog.incremental import ingest
from pcb_assistant.catalog.cli import batches, growth, related, validate, version_diff, versions
from .test_catalog import fixture, write_json, write_rows


def make_manifest(root: Path, directory_name: str, source_version: str, records: list[dict],
                  source_name: str = "synthetic_mail_source", source_type: str = "mail") -> Path:
    relative = "data/" + directory_name
    records_path = root / relative / "records.jsonl"
    write_rows(root, relative + "/records.jsonl", records)
    manifest_path = root / relative / "manifest.json"
    write_json(manifest_path, {
        "source_name": source_name,
        "source_type": source_type,
        "source_version": source_version,
        "fingerprint": digest_file(records_path),
        "records": "records.jsonl",
    })
    return manifest_path


def recovered_mail(text: str = "Recovered email text with lamination details.") -> dict:
    return {
        "entity_type": "MAIL",
        "source_id": "mail_missing",
        "title": "Recovered source mail",
        "text": text,
        "topics": ["lamination"],
        "metadata": {},
        "evidence_status": "unverified_email_discussion",
        "independently_verified": False,
        "parent_source_ids": [],
        "relationships": [{
            "target_type": "MAIL",
            "target_source_id": "mail_still_missing",
            "relation": "REFERENCES",
            "metadata": {},
        }],
    }


def tree_state(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): (digest_file(path), path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def add_source_alias(catalog: Path, doc_id: str, source_id: str) -> None:
    mapping_id = stable_id("SOURCE", doc_id, "previous_version", "legacy_alias", source_id, "{}")
    with closing(sqlite3.connect(catalog / "catalog.sqlite3")) as connection:
        connection.execute("INSERT OR IGNORE INTO source_map VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            mapping_id, doc_id, "previous_version", source_id, "legacy", "data/legacy/records.jsonl",
            "{}", digest_text("{}"), digest_text("legacy source text"), "{}"))
        connection.commit()


class IncrementalCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-catalog-incremental-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.base = cls.root / "data/phase3_catalog_test"
        with redirect_stdout(io.StringIO()):
            build(cls.root, cls.base)
        cls.source_before = source_snapshots(cls.root)
        cls.base_before = tree_state(cls.base)

    def output(self, name: str) -> Path:
        return self.root / "data" / ("phase3_catalog_" + name)

    def test_ingest_creates_stable_entity_and_resolves_existing_missing_reference(self):
        manifest = make_manifest(self.root, "incremental_source_one", "v1", [recovered_mail()])
        output = self.output("incremental_one")
        before_manifest = digest_file(manifest)
        before_records = digest_file(manifest.parent / "records.jsonl")
        with redirect_stdout(io.StringIO()):
            result = ingest(self.base, manifest, output, root=self.root)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["batch"]["new_entities"], 1)
        self.assertEqual(result["batch"]["existing_entities"], 0)
        self.assertEqual(result["batch"]["new_relations"], 1)
        self.assertEqual(result["batch"]["resolved_references"], 1)
        self.assertEqual(result["batch"]["missing_links"], 1)
        self.assertEqual(validate(output)["status"], "passed")
        self.assertTrue((output / "catalog.jsonl.gz").is_file())
        self.assertFalse((output / "catalog.jsonl").exists())
        self.assertEqual(validate_version_metadata(output)["status"], "passed")

        doc_id = stable_id("MAIL", "mail_missing")
        found = related(output, stable_id("ATTACHMENT", "att_a"))
        self.assertIn(doc_id, {item["to_id"] for item in found["relationships"]})
        self.assertNotIn("mail_missing", {item["target_source_id"] for item in found["unresolved_references"]})
        new_found = related(output, doc_id)
        self.assertIn("mail_still_missing",
                      {item["target_source_id"] for item in new_found["unresolved_references"]})

        report = growth(output)
        self.assertTrue(report["registered"])
        self.assertEqual(report["status"], "registered")
        self.assertEqual(report["reconciliation_status"], "matched")
        self.assertTrue(all(report["reconciliation"].values()), report["reconciliation"])
        self.assertEqual(report["current"]["documents"], 13)
        self.assertEqual(report["current"]["unresolved_references"], 1)
        self.assertEqual(len(versions(output)["versions"]), 2)
        self.assertEqual(len(batches(output)["batches"]), 2)

        self.assertEqual(digest_file(manifest), before_manifest)
        self.assertEqual(digest_file(manifest.parent / "records.jsonl"), before_records)
        self.assertEqual(tree_state(self.base), self.base_before)
        self.assertEqual(source_snapshots(self.root), self.source_before)

    def test_reimport_is_idempotent_and_updates_snapshot_without_new_entity(self):
        manifest = make_manifest(self.root, "incremental_source_repeat", "v1", [recovered_mail()])
        first = self.output("incremental_repeat_first")
        second = self.output("incremental_repeat_second")
        with redirect_stdout(io.StringIO()):
            first_result = ingest(self.base, manifest, first, root=self.root)
            second_result = ingest(first, manifest, second, root=self.root)

        self.assertEqual(first_result["batch"]["new_entities"], 1)
        self.assertEqual(second_result["batch"]["new_entities"], 0)
        self.assertEqual(second_result["batch"]["existing_entities"], 1)
        self.assertEqual(second_result["batch"]["new_relations"], 0)
        self.assertEqual(second_result["batch"]["resolved_references"], 0)
        self.assertEqual(growth(first)["current"], growth(second)["current"])
        self.assertEqual(growth(second)["reconciliation_status"], "matched")
        self.assertTrue(all(growth(second)["reconciliation"].values()))
        self.assertEqual(len(versions(second)["versions"]), 2)
        self.assertEqual(len(batches(second)["batches"]), 3)

        version_id = second_result["version"]["version_id"]
        snapshot = json.loads((second / "versions/snapshots" / (version_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["batch_id"], second_result["batch"]["batch_id"])
        self.assertEqual(snapshot["document_count"], 13)
        diff = version_diff(second, versions(second)["versions"][0]["version_id"], version_id)
        self.assertEqual(diff["document_delta"], 1)
        self.assertEqual(diff["relationship_delta"], 1)
        self.assertEqual(diff["unresolved_reference_delta"], 0)

    def test_conflicting_identity_is_rejected_without_publishing_output(self):
        initial_manifest = make_manifest(self.root, "incremental_source_conflict_initial", "v1",
                                        [recovered_mail()])
        seed = self.output("incremental_conflict_seed")
        with redirect_stdout(io.StringIO()):
            ingest(self.base, initial_manifest, seed, root=self.root)
        seed_before = tree_state(seed)

        manifest = make_manifest(self.root, "incremental_source_conflict", "v2",
                                 [recovered_mail("Different text for the same source identity.")])
        output = self.output("incremental_conflict")
        existing_staging = {path.name for path in (self.root / "data").glob(".phase3_catalog_ingest_*")}
        with self.assertRaises(CatalogError) as caught:
            with redirect_stdout(io.StringIO()):
                ingest(seed, manifest, output, root=self.root)
        self.assertEqual(str(caught.exception), "conflicting_source_identity")
        self.assertFalse(output.exists())
        new_staging = {path.name for path in (self.root / "data").glob(".phase3_catalog_ingest_*")} - existing_staging
        self.assertEqual(len(new_staging), 1)
        staging = self.root / "data" / next(iter(new_staging))
        self.assertTrue((staging / "INGEST_FAILED.json").is_file())
        self.assertEqual(tree_state(seed), seed_before)
        self.assertEqual(tree_state(self.base), self.base_before)
        self.assertEqual(source_snapshots(self.root), self.source_before)

    def test_legacy_catalog_gets_deterministic_synthetic_baseline(self):
        legacy = self.root / "data/phase3_catalog_legacy"
        shutil.copytree(self.base, legacy)
        shutil.rmtree(legacy / "versions")
        self.assertEqual(validate_version_metadata(legacy)["status"], "not_present")
        self.assertEqual(validate(legacy)["status"], "passed")
        legacy_growth = growth(legacy)
        self.assertFalse(legacy_growth["registered"])
        self.assertEqual(legacy_growth["status"], "not_registered")
        self.assertEqual(legacy_growth["reconciliation_status"], "not_registered")
        self.assertIsNone(legacy_growth["reconciliation"])
        self.assertEqual(legacy_growth["current"]["documents"], 12)

        manifest = make_manifest(self.root, "incremental_source_legacy", "v1", [recovered_mail()])
        first = self.output("incremental_legacy_first")
        second = self.output("incremental_legacy_second")
        with redirect_stdout(io.StringIO()):
            first_result = ingest(legacy, manifest, first, root=self.root)
            second_result = ingest(legacy, manifest, second, root=self.root)

        first_versions = versions(first)["versions"]
        second_versions = versions(second)["versions"]
        self.assertEqual(first_versions[0]["source_type"], "catalog")
        self.assertEqual(first_versions[0]["source_version"], "baseline")
        self.assertEqual(first_versions[0]["source_name"], legacy.name)
        self.assertEqual(first_versions[0]["version_id"], second_versions[0]["version_id"])
        self.assertEqual(first_result["batch"]["resolved_references"], 1)
        self.assertEqual(second_result["batch"]["resolved_references"], 1)
        self.assertTrue(all(growth(first)["reconciliation"].values()))
        self.assertEqual(validate(first)["status"], "passed")

    def test_version_diff_only_accepts_registered_safe_ids(self):
        manifest = make_manifest(self.root, "incremental_source_diff_safe", "v1", [recovered_mail()])
        output = self.output("incremental_diff_safe")
        with redirect_stdout(io.StringIO()):
            ingest(self.base, manifest, output, root=self.root)
        baseline = versions(output)["versions"][0]["version_id"]
        current = versions(output)["versions"][-1]["version_id"]
        self.assertEqual(version_diff(output, baseline, current)["document_delta"], 1)
        for invalid in ("../VER_X", "C:\\tmp\\VER_X", "VER_WITH.DOT", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CatalogError) as caught:
                    version_diff(output, invalid, current)
                self.assertEqual(str(caught.exception), "invalid_from_version_id")
        with self.assertRaises(CatalogError) as caught:
            version_diff(output, baseline, "../VER_X")
        self.assertEqual(str(caught.exception), "invalid_to_version_id")
        with self.assertRaises(CatalogError) as caught:
            version_diff(output, "VER_NOT_REGISTERED", current)
        self.assertEqual(str(caught.exception), "from_version_not_found")
        fake = output / "versions/snapshots/VER_UNREGISTERED.json"
        fake.write_text(json.dumps({"version_id": "VER_UNREGISTERED"}), encoding="utf-8")
        try:
            with self.assertRaises(CatalogError) as caught:
                version_diff(output, "VER_UNREGISTERED", current)
            self.assertEqual(str(caught.exception), "from_version_not_found")
        finally:
            fake.unlink()
        snapshot = output / "versions/snapshots" / (current + ".json")
        snapshot_original = snapshot.read_bytes()
        try:
            payload = json.loads(snapshot_original)
            payload["document_count"] = "invalid"
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(CatalogError) as caught:
                version_diff(output, baseline, current)
            self.assertEqual(str(caught.exception), "invalid_version_snapshot")
        finally:
            snapshot.write_bytes(snapshot_original)

    def test_alias_source_mapping_is_reused_without_creating_a_duplicate(self):
        alias_base = self.output("incremental_alias_base")
        shutil.copytree(self.base, alias_base)
        canonical = stable_id("MAIL", "mail_a")
        add_source_alias(alias_base, canonical, "mail_legacy_alias")
        mail = json.loads((self.root / "data/sanitized_v3/candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
        alias_record = {
            "entity_type": "MAIL", "source_id": "mail_legacy_alias", "title": mail["subject"],
            "text": mail["text"], "topics": mail["topics"], "metadata": {},
            "evidence_status": "unverified_email_discussion", "independently_verified": False,
            "parent_source_ids": [], "relationships": [],
        }
        manifest = make_manifest(self.root, "incremental_source_alias", "v1", [alias_record])
        output = self.output("incremental_alias")
        with redirect_stdout(io.StringIO()):
            result = ingest(alias_base, manifest, output, root=self.root)
        self.assertEqual(result["batch"]["new_entities"], 0)
        self.assertEqual(result["batch"]["existing_entities"], 1)
        self.assertEqual(validate(output)["status"], "passed")
        with closing(sqlite3.connect(output / "catalog.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 12)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM documents WHERE doc_id=?", (stable_id("MAIL", "mail_legacy_alias"),)).fetchone())
            mapped = connection.execute("""SELECT doc_id FROM source_map
                WHERE source_id='mail_legacy_alias' AND role='incremental_version'""").fetchall()
            self.assertEqual(mapped, [(canonical,)])
        mappings = [json.loads(line) for line in (output / "versions/entity_mappings.jsonl").read_text(encoding="utf-8").splitlines()]
        alias_mapping = next(row for row in mappings if row.get("source_record_id") == "mail_legacy_alias")
        self.assertEqual(alias_mapping["entity_id"], canonical)
        self.assertEqual(alias_mapping["status"], "existing")
        self.assertEqual(growth(output)["reconciliation_status"], "matched")

        conflict = dict(alias_record, text="Different text for the same alias identity.")
        conflict_manifest = make_manifest(self.root, "incremental_source_alias_conflict", "v2", [conflict])
        with self.assertRaises(CatalogError) as caught:
            with redirect_stdout(io.StringIO()):
                ingest(alias_base, conflict_manifest, self.output("incremental_alias_conflict"), root=self.root)
        self.assertEqual(str(caught.exception), "conflicting_source_identity")

        ambiguous_base = self.output("incremental_alias_ambiguous_base")
        shutil.copytree(self.base, ambiguous_base)
        add_source_alias(ambiguous_base, canonical, "mail_ambiguous_alias")
        add_source_alias(ambiguous_base, stable_id("MAIL", "mail_b"), "mail_ambiguous_alias")
        ambiguous = dict(alias_record, source_id="mail_ambiguous_alias")
        ambiguous_manifest = make_manifest(self.root, "incremental_source_alias_ambiguous", "v1", [ambiguous])
        with self.assertRaises(CatalogError) as caught:
            with redirect_stdout(io.StringIO()):
                ingest(ambiguous_base, ambiguous_manifest, self.output("incremental_alias_ambiguous"), root=self.root)
        self.assertEqual(str(caught.exception), "ambiguous_source_identity")

    def test_version_metadata_consistency_and_parent_cycles_are_detected(self):
        manifest = make_manifest(self.root, "incremental_source_metadata", "v1", [recovered_mail()])
        output = self.output("incremental_metadata")
        with redirect_stdout(io.StringIO()):
            ingest(self.base, manifest, output, root=self.root)
        version_rows = versions(output)["versions"]
        baseline = version_rows[0]["version_id"]
        current = version_rows[-1]["version_id"]
        snapshot_path = output / "versions/snapshots" / (current + ".json")
        snapshot_original = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_original)
        try:
            snapshot["source_name"] = "different_source"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = validate_version_metadata(output)
            self.assertIn("version_snapshot_source_name_mismatch", {item["code"] for item in result["errors"]})
            snapshot["source_name"] = version_rows[-1]["source_name"]
            snapshot["batch_id"] = "BATCH_NOT_REGISTERED"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = validate_version_metadata(output)
            self.assertIn("version_snapshot_batch_not_registered", {item["code"] for item in result["errors"]})
        finally:
            snapshot_path.write_bytes(snapshot_original)

        registry_path = output / "versions/version_registry.jsonl"
        registry_original = registry_path.read_bytes()
        try:
            rows = [json.loads(line) for line in registry_original.decode("utf-8").splitlines() if line.strip()]
            rows[0]["parent_version_id"] = current
            registry_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            result = validate_version_metadata(output)
            self.assertIn("version_parent_cycle", {item["code"] for item in result["errors"]})
        finally:
            registry_path.write_bytes(registry_original)
        self.assertEqual(validate_version_metadata(output)["status"], "passed")
        self.assertNotEqual(baseline, current)

    def test_version_metadata_corruption_is_detected(self):
        manifest = make_manifest(self.root, "incremental_source_corrupt", "v1", [recovered_mail()])
        output = self.output("incremental_corrupt")
        with redirect_stdout(io.StringIO()):
            ingest(self.base, manifest, output, root=self.root)
        registry = output / "versions/version_registry.jsonl"
        original = registry.read_bytes()
        try:
            registry.write_bytes(original + b'{"version_id":"bad id"}\n')
            result = validate_version_metadata(output)
            self.assertEqual(result["status"], "failed")
            self.assertIn("invalid_version_id", {item["code"] for item in result["errors"]})
        finally:
            registry.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
