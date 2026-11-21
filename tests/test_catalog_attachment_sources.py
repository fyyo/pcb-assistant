"""Multi-PST attachment coverage, provenance and stable-identity regressions."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from pcb_assistant.catalog.build import ATTACHMENT_FILES, build, normalize_attachment_sources, source_snapshots
from pcb_assistant.catalog.common import CatalogError, connect_readonly, digest_file, stable_id
from pcb_assistant.catalog.cli import related, search, validate
from tests.test_catalog import fixture, write_json, write_rows


class AttachmentSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.sources = ["data/attachments_v2", "data/attachments_v3"]
        self.documents = [
            {"document_id": "att_a", "parent_source_ids": ["mail_b"], "format": "pdf",
             "status": "processed", "exported_units": 2},
            {"document_id": "att_c", "parent_source_ids": ["mail_b"], "format": "xlsx",
             "status": "processed", "exported_units": 1},
        ]
        first = json.loads((self.root / "data/attachments_v2/engineering_candidates.jsonl")
                           .read_text(encoding="utf-8").splitlines()[0])
        self.units = [
            {**first, "parent_source_ids": ["mail_b"], "document_topics": ["压力与真空"]},
            {**first, "id": "unit_c", "parent_source_ids": ["mail_b"],
             "text": "newsource 第二来源新增压合温度记录"},
            {**first, "id": "unit_d", "document_id": "att_c", "parent_source_ids": ["mail_b"],
             "text": "newsource 第二来源新增叠构记录"},
        ]
        self.occurrences = [
            {"document_id": "att_a", "parent_source_id": "mail_b", "attachment_ordinal": 2},
            {"document_id": "att_c", "parent_source_id": "mail_b", "attachment_ordinal": 0},
        ]

    def write_source(self):
        for filename, rows in zip(ATTACHMENT_FILES,
                                  (self.documents, self.occurrences, self.units)):
            write_rows(self.root, "data/attachments_v3/" + filename, rows)
        write_json(self.root / "data/attachments_v3/report.json", {"artifacts": {
            name: {"sha256": digest_file(self.root / "data/attachments_v3" / name)}
            for name in ATTACHMENT_FILES}})

    def build_candidate(self, write=True):
        if write:
            self.write_source()
        self.output = self.root / "data/phase3_catalog_multi_attachment"
        with redirect_stdout(io.StringIO()):
            return build(self.root, self.output, attachment_sources=self.sources)

    def test_merges_sources_preserving_old_ids_and_all_parent_links(self):
        baseline = self.root / "data/phase3_catalog_baseline"
        with redirect_stdout(io.StringIO()):
            build(self.root, baseline)
        self.write_source()
        before = source_snapshots(self.root, attachment_sources=self.sources)
        report = self.build_candidate(write=False)
        self.assertEqual(source_snapshots(self.root, attachment_sources=self.sources), before)
        self.assertEqual(validate(self.output)["status"], "passed")
        with closing(connect_readonly(baseline)) as old, closing(connect_readonly(self.output)) as new:
            old_ids = {r[0] for r in old.execute("SELECT doc_id FROM documents")}
            new_ids = {r[0] for r in new.execute("SELECT doc_id FROM documents")}
            self.assertEqual(old_ids - new_ids, set())
            self.assertEqual(len(new_ids - old_ids), 3)
            sources = new.execute("SELECT source_version FROM source_map WHERE doc_id=?",
                                  (stable_id("KNOWLEDGE", "attachment", "unit_a"),)).fetchall()
            self.assertTrue({"attachments_v2", "attachments_v3"} <= {r[0] for r in sources})
        links = related(self.output, stable_id("ATTACHMENT", "att_a"))
        parents = {r["to_id"] for r in links["relationships"] if r["relation"] == "ATTACHED_TO"}
        self.assertEqual(parents, {stable_id("MAIL", "mail_a"), stable_id("MAIL", "mail_b")})
        self.assertEqual(links["unresolved_total"], 1)
        self.assertEqual(report["processing"]["attachment_occurrences"], 6)
        self.assertEqual(search(self.output, "newsource", source_version="attachments_v3")["total"], 2)

    def test_changed_text_is_preserved_as_versioned_document(self):
        self.units[0]["text"] = "newvariant 新版压合正文，不能被原版覆盖"
        self.build_candidate()
        with closing(connect_readonly(self.output)) as con:
            rows = con.execute("""SELECT d.doc_id,c.original_text FROM documents d
                JOIN contents c USING(content_id) WHERE d.type='KNOWLEDGE' AND d.source_id='unit_a'""").fetchall()
        self.assertEqual({r["doc_id"] for r in rows}, {
            stable_id("KNOWLEDGE", "attachment", "unit_a"),
            stable_id("KNOWLEDGE", "attachment", "unit_a", "attachments_v3")})
        self.assertEqual(len({r["original_text"] for r in rows}), 2)
        self.assertEqual(search(self.output, "newvariant", source_version="attachments_v3")["total"], 1)

    def test_changed_extraction_coverage_preserves_variant(self):
        self.units[0]["source_coverage"] = {"ocr_disabled": 1}
        self.build_candidate()
        with closing(connect_readonly(self.output)) as con:
            rows = con.execute("SELECT metadata_json FROM documents WHERE source_id='unit_a'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(json.loads(r[0]).get("source_coverage") == {"ocr_disabled": 1}
                            for r in rows))

    def test_shared_mail_occurrences_are_counted_with_both_versions(self):
        self.documents[0]["parent_source_ids"] = ["mail_a"]
        self.units[0]["parent_source_ids"] = ["mail_a"]
        self.units[1]["parent_source_ids"] = ["mail_a"]
        self.occurrences[0]["parent_source_id"] = "mail_a"
        self.build_candidate()
        links = related(self.output, stable_id("ATTACHMENT", "att_a"))
        edge = next(r for r in links["relationships"] if r["relation"] == "ATTACHED_TO")
        self.assertEqual(edge["metadata"], {"attachment_ordinals": [0, 1, 2], "occurrences": 3,
                                          "source_versions": ["attachments_v2", "attachments_v3"]})

    def test_rejects_count_mismatch_in_second_manifest(self):
        self.documents[0]["exported_units"] = 3
        with self.assertRaisesRegex(CatalogError, "attachment_unit_count_mismatch"):
            self.build_candidate()
        self.assertFalse(self.output.exists())

    def test_rejects_unit_without_manifest_in_its_own_source(self):
        self.documents.pop(0)
        with self.assertRaisesRegex(CatalogError, "attachment_unit_missing_source_manifest"):
            self.build_candidate()

    def test_rejects_occurrence_without_manifest_in_its_own_source(self):
        self.occurrences.append({"document_id": "att_b", "parent_source_id": "mail_a",
                                 "attachment_ordinal": 3})
        with self.assertRaisesRegex(CatalogError, "occurrence_missing_attachment_document"):
            self.build_candidate()

    def test_rejects_parent_mismatch_in_second_source(self):
        self.occurrences[0]["parent_source_id"] = "mail_a"
        with self.assertRaisesRegex(CatalogError, "attachment_occurrence_parent_set_mismatch"):
            self.build_candidate()

    def test_rejects_duplicate_units_in_one_source(self):
        self.units.append(dict(self.units[0]))
        with self.assertRaisesRegex(CatalogError, "duplicate_attachment_unit_record"):
            self.build_candidate()

    def test_source_arguments_accept_manifest_path_and_reject_duplicates(self):
        self.assertEqual(normalize_attachment_sources(["data/attachments_v3/documents.jsonl"]),
                         [{"base": "data/attachments_v3", "version": "attachments_v3"}])
        for sources in (["data/attachments_v3", "data/attachments_v3/documents.jsonl"],
                        [{"base": "a", "version": "same"}, {"base": "b", "version": "same"}]):
            with self.subTest(sources=sources), self.assertRaises(CatalogError):
                normalize_attachment_sources(sources)


if __name__ == "__main__":
    unittest.main()
