"""Build and search a local reference pack from the existing public archives."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from pcb_assistant.catalog.common import CatalogError
from pcb_assistant.catalog.evidence import _excerpt_window

from .models import canonical_json
from .providers import _safe_url
from .source_policy import PUBLIC_KINDS, public_source_tier
from .taxonomy import CAUSES, DEFECT_MODES, term_present


PACK_SCHEMA = "lamination-public-pack-v1"
DEFAULT_PACK_PATH = Path("data/agent_public_pack_v1.json")
MAX_PACK_BYTES = 20 * 1024 * 1024
SOURCE_FIELDS = (
    "id", "title", "url", "publisher", "kind", "split", "sha256",
    "document_revision", "revision_status", "rights_status", "usage",
    "raw_text_training_allowed", "source_group",
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("public_pack_input_unavailable") from exc


def _fingerprint(pack: dict) -> str:
    return _hash(canonical_json({key: value for key, value in pack.items()
                                 if key != "fingerprint"}).encode("utf-8"))


def _source_key(version: str, source_id: str) -> str:
    return version + ":" + source_id


def _unit_id(source_key: str, ordinal: int, text_sha256: str) -> str:
    return "PUBUNIT_" + _hash(canonical_json([source_key, ordinal, text_sha256]).encode("utf-8"))[:24]


def _notes(source: dict, findings: list) -> list[str]:
    notes = []
    for finding in findings:
        ids = str(finding.get("source") or "").split(",")
        applies = source["id"] in ids or (
            "ipc_test_methods" in ids and source.get("kind") == "test_method"
        ) or (
            "all_non_PCB_studies" in ids
            and source.get("kind") in {"university_course", "open_access_article"}
        )
        if applies and finding.get("issue"):
            # Keep whole-source warnings: a finding's section/page scope must
            # not be interpreted as permission to ignore it elsewhere.
            notes.append(str(finding["issue"]))
    return notes


def build_pack(*, root: Path | str = Path.cwd(), output: Path | str = DEFAULT_PACK_PATH) -> dict:
    root = Path(root).resolve()
    target = Path(output).resolve()
    if target.suffix.lower() != ".json":
        raise CatalogError("public_pack_requires_json")
    protected = [root / "data" / ("public_lamination_" + version) for version in ("v1", "v2")]
    if any(target == directory or directory in target.parents for directory in protected):
        raise CatalogError("public_pack_output_inside_source")
    if target.exists():
        raise CatalogError("public_pack_output_exists")
    sources, units, skipped = [], [], Counter()
    input_hashes = {}
    for version, filename, ordinal_key in (("v1", "pages.jsonl", "page"), ("v2", "units.jsonl", "unit")):
        base = root / "data" / ("public_lamination_" + version)
        manifest = _json(base / "manifest.json")
        findings = _json(base / "quality_findings.json")
        if (not isinstance(manifest, list) or not isinstance(findings, list)
                or not all(isinstance(row, dict) for row in manifest + findings)):
            raise CatalogError("invalid_public_pack_source_manifest")
        selected = {}
        for row in manifest:
            if row.get("split") != "train":
                skipped["evaluation_sources"] += 1
                continue
            if row.get("content_status") != "usable_text":
                skipped["unusable_sources"] += 1
                continue
            if (row.get("kind") not in PUBLIC_KINDS or not _safe_url(row.get("url"))
                    or not isinstance(row.get("id"), str) or not row["id"]):
                raise CatalogError("invalid_public_pack_source_manifest")
            source_id = row["id"]
            if source_id in selected:
                raise CatalogError("duplicate_public_pack_source")
            raw = (base / str(row.get("raw_path") or "")).resolve()
            if not raw.is_relative_to(base) or not raw.is_file():
                raise CatalogError("public_pack_raw_unavailable")
            if _hash(raw.read_bytes()) != row.get("sha256"):
                raise CatalogError("public_pack_raw_hash_mismatch")
            source = {key: row[key] for key in SOURCE_FIELDS if key in row}
            source.update({"source_key": _source_key(version, source_id), "version": version,
                           "source_tier": public_source_tier(row), "quality_notes": _notes(row, findings)})
            selected[source_id] = source
        for name in ("manifest.json", "quality_findings.json", filename):
            try:
                input_hashes[version + "/" + name] = _hash((base / name).read_bytes())
            except OSError as exc:
                raise CatalogError("public_pack_input_unavailable") from exc
        try:
            lines = (base / filename).read_text(encoding="utf-8").splitlines()
            rows = [json.loads(line) for line in lines if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError("public_pack_input_unavailable") from exc
        for row in rows:
            if not isinstance(row, dict):
                raise CatalogError("invalid_public_pack_unit")
            source = selected.get(row.get("source_id"))
            if source is None:
                continue
            text, ordinal = row.get("text"), row.get(ordinal_key)
            if not isinstance(text, str) or row.get("split") != "train" or type(ordinal) is not int or ordinal < 1:
                raise CatalogError("invalid_public_pack_unit")
            if _hash(text.encode("utf-8")) != row.get("sha256"):
                raise CatalogError("public_pack_text_hash_mismatch")
            if not text.strip():
                skipped["empty_units"] += 1
                continue
            units.append({
                "unit_id": _unit_id(source["source_key"], ordinal, row["sha256"]),
                "source_key": source["source_key"], "ordinal": ordinal,
                "locator_type": row.get("locator_type", "pdf_page"),
                "locator": str(row.get("locator", ordinal)),
                "text": text, "sha256": row["sha256"],
            })
        used = {unit["source_key"] for unit in units}
        sources.extend(source for source in selected.values() if source["source_key"] in used)
    if not units:
        raise CatalogError("public_pack_empty")
    pack = {"schema": PACK_SCHEMA, "scope": "public_train_reference_only",
            "sources": sorted(sources, key=lambda item: item["source_key"]),
            "units": sorted(units, key=lambda item: item["unit_id"]),
            "input_sha256": input_hashes, "skipped": dict(skipped)}
    pack["fingerprint"] = _fingerprint(pack)
    _validate_pack(pack)
    payload = (canonical_json(pack, pretty=True) + "\n").encode("utf-8")
    if len(payload) > MAX_PACK_BYTES:
        raise CatalogError("public_pack_too_large")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CatalogError("public_pack_output_exists") from exc
    return {**pack_summary(pack), "output": str(target)}


def _validate_pack(pack: dict) -> None:
    if (not isinstance(pack, dict) or pack.get("schema") != PACK_SCHEMA
            or pack.get("scope") != "public_train_reference_only"):
        raise CatalogError("invalid_public_pack")
    if pack.get("fingerprint") != _fingerprint(pack):
        raise CatalogError("public_pack_fingerprint_mismatch")
    if (not isinstance(pack.get("skipped"), dict) or not isinstance(pack.get("input_sha256"), dict)
            or not all(isinstance(value, str) and HASH_RE.fullmatch(value)
                       for value in pack["input_sha256"].values())):
        raise CatalogError("invalid_public_pack")
    sources, units = pack.get("sources"), pack.get("units")
    if (not isinstance(sources, list) or not sources or not isinstance(units, list) or not units
            or not all(isinstance(row, dict) for row in sources + units)):
        raise CatalogError("invalid_public_pack")
    by_key = {}
    for source in sources:
        key = source.get("source_key")
        if (not isinstance(key, str) or not key or key in by_key
                or source.get("split") != "train" or source.get("kind") not in PUBLIC_KINDS
                or not _safe_url(source.get("url")) or not isinstance(source.get("id"), str)
                or not isinstance(source.get("title"), str) or not source["title"]
                or not isinstance(source.get("publisher"), str)
                or not isinstance(source.get("sha256"), str) or not HASH_RE.fullmatch(source["sha256"])
                or source.get("source_tier") != public_source_tier(source)
                or not isinstance(source.get("quality_notes"), list)
                or not all(isinstance(note, str) for note in source["quality_notes"])):
            raise CatalogError("invalid_public_pack_source")
        by_key[key] = source
    seen = set()
    for unit in units:
        text, ordinal = unit.get("text"), unit.get("ordinal")
        if (unit.get("source_key") not in by_key or not isinstance(text, str) or not text.strip()
                or type(ordinal) is not int or ordinal < 1
                or not isinstance(unit.get("locator"), str) or not isinstance(unit.get("locator_type"), str)
                or _hash(text.encode("utf-8")) != unit.get("sha256")):
            raise CatalogError("invalid_public_pack_unit")
        expected_id = _unit_id(unit["source_key"], ordinal, unit["sha256"])
        if unit.get("unit_id") != expected_id or expected_id in seen:
            raise CatalogError("invalid_public_pack_unit_id")
        seen.add(expected_id)


def load_pack(path: Path | str) -> dict:
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PACK_BYTES + 1)
    except OSError as exc:
        raise CatalogError("public_pack_unavailable") from exc
    if len(raw) > MAX_PACK_BYTES:
        raise CatalogError("public_pack_too_large")
    try:
        pack = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("invalid_public_pack") from exc
    try:
        _validate_pack(pack)
    except CatalogError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError("invalid_public_pack") from exc
    return pack


def pack_summary(pack: dict) -> dict:
    return {"schema": PACK_SCHEMA, "valid": True, "scope": pack["scope"],
            "source_count": len(pack["sources"]), "unit_count": len(pack["units"]),
            "source_tier_counts": dict(Counter(source["source_tier"] for source in pack["sources"])),
            "skipped": pack["skipped"], "fingerprint": pack["fingerprint"],
            "external_services": False, "engineering_conclusion_supported": False}


class PublicPackRetriever:
    """Deterministic literal keyword retrieval without a Catalog or network."""

    provider_name = "local_public_pack"

    def __init__(self, path: Path | str):
        self.pack = load_pack(path)
        self.sources = {source["source_key"]: source for source in self.pack["sources"]}

    def search(self, query: str, *, limit: int = 6) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise CatalogError("query_required")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CatalogError("invalid_search_limit")
        vocabulary = {term for cause in CAUSES.values() for term in cause.keywords}
        vocabulary.update(term for mode in DEFECT_MODES.values() for term in mode.aliases)
        folded = query.casefold()
        terms = sorted(term.casefold() for term in vocabulary if term_present(folded, term))
        matches = []
        for unit in self.pack["units"]:
            lower = unit["text"].casefold()
            hits = [term for term in terms if term_present(lower, term)]
            if not hits:
                continue
            source = self.sources[unit["source_key"]]
            tier = source["source_tier"]
            # Select a literal original-text window. ASCII keywords have
            # stable casefold offsets; Chinese terms are unchanged.
            term = max(hits, key=lambda term: (len(term), term))
            match = re.search(re.escape(term), unit["text"], re.IGNORECASE)
            if match is None:
                continue
            start, end = _excerpt_window(unit["text"], match.start(), match.end())
            text = unit["text"][start:end]
            excerpt_hash = _hash(text.encode("utf-8"))
            citation_id = "EXC_PUB_" + _hash(canonical_json(
                [unit["unit_id"], start, end, excerpt_hash]).encode("utf-8"))[:24]
            matches.append({
                "citation_id": citation_id, "doc_id": unit["unit_id"],
                "source_id": source["source_key"], "source_type": "public_reference",
                "source_tier": tier, "tier_weight": {"A": 3, "B": 2}[tier],
                "title": source["title"], "text": text, "url": source["url"],
                "provider": self.provider_name, "evidence_status": "published_reference_not_independently_verified",
                "matched_terms": [term for term in hits if term_present(text.casefold(), term)],
                "retrieval_score": -len(hits), "locator": {
                    "type": unit["locator_type"], "value": unit["locator"],
                    "start": start, "end": end, "sha256": excerpt_hash,
                },
                "provenance": {key: source.get(key) for key in (
                    "publisher", "kind", "document_revision", "revision_status", "rights_status",
                    "quality_notes", "sha256", "split",
                )},
            })
        matches.sort(key=lambda item: (item["retrieval_score"], -item["tier_weight"], item["citation_id"]))
        # A single long document must not consume the whole excerpt budget.
        selected, source_counts = [], Counter()
        for item in matches:
            if source_counts[item["source_id"]] >= 2:
                continue
            selected.append(item)
            source_counts[item["source_id"]] += 1
            if len(selected) == limit:
                break
        return selected
