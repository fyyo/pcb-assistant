"""Anonymized per-message processing status ledger.

The ledger records only the opaque message ID, a processing status, and an
optional duplicate link. It must never contain subjects, bodies, titles, source
paths, identities, or content fingerprints.
"""
from __future__ import annotations

import collections
import re
from collections.abc import Mapping

MESSAGE_ID = re.compile(r"mail_[0-9a-f]{24}")
MESSAGE_STATUS_SCHEMA_VERSION = 1
MESSAGE_STATUS_VALUES = (
    "candidate",
    "duplicate",
    "no_text_body",
    "empty_after_redaction",
    "residual_quarantine",
    "oversize_body",
    "contact_read_error",
    "body_read_error",
    "processing_error",
)
MESSAGE_STATUS_SET = frozenset(MESSAGE_STATUS_VALUES)
MESSAGE_STATUS_ROW_FIELDS = frozenset({"id", "status", "duplicate_of"})
STATUS_STAT_FIELDS = {
    "candidate": "unique_candidates",
    "duplicate": "duplicate_candidates",
    "no_text_body": "no_text_body",
    "empty_after_redaction": "empty_after_redaction",
    "residual_quarantine": "residual_quarantine",
    "oversize_body": "oversize_body",
    "contact_read_error": "contact_read_error",
    "body_read_error": "body_read_error",
    "processing_error": "processing_error",
}


class MessageStatusError(ValueError):
    """Raised when a status row would be ambiguous or unsafe."""


def _validate_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not MESSAGE_ID.fullmatch(value):
        raise MessageStatusError(f"invalid {field}")
    return value


def _validate_sequence(value: object) -> int:
    if type(value) is not int or value < 1:
        raise MessageStatusError("invalid message sequence")
    return value


class MessageStatusLedger:
    """Collect one deterministic status row for every visited message."""

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, object]] = {}
        self._ids: set[str] = set()
        self._first_by_fingerprint: dict[bytes, str] = {}

    def _add(
        self,
        sequence: int,
        ident: str,
        status: str,
        duplicate_of: str | None = None,
    ) -> dict[str, object]:
        sequence = _validate_sequence(sequence)
        ident = _validate_id(ident, "message id")
        if not isinstance(status, str) or status not in MESSAGE_STATUS_SET:
            raise MessageStatusError("invalid message status")
        if status == "duplicate":
            duplicate_of = _validate_id(duplicate_of, "duplicate target")
            if duplicate_of == ident:
                raise MessageStatusError("message cannot duplicate itself")
        elif duplicate_of is not None:
            raise MessageStatusError("duplicate target only allowed for duplicate status")
        if sequence in self._rows:
            raise MessageStatusError("duplicate message sequence")
        if ident in self._ids:
            raise MessageStatusError("duplicate opaque message id")
        row = {"id": ident, "status": status, "duplicate_of": duplicate_of}
        self._rows[sequence] = row
        self._ids.add(ident)
        return dict(row)

    def record_reject(self, sequence: int, ident: str, status: str) -> dict[str, object]:
        """Record a non-candidate terminal status."""
        if status in {"candidate", "duplicate"}:
            raise MessageStatusError("candidate statuses require record_candidate")
        return self._add(sequence, ident, status)

    def record_candidate(
        self, sequence: int, ident: str, fingerprint: bytes
    ) -> tuple[dict[str, object], bool]:
        """Record a candidate or a duplicate linked to the retained candidate.

        The content fingerprint is used only in memory to establish the link. It
        is never written to the ledger.
        """
        if not isinstance(fingerprint, (bytes, bytearray)) or not fingerprint:
            raise MessageStatusError("invalid content fingerprint")
        fingerprint = bytes(fingerprint)
        validated_ident = _validate_id(ident, "message id")
        validated_sequence = _validate_sequence(sequence)
        first = self._first_by_fingerprint.get(fingerprint)
        if first is not None:
            row = self._add(validated_sequence, validated_ident, "duplicate", first)
            return row, True
        row = self._add(validated_sequence, validated_ident, "candidate")
        self._first_by_fingerprint[fingerprint] = validated_ident
        return row, False

    def rows(self) -> list[dict[str, object]]:
        return [dict(self._rows[sequence]) for sequence in sorted(self._rows)]

    def counts(self) -> dict[str, int]:
        return dict(sorted(collections.Counter(
            row["status"] for row in self._rows.values()
        ).items()))


def validate_message_status_rows(
    rows: list[dict[str, object]], stats: Mapping[str, object]
) -> dict[str, int]:
    """Validate the complete ledger against the sanitizer aggregate counters."""
    if not isinstance(rows, list):
        raise MessageStatusError("message status rows must be a list")
    if not isinstance(stats, Mapping):
        raise MessageStatusError("message status stats must be a mapping")
    expected_count = stats.get("messages_visited")
    if type(expected_count) is not int or expected_count < 0:
        raise MessageStatusError("invalid messages_visited count")

    counts: collections.Counter[str] = collections.Counter()
    ids: set[str] = set()
    candidate_ids: set[str] = set()
    duplicate_links: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != MESSAGE_STATUS_ROW_FIELDS:
            raise MessageStatusError("unexpected message status schema")
        ident = _validate_id(row["id"], "message id")
        status = row["status"]
        if not isinstance(status, str) or status not in MESSAGE_STATUS_SET:
            raise MessageStatusError("invalid message status")
        duplicate_of = row["duplicate_of"]
        if status == "duplicate":
            duplicate_of = _validate_id(duplicate_of, "duplicate target")
            if duplicate_of == ident:
                raise MessageStatusError("message cannot duplicate itself")
            duplicate_links.append((ident, duplicate_of))
        elif duplicate_of is not None:
            raise MessageStatusError("duplicate target only allowed for duplicate status")
        if ident in ids:
            raise MessageStatusError("duplicate opaque message id")
        ids.add(ident)
        if status == "candidate":
            candidate_ids.add(ident)
        counts[status] += 1

    if len(rows) != expected_count:
        raise MessageStatusError("message status count does not match messages_visited")
    for status, stat_field in STATUS_STAT_FIELDS.items():
        expected = stats.get(stat_field, 0)
        if type(expected) is not int or expected < 0:
            raise MessageStatusError(f"invalid aggregate count {stat_field}")
        if counts[status] != expected:
            raise MessageStatusError(f"message status count mismatch: {status}")
    for ident, duplicate_of in duplicate_links:
        if duplicate_of not in candidate_ids:
            raise MessageStatusError(
                f"duplicate target is not a retained candidate: {ident}"
            )
    return dict(sorted(counts.items()))
