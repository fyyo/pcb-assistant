"""Source provenance and explicit limits on diagnostic applicability."""
import re
from urllib.parse import urlsplit


PUBLIC_KINDS = {
    "technical_guide", "vendor_technical_paper", "vendor_hosted_technical_paper",
    "test_method", "government_handbook", "equipment_overview",
    "equipment_brochure", "university_course", "open_access_article",
}

OCR_CAUTION = "OCR 摘录的文字、数值和单位尚未对照原图核实，不可直接作为工艺设定。"

# Archived, reviewed CFRP patch-repair study. Scope applies to every section,
# including excerpts that do not repeat the study's material or repair context.
# Its published content remains available for reference retrieval.
CFRP_REPAIR_SOURCE_SHA256 = {
    "b383ba2a9adbd0cdbd2e8a29aa38c5d5125972a1532143d5f167e38bccac1897",
}
_CFRP = re.compile(r"\bcfrp\b|\bcarbon[ -](?:fibre|fiber)[ -]reinforced\b|碳[纤纖][维維]", re.I)
_REPAIR = re.compile(r"\brepair(?:s|ing)?\b|修[复復補补]", re.I)


def diagnostic_background_scope(item: dict) -> dict | None:
    """Restrict explicit CFRP-repair research, without certifying other sources.

    Source titles can establish a domain restriction, but never add causal
    support. Generic epoxy/glass-fabric terms do not establish such a restriction.
    """
    provenance = item.get("provenance") or {}
    if provenance.get("sha256") in CFRP_REPAIR_SOURCE_SHA256:
        basis = "reviewed_source_sha256"
    elif item.get("source_type") in {"public_reference", "external_web_search"}:
        title = " ".join(str(value or "") for value in (
            item.get("title"), provenance.get("source_title")))
        text = " ".join(str(item.get("text") or "").split())[:600]
        if _CFRP.search(title) and _REPAIR.search(title):
            basis = "explicit_source_title"
        elif _CFRP.search(text) and _REPAIR.search(text):
            basis = "quoted_study_context"
        else:
            return None
    else:
        return None
    return {"scope": "background_only", "reason": "non_pcb_composite_repair", "basis": basis}


def is_ocr_evidence(item: dict) -> bool:
    return "ocr" in str(item.get("evidence_status") or "").casefold()


def public_source_tier(source: dict) -> str:
    """Use explicit publisher/kind/official-host metadata, never title tokens."""
    try:
        host = (urlsplit(source.get("url") or "").hostname or "").casefold()
    except (ValueError, TypeError):
        host = ""
    if (source.get("kind") == "test_method"
            and str(source.get("publisher") or "").casefold() in {
                "ipc", "ipc / global electronics association", "global electronics association",
            }
            and host in {"www.electronics.org", "electronics.org", "www.ipc.org", "ipc.org"}):
        return "A"
    return "B"
