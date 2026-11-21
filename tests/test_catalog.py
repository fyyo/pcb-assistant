"""Synthetic end-to-end checks for non-destructive catalog preparation."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import gzip
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from catalog_build import PRIMARY_INPUTS, build, source_snapshots
from catalog_common import CatalogError, connect_readonly, digest_file, digest_text, json_text, normalize_text, stable_id
from knowledge_catalog import filtered_statistics, related, search, status, validate


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")


def write_rows(root, relative, rows, reverse=False):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = list(reversed(rows)) if reverse else rows
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered),
                    encoding="utf-8", newline="\n")


def fixture(root: Path, reverse=False):
    mail_text = "层间分层 0.5 MPa\t170 ℃\r\n\r\n\r\n试验记录  "
    mail_rows = [
        {"id": "mail_a", "subject": "  分层  记录 ", "text": mail_text, "topics": ["壓合"],
         "source_kind": "untrusted_email", "review_required": True, "training_allowed": False},
        {"id": "mail_b", "subject": "日常记录", "text": "前置上下文" + "说明" * 140 + "另一封保留的邮件提到了层偏", "topics": [],
         "source_kind": "untrusted_email", "review_required": True, "training_allowed": False},
    ]
    write_rows(root, "data/sanitized_v3/candidates.jsonl", mail_rows, reverse)
    fields = {"phenomenon": [{"text": "层间分层", "start": 0, "end": 4}]}
    cases = [{"source_id": "mail_a", "subject": "分层记录", "topics": ["压合"], "fields": fields,
              "evidence_status": "unverified_email_discussion", "local_use_allowed": True,
              "manual_review_required": False, "independently_verified": False}]
    write_rows(root, "data/knowledge_v1/cases.jsonl", cases)
    with closing(sqlite3.connect(root / "data/knowledge_v1/knowledge.sqlite3")) as connection:
        connection.execute("CREATE TABLE cases(source_id TEXT PRIMARY KEY,body TEXT)")
        connection.execute("INSERT INTO cases VALUES (?,?)", ("mail_a", mail_text))
        connection.commit()
    documents = [
        {"document_id": "att_a", "parent_source_ids": ["mail_a", "mail_missing"], "format": "pdf",
         "status": "processed", "exported_units": 1},
        {"document_id": "att_b", "parent_source_ids": ["mail_a"], "format": "png",
         "status": "processed", "exported_units": 1},
    ]
    units = [
        {"id": "unit_a", "document_id": "att_a", "subject": "压合试验", "text": "压合试验 1 2 3\r\n温度\t170 ℃  ",
         "topics": ["温度与固化"], "document_topics": ["壓合"], "parent_source_ids": ["mail_a", "mail_missing"],
         "extraction_method": "local_ocr", "ocr_min_confidence": 0.8, "source_kind": "untrusted_attachment",
         "independently_verified": False, "training_allowed": True, "manual_review_required": False},
        {"id": "unit_b", "document_id": "att_b", "subject": "压合试验", "text": "\ufeff压合试验 1 2 3\n温度\t170 ℃",
         "topics": ["叠构与材料"], "document_topics": ["压合"], "parent_source_ids": ["mail_a"],
         "extraction_method": "native_text", "ocr_min_confidence": None, "source_kind": "untrusted_attachment",
         "independently_verified": False, "training_allowed": True, "manual_review_required": False},
    ]
    write_rows(root, "data/attachments_v2/documents.jsonl", documents, reverse)
    write_rows(root, "data/attachments_v2/engineering_candidates.jsonl", units, reverse)
    occurrences = [
        {"document_id": "att_a", "parent_source_id": "mail_a", "attachment_ordinal": 0, "status": "read"},
        {"document_id": "att_a", "parent_source_id": "mail_a", "attachment_ordinal": 1, "status": "read"},
        {"document_id": "att_a", "parent_source_id": "mail_missing", "attachment_ordinal": 0, "status": "read"},
        {"document_id": "att_b", "parent_source_id": "mail_a", "attachment_ordinal": 0, "status": "read"},
    ]
    write_rows(root, "data/attachments_v2/occurrences.jsonl", occurrences, reverse)
    for version, ordinal, filename in (("v1", "page", "pages.jsonl"), ("v2", "unit", "units.jsonl")):
        base = root / ("data/public_lamination_" + version)
        write_json(base / "manifest.json", [{"id": "public_source", "title": "Public reference " + version,
                   "publisher": "Synthetic", "rights_status": "synthetic", "raw_text_training_allowed": False}])
        text = "prepreg lamination test reference" if version == "v1" else "press pressure and curing reference"
        public = [{"source_id": "public_source", ordinal: 1, "text": text, "sha256": digest_text(text)}]
        if version == "v1":
            public.append({"source_id": "public_source", ordinal: 2, "text": "", "sha256": digest_text("")})
        write_rows(root, "data/public_lamination_" + version + "/" + filename, public, reverse)
    write_rows(root, "data/attachments_v1/documents.jsonl", documents, reverse)
    write_rows(root, "data/attachments_v1/engineering_candidates.jsonl", [
        {**units[0], "text": "Older text retained as a historical variant"},
        {**units[0], "id": "obsolete_unit", "text": "A unit absent from the current version"},
    ], reverse)
    write_rows(root, "data/combined_sources_v1/engineering_candidates.jsonl",
               [mail_rows[0], {**units[0], "text": "Training derivative", "ocr_ambiguous_numeric_lines_excluded": 1}], reverse)
    base = root / "dist/dify_export_20261011_complete/cases"
    (base / "documents").mkdir(parents=True)
    markdown = "# Historical source card\n"
    (base / "documents/one.md").write_bytes(markdown.encode("utf-8"))
    write_rows(root, "dist/dify_export_20261011_complete/cases/manifest.jsonl", [{
        "document_id": "case_mail_a", "path": "documents/one.md", "sha256": digest_text(markdown), "metadata": {},
    }])
    reports = {}
    for relative in PRIMARY_INPUTS:
        path = root / relative
        reports.setdefault(path.parent, {"artifacts": {}})["artifacts"][path.name] = {"sha256": digest_file(path)}
    for parent, report in reports.items():
        write_json(parent / "report.json", report)


class CatalogTests(unittest.TestCase):
    def test_stable_json_preserves_fingerprint_and_export_encodings(self):
        value = {'z': ['压合', 0.20, None], 'a': {'b': True}}
        for pretty in (False, True):
            with self.subTest(pretty=pretty):
                options = {'indent': 2} if pretty else {'separators': (',', ':')}
                expected = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      allow_nan=False, **options)
                self.assertEqual(json_text(value, pretty=pretty), expected)
                for invalid in (float('nan'), float('inf'), float('-inf')):
                    with self.assertRaises(ValueError):
                        json_text({'number': invalid}, pretty=pretty)

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name) / "first"
        fixture(cls.root)
        cls.before = source_snapshots(cls.root)
        cls.output = cls.root / "data/phase3_catalog_test"
        with redirect_stdout(io.StringIO()):
            cls.report = build(cls.root, cls.output)

    def test_source_assets_unchanged_and_complete_output_validates(self):
        self.assertEqual(source_snapshots(self.root), self.before)
        self.assertEqual(validate(self.output)["status"], "passed")
        self.assertEqual(status(self.output)["totals"]["documents"], 12)
        self.assertEqual(self.report["processing"]["legacy_markdown_files_verified"], 1)

    def test_layout_cleaning_preserves_engineering_values_and_tabs(self):
        text, flags = normalize_text("\ufeffＴg=170℃\r\n0.5 MPa\t18 µm  \r\n\r\n\r\n1\x002")
        self.assertEqual(text, "Ｔg=170℃\n0.5 MPa\t18 µm\n\n1 2")
        self.assertIn("control_characters_replaced_with_space", flags)
        self.assertEqual(normalize_text(text), (text, []))

    def test_deduplication_keeps_sources_and_applies_after_filters(self):
        grouped = search(self.output, "压合", doc_type="KNOWLEDGE", source_type="attachment")
        self.assertEqual(grouped["total"], 1)
        self.assertEqual(grouped["results"][0]["matching_duplicates"], 2)
        expanded = search(self.output, "压合", doc_type="KNOWLEDGE", source_type="attachment", deduplicate=False)
        self.assertEqual(expanded["total"], 2)
        filtered = search(self.output, "压合", doc_type="KNOWLEDGE", source_type="attachment", tags=["叠构与材料"])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["results"][0]["doc_id"], stable_id("KNOWLEDGE", "attachment", "unit_b"))
        self.assertEqual(filtered["results"][0]["matching_duplicates"], 1)
        self.assertEqual(search(self.output, doc_type="KNOWLEDGE", source_type="attachment", tags=["叠构与材料", "温度与固化"])["total"], 0)

    def test_filtered_statistics_reconcile_to_search_scope(self):
        cases = [
            {"query": "\u538b\u5408", "doc_type": "KNOWLEDGE", "source_type": "attachment",
             "tags": ["\u53e0\u6784\u4e0e\u6750\u6599"]},
            {"query": "", "doc_type": "MAIL"},
            {"query": "unit_b", "query_mode": "metadata", "doc_type": "KNOWLEDGE"},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                report = filtered_statistics(self.output, **filters)
                raw = search(self.output, filters.get("query", ""), deduplicate=False,
                             doc_type=filters.get("doc_type"), source_type=filters.get("source_type"),
                             tags=filters.get("tags", ()), query_mode=filters.get("query_mode", "fulltext"))
                self.assertEqual(report["totals"]["documents"], raw["total"])
                self.assertEqual(sum(row["documents"] for row in report["documents_by_type"]),
                                 report["totals"]["documents"])
                self.assertEqual(sum(report["relationships_by_type"].values()),
                                 report["totals"]["relationships"])
                self.assertEqual(report["scope"], "filtered_catalog_records_before_deduplication")
                self.assertEqual(report["filters"]["query"], filters.get("query", ""))
        metadata = filtered_statistics(self.output, "unit_b", query_mode="metadata", doc_type="KNOWLEDGE")
        fulltext = filtered_statistics(self.output, "unit_b", doc_type="KNOWLEDGE")
        self.assertGreater(metadata["totals"]["documents"], 0)
        self.assertEqual(fulltext["totals"]["documents"], 0)
        empty = filtered_statistics(self.output, "synthetic_no_match_987654")
        self.assertEqual(empty["totals"]["documents"], 0)
        self.assertEqual(empty["documents_by_type"], [])
        multi_tag = filtered_statistics(self.output, doc_type="KNOWLEDGE",
                                        tags=["\u53e0\u6784\u4e0e\u6750\u6599", "\u6e29\u5ea6\u4e0e\u56fa\u5316"])
        self.assertEqual(multi_tag["totals"]["documents"], 0)

    def test_filtered_statistics_reject_invalid_parameters(self):
        invalid = [
            {"doc_type": "invalid"},
            {"source_type": "invalid"},
            {"content_status": "invalid"},
            {"query_mode": "sql"},
            {"query": "x" * 1001},
            {"tags": None},
            {"tags": [""]},
            {"tags": ["x"] * 21},
            {"source_version": ""},
            {"source_version": "x" * 151},
        ]
        for parameters in invalid:
            with self.subTest(parameters=str(parameters)[:80]):
                with self.assertRaises(CatalogError):
                    filtered_statistics(self.output, **parameters)

    def test_missing_mail_is_reported_without_creating_an_entity(self):
        found = related(self.output, stable_id("ATTACHMENT", "att_a"))
        self.assertEqual(found["unresolved_total"], 1)
        self.assertEqual(found["unresolved_references"][0]["target_source_id"], "mail_missing")
        with closing(connect_readonly(self.output)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM documents WHERE doc_id=?", (stable_id("MAIL", "mail_missing"),)).fetchone())
        edge = next(row for row in found["relationships"] if row["relation"] == "ATTACHED_TO")
        self.assertEqual(edge["metadata"], {"attachment_ordinals": [0, 1], "occurrences": 2})

    def test_multiple_mail_sources_keep_overlapping_ids_without_conflict(self):
        root = Path(self.temp.name) / "multiple_mail_sources"
        fixture(root)
        extra = [{"id": "mail_a", "subject": "重叠邮件", "text": "另一版本正文",
                  "topics": [], "source_kind": "untrusted_email"},
                 {"id": "mail_c", "subject": "新增邮件", "text": "新增来源正文",
                  "topics": [], "source_kind": "untrusted_email"}]
        write_rows(root, "data/sanitized_v9/candidates.jsonl", extra)
        write_json(root / "data/sanitized_v9/report.json",
                   {"artifacts": {"candidates.jsonl": {"sha256": digest_file(
                       root / "data/sanitized_v9/candidates.jsonl")}}})
        output = root / "data/phase3_catalog_multiple_mail_test"
        with redirect_stdout(io.StringIO()):
            build(root, output, mail_sources=[
                "data/sanitized_v3/candidates.jsonl",
                "data/sanitized_v9/candidates.jsonl",
            ])
        self.assertEqual(status(output)["totals"]["documents"], 14)
        with closing(connect_readonly(output)) as connection:
            rows = connection.execute("SELECT doc_id FROM documents WHERE type='MAIL' AND source_id='mail_a'").fetchall()
        self.assertEqual({row["doc_id"] for row in rows},
                         {stable_id("MAIL", "mail_a"), stable_id("MAIL", "mail_a", "sanitized_v9")})

    def test_original_quote_offsets_and_evidence_state_survive(self):
        with closing(connect_readonly(self.output)) as connection:
            row = connection.execute("""SELECT d.*,c.original_text FROM documents d
                JOIN contents c USING(content_id) WHERE d.doc_id=?""", (stable_id("CASE", "mail_a"),)).fetchone()
            quote = json.loads(row["metadata_json"])["fields"]["phenomenon"][0]
            self.assertEqual(row["original_text"][quote["start"]:quote["end"]], quote["text"])
            self.assertEqual(row["independently_verified"], 0)
            unit = connection.execute("""SELECT c.original_text FROM documents d JOIN contents c USING(content_id)
                WHERE d.doc_id=?""", (stable_id("KNOWLEDGE", "attachment", "unit_a"),)).fetchone()[0]
            self.assertIn("1 2 3", unit)
        self.assertEqual(self.report["quality_issue_counts"]["ocr_spaced_digits_preserved"], 1)

    def test_empty_public_page_and_old_variant_are_accounted_for(self):
        self.assertEqual(self.report["excluded_by_reason"], {"empty_public_text": 1, "historical_record_without_current_identity": 1})
        self.assertEqual(self.report["processing"]["training_derivative_text_different"], 1)
        self.assertEqual(self.report["processing"]["previous_version_text_different"], 1)

    def test_identifiers_survive_relocation_and_source_order_changes(self):
        other_root = Path(self.temp.name) / "second"
        fixture(other_root, reverse=True)
        other_output = other_root / "data/phase3_catalog_reordered"
        with redirect_stdout(io.StringIO()):
            build(other_root, other_output)
        def identities(path):
            with closing(connect_readonly(path)) as connection:
                return [tuple(row) for row in connection.execute("SELECT doc_id,duplicate_group_id FROM documents ORDER BY doc_id")]
        self.assertEqual(identities(self.output), identities(other_output))

    def test_existing_output_and_source_paths_are_protected(self):
        for path in (self.output, self.root / "data/knowledge_v1"):
            with self.assertRaises(CatalogError):
                build(self.root, path)
        self.assertEqual(source_snapshots(self.root), self.before)

    def test_readonly_connection_rejects_writes_and_query_syntax_is_data(self):
        with closing(connect_readonly(self.output)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM documents")
        search(self.output, "') OR DROP TABLE documents; --", doc_type="KNOWLEDGE")
        self.assertEqual(status(self.output)["totals"]["documents"], 12)

    def test_pagination_and_bad_query_arguments(self):
        first = search(self.output, doc_type="KNOWLEDGE", source_type="attachment", deduplicate=False, limit=1)
        second = search(self.output, doc_type="KNOWLEDGE", source_type="attachment", deduplicate=False, limit=1, offset=1)
        self.assertEqual(first["total"], 2)
        self.assertNotEqual(first["results"][0]["doc_id"], second["results"][0]["doc_id"])
        self.assertEqual(search(self.output, "???")["total"], 0)
        with self.assertRaises(CatalogError):
            search(self.output, limit=0)
        with self.assertRaises(CatalogError):
            search(self.output, doc_type="invalid")

    def test_unsegmented_pcb_phrase_still_finds_original_text(self):
        result = search(self.output, "层偏", doc_type="MAIL")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["doc_id"], stable_id("MAIL", "mail_b"))

    def test_search_preview_locates_hits_and_exposes_safe_terms(self):
        chinese = search(self.output, "层偏", doc_type="MAIL")
        self.assertEqual(chinese["total"], 1)
        self.assertIn("层偏", chinese["match_terms"])
        preview = chinese["results"][0]["preview"]
        self.assertIn("层偏", preview)
        self.assertNotIn("前置上下文", preview)
        self.assertTrue(preview.startswith("…"))

        numeric = search(self.output, "170", doc_type="KNOWLEDGE")
        self.assertIn("170", numeric["match_terms"])
        self.assertIn("170", numeric["results"][0]["preview"])

        synonym = search(self.output, "delamination", doc_type="MAIL")
        self.assertIn("分层", synonym["match_terms"])
        self.assertIn("层间分层", synonym["results"][0]["preview"])

        metadata = search(self.output, "MAIL_B", doc_type="MAIL", query_mode="metadata")
        self.assertEqual(metadata["match_terms"], ["MAIL_B"])
        self.assertEqual(metadata["filters"]["query_mode"], "metadata")
        self.assertEqual(search(self.output, doc_type="MAIL", limit=1)["match_terms"], [])

    def test_catalog_export_corruption_is_detected(self):
        path = self.output / "catalog.jsonl.gz"
        original = path.read_bytes()
        try:
            path.write_bytes(b" " + original[1:])
            result = validate(self.output)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["artifact_failures"], [{"artifact": "catalog.jsonl.gz", "reason": "checksum_mismatch"}])
        finally:
            path.write_bytes(original)

    def test_jsonl_exports_are_complete_and_gzip_readable(self):
        exports = (
            "catalog.jsonl.gz", "source_map.jsonl.gz", "relationships.jsonl.gz",
            "unresolved_references.jsonl.gz", "quality_issues.jsonl.gz",
            "excluded_records.jsonl.gz", "duplicate_groups.jsonl.gz",
        )
        for name in exports:
            path = self.output / name
            self.assertTrue(path.is_file(), name)
            self.assertFalse((self.output / name.removesuffix(".gz")).exists(), name)
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                first = json.loads(stream.readline())
                self.assertIsInstance(first, dict)


if __name__ == "__main__":
    unittest.main()
