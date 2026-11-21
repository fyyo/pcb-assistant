"""Validated input and result helpers for the lamination agent."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from pcb_assistant.catalog.common import CatalogError


AGENT_SCHEMA = "lamination-agent-v1"
AGENT_FINGERPRINT_ALGORITHM = "sha256-canonical-json-v1"
MAX_QUESTION_CHARS = 4000
MAX_FIELD_CHARS = 500
MAX_PARAMETER_ITEMS = 40


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    options = {"allow_nan": False, "ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    try:
        return json.dumps(value, **options)
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_agent_result") from exc


def fingerprint_result(result: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in result.items()
        if key not in {"fingerprint_algorithm", "result_fingerprint"}
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise CatalogError(f"{name}_required")
        return None
    if not isinstance(value, str):
        raise CatalogError(f"invalid_{name}")
    value = value.strip()
    if required and not value:
        raise CatalogError(f"{name}_required")
    if len(value) > MAX_FIELD_CHARS:
        raise CatalogError(f"{name}_too_long")
    return value or None


@dataclass(frozen=True)
class CaseInput:
    """One bounded lamination case supplied by a user or agent platform."""

    question: str
    case_id: str = ""
    defect: str | None = None
    stage: str | None = None
    location: str | None = None
    material: str | None = None
    stackup: str | None = None
    equipment: str | None = None
    parameters: Mapping[str, str] = field(default_factory=dict)
    batch: str | None = None
    timing: str | None = None
    domain: str | None = None
    notes: str | None = None
    language: str = "zh"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CaseInput":
        if not isinstance(value, Mapping):
            raise CatalogError("invalid_case")
        question = _text(value.get("question"), "question", required=True)
        if len(question) > MAX_QUESTION_CHARS:
            raise CatalogError("question_too_long")
        parameters = value.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            raise CatalogError("invalid_parameters")
        if len(parameters) > MAX_PARAMETER_ITEMS:
            raise CatalogError("too_many_parameters")
        clean_parameters = {}
        for key, item in parameters.items():
            clean_key = _text(key, "parameter_name", required=True)
            clean_value = _text(item, "parameter_value", required=True)
            clean_parameters[clean_key] = clean_value
        return cls(
            question=question,
            case_id=_text(value.get("case_id"), "case_id") or "",
            defect=_text(value.get("defect"), "defect"),
            stage=_text(value.get("stage"), "stage"),
            location=_text(value.get("location"), "location"),
            material=_text(value.get("material"), "material"),
            stackup=_text(value.get("stackup"), "stackup"),
            equipment=_text(value.get("equipment"), "equipment"),
            parameters=clean_parameters,
            batch=_text(value.get("batch"), "batch"),
            timing=_text(value.get("timing"), "timing"),
            domain=_text(value.get("domain"), "domain"),
            notes=_text(value.get("notes"), "notes"),
            language=_text(value.get("language"), "language") or "zh",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value for key, value in {
                "question": self.question,
                "case_id": self.case_id,
                "defect": self.defect,
                "stage": self.stage,
                "location": self.location,
                "material": self.material,
                "stackup": self.stackup,
                "equipment": self.equipment,
                "parameters": dict(self.parameters),
                "batch": self.batch,
                "timing": self.timing,
                "domain": self.domain,
                "notes": self.notes,
                "language": self.language,
            }.items() if value not in (None, "", {})
        }
