"""Read-only, aggregate-only diagnostics for Catalog mail documents."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


DEFAULT_DATABASE = Path("data/phase3_catalog_v3/catalog.sqlite3")
DEFAULT_PRIVACY_POLICY = Path("privacy_policy.json")
BRACKET_MARKER = re.compile(r"\[[^\]]{1,32}\]")
HAN = re.compile(r"[\u3400-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
REPLY_OR_FORWARD = re.compile(r"^(?:RE|FW|FWD)\s*:", re.IGNORECASE)
REPLY_PREFIX = re.compile(r"^RE\s*:", re.IGNORECASE)
FORWARD_PREFIX = re.compile(r"^(?:FW|FWD)\s*:", re.IGNORECASE)


def _percentile(sorted_values: list[int], fraction: float) -> int | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _readonly_connection(database: Path) -> sqlite3.Connection:
    uri = database.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_collection_policy(path: Path) -> dict[str, bool]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("privacy_policy_unavailable") from exc
    if not isinstance(raw, dict):
        raise ValueError("privacy_policy_invalid")
    fields = ("export_exact_dates", "export_headers")
    if any(type(raw.get(field)) is not bool for field in fields):
        raise ValueError("privacy_policy_flags_invalid")
    return {field: raw[field] for field in fields}


def audit(
    database: Path = DEFAULT_DATABASE,
    privacy_policy: Path = DEFAULT_PRIVACY_POLICY,
) -> dict[str, object]:
    """Return aggregate mail-quality metrics without exposing source values."""
    collection_policy = _load_collection_policy(privacy_policy)
    with closing(_readonly_connection(database)) as connection:
        rows = connection.execute(
            """
            SELECT d.title, d.source_created_at, d.duplicate_group_id, d.metadata_json,
                   COALESCE(c.normalized_text, c.original_text)
            FROM documents AS d
            JOIN contents AS c ON c.content_id=d.content_id
            WHERE d.type='MAIL'
            """
        ).fetchall()

    lengths: list[int] = []
    buckets: Counter[str] = Counter()
    language: Counter[str] = Counter()
    attachment_counts: Counter[str] = Counter()
    metadata_date_count = 0
    parent_link_fields = 0
    parent_links = 0
    reply_titles = 0
    forward_titles = 0
    linked_reply_titles = 0
    timestamps = 0
    bracket_markers = 0
    replies = 0
    duplicate_groups: Counter[str] = Counter()
    repeated_line_ratios: list[float] = []

    for title, source_created_at, group_id, metadata_json, body in rows:
        text = body or ""
        lengths.append(len(text))
        if len(text) < 100:
            buckets["under_100"] += 1
        elif len(text) < 300:
            buckets["100_to_299"] += 1
        elif len(text) < 1000:
            buckets["300_to_999"] += 1
        elif len(text) < 3000:
            buckets["1000_to_2999"] += 1
        else:
            buckets["3000_plus"] += 1

        han_count = len(HAN.findall(text))
        latin_count = len(LATIN.findall(text))
        language["mixed" if han_count and latin_count else
                 "han_only" if han_count else "latin_only" if latin_count else "none"] += 1
        bracket_markers += bool(BRACKET_MARKER.search(text))
        title = title or ""
        is_reply = bool(REPLY_PREFIX.match(title))
        is_forward = bool(FORWARD_PREFIX.match(title))
        is_reply_or_forward = bool(REPLY_OR_FORWARD.match(title))
        replies += is_reply_or_forward
        reply_titles += is_reply
        forward_titles += is_forward
        timestamps += bool(source_created_at)
        duplicate_groups[group_id] += 1

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 10:
            repeated_line_ratios.append(1 - len(set(lines)) / len(lines))

        try:
            metadata = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        nested = metadata.get("metadata")
        sources = (metadata, nested if isinstance(nested, dict) else {})
        parent_link_fields += any("parent_source_ids" in source for source in sources)
        has_parent_link = any(
            isinstance(source.get("parent_source_ids"), list)
            and bool(source["parent_source_ids"])
            for source in sources
        )
        parent_links += has_parent_link
        linked_reply_titles += is_reply and has_parent_link
        metadata_date_count += any(
            any(token in key.lower() for token in ("date", "time", "sent", "received"))
            for source in sources
            for key in source
            if isinstance(key, str)
        )
        try:
            attachment_counts[str(max(0, int(metadata.get("excluded_attachment_count") or 0)))] += 1
        except (TypeError, ValueError):
            attachment_counts["invalid"] += 1

    lengths.sort()
    repeated_line_ratios.sort()
    count = len(rows)
    duplicate_docs = sum(size for size in duplicate_groups.values() if size > 1)
    repeated_over_30 = sum(ratio > 0.30 for ratio in repeated_line_ratios)
    signals: list[dict[str, object]] = []
    timestamp_coverage = _rate(timestamps, count)
    if (collection_policy["export_exact_dates"] and timestamp_coverage is not None
            and timestamp_coverage < 0.90):
        signals.append({
            "code": "source_timestamp_coverage_low",
            "observed_rate": timestamp_coverage,
            "minimum_rate": 0.90,
            "next_step": "Trace original mail date fields into source_created_at; do not infer them from indexed_at.",
        })
    reply_rate = _rate(replies, count)
    if reply_rate is not None and reply_rate > 0.50:
        signals.append({
            "code": "reply_forward_titles_common",
            "observed_rate": reply_rate,
            "minimum_rate": 0.50,
            "next_step": "Review thread-link coverage before treating title prefixes as conversation boundaries.",
        })
    reply_link_coverage = _rate(linked_reply_titles, reply_titles)
    if (collection_policy["export_headers"] and reply_link_coverage is not None
            and reply_link_coverage < 0.80):
        signals.append({
            "code": "reply_parent_link_coverage_low",
            "observed_rate": reply_link_coverage,
            "minimum_rate": 0.80,
            "next_step": "Check whether the source archive exposes parent or conversation IDs for replies.",
        })
    repeated_rate = _rate(repeated_over_30, len(repeated_line_ratios))
    if repeated_rate is not None and repeated_rate > 0.25:
        signals.append({
            "code": "repeated_lines_common",
            "observed_rate": repeated_rate,
            "minimum_rate": 0.25,
            "next_step": "Review quote and signature handling while preserving source text and excerpt offsets.",
        })
    return {
        "schema": "pcb-mail-corpus-audit-v1",
        "read_only": True,
        "aggregate_only": True,
        "mail_documents": count,
        "body_length": {
            "min": lengths[0] if lengths else None,
            "p10": _percentile(lengths, 0.10),
            "median": _percentile(lengths, 0.50),
            "p90": _percentile(lengths, 0.90),
            "max": lengths[-1] if lengths else None,
            "buckets": dict(sorted(buckets.items())),
        },
        "body_language_mix": dict(sorted(language.items())),
        "documents_with_bracket_markers": bracket_markers,
        "reply_or_forward_titles": replies,
        "reply_or_forward_title_rate": reply_rate,
        "reply_title_count": reply_titles,
        "forward_title_count": forward_titles,
        "reply_titles_with_parent_link": linked_reply_titles,
        "reply_parent_link_coverage": reply_link_coverage,
        "source_created_at_present": timestamps,
        "source_created_at_coverage": timestamp_coverage,
        "metadata_date_fields_present": metadata_date_count,
        "metadata_parent_source_ids_field_present": parent_link_fields,
        "metadata_parent_source_ids_nonempty": parent_links,
        "collection_policy_context": {
            "exact_dates": "collected" if collection_policy["export_exact_dates"] else "excluded_by_policy",
            "mail_headers": "collected" if collection_policy["export_headers"] else "excluded_by_policy",
        },
        "duplicate_groups_with_multiple_mails": sum(size > 1 for size in duplicate_groups.values()),
        "mail_documents_in_duplicate_groups": duplicate_docs,
        "excluded_attachment_count_distribution": dict(sorted(
            attachment_counts.items(),
            key=lambda item: (item[0] == "invalid", int(item[0]) if item[0].isdigit() else -1),
        )),
        "repeated_line_ratio": {
            "eligible_documents": len(repeated_line_ratios),
            "median": round(_percentile_float(repeated_line_ratios, 0.50), 4),
            "p90": round(_percentile_float(repeated_line_ratios, 0.90), 4),
            "over_30_percent": repeated_over_30,
            "over_30_percent_rate": repeated_rate,
        },
        "signals": signals,
        "limits": [
            "Counts describe indexed mail documents, not unique conversations or verified cases.",
            "Text-derived indicators are heuristic and do not establish evidence quality.",
            "No source text, title, identifier, path, or metadata value is included.",
        ],
    }


def _percentile_float(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.database), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
