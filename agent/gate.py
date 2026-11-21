"""Domain gate, fact extraction and missing-information checks."""
from __future__ import annotations

import re
from typing import Any

from .models import CaseInput
from .taxonomy import DEFECT_MODES, detect_defects, resolve_defect, term_present
from pcb_assistant.knowledge import query_context_groups


DOMAIN_TERMS = (
    "pcb", "印制电路板", "线路板", "电路板", "压合", "层压", "压板", "lamination",
    "分层", "分層", "白斑", "白点", "白影", "起泡", "爆板", "空洞", "气泡",
    "缺胶", "缺树脂", "树脂", "prepreg", "半固化片", "pp", "叠构", "板材",
    "fr4", "fr-4", "铜箔", "铜面", "真空", "压力", "温度", "固化", "压机",
    "层压机", "板边", "板厚", "铜厚", "翘曲", "弯曲", "切片", "剥离",
    "热冲击", "回流焊", "smt", "bga", "hdi", "棕化", "填胶", "流胶",
)

OUT_OF_DOMAIN_TERMS = (
    "天气", "气温", "股票", "基金", "汇率", "彩票", "旅游", "食谱",
    "写诗", "写作文", "翻译", "编程", "python", "java", "javascript",
    "代码", "电影", "音乐", "游戏", "新闻", "星座", "医学", "法律",
)

LOCATION_TERMS = {
    "board_edge": ("板边", "边缘", "板缘", "板邊", "板緣"),
    "board_center": ("板中", "中央", "板中心"),
    "hole_area": ("孔区", "孔边", "定位孔", "过孔", "pth"),
    "large_area": ("大面积", "整板", "整面"),
    "local": ("局部", "点状", "小面积"),
    "bga_area": ("bga", "bga区"),
    "panel_edge": ("拼板边", "panel边"),
}

STAGE_TERMS = {
    "lamination": ("压合", "层压", "压板", "lamination", "pressing"),
    "drilling": ("钻孔", "drill"),
    "plating": ("沉铜", "电镀", "plating"),
    "etching": ("蚀刻", "etching"),
    "soldermask": ("阻焊", "soldermask"),
    "reflow": ("回流焊", "回流", "reflow"),
    "incoming": ("来料", "入库", "incoming"),
    "storage": ("库存", "存放", "storage"),
    "microsection": ("切片", "microsection"),
}

MATERIAL_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:fr-?4|it-?180|it-?150|s1000-?2|rogers|isola|panasonic|taiyo|pp)(?![a-z0-9])"
    r"|生益|松下|罗杰斯|半固化片|prepreg|板材"
)
STACKUP_PATTERN = re.compile(
    r"\d+\s*层|L\d+\s*[-~至到]\s*L\d+|叠构|疊構|stack-?up", re.IGNORECASE
)
EQUIPMENT_PATTERN = re.compile(
    r"真空压机|真空壓機|热压机|熱壓機|层压机|層壓機|压机|壓機|真空系统|载具|載具"
)
BATCH_PATTERN = re.compile(r"批次|批号|批號|lot|LOT|生产日期|生產日期")
TIMING_PATTERN = re.compile(r"压合后|壓合後|压合前|壓合前|回流后|回流後|放置|停放|烘烤后|烘烤後")

PARAMETER_PATTERNS = {
    "temperature": re.compile(r"\d{2,3}(?:\.\d+)?\s*(?:°C|℃|度C|度|C)\b", re.IGNORECASE),
    "pressure": re.compile(r"\d+(?:\.\d+)?\s*(?:MPa|mPa|Bar|bar|kg/cm2|kg/cm²|psi)\b", re.IGNORECASE),
    "vacuum": re.compile(r"\d+(?:\.\d+)?\s*(?:Pa|kPa|mbar|Torr|mmHg)\b", re.IGNORECASE),
    "time": re.compile(r"\d+(?:\.\d+)?\s*(?:min|分钟|分鐘|小时|小時|h|秒|s)\b", re.IGNORECASE),
}

MISSING_FACT_PROMPTS = {
    "material": "板材和 PP 的牌号、批次是什么？",
    "stackup": "叠构、层数、铜厚和板厚是什么？",
    "stage": "缺陷是在压合后、钻孔后还是回流后发现的？",
    "location": "缺陷主要在板边、板中、孔区还是大面积？",
    "parameters": "本批的温度、压力、真空和时间曲线是否异常？",
    "equipment": "使用的是哪台压机，真空和压力分区是否正常？",
    "batch": "生产批次和材料批次是什么？",
    "storage": "PP 的库存条件、开封后放置时间和烘烤记录是什么？",
    "timing": "压合后多久出现，是否经过回流或放置后加重？",
}


