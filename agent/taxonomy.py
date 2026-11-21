"""Controlled lamination defect and cause taxonomy used by the agent."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Cause:
    cause_id: str
    label: str
    category: str
    keywords: tuple[str, ...]
    required_facts: tuple[str, ...]
    verification_steps: tuple[str, ...]
    order: int
    evidence_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class DefectMode:
    defect_id: str
    label: str
    aliases: tuple[str, ...]
    cause_ids: tuple[str, ...]
    checklist: tuple[str, ...]


CAUSES = {
    "vacuum_pressure_loss": Cause(
        cause_id="vacuum_pressure_loss",
        label="真空或压力不足",
        category="equipment",
        keywords=("真空", "真空度", "压力", "压合压力", "vacuum", "pressure", "排气"),
        required_facts=("equipment", "parameters", "stage"),
        verification_steps=(
            "核对本批真空曲线、抽真空时间和保压曲线",
            "检查压机分区压力、密封圈和管路是否泄漏",
            "确认排气窗口、板边流胶通道和载具是否堵塞",
        ),
        order=10,
    ),
    "prepreg_moisture": Cause(
        cause_id="prepreg_moisture",
        label="PP/板材受潮或挥发物超标",
        category="material",
        keywords=("受潮", "水分", "含水率", "挥发", "干燥", "烘烤", "moisture", "volatile"),
        required_facts=("material", "batch", "storage", "parameters"),
        verification_steps=(
            "核对 PP 牌号、批次、库存条件和开封后放置时间",
            "确认烘烤条件、烘烤后停放时间和回潮风险",
            "对同批材料做含水率或挥发物复测",
        ),
        order=20,
    ),
    "resin_flow_insufficient": Cause(
        cause_id="resin_flow_insufficient",
        label="树脂流动不足",
        category="process",
        keywords=("树脂流动", "流胶", "填胶", "缺胶", "resin flow", "flow"),
        required_facts=("material", "stackup", "parameters"),
        verification_steps=(
            "核对升温速率、保温时间和树脂流动窗口",
            "检查 PP 树脂含量、流动度和叠构设计",
            "用切片确认层间填胶和树脂分布",
        ),
        order=30,
    ),
    "temperature_profile": Cause(
        cause_id="temperature_profile",
        label="温度曲线或固化异常",
        category="process",
        keywords=("温度", "升温", "保温", "固化", "曲线", "temperature", "cure", "profile"),
        required_facts=("equipment", "parameters", "stage"),
        verification_steps=(
            "核对升温速率、保温平台、固化温度和冷却速率",
            "比对压机程序记录与材料推荐曲线",
            "确认热电偶位置和实际板温是否一致",
        ),
        order=40,
    ),
    "material_stackup_mismatch": Cause(
        cause_id="material_stackup_mismatch",
        label="材料体系或叠构不匹配",
        category="design",
        keywords=("材料", "叠构", "板材", "PP", "prepreg", "FR-4", "stackup", "CTE"),
        required_facts=("material", "stackup"),
        verification_steps=(
            "核对板材、PP 牌号、Tg、CTE 和批次是否匹配",
            "检查叠构对称性和铜厚分布",
            "对照材料商推荐压合窗口复核设计",
        ),
        order=50,
    ),
    "copper_surface_contamination": Cause(
        cause_id="copper_surface_contamination",
        label="铜面污染或氧化",
        category="material",
        keywords=("铜面", "氧化", "污染", "残胶", "棕化", "black oxide", "contamination"),
        required_facts=("stage", "material"),
        verification_steps=(
            "检查棕化、酸洗和烘干后的铜面状态",
            "确认板面残胶、氧化点和异物",
            "对异常批做切片和表面分析",
        ),
        order=60,
        evidence_context=("铜面", "銅面", "铜箔", "銅箔", "板面", "界面", "层间", "層間",
                          "树脂", "樹脂", "PP", "prepreg", "棕化", "black oxide", "copper", "laminate"),
    ),
    "volatile_outgassing": Cause(
        cause_id="volatile_outgassing",
        label="挥发物排气不畅",
        category="process",
        keywords=("挥发", "排气", "气泡", "outgassing", "volatile"),
        required_facts=("material", "parameters", "equipment"),
        verification_steps=(
            "确认材料烘烤和排气窗口",
            "核对抽真空时机和保压阶段",
            "检查板边排气通道和载具遮挡",
        ),
        order=70,
    ),
    "resin_budget_shortage": Cause(
        cause_id="resin_budget_shortage",
        label="叠构树脂量不足",
        category="design",
        keywords=("树脂量", "树脂不足", "铜厚", "填胶", "resin content", "resin starvation"),
        required_facts=("stackup", "material"),
        verification_steps=(
            "核算叠构树脂量与铜厚、板厚要求",
            "检查高铜密度区域的填胶余量",
            "用切片确认缺胶位置和树脂分布",
        ),
        order=80,
    ),
    "warpage_cte_mismatch": Cause(
        cause_id="warpage_cte_mismatch",
        label="材料 CTE 或应力不匹配",
        category="design",
        keywords=("CTE", "热膨胀", "应力", "翘曲", "warpage", "mismatch"),
        required_facts=("material", "stackup"),
        verification_steps=(
            "核对材料 CTE、Tg 和固化收缩",
            "检查叠构对称性和铜分布",
            "记录压合后冷却和放置过程的翘曲变化",
        ),
        order=90,
    ),
    "stackup_asymmetry": Cause(
        cause_id="stackup_asymmetry",
        label="叠构不对称",
        category="design",
        keywords=("叠构", "对称", "铜厚", "板厚", "stackup", "symmetry"),
        required_facts=("stackup",),
        verification_steps=(
            "核对层间 PP 规格和铜厚是否对称",
            "检查板厚中心和材料分布",
            "对比同设计历史批次的翘曲数据",
        ),
        order=100,
    ),
    "cooling_rate": Cause(
        cause_id="cooling_rate",
        label="冷却速率或后处理异常",
        category="process",
        keywords=("冷却", "降温", "后处理", "cooling", "annealing"),
        required_facts=("parameters", "equipment"),
        verification_steps=(
            "核对降温速率和出板温度",
            "确认压合后放置方式和堆叠压力",
            "比较不同冷却条件的翘曲结果",
        ),
        order=110,
    ),
    "copper_balance": Cause(
        cause_id="copper_balance",
        label="铜分布不平衡",
        category="design",
        keywords=("铜分布", "铜平衡", "残铜率", "copper balance", "copper density"),
        required_facts=("stackup",),
        verification_steps=(
            "检查各层残铜率和铜分布对称性",
            "确认大面积无铜区和板边铜分布",
            "对比同设计不同拼版的翘曲结果",
        ),
        order=120,
    ),
    "mechanical_stress": Cause(
        cause_id="mechanical_stress",
        label="机械应力或板边损伤",
        category="handling",
        keywords=("板边", "机械", "应力", "搬运", "分板", "stress", "handling"),
        required_facts=("location", "stage"),
        verification_steps=(
            "检查板边、定位孔和分板位置是否有机械损伤",
            "确认搬运、堆叠和周转过程",
            "对异常位置做切片或外观复核",
        ),
        order=130,
    ),
    "contamination": Cause(
        cause_id="contamination",
        label="异物或制程污染",
        category="material",
        keywords=("异物", "污染", "残留", "清洁", "contamination", "residue"),
        required_facts=("stage", "material"),
        verification_steps=(
            "检查压合前清洁、烘烤和周转环境",
            "确认板面异物、残胶和手套污染",
            "对异常点做成分分析",
        ),
        order=140,
        evidence_context=("铜面", "銅面", "板面", "界面", "层间", "層間", "树脂", "樹脂",
                          "压合", "壓合", "PP", "prepreg", "PCB", "copper", "lamination"),
    ),
}


DEFECT_MODES = {
    "white_spot": DefectMode(
        defect_id="white_spot",
        label="白斑/白点",
        aliases=("白斑", "白点", "白點", "白影", "measling", "white spot"),
        cause_ids=(
            "vacuum_pressure_loss", "prepreg_moisture", "resin_flow_insufficient",
            "temperature_profile", "material_stackup_mismatch",
            "copper_surface_contamination",
        ),
        checklist=(
            "确认白斑位置、面积、比例和是否集中在板边或孔区",
            "确认 PP 牌号、批次、库存和烘烤记录",
            "核对真空曲线、压力曲线和温度曲线",
            "用切片确认层间树脂和界面状态",
        ),
    ),
    "delamination": DefectMode(
        defect_id="delamination",
        label="分层",
        aliases=("分层", "分層", "层间分离", "delamination"),
        cause_ids=(
            "vacuum_pressure_loss", "prepreg_moisture", "resin_flow_insufficient",
            "temperature_profile", "material_stackup_mismatch",
            "copper_surface_contamination", "mechanical_stress",
        ),
        checklist=(
            "确认分层位置、层别、面积和是否从板边起始",
            "确认材料批次、烘烤和库存条件",
            "核对真空、压力和温度曲线",
            "用切片确认分层界面和树脂状态",
        ),
    ),
    "blister": DefectMode(
        defect_id="blister",
        label="起泡/爆板",
        aliases=("起泡", "爆板", "起泡分层", "blister", "blistering"),
        cause_ids=(
            "prepreg_moisture", "volatile_outgassing", "temperature_profile",
            "vacuum_pressure_loss", "resin_flow_insufficient",
        ),
        checklist=(
            "确认起泡位置、尺寸、层别和是否在回流后出现",
            "确认材料烘烤、库存和挥发物控制",
            "核对升温、保温和抽真空曲线",
            "用切片确认泡内气体和界面状态",
        ),
    ),
    "void": DefectMode(
        defect_id="void",
        label="空洞/气泡",
        aliases=("空洞", "气泡", "氣泡", "气孔", "氣孔", "void", "bubble"),
        cause_ids=(
            "vacuum_pressure_loss", "resin_flow_insufficient", "prepreg_moisture",
            "resin_budget_shortage", "contamination",
        ),
        checklist=(
            "确认空洞位置、尺寸、层别和分布规律",
            "核对真空、压力和树脂流动窗口",
            "确认材料受潮、挥发物和异物风险",
            "用切片确认空洞边界和填胶状态",
        ),
    ),
    "resin_shortage": DefectMode(
        defect_id="resin_shortage",
        label="缺胶/树脂不足",
        aliases=(
            "缺胶", "缺膠", "缺树脂", "缺樹脂", "树脂不足", "樹脂不足",
            "resin shortage", "resin starvation",
        ),
        cause_ids=(
            "resin_flow_insufficient", "resin_budget_shortage",
            "vacuum_pressure_loss", "temperature_profile", "prepreg_moisture",
        ),
        checklist=(
            "确认缺胶位置、层别和是否集中在高铜密度区",
            "核算叠构树脂量、PP 规格和铜厚",
            "核对压力、温度和树脂流动窗口",
            "用切片确认填胶和层间空隙",
        ),
    ),
    "warpage": DefectMode(
        defect_id="warpage",
        label="翘曲/弯曲",
        aliases=("翘曲", "翹曲", "弯曲", "彎曲", "板翘", "板翹", "warpage", "bow", "twist"),
        cause_ids=(
            "warpage_cte_mismatch", "stackup_asymmetry", "cooling_rate",
            "copper_balance", "temperature_profile",
        ),
        checklist=(
            "确认翘曲方向、测量方法和环境条件",
            "核对叠构对称性、铜分布和板厚",
            "核对压合温度、冷却速率和后处理",
            "比较同设计历史批次的翘曲数据",
        ),
    ),
}


# Expert priors are a ranking prior, not evidence. They keep a common cause
# such as moisture from outranking the most specific cause for every defect.
DEFECT_CAUSE_PRIORS = {
    "white_spot": {
        "vacuum_pressure_loss": 0.60, "prepreg_moisture": 0.60,
        "resin_flow_insufficient": 0.45, "temperature_profile": 0.30,
        "material_stackup_mismatch": 0.20, "copper_surface_contamination": 0.20,
    },
    "delamination": {
        "vacuum_pressure_loss": 0.60, "prepreg_moisture": 0.60,
        "resin_flow_insufficient": 0.45, "temperature_profile": 0.35,
        "material_stackup_mismatch": 0.30, "copper_surface_contamination": 0.25,
        "mechanical_stress": 0.20,
    },
    "blister": {
        "prepreg_moisture": 0.70, "volatile_outgassing": 0.55,
        "temperature_profile": 0.45, "vacuum_pressure_loss": 0.40,
        "resin_flow_insufficient": 0.30,
    },
    "void": {
        "vacuum_pressure_loss": 0.70, "resin_flow_insufficient": 0.50,
        "resin_budget_shortage": 0.45, "prepreg_moisture": 0.25,
        "contamination": 0.20,
    },
    "resin_shortage": {
        "resin_flow_insufficient": 0.70, "resin_budget_shortage": 0.65,
        "vacuum_pressure_loss": 0.30, "temperature_profile": 0.25,
        "prepreg_moisture": 0.20,
    },
    "warpage": {
        "warpage_cte_mismatch": 0.60, "stackup_asymmetry": 0.60,
        "cooling_rate": 0.45, "copper_balance": 0.40,
        "temperature_profile": 0.20,
    },
}


def cause_prior(defect_id: str, cause_id: str) -> float:
    return float(DEFECT_CAUSE_PRIORS.get(defect_id, {}).get(cause_id, 0.0))


_ALIAS_INDEX = tuple(sorted(
    ((alias.casefold(), mode.defect_id) for mode in DEFECT_MODES.values()
     for alias in (mode.defect_id, mode.label, *mode.aliases)),
    key=lambda item: (-len(item[0]), item[1]),
))

# Case notes often use "爆板" as a broad symptom and then state the
# confirmed interface failure as "分层". Resolve that ambiguity before the
# generic alias order is used.
_DEFECT_CONTEXT_PRIORS = (
    ("已爆板分层", "blister"),
    ("已爆板分層", "blister"),
    ("爆板及板边分层", "delamination"),
    ("爆板及板邊分層", "delamination"),
    ("爆板分层", "delamination"),
    ("爆板分層", "delamination"),
    ("板边分层", "delamination"),
    ("板邊分層", "delamination"),
    ("铜皮分离", "delamination"),
    ("層間分離", "delamination"),
    ("分層異常", "delamination"),
    ("爆板异常", "blister"),
    ("爆板異常", "blister"),
    ("大孔爆板", "blister"),
)


def resolve_defect(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().casefold()
    for alias, defect_id in _ALIAS_INDEX:
        if key == alias:
            return defect_id
    return None


def term_present(text: str, term: str) -> bool:
    """Match a term without matching a short ASCII token inside another word."""
    term = term.casefold()
    if term.isascii() and term.isalnum() and len(term) <= 8:
        return bool(re.search(
            r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text
        ))
    return term in text


def _contains_alias(text: str, alias: str) -> bool:
    return term_present(text, alias)


def detect_defects(text: str) -> list[str]:
    lowered = (text or "").casefold()
    found = []
    for alias, defect_id in _ALIAS_INDEX:
        if alias and _contains_alias(lowered, alias) and defect_id not in found:
            found.append(defect_id)
    for phrase, defect_id in _DEFECT_CONTEXT_PRIORS:
        if phrase.casefold() in lowered and defect_id in found:
            found.remove(defect_id)
            found.insert(0, defect_id)
            break
    return found


def causes_for(defect_id: str) -> tuple[Cause, ...]:
    mode = DEFECT_MODES[defect_id]
    return tuple(CAUSES[cause_id] for cause_id in mode.cause_ids)
