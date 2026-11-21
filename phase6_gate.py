"""Deterministic local request gate for Phase 6 answers.

The gate runs before any Catalog retrieval and before any managed (cloud)
synthesis. It is pure local string classification: no Catalog access, no file
access, no network access, no model call.

Rules are ordered and the first match wins, so narrower refusals (for example a
destructive request aimed at the Catalog) take precedence over broader ones.
Only four outcomes exist, matching the Phase 6 contract:

``policy_blocked``
    The request asks for secrets, system prompts, raw mail or attachment dumps,
    bulk identity exports, forged citations, or a bypass of the read-only and
    data-egress rules. Nothing is retrieved and nothing can leave the host.
``no_evidence``
    The request is outside the PCB lamination knowledge domain (procurement,
    contact lookup, live plant data, image generation, forecasting, translation,
    generic product advice) or asks for a destructive change this read-only
    service never performs. The local library cannot answer it.
``needs_clarification``
    The request is on-topic but too vague to retrieve: no concrete defect,
    material, stackup or process variable is named.
``in_scope``
    Everything else proceeds to bounded local retrieval.

The gate deliberately does not treat the word "supplier" as out of scope, so a
legitimate question such as "same stackup, new PP supplier, bubbles" still goes
to retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


GATE_VERSION = "pcb-request-gate-v1"

IN_SCOPE = "in_scope"
POLICY_BLOCKED = "policy_blocked"
NO_EVIDENCE = "no_evidence"
NEEDS_CLARIFICATION = "needs_clarification"

STATUSES = (IN_SCOPE, POLICY_BLOCKED, NO_EVIDENCE, NEEDS_CLARIFICATION)


@dataclass(frozen=True)
class Triage:
    """Result of the local request gate."""

    status: str
    reason: str
    matched_rule: str | None = None
    clarifying_questions: tuple[str, ...] = ()

    @property
    def blocked(self):
        return self.status == POLICY_BLOCKED

    def as_dict(self):
        return {
            "gate_version": GATE_VERSION,
            "status": self.status,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
            "retrieval_skipped": self.status != IN_SCOPE,
        }


def _compact(text):
    return re.sub(r"\s+", "", (text or "").casefold())


def _re(pattern):
    return re.compile(pattern, re.IGNORECASE)


# --- concrete engineering anchors -------------------------------------------
# Named failure modes and named process/material/test variables. A question that
# names one of these is specific enough to retrieve. Generic nouns such as
# 材料, 叠构, 曲线, 参数, 缺陷, 外观 and 不良率 are intentionally NOT anchors,
# because "this stackup ok?" style questions must ask for specifics first.
_ANCHOR = _re(
    "白斑|白点|白點|发花|發花|分层|分層|层间分离|爆板|气泡|氣泡|起泡|空洞"
    "|缺胶|缺膠|板翘|板翹|翘曲|翹曲|变形|變形|弯曲|偏移"
    "|delamination|void|blister|whitespot|warpage"
    "|温度|溫度|升温|升溫|降温|保溫|保温|固化|固化度|压力|壓力|真空|排气|排氣"
    "|压机|壓機|热板|熱板|热应力|熱應力|回流焊|熱衝擊|热冲击|可靠性"
    "|切片|微切片|剥离|剝離|剥离强度|热风|试验|試驗|验证|驗證|对照|對照|复现|復現"
    "|离型膜|離型膜|辅材|銅箔|铜箔|铜厚|铜分布|树脂|樹脂|胶量|膠量|流动度|流動度"
    "|吸潮|挥发物|揮發物|板厚|厚度|层数|層數|开窗|開窗|盲槽|台阶|階梯|拼接|搭接"
    "|经纬|經緯|公差|收缩|收縮|应力|應力|均匀性|均勻性|对称性|對稱性|对称|對稱"
    "|pp|prepreg|介电|玻纤|玻璃布|含胶量|残铜|残铜率|蚀刻|棕化|黑化|氧化"
)

# Generic on-topic wording; together with a vague marker it means "ask first".
_DOMAIN_HINT = _re(
    "压合|壓合|层压|層壓|lamination|板|工艺|工藝|制程|材料|叠构|疊構|pp|铜|銅"
    "|树脂|樹脂|缺陷|不良|外观|外觀|参数|參數|曲线|曲線|客户|客戶|品质|品質"
    "|制程|生产|生產|工程师|工程"
)

_STRONG_VAGUE = _re(
    "有问题|有問題|怎么办|怎麼辦|怎么解决|怎麼解決|如何解决|什么原因|什麼原因"
    "|合理吗|合理嗎|严重吗|嚴重嗎|有没有风险|有沒有風險|有风险吗|还能用|還能用"
    "|能不能用|可用吗|可用嗎|需要优化|需要優化|怎么优化|怎麼優化"
    "|判断一下|判斷一下|评估一下|評估一下|看一下|外观不好|外觀不好"
    "|不良率|合格吗|合格嗎|行不行|好不好|够不够|夠不夠|值得|建议吗|建議嗎"
)

_FOLLOW_UP = _re(
    "这个|這個|这批|這批|这种|這種|该|該|我们的|我們的|上述|刚才|剛才|继续|繼續|那"
)

# --- rule table -------------------------------------------------------------
# (rule code, status, reason code, pattern)
_RULES = (
    # 1. Protected system state: never a read-only question.
    (
        "destructive_system_change",
        POLICY_BLOCKED,
        "policy_blocked_destructive_system_change",
        _re(
            "(删除|清空|移除|抹除|擦除|覆盖|重建|drop|truncate|delete).{0,8}"
            "(catalog|知识库|知識庫|索引|数据库|資料庫|会话|會話|记录|記錄|审计|審計|日志|日誌|模型)"
            "|(catalog|知识库|知識庫|索引|数据库|資料庫|会话|會話|审计|審計)"
            ".{0,8}(删除|清空|移除|抹除|擦除|drop|truncate|delete)"
        ),
    ),
    # 2. Destructive mail requests: this service is read-only, so the answer is
    #    "no evidence / not handled here" rather than a policy refusal.
    (
        "destructive_mail_request",
        NO_EVIDENCE,
        "out_of_scope_destructive_mail_request",
        _re(
            "(删除|清空|清理|移除|彻底删除|徹底刪除|批量删除|批量刪除).{0,10}"
            "(邮件|郵件|附件|收件箱|信箱|pst|ost|eml)"
        ),
    ),
    # 3. Bulk raw mail / attachment dumps.
    (
        "bulk_raw_archive_request",
        POLICY_BLOCKED,
        "policy_blocked_bulk_raw_archive",
        _re(
            "(所有|全部|整个|整個|完整|全文|原文|原始|逐封|每封|批量|整批).{0,8}"
            "(邮件|郵件|附件|pst|ost|eml|原始资料|原始資料|原文)"
            "|(邮件|郵件|附件).{0,6}(全文|原文|全部发|全部發|导出|匯出|导出)"
        ),
    ),
    # 4. Secrets and system prompts.
    (
        "secret_request",
        POLICY_BLOCKED,
        "policy_blocked_secret_request",
        _re(
            "(api[_\\-]?key|apikey|密钥|密鑰|秘钥|token|口令|密码|密碼|凭证|憑證|credential)"
            ".{0,10}(值|告诉|告訴|输出|輸出|打印|给我|給我|显示|顯示|列出|是多少|多少|贴|貼)"
            "|(告诉|告訴|输出|輸出|打印|给我|給我|显示|顯示|列出|泄露|洩露)"
            ".{0,10}(api[_\\-]?key|apikey|密钥|密鑰|秘钥|token|密码|密碼|凭证|憑證)"
        ),
    ),
    (
        "system_prompt_request",
        POLICY_BLOCKED,
        "policy_blocked_system_prompt_request",
        _re(
            "系统提示词|系統提示詞|系统提示|系統提示|systemprompt|提示词原文|提示詞原文"
            "|你的指令|你的规则|你的規則|developerprompt|内部指令|內部指令"
        ),
    ),
    # 5. Rule override / injected instruction execution.
    (
        "rule_override",
        POLICY_BLOCKED,
        "policy_blocked_rule_override",
        _re(
            "(忽略|无视|無視|跳过|跳過|绕开|繞開|绕过|繞過|不要管|不用管|不必遵守|不遵守)"
            ".{0,12}(规则|規則|指令|限制|约束|約束|提示词|提示詞|边界|邊界|策略|政策|审核|審核|开关|開關)"
            "|不要管系统规则|不要管系統規則|systemoverride"
        ),
    ),
    (
        "embedded_command_execution",
        POLICY_BLOCKED,
        "policy_blocked_embedded_command_execution",
        _re(
            "(执行|執行|运行|運行|照做|按).{0,12}(证据|證據|文本|邮件|郵件|附件|来源|來源)"
            ".{0,12}(命令|指令|脚本|腳本)"
            "|(证据|證據|邮件|郵件|附件).{0,10}(里的|裡的|中的).{0,6}(命令|指令)"
        ),
    ),
    # 6. Citation forgery and unsupported certainty shortcuts.
    (
        "citation_forgery",
        POLICY_BLOCKED,
        "policy_blocked_citation_forgery",
        _re(
            "(伪造|偽造|造假|捏造|虚构|虛構|编一个|編一個)"
            ".{0,10}(引用|exc|校验|校驗|证据|證據)"
            "|(引用校验|引用校驗|校验显示|校驗顯示|验证通过|驗證通過).{0,10}(伪造|偽造|造假|通过)"
        ),
    ),
    (
        "citation_bypass",
        POLICY_BLOCKED,
        "policy_blocked_citation_bypass",
        _re("(不要|不需要|无需|無需|别|別|不用)引用|(不要|不需要|无需|無需|不用)(来源|來源)"),
    ),
    (
        "fabricated_engineering_data",
        POLICY_BLOCKED,
        "policy_blocked_fabricated_data",
        _re(
            "(编造|編造|虚构|虛構|捏造|编一个|編一個|随便写|隨便寫)"
            ".{0,12}(曲线|曲線|数据|數據|结果|結果|记录|記錄|报告|報告|试验|試驗|参数|參數)"
        ),
    ),
    # 7. Egress bypass.
    (
        "egress_bypass",
        POLICY_BLOCKED,
        "policy_blocked_egress_bypass",
        _re(
            "(绕过|繞過|无视|無視|跳过|跳過|关闭|關閉|解除|取消).{0,8}"
            "(出境|云端|雲端|外网|外網|开关|開關|审核|審核|网关|網關)"
            "|(发到|發到|发送到|發送到|上传到|上傳到)(外网|外網|公网|公網|互联网|互聯網)"
        ),
    ),
    # 8. Bulk identity export.
    (
        "bulk_identity_export",
        POLICY_BLOCKED,
        "policy_blocked_bulk_identity_export",
        _re(
            "(客户|客戶|客户名单|客戶名單|联系人|聯繫人|联系方式|聯繫方式|邮箱|郵箱|电话|電話|手机号|手機號|订单号|訂單號|身份证|身份證)"
            ".{0,12}(名单|名單|所有|全部|清单|清單|表格|整理|汇总|匯總|导出|匯出)"
            "|(名单|名單|所有|全部|清单|清單|整理成|汇总).{0,12}(客户|客戶|联系人|聯繫人|联系方式|聯繫方式|邮箱|郵箱|订单号|訂單號)"
        ),
    ),
    # --- scope rules -------------------------------------------------------
    (
        "destructive_or_write_request",
        NO_EVIDENCE,
        "out_of_scope_write_request",
        _re(
            "(删除|清空|清理|移除|修改|更改|写入|寫入|更新|提交|保存|覆盖|覆蓋|创建|創建|新建)"
            ".{0,8}(记录|記錄|案例|条目录|條目錄|工单|工單|审批|審批|流程)"
        ),
    ),
    (
        "procurement_or_legal",
        NO_EVIDENCE,
        "out_of_scope_procurement_or_legal",
        _re(
            "(写|寫|起草|生成|拟|擬|草拟|草擬|准备|準備)"
            ".{0,6}(合同|協議|协议|标书|標書|报价单|報價單|发票|發票|邮件模板)"
            "|(采购合同|採購合同|合同条款|合同條款)"
        ),
    ),
    (
        "vendor_or_product_recommendation",
        NO_EVIDENCE,
        "out_of_scope_recommendation",
        _re(
            "(推荐|推薦|介绍|介紹|找一家|联系一家|聯繫一家|哪家)"
            ".{0,8}(供应商|供應商|厂家|廠家|厂商|廠商|品牌|设备商|設備商|外协|外協|手机|手機|电脑|電腦|耳机|耳機|相机|相機|车|車)"
            "|(推荐|推薦).{0,6}(一款|几个|幾個|几家|幾家)"
        ),
    ),
    (
        "contact_lookup",
        NO_EVIDENCE,
        "out_of_scope_contact_lookup",
        _re(
            "(查询|查詢|查找|查一下|告诉我|告訴我|提供|给出|給出|找|获取|獲取)"
            ".{0,8}(客户联系人|客戶聯繫人|联系人|聯繫人|联系方式|聯繫方式|邮箱|郵箱|手机号|手機號|电话|電話|身份证|身份證)"
        ),
    ),
    (
        "live_plant_data",
        NO_EVIDENCE,
        "out_of_scope_live_operational_data",
        _re(
            "(今天|今天|现在|現在|当前|當前|实时|實時|本周|本週|本月|昨天|昨日)"
            ".{0,10}(车间|車間|产线|產線|工厂|工廠|现场|現場|产线|库存|庫存|产量|產量|良率|人数|人數)"
            "|(今天|现在|現在|当前|當前|实时|實時).{0,8}(温度|溫度|湿度|濕度|产量|產量)"
            "|(排班|班次|值班表|考勤|交接班|车间温度|車間溫度)"
        ),
    ),
    (
        "image_generation",
        NO_EVIDENCE,
        "out_of_scope_image_generation",
        _re(
            "(生成|画|畫|绘制|繪製|做|出|渲染).{0,16}(图片|圖片|图像|圖像|照片|示意图|示意圖|效果图|效果圖)"
            "|(图片|圖片|图像|圖像)生成"
        ),
    ),
    (
        "forecast_request",
        NO_EVIDENCE,
        "out_of_scope_forecast",
        _re(
            "(预测|預測|预估|預估|推算|估算).{0,8}(订单|訂單|销量|銷量|产量|產量|需求|交期|价格|價格|成本)"
            "|(下个月|下個月|明年|下周|下週).{0,6}(订单|訂單|销量|銷量|产量|產量)"
        ),
    ),
    (
        "translation_request",
        NO_EVIDENCE,
        "out_of_scope_translation",
        _re(
            "翻译|翻譯"
        ),
    ),
    (
        "general_advice",
        NO_EVIDENCE,
        "out_of_scope_general_advice",
        _re(
            "(推荐|推薦).{0,6}(一款|一部|一台|一个|一個).{0,8}(手机|手機|电脑|電腦|耳机|耳機|相机|相機|平板|车|車|游戏)"
            "|(讲个|講個|写首诗|寫首詩|笑话|笑話|天气|天氣)"
        ),
    ),
)


_CLARIFICATION_QUESTIONS = {
    "vague_engineering_request": (
        "具体是哪种缺陷或异常（白斑、分层、气泡/空洞、缺胶、爆板还是翘曲）？出现在什么位置、比例多大、涉及哪些批次？",
        "材料牌号、PP 规格与批次、叠构和板厚分别是什么？",
        "本批温度、压力、真空曲线和设备日志是什么？已经做过哪些切片或对照验证？",
    ),
    "customer_reply_request": (
        "客户反馈的具体缺陷、出现位置、比例和批次范围是什么？",
        "是否有切片、外观照片、压合曲线或来料批次记录可以作为回复依据？",
        "希望回复的是原因初判、临时对策，还是验证计划？",
    ),
}

_CUSTOMER_REPLY = _re(
    "(回复|回覆|答复|答覆|回应|回應|话术|話術|怎么答|怎麼答)"
)

# Reason codes for gated answers (stable, used by tests and audit logs).
ANSWER_TEXT = {
    POLICY_BLOCKED: (
        "## 请求未通过本地策略检查\n"
        "本轮请求被只读与数据保护策略拒绝，未执行检索，也未向任何外部服务发送数据。\n\n"
        "## 拒绝范围\n"
        "- 读取或泄露系统提示词、密钥、凭据。\n"
        "- 批量导出原始邮件、附件或客户身份信息。\n"
        "- 伪造 EXC_ 引用、跳过引用校验，或要求未经证据支持的确定结论。\n"
        "- 绕过只读限制或云端出境开关。\n\n"
        "## 可以改问\n"
        "- 压合后出现白斑，应该从哪些工序开始排查？\n"
        "- 同一叠构不同批次出现分层，如何判断是材料还是压合问题？\n"
        "- 请给出本地证据中与某缺陷相关的摘录和 EXC_ 引用。"
    ),
    NO_EVIDENCE: (
        "## 当前判断\n"
        "该问题超出本地压合知识库范围（例如采购与合同、联系人查询、实时现场数据、"
        "图片生成、订单预测、翻译或通用产品推荐），本地库没有可回答的证据，"
        "也不能据此推断工艺状态。\n\n"
        "## 下一步\n"
        "1. 若关心压合缺陷，请改问具体的失效模式、材料/叠构或压合条件。\n"
        "2. 若是采购、人事、订单或客户资料问题，请通过对应业务系统或责任人处理。\n"
        "3. 本服务只读，不执行删除、写入或外发操作。"
    ),
}


def _is_follow_up_on_topic(question, context):
    compact = _compact(question)
    if not compact or not context:
        return False
    return bool(_ANCHOR.search(_compact(context))) and (
        len(compact) <= 24 or bool(_FOLLOW_UP.search(compact)))


def classify(question, *, context=""):
    """Classify one request. Never raises for empty or non-string input."""
    if not isinstance(question, str):
        return Triage(POLICY_BLOCKED, "policy_blocked_invalid_question")
    compact = _compact(question)
    if not compact:
        return Triage(POLICY_BLOCKED, "policy_blocked_empty_question")
    for rule, status, reason, pattern in _RULES:
        if pattern.search(compact):
            return Triage(status, reason, rule)
    follow_up = _is_follow_up_on_topic(question, context)
    has_anchor = bool(_ANCHOR.search(compact)) or follow_up
    if _CUSTOMER_REPLY.search(compact) and _re("客户|客戶").search(compact):
        return Triage(
            NEEDS_CLARIFICATION,
            "needs_clarification_customer_reply",
            "customer_reply_request",
            _CLARIFICATION_QUESTIONS["customer_reply_request"],
        )
    if has_anchor:
        return Triage(IN_SCOPE, "in_scope_engineering_question")
    if _DOMAIN_HINT.search(compact) and _STRONG_VAGUE.search(compact):
        return Triage(
            NEEDS_CLARIFICATION,
            "needs_clarification_vague_engineering_request",
            "vague_engineering_request",
            _CLARIFICATION_QUESTIONS["vague_engineering_request"],
        )
    return Triage(IN_SCOPE, "in_scope_unclassified_local_retrieval")


def gated_answer(triage):
    """Return the deterministic answer text for a non-retrieved request."""
    if triage.status == NEEDS_CLARIFICATION:
        lines = [
            "## 当前判断",
            "这个问题缺少可检索的必要条件，检索本身无法给出可靠排查方向，"
            "因此本轮没有检索，也没有调用任何外部服务。",
            "",
            "## 需要你确认",
        ]
        lines.extend(
            f"{index}. {item}"
            for index, item in enumerate(triage.clarifying_questions, 1))
        lines.extend([
            "",
            "## 说明",
            "补充上述信息后，可以按缺陷现象、材料/叠构、压合条件三条线重新提问；"
            "本服务只基于本地证据回答，不会推断未经证据支持的根因。",
        ])
        return "\n".join(lines)
    return ANSWER_TEXT.get(triage.status, ANSWER_TEXT[NO_EVIDENCE])


def summary():
    """Return a machine-readable description of the gate for documentation."""
    return {
        "gate_version": GATE_VERSION,
        "statuses": list(STATUSES),
        "rules": [
            {"rule": rule, "status": status, "reason": reason}
            for rule, status, reason, _pattern in _RULES
        ],
        "external_services": False,
        "retrieval": False,
        "model_call": False,
    }
