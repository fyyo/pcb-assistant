"""Export sanitized PCB knowledge into Dify-friendly Markdown bundles.

The exporter is deliberately offline. It writes one Markdown document per
case/text unit/incremental record plus a JSONL manifest containing metadata.
It does not call Dify or upload any source data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL: {path}:{line_no}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row is not an object: {path}:{line_no}")
            yield value


def compact(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(compact(item) for item in value if compact(item))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def slug(value: str, fallback: str) -> str:
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._-")
    return (value or fallback)[:120]


def write_bundle(output: Path, rows: Iterable[tuple[str, str, dict[str, Any]]],
                 limit: int | None = None) -> dict[str, int]:
    """Publish a complete new directory; never mix a new manifest with old files."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be nonnegative")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists; choose a new directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".dify-building-", dir=output.parent))
    try:
        summary = _write_bundle(temporary, rows, limit)
        temporary.rename(output)
        return summary
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _write_bundle(output: Path, rows: Iterable[tuple[str, str, dict[str, Any]]],
                  limit: int | None) -> dict[str, int]:
    docs = output / "documents"
    docs.mkdir(exist_ok=True)
    manifest = output / "manifest.jsonl"
    count = chars = 0
    seen = set()
    with manifest.open("w", encoding="utf-8") as stream:
        for doc_id, body, metadata in rows:
            if limit is not None and count >= limit:
                break
            if not body.strip():
                raise ValueError(f"empty document: {doc_id}")
            if not doc_id or doc_id in seen:
                raise ValueError(f"missing or duplicate document ID: {doc_id}")
            seen.add(doc_id)
            body = body.strip() + "\n"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            safe_id = slug(doc_id, f"document_{count + 1}")
            identity_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
            filename = f"{safe_id}_{identity_hash}.md"
            path = docs / filename
            path.write_bytes(body.encode("utf-8"))
            entry = {
                "document_id": doc_id,
                "path": str(path.relative_to(output)),
                "sha256": digest,
                "characters": len(body),
                "metadata": metadata,
                "sync_sha256": hashlib.sha256(json.dumps(
                    {"body": body, "metadata": metadata}, ensure_ascii=False,
                    sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest(),
            }
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
            chars += len(body)
    summary = {"documents": count, "characters": chars}
    (output / "summary.json").write_text(
        json.dumps({**summary, "schema_version": 1, "offline_only": True,
                    "limited_export": limit is not None,
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def field_text(fields: dict[str, Any], key: str) -> str:
    values = fields.get(key) or []
    result = []
    for item in values:
        if isinstance(item, dict):
            text = compact(item.get("text"))
        else:
            text = compact(item)
        if text:
            result.append(text)
    return "\n".join(f"- {item}" for item in result)


def case_rows(root: Path) -> Iterable[tuple[str, str, dict[str, Any]]]:
    source = root / "data/knowledge_v1/cases.jsonl"
    for row in read_jsonl(source):
        source_id = compact(row.get("source_id"))
        if not source_id:
            raise ValueError("case missing source_id")
        subject = compact(row.get("subject")) or source_id
        fields = row.get("fields") or {}
        sections = []
        for key, title in (
            ("phenomenon", "现象"), ("materials", "材料"),
            ("conditions", "条件"), ("analysis", "分析"),
            ("actions", "措施"), ("verification", "验证"),
            ("limitations", "限制"),
        ):
            sections.append(f"## {title}\n{field_text(fields, key)}")
        body = (f"# 工程案例：{subject}\n\n案例 ID：{source_id}\n"
                f"主题：{compact(row.get('topics'))}\n"
                f"证据状态：{compact(row.get('evidence_status'))}\n"
                f"独立验证标记：{row.get('independently_verified', False)}\n"
                "字段为原始记录摘录；讨论或验证措辞不等于已确认的因果关系。\n\n"
                + "\n\n".join(sections))
        metadata = {
            "source_id": source_id,
            "source_type": "engineering_case",
            "topics": compact(row.get("topics")),
            "evidence_status": compact(row.get("evidence_status")),
            "independently_verified": bool(row.get("independently_verified")),
            "local_use_allowed": bool(row.get("local_use_allowed")),
            "manual_review_required": bool(row.get("manual_review_required")),
            "extraction_method": compact(row.get("extraction_method")),
            "knowledge_version": "knowledge_v1",
        }
        if metadata["local_use_allowed"]:
            yield "case_" + source_id, body, metadata


def attachment_rows(root: Path) -> Iterable[tuple[str, str, dict[str, Any]]]:
    docs = {row.get("document_id"): row for row in read_jsonl(
        root / "data/attachments_v2/documents.jsonl")}
    source = root / "data/attachments_v2/engineering_candidates.jsonl"
    for row in read_jsonl(source):
        text = compact(row.get("text"))
        document_id = compact(row.get("document_id"))
        if not text or not document_id:
            continue
        if document_id not in docs:
            raise ValueError(f"attachment missing manifest parent: {document_id}")
        parent = docs[document_id]
        if parent.get("status") != "processed" or row.get("training_allowed") is not True:
            continue
        if not compact(row.get("id")):
            raise ValueError("attachment missing unit ID")
        subject = compact(row.get("subject")) or "附件工程资料"
        body = (f"# 附件工程资料：{subject}\n\n附件文档 ID：{document_id}\n"
                f"文本单元 ID：{compact(row.get('id'))}\n"
                f"原附件格式：{compact(parent.get('format'))}\n"
                f"提取方式：{compact(row.get('extraction_method'))}\n"
                f"主题：{compact(row.get('topics') or row.get('document_topics'))}\n\n"
                f"## 提取内容\n{text}\n\n"
                "## 覆盖说明\n"
                "内容来自本地文本解析或 OCR；OCR 不表达图形、曲线或剖面的几何含义。\n"
                f"证据来源：{compact(row.get('source_kind'))}；"
                f"独立验证标记：{row.get('independently_verified', False)}\n"
                f"覆盖范围：{compact(row.get('source_coverage'))}\n"
                f"解析统计：{compact(parent.get('parser_stats'))}")
        metadata = {
            "document_id": document_id,
            "unit_id": compact(row.get("id")),
            "source_type": "engineering_attachment",
            "format": compact(parent.get("format")),
            "extraction_method": compact(row.get("extraction_method")),
            "ocr_min_confidence": row.get("ocr_min_confidence"),
            "topics": compact(row.get("topics")),
            "document_topics": compact(row.get("document_topics")),
            "source_kind": compact(row.get("source_kind")),
            "independently_verified": bool(row.get("independently_verified")),
            "manual_review_required": bool(row.get("manual_review_required")),
            "parent_source_ids": compact(row.get("parent_source_ids")),
            "knowledge_version": "attachments_v2",
            "source_coverage": row.get("source_coverage"),
            "parser_stats": parent.get("parser_stats", {}),
            "training_allowed": row.get("training_allowed"),
        }
        yield "attachment_" + compact(row.get("id")), body, metadata


def public_rows(root: Path) -> Iterable[tuple[str, str, dict[str, Any]]]:
    for version in ("v1", "v2"):
        manifests = {}
        base = root / f"data/public_lamination_{version}"
        for row in json.loads((base / "manifest.json").read_text(encoding="utf-8")):
            manifests[row.get("id")] = {**row, "knowledge_version": f"public_lamination_{version}"}
        filename = "pages.jsonl" if version == "v1" else "units.jsonl"
        for row in read_jsonl(base / filename):
            text = compact(row.get("text"))
            if not text:
                continue
            source_id = compact(row.get("source_id"))
            if source_id not in manifests:
                raise ValueError(f"public unit missing manifest source: {source_id}")
            info = manifests[source_id]
            locator = compact(row.get("page", row.get("locator", "")))
            body = (f"# 公开工程资料：{compact(info.get('title')) or source_id}\n\n"
                    f"来源 ID：{source_id}\n定位：{locator}\n"
                    f"发布者：{compact(info.get('publisher'))}\n"
                    f"来源链接：{compact(info.get('url'))}\n"
                    f"版本说明：{compact(info.get('document_revision') or info.get('revision_status'))}\n"
                    f"权利说明：{compact(info.get('rights_status'))}\n\n## 原文摘录\n{text}")
            metadata = {
                "source_id": source_id,
                "source_type": "public_reference",
                "source_group": compact(row.get("source_group") or info.get("source_group")),
                "publisher": compact(info.get("publisher")),
                "kind": compact(info.get("kind")),
                "split": compact(row.get("split") or info.get("split")),
                "locator_type": compact(row.get("locator_type")) or ("page" if "page" in row else "unit"),
                "locator": locator,
                "url": compact(info.get("url")),
                "document_revision": compact(info.get("document_revision")),
                "rights_status": compact(info.get("rights_status")),
                "raw_text_training_allowed": bool(info.get("raw_text_training_allowed", False)),
                "content_status": compact(info.get("content_status")),
                "knowledge_version": compact(info.get("knowledge_version")),
                "document_sha256": info.get("sha256"),
                "unit_sha256": row.get("sha256"),
                "license_statement": info.get("license_statement", ""),
                "cloud_usage_status": "not_assessed_by_exporter",
            }
            unit = row.get("page", row.get("unit"))
            yield f"public_{version}_{source_id}_{unit}", body, metadata


def incremental_rows(root: Path) -> Iterable[tuple[str, str, dict[str, Any]]]:
    records = root / "data/continuous/records"
    latest = {}
    for path in sorted(records.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not row.get("id") or not row.get("group_id"):
            raise ValueError(f"incremental record missing identity: {path.name}")
        group = row["group_id"]
        previous = latest.get(group)
        if previous is None or (row["created_at"], row["id"]) > (previous["created_at"], previous["id"]):
            latest[group] = row
    for group, row in sorted(latest.items()):
        if not row.get("eligible"):
            continue
        samples = row.get("samples") or []
        for index, sample in enumerate(samples):
            text = compact(sample.get("text"))
            if not text:
                continue
            body = (f"# 企业经验增量记录：{compact(row.get('id'))}\n\n"
                    f"证据状态：{compact(row.get('evidence_status'))}\n\n{text}")
            metadata = {
                "record_id": compact(row.get("id")),
                "group_id": compact(row.get("group_id")),
                "source_type": "enterprise_incremental",
                "source_kind": compact(sample.get("source_kind")),
                "evidence_status": compact(row.get("evidence_status")),
                "created_at": row.get("created_at"),
                "sample_index": index,
                "knowledge_version": "continuous_records",
            }
            yield f"incremental_{group}_{index}", body, metadata


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("cases", "attachments", "public", "incremental"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None, help="write at most N documents (for smoke tests)")
    args = parser.parse_args()
    factories = {
        "cases": case_rows, "attachments": attachment_rows,
        "public": public_rows, "incremental": incremental_rows,
    }
    summary = write_bundle(args.output, factories[args.kind](ROOT), args.limit)
    print(json.dumps({"kind": args.kind, **summary, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
