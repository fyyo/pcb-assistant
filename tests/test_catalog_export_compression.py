"""Checks for deterministic catalog JSONL export compression."""
from __future__ import annotations

import gzip
from pathlib import Path
import tempfile
import unittest

from catalog_common import jsonl_records
from scripts.compress_catalog_jsonl import CatalogCompressionError, compress_jsonl_file


class CatalogExportCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_round_trip_preserves_bytes_and_uses_fixed_header_time(self):
        source = self.root / "catalog.jsonl"
        target = self.root / "catalog.jsonl.gz"
        payload = ("{\"id\":1,\"text\":\"压合\"}\n" * 1000).encode("utf-8")
        source.write_bytes(payload)

        result = compress_jsonl_file(source, target)

        self.assertEqual(result["source_bytes"], len(payload))
        self.assertEqual(result["compressed_bytes"], target.stat().st_size)
        with gzip.open(target, "rb") as stream:
            self.assertEqual(stream.read(), payload)
        self.assertEqual(len(list(jsonl_records(target))), 1000)
        self.assertEqual(int.from_bytes(target.read_bytes()[4:8], "little"), 0)
        self.assertTrue(source.is_file())

    def test_existing_target_is_never_overwritten(self):
        source = self.root / "catalog.jsonl"
        target = self.root / "catalog.jsonl.gz"
        source.write_text('{"id":1}\n', encoding="utf-8")
        target.write_bytes(b"existing")
        with self.assertRaisesRegex(CatalogCompressionError, "target_exists"):
            compress_jsonl_file(source, target)
        self.assertEqual(target.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
