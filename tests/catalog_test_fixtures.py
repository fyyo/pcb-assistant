"""Synthetic catalog fixtures shared by non-web tests."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path

from catalog_build import PRIMARY_INPUTS, build
from catalog_common import digest_file
from .test_catalog import fixture, write_json, write_rows


HTML_TITLE = '压合试验 <img src=x onerror="window.sourceExecuted=true">'


def make_web_fixture(root: Path) -> Path:
    """Enough records to exercise deterministic retrieval and relationship pagination without private text."""
    fixture(root)
    def rows(relative):
        return [json.loads(line) for line in (root / relative).read_text(encoding="utf-8").splitlines()]
    mails = rows("data/sanitized_v3/candidates.jsonl")
    extra_ids = [f"mail_synthetic_{index:02d}" for index in range(12)]
    missing_ids = [f"mail_missing_{index:02d}" for index in range(12)]
    mails.extend({"id": source_id, "subject": f"合成邮件记录 {index + 1:02d}",
                  "text": f"第 {index + 1} 组试验的合成正文，仅供软件检查。", "topics": [],
                  "source_kind": "untrusted_email", "independently_verified": False}
                 for index, source_id in enumerate(extra_ids))
    write_rows(root, "data/sanitized_v3/candidates.jsonl", mails)
    documents = rows("data/attachments_v2/documents.jsonl")
    documents[0]["parent_source_ids"].extend(extra_ids + missing_ids)
    documents[0]["exported_units"] = 26
    write_rows(root, "data/attachments_v2/documents.jsonl", documents)
    occurrences = rows("data/attachments_v2/occurrences.jsonl")
    occurrences.extend({"document_id": "att_a", "parent_source_id": source_id,
                        "attachment_ordinal": 0, "status": "read"} for source_id in extra_ids + missing_ids)
    write_rows(root, "data/attachments_v2/occurrences.jsonl", occurrences)
    units = rows("data/attachments_v2/engineering_candidates.jsonl")
    units[0]["subject"] = HTML_TITLE
    units[0]["topics"].append("测试自定义主题")
    units.extend({"id": f"unit_synthetic_{index:02d}", "document_id": "att_a",
                  "subject": f"PCB 工程合成记录 {index + 1:02d}",
                  "text": f"软件分页检查第 {index + 1} 组记录：合成试验文本，不是工程建议。",
                  "topics": ["质量与验证"], "document_topics": [], "parent_source_ids": ["mail_a"],
                  "source_kind": "untrusted_attachment", "independently_verified": False,
                  "extraction_method": "native_text"} for index in range(25))
    for unit in units:
        if unit["document_id"] == "att_a":
            unit["parent_source_ids"] = list(documents[0]["parent_source_ids"])
    write_rows(root, "data/attachments_v2/engineering_candidates.jsonl", units)
    reports = {}
    for relative in PRIMARY_INPUTS:
        path = root / relative
        reports.setdefault(path.parent, {"artifacts": {}})["artifacts"][path.name] = {"sha256": digest_file(path)}
    for parent, report in reports.items():
        write_json(parent / "report.json", report)
    output = root / "data/phase3_catalog_synthetic_test"
    with redirect_stdout(io.StringIO()):
        build(root, output)
    return output