def _first_match(text: str, mapping: dict[str, tuple[str, ...]]) -> str | None:
    lowered = text.casefold()
    for key, aliases in mapping.items():
        if any(term_present(lowered, alias) for alias in aliases):
            return key
    return None


def _pattern_hits(text: str, pattern: re.Pattern) -> list[str]:
    return [match.group(0).strip() for match in pattern.finditer(text)]


def extract_facts(case: CaseInput) -> dict[str, Any]:
    text = " ".join(
        part for part in (
            case.question, case.notes, case.defect, case.stage, case.location,
            case.material, case.stackup, case.equipment, case.batch, case.timing,
        ) if part
    )
    defects = detect_defects(text)
    explicit_defect = resolve_defect(case.defect)
    primary = explicit_defect or (defects[0] if defects else None)
    parameters = dict(case.parameters)
    for name, pattern in PARAMETER_PATTERNS.items():
        hits = _pattern_hits(text, pattern)
        if hits and name not in parameters:
            parameters[name] = ", ".join(dict.fromkeys(hits))
    material_hits = _pattern_hits(text, MATERIAL_PATTERN)
    equipment_hits = _pattern_hits(text, EQUIPMENT_PATTERN)
    stackup_hits = _pattern_hits(text, STACKUP_PATTERN)
    batch_hits = _pattern_hits(text, BATCH_PATTERN)
    timing_hits = _pattern_hits(text, TIMING_PATTERN)
    return {
        "question": case.question,
        "product_context": [alias for group in query_context_groups(text)
                            for alias in group if alias in text.casefold()],
        "defect": primary,
        "secondary_defects": [item for item in defects if item != primary],
        "stage": case.stage or _first_match(text, STAGE_TERMS),
        "location": case.location or _first_match(text, LOCATION_TERMS),
        "material": case.material or (material_hits[0] if material_hits else None),
        "stackup": case.stackup or (stackup_hits[0] if stackup_hits else None),
        "equipment": case.equipment or (equipment_hits[0] if equipment_hits else None),
        "parameters": parameters,
        "batch": case.batch or (batch_hits[0] if batch_hits else None),
        "timing": case.timing or (timing_hits[0] if timing_hits else None),
        "domain_hits": [term for term in DOMAIN_TERMS if term_present(text, term)],
        "out_of_domain_hits": [term for term in OUT_OF_DOMAIN_TERMS if term_present(text, term)],
    }


def missing_fact_codes(facts: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    missing = []
    for code in required:
        if code == "parameters":
            present = bool(facts.get("parameters"))
        else:
            present = bool(facts.get(code))
        if not present and code not in missing:
            missing.append(code)
    return missing


def gate_case(case: CaseInput, facts: dict[str, Any]) -> dict[str, Any]:
    defect = facts.get("defect")
    domain_hits = facts.get("domain_hits") or []
    out_hits = facts.get("out_of_domain_hits") or []
    explicit_domain = (case.domain or "").strip().casefold()

    if explicit_domain in {"pcb_lamination", "pcb", "lamination"}:
        status = "accepted" if defect else "needs_clarification"
        reason = "explicit_pcb_lamination_domain"
    elif not domain_hits and out_hits:
        status = "refused"
        reason = "out_of_domain"
    elif not domain_hits and not defect:
        status = "refused"
        reason = "no_pcb_lamination_signal"
    elif defect:
        status = "accepted"
        reason = "defect_mode_detected"
    else:
        status = "needs_clarification"
        reason = "defect_mode_required"

    missing = missing_fact_codes(
        facts, ("material", "stackup", "stage", "location", "parameters")
    ) if status == "accepted" else []
    return {
        "status": status,
        "reason": reason,
        "is_pcb_lamination": status != "refused",
        "confidence": 0.95 if status == "accepted" else (0.60 if status == "needs_clarification" else 0.0),
        "domain_hits": domain_hits,
        "out_of_domain_hits": out_hits,
        "detected_defects": [defect] if defect else [],
        "missing_facts": missing,
        "missing_fact_questions": [MISSING_FACT_PROMPTS[code] for code in missing],
        "clarifying_question": (
            "请先确认缺陷类型：白斑、分层、起泡、空洞、缺胶还是翘曲？"
            if status == "needs_clarification" else None
        ),
        "canonical_defect_label": DEFECT_MODES[defect].label if defect else None,
    }
