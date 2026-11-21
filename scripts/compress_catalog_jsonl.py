"""Compress an existing catalog's JSONL exports without rebuilding it.

The SQLite database remains the primary query surface. This maintenance command
verifies the current artifact manifest, writes deterministic gzip exports,
updates the generated README and manifest, validates the catalog, and only then
removes the legacy uncompressed JSONL files.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from catalog_common import (
    CATALOG_JSONL_EXPORT_BASES, CATALOG_JSONL_EXPORTS,
    LEGACY_CATALOG_JSONL_EXPORTS, ROOT, digest_file, read_json,
)


class CatalogCompressionError(RuntimeError):
    """Raised when a catalog cannot be compressed safely."""


def _replace_bytes(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def compress_jsonl_file(source: Path, target: Path) -> dict:
    """Write a deterministic gzip copy and verify its decompressed bytes."""
    if not source.is_file():
        raise CatalogCompressionError("source_missing")
    if target.exists():
        raise CatalogCompressionError("target_exists")
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        raise CatalogCompressionError("partial_exists")
    source_hash = digest_file(source)
    try:
        with source.open("rb") as input_stream, partial.open("xb") as output_stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output_stream, mtime=0) as compressor:
                shutil.copyfileobj(input_stream, compressor, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        decompressed = hashlib.sha256()
        with gzip.open(partial, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                decompressed.update(chunk)
        if decompressed.hexdigest() != source_hash:
            raise CatalogCompressionError("gzip_round_trip_mismatch")
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()
    return {
        "source_bytes": source.stat().st_size,
        "compressed_bytes": target.stat().st_size,
        "source_sha256": source_hash,
        "compressed_sha256": digest_file(target),
    }


def _update_readme(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    original = path.read_bytes()
    text = original.decode("utf-8")
    for name in CATALOG_JSONL_EXPORT_BASES:
        text = re.sub(re.escape(name) + r"(?!\.gz)", name + ".gz", text)
    updated = text.encode("utf-8")
    if updated != original:
        _replace_bytes(path, updated)
    return original


def _refresh_manifest(catalog: Path, excluded_names=()) -> dict:
    excluded = set(excluded_names)
    artifacts = {}
    for path in sorted(catalog.iterdir()):
        if path.is_file() and path.name != "artifact_manifest.json" and path.name not in excluded:
            artifacts[path.name] = {"bytes": path.stat().st_size, "sha256": digest_file(path)}
    payload = {"schema_version": 1, "artifacts": artifacts}
    _replace_bytes(
        catalog / "artifact_manifest.json",
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return payload


def _verify_compressed_exports(catalog: Path, artifacts: dict) -> None:
    for name in CATALOG_JSONL_EXPORTS:
        expected = artifacts.get(name)
        path = catalog / name
        if not isinstance(expected, dict) or not path.is_file():
            raise CatalogCompressionError("manifest_missing_compressed_export")
        if path.stat().st_size != expected.get("bytes") or digest_file(path) != expected.get("sha256"):
            raise CatalogCompressionError("compressed_manifest_mismatch")


def compress_catalog(catalog: Path, root: Path = ROOT) -> dict:
    catalog = Path(catalog).resolve()
    root = Path(root).resolve()
    if catalog.parent != (root / "data").resolve():
        raise CatalogCompressionError("catalog_must_be_directly_under_data")
    if not (catalog / "catalog.sqlite3").is_file() or not (catalog / "artifact_manifest.json").is_file():
        raise CatalogCompressionError("catalog_not_found")

    manifest_path = catalog / "artifact_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = read_json(manifest_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CatalogCompressionError("invalid_artifact_manifest")

    pending = []
    for name in CATALOG_JSONL_EXPORT_BASES:
        source = catalog / name
        target = catalog / (name + ".gz")
        if source.exists() and target.exists():
            raise CatalogCompressionError("both_export_formats_present")
        if target.exists():
            continue
        if not source.is_file():
            raise CatalogCompressionError("missing_jsonl_export")
        expected = artifacts.get(name)
        if not isinstance(expected, dict):
            raise CatalogCompressionError("manifest_missing_jsonl_export")
        if source.stat().st_size != expected.get("bytes") or digest_file(source) != expected.get("sha256"):
            raise CatalogCompressionError("jsonl_manifest_mismatch")
        pending.append((name, source, target))
    if not pending:
        _verify_compressed_exports(catalog, artifacts)
        return {"status": "already_compressed", "catalog": catalog.name, "files": []}

    readme_path = catalog / "README.md"
    readme_bytes = readme_path.read_bytes() if readme_path.is_file() else None
    created = []
    try:
        results = []
        for name, source, target in pending:
            details = compress_jsonl_file(source, target)
            created.append(target)
            results.append({"name": name, **details})
        _update_readme(readme_path)
        _refresh_manifest(catalog, excluded_names=LEGACY_CATALOG_JSONL_EXPORTS)
        from knowledge_catalog import validate
        validation = validate(catalog)
        if validation.get("status") != "passed":
            raise CatalogCompressionError("catalog_validation_failed")
    except Exception:
        for target in created:
            if target.exists():
                target.unlink()
        _replace_bytes(manifest_path, manifest_bytes)
        if readme_bytes is not None:
            _replace_bytes(readme_path, readme_bytes)
        raise

    remaining = []
    for _, source, _ in pending:
        try:
            source.unlink()
        except OSError:
            remaining.append(source.name)

    return {
        "status": "completed",
        "catalog": catalog.name,
        "files": results,
        "source_bytes": sum(item["source_bytes"] for item in results),
        "compressed_bytes": sum(item["compressed_bytes"] for item in results),
        "bytes_saved": sum(item["source_bytes"] - item["compressed_bytes"] for item in results),
        "exports": list(CATALOG_JSONL_EXPORTS),
        "validation_status": validation.get("status"),
        "legacy_files_remaining": remaining,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/phase3_catalog_v3")
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args()
    try:
        result = compress_catalog(arguments.catalog, arguments.root)
    except CatalogCompressionError as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
