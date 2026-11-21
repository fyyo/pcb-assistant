"""Local-only mail anonymization. Never log source text or exception messages."""
from __future__ import annotations

import collections
import hashlib
import hmac
import html
import re
import unicodedata
from email.header import decode_header
from email.utils import getaddresses

PLACEHOLDER = re.compile(r"\[(?:身份|实体|邮箱|网址|电话|编号|日期|地址|业务信息|附件|凭据|邮件头|签名|金额|内部路径|内容标识)\]")
EMAIL = re.compile(r"(?i)[\w.!#$%&'*+/=?^`{|}~-]+\s*@\s*(?:[\w-]+\s*\.\s*)+[\w-]{2,}")
URL = re.compile(r"(?i)(?:https?://|ftp://|www\.)[^\s<>\[\]，。；）]+|\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|tw|hk|io|biz|tech|co)(?:/[^\s<>\[\]，。；）]*)?\b")
PHONE = re.compile(r"(?<![\w.])(?:\+?86[-\s]?)?1[3-9](?:[- ]?\d){9}(?!\d)|(?<!\d)0\d{2,3}[-－ ]\d{7,8}(?:[-转 ]\d{1,6})?(?!\d)|(?<!\w)\+\d[\d ()-]{7,}\d(?!\d)")
LONG_ID = re.compile(r"(?<![\w.])\d{7,}(?:[xX])?(?![\w.])")
DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)|(?<!\d)\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2}(?!\d)|(?<!\d)\d{1,2}月\d{1,2}日")
EN_DATE = re.compile(r"(?i)\b(?:(?:\d{1,2}[- /])?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[- /]+\d{1,2}(?:,?[- /]+(?:19|20)\d{2})?|\d{1,2}[- /](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- /](?:19|20)\d{2})\b")
OBFUSCATED_EMAIL = re.compile(r"(?i)[\w.+-]+\s*(?:\[at\]|\(at\)|＠)\s*[\w.-]+(?:\s*(?:\[dot\]|\(dot\)|\.)\s*[\w-]+)+")
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
FILE = re.compile(r"(?i)[^\s<>\[\]，。；：:]+\.(?:xlsx?|docx?|pptx?|pdf|zip|rar|7z|jpg|jpeg|png|gif|bmp|dwg|dxf|gerber|gbr|pst|msg|eml|csv|txt)\b")
PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\n<>\[\]]+")
MIXED_ID = re.compile(r"(?i)(?<![\w])(?=[a-z0-9_.-]{5,}(?![\w]))(?=[a-z0-9_.-]*[a-z])(?=[a-z0-9_.-]*\d)[a-z][a-z0-9_.-]*(?![\w])")
SAFE_CODE = re.compile(r"(?i)^(?:fr-?4|fr-?406|ipc-?\d{3,5}(?:[a-z-]\d*)?|iso-?\d{4,5}|iatf-?\d{4,5}|hdi|pcb|tg\d+|cti\d+)$")
BUSINESS = re.compile(r"(?im)(客户(?:名称|代码|编号|料号)?|客戶(?:名稱|代碼|編號|料號)?|供应商(?:名称)?|供應商(?:名稱)?|公司名称|公司名稱|项目(?:名称|编号|代码)?|專案(?:名稱|編號)?|订单(?:号|编号)?|訂單(?:號|編號)?|合同号|合同號|工单(?:号)?|工單(?:號)?|料号|料號|产品编号|產品編號|批号|批號|批次号|批次號|图号|圖號|customer|supplier|project|order\s*(?:no\.?|number|#)|lot\s*(?:no\.?|number|#)|p/?n|part\s*(?:no\.?|number))\s*[:：=]\s*[^\n，,;；]+")
CONTACT_LINE = re.compile(r"(?i)^\s*[> ]*(?:tel(?:ephone)?|fax|mobile|phone|e-?mail|website|address|we\s*chat|qq|手机|手機|电话|電話|传真|傳真|地址|住址|聯絡|联系地址|微信|联系人|聯絡人|收件地址|联络人|公司地址)\s*[:：.＋+\d].*$")
HEADER_LINE = re.compile(r"(?i)^\s*[> ]*(?:from|to|cc|bcc|sent|date|subject|发件人|寄件者|寄件人|收件人|收件者|抄送|副本|密送|发送时间|寄件日期|发送日期|主旨|主题)\s*[:：].*$")
QUOTE_BOUNDARY = re.compile(r"(?i)^\s*(?:[-_]{2,}.*(?:original|forwarded|原始|转发|轉寄|邮件|郵件)|on .{1,200}wrote:|在.{1,120}(?:写道|寫道)[：:])")
SIGNOFF = re.compile(
    r"(?i)^\s*(?:(?:best|kind|warm)\s+regards|regards|sincerely|此致|顺祝商祺|順祝商祺|--)[,，!！。\s]*$|"
    r"^\s*[_\-\s]{6,}$")
DISCLAIMER = re.compile(r"(?i)^\s*(?:免责声明|免責聲明|保密声明|保密聲明|confidentiality\s*(?:notice|statement)|this (?:e-?mail|message).*(?:confidential|privileged))")
SALUTATION = re.compile(
    r"(?i)^\s*(?:dear|hi|hello)\b[^\n]{0,160}[,，:：!！]\s*$|"
    r"^\s*[^\n]{1,80}(?:先生|女士|经理|經理|副理|主任|课长|課長|厂长|廠長|"
    r"组长|組長|处长|處長|部长|部長|主管|总|總|工)[：:,，!！\s]*$")
ORG = re.compile(r"[\u3400-\u9fffA-Za-z0-9（）()·&]{2,50}(?:股份有限公司|有限责任公司|有限責任公司|有限公司|集团|集團|研究院|研究所|大学|大學)")
ADDRESS = re.compile(r"[\u3400-\u9fff]{2,12}(?:省|市|縣|县|区|區|镇|鎮)[\u3400-\u9fff\d\-]{0,35}(?:路|街|大道)[\u3400-\u9fff\d\-号號栋棟楼樓室]{0,20}")
EN_ADDRESS = re.compile(r"(?i)\b\d{1,6}\s+(?:[a-z]+\s+){1,6}(?:road|street|avenue|lane|boulevard|drive|rd\.?|st\.?|ave\.?)\b[^\n,;]{0,35}")
CREDENTIAL = re.compile(r"(?im)(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|secret|密码|密碼|口令)\s*[:：=]\s*[^\n]+")
MONEY = re.compile(r"(?i)(?:[¥￥$€]|rmb\s*|usd\s*)\s*[\d,.]+|[\d,.]+\s*(?:万元|萬圓|元/片|元/平米|美元)")
CJK = re.compile(r"[\u3400-\u9fff]")

# Common simplified and traditional surnames. This is only a hint for
# residual-name cleanup; it is deliberately not used to rewrite technical text
# unless the name is anchored by a role, greeting or signature context.
CJK_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
    "趙錢孫李周吳鄭王馮陳褚衛蔣沈韓楊朱秦尤許何呂施張孔曹嚴華金魏陶姜戚謝鄒喻柏水竇章雲蘇潘葛奚範彭郎魯韋昌馬苗鳳花方俞任袁柳酆鮑史唐費廉岑薛雷賀倪湯滕殷羅畢郝鄔安常樂於時傅皮卞齊康伍餘元卜顧孟平黃和穆蕭尹姚邵湛汪祁毛禹狄米貝明臧計伏成戴談宋茅龐熊紀舒屈項祝董梁杜阮藍閔席季麻強賈路婁危江童顏郭梅盛林刁鐘徐邱駱高夏蔡田樊胡凌霍虞萬支柯昝管盧莫經房裘繆乾解應宗丁宣賁鄧鬱單杭洪包諸左石崔吉鈕龔程嵇邢滑裴陸榮翁荀羊於惠甄曲家封芮羿儲靳汲邴糜松井段富巫烏焦巴弓牧隗山谷車侯宓蓬全郗班仰秋仲伊宮寧仇欒暴甘鈄厲戎祖武符劉景詹束龍葉幸司韶郜黎薊薄印宿白懷蒲邰從鄂索咸籍賴卓藺屠蒙池喬陰胥能蒼雙聞莘黨翟譚貢勞逄姬申扶堵冉宰酈雍卻璩桑桂濮牛壽通邊扈燕冀郟浦尚農溫別莊晏柴瞿閻充慕連茹習宦艾魚容向古易慎戈廖庾終暨居衡步都耿滿弘匡國文寇廣祿闕東歐殳沃利蔚越夔隆師鞏厙聶晁勾敖融冷訾辛闞那簡饒空曾毋沙乜養鞠須豐巢關蒯相查後荊紅游竺權逯蓋益桓公閆闫"
)

ROLE_TITLES = (
    "总经理", "總經理", "副总经理", "副總經理", "董事长", "董事長", "总监", "總監",
    "经理", "經理", "副理", "主任", "课长", "課長", "组长", "組長", "厂长", "廠長",
    "处长", "處長", "部长", "部長", "主管", "协理", "協理", "先生", "女士",
)
_ROLE_PATTERN = "|".join(sorted(ROLE_TITLES, key=len, reverse=True))
_SURNAME_PATTERN = "".join(sorted(CJK_SURNAMES))
_GENERIC_NAME_WORDS = frozenset({
    "维护", "維護", "不含", "专案", "專案", "高级", "高級", "资深", "資深",
    "各位", "大家", "我们", "我們", "相关", "相關", "上述", "以下", "所有",
    "全部", "每个", "每個", "一名", "两名", "兩名", "多名", "同仁", "长官",
    "長官", "主管", "经理", "經理", "副理", "主任", "课长", "課長", "厂长",
    "廠長", "处长", "處長", "部长", "部長", "总监", "總監", "副总", "副總",
    "协理", "協理", "董事长", "董事長", "总经理", "總經理", "先生", "女士",
    "人员", "人員", "窗口", "负责人", "負責人", "承办", "承辦", "主办", "主辦",
    "协办", "協辦", "产品", "產品", "品质", "品質", "工程", "制造", "製造",
    "生产", "生產", "设备", "設備", "制程", "製程", "品保", "业务", "業務",
    "客服", "采购", "採購", "财务", "財務", "人事", "资讯", "資訊", "高阶",
    "高階", "中阶", "中階", "基层", "基層", "现场", "現場", "线上", "線上",
    "部门", "部門", "单位", "單位",
})
_NAME_ROLE_SURNAME = re.compile(
    r"(?P<name>[" + _SURNAME_PATTERN + r"](?:[ \t]*[\u3400-\u9fff]){0,2})"
    r"[ \t]*(?P<role>" + _ROLE_PATTERN + r")")
_NAME_ROLE_GENERIC = re.compile(
    r"(?m)(?P<prefix>^|[ \t，,。；;：:、])(?P<name>[\u3400-\u9fff]{2,3})"
    r"[ \t]*(?P<role>" + _ROLE_PATTERN + r")")
_ROLE_NAME = re.compile(
    r"(?P<role>" + _ROLE_PATTERN + r")[ \t]*(?P<sep>[:：]?[ \t]*)"
    r"(?P<name>[" + _SURNAME_PATTERN + r"](?:[ \t]*[\u3400-\u9fff]){1,2})")
_LABELED_NAME = re.compile(
    r"(?i)(?P<label>姓名|名字|联系人|聯絡人|负责人|負責人|承办人|承辦人)[ \t]*[:：][ \t]*"
    r"(?P<name>[^\s，,。；;]{1,20})")
_ROMAN_CJK_NAME_LINE = re.compile(
    r"(?m)^[ \t]*(?:[A-Z][ \t]*){1,5}[A-Z]{1,4}[ \t]+[\u3400-\u9fff]{2,4}[ \t]*$")
_PLACEHOLDER_ARTIFACT = re.compile(
    r"(\[(?:身份|实体|邮箱|网址|电话|编号|日期|地址|业务信息|附件|凭据|邮件头|签名|金额|内部路径|内容标识)\])[ \t]*\]")
_LEADING_PLACEHOLDER_ARTIFACT = re.compile(
    r"\[\[(身份|实体|邮箱|网址|电话|编号|日期|地址|业务信息|附件|凭据|邮件头|签名|金额|内部路径|内容标识)\]")
_GREETING_NAME_LINE = re.compile(r"(?m)^[ \t]*(?P<names>[^\n:：]{1,60})[:：][ \t]*$")
_TECHNICAL_LABELS = frozenset({
    "孔破", "板彎", "板翹", "披鋒", "缺膠", "分層", "爆板", "介厚", "銅箔", "壓合",
    "棕化", "黑化", "除膠", "雷射", "鑽孔", "電鍍", "蝕刻", "防焊", "成型", "測試",
    "包裝", "出貨", "白斑", "白點", "氣泡", "刮傷", "殘銅", "短路", "開路", "偏移",
    "層偏", "異物", "汙染", "污染", "氧化", "變色", "翹曲", "壓傷", "壓痕", "缺角",
    "破損", "裂紋", "龜裂", "樹脂", "玻纖", "銅厚", "板厚", "阻抗", "疊構", "程式",
    "參數", "條件", "溫度", "壓力", "真空", "保溫", "升溫", "降溫", "時間", "速度",
    "流量", "濃度", "黏度", "粘度", "膠量", "填料", "固化", "流動", "原因", "對策",
    "備註", "說明", "現象", "分析", "驗證", "材料", "設備", "品質", "異常", "改善",
    "責任人", "序號", "項目", "現狀", "不良率", "料號", "客戶", "供應商", "訂單",
    "批號", "日期", "數量", "單位", "規格", "厚度", "結果", "結論", "建議", "風險",
    "負責人", "窗口", "部門", "廠區", "產線", "機台", "版本", "檔案", "附件", "郵件",
    "主旨", "寄件者", "收件者", "副本", "主题", "备注", "说明", "现象", "分析", "验证",
    "材料", "设备", "品质", "异常", "改善", "责任人", "序号", "项目", "现状", "不良率",
    "料号", "客户", "供应商", "订单", "批号", "日期", "数量", "单位", "规格", "厚度",
    "结果", "结论", "建议", "风险", "负责人", "窗口", "部门", "厂区", "产线", "机台",
    "版本", "档案", "附件", "邮件", "主旨", "寄件者", "收件者", "副本",
    "圖一", "圖二", "圖三", "圖四", "圖五", "圖六", "圖七", "圖八", "圖九",
    "图一", "图二", "图三", "图四", "图五", "图六", "图七", "图八", "图九",
    "改前", "改後", "改后", "廠別", "厂别", "調整前", "调整前", "調整後", "调整后",
    "疊構一", "疊構二", "疊構三", "叠构一", "叠构二", "叠构三",
    "疊遘一", "疊遘二", "设计", "设计", "製造", "制造", "流程", "標準", "标准",
    "規範", "规范", "表單", "表单", "欄位", "栏位", "內容", "内容", "說明", "说明",
    "備註", "备注",
    "備注", "調整后", "實測", "实测", "內層", "内层", "外層", "外层", "分机号",
    "舉例", "举例", "如下", "主席", "文件", "進行中", "进行中", "如所述", "長期",
    "长期", "圖十", "圖十一", "圖十二", "圖十三", "图十", "图十一", "图十二",
    "图十三",
    "修改前", "修改后", "日系", "另外", "明細", "明细", "实际", "實際", "要求",
    "請問", "请问", "方式一", "方式二", "方式三",
    "原備料", "原备料", "甘特圖", "甘特图", "切片圖", "切片图", "外觀圖", "外观图",
    "疊遘三", "叠遘三", "改善批", "治具", "儀校", "仪校", "體系", "体系", "設計",
    "路徑", "路径", "規格為", "规格为", "新吉欣",
})
_TECHNICAL_TOKEN = re.compile(r"^(?:[圖图]|方式)[一二三四五六七八九十百]+$")
_GENERIC_SALUTATIONS = frozenset({
    "all", "team", "sir", "madam", "everyone", "colleagues", "guys",
    "各位", "大家", "同仁", "团队", "團隊", "长官", "長官", "all.",
})


def _looks_like_name_token(value, allow_long=False):
    token = (value or "").strip()
    pattern = r"[\u3400-\u9fff]{2,4}" if allow_long else r"[\u3400-\u9fff]{2,3}"
    if not re.fullmatch(pattern, token):
        return False
    if (_TECHNICAL_TOKEN.fullmatch(token) or token in _TECHNICAL_LABELS
            or _is_generic_name(token)):
        return False
    return True


def _redact_greeting_lines(text, counts=None):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = re.match(r"(?i)^(dear|hi|hello)[ \t]+(.+?)([，,：:!！]?[ \t]*)$",
                         stripped)
        if match:
            name = match.group(2).strip(" ,，:：!！")
            if name and name.casefold() not in _GENERIC_SALUTATIONS:
                lines[index] = "{} [身份]{}".format(match.group(1), match.group(3))
                if counts is not None:
                    counts["residual_person_names"] = counts.get("residual_person_names", 0) + 1
            continue
        match = _GREETING_NAME_LINE.match(line)
        if not match:
            continue
        names = [item.strip() for item in re.split(r"[，,、;；]", match.group("names"))
                 if item.strip()]
        if not 1 <= len(names) <= 4:
            continue
        allow_long = len(names) > 1
        if all(_looks_like_name_token(item, allow_long=allow_long) for item in names):
            lines[index] = "[身份]:"
            if counts is not None:
                counts["residual_person_names"] = counts.get("residual_person_names", 0) + 1
    return "\n".join(lines)


def _is_generic_name(value):
    compact = re.sub(r"[\s]+", "", value or "")
    return (compact in _GENERIC_NAME_WORDS
            or compact.endswith(("副", "正", "各", "级", "級")))


def _replace_name_role(match, counts=None):
    if _is_generic_name(match.group("name")):
        return match.group(0)
    if counts is not None:
        counts["residual_person_names"] = counts.get("residual_person_names", 0) + 1
    return "[身份]" + match.group("role")


def _replace_generic_name_role(match, counts=None):
    if _is_generic_name(match.group("name")):
        return match.group(0)
    if counts is not None:
        counts["residual_person_names"] = counts.get("residual_person_names", 0) + 1
    return match.group("prefix") + "[身份]" + match.group("role")


def _redact_signature_lines(text, counts=None):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _ROMAN_CJK_NAME_LINE.match(line):
            lines[index] = "[身份]"
            if counts is not None:
                counts["residual_person_names"] = counts.get("residual_person_names", 0) + 1
    return "\n".join(lines)


def redact_residual_person_names(text, counts=None):
    """Remove person names that survived header/signature/NER cleanup.

    This is intentionally conservative: generic role phrases such as
    "高级经理" or "维护课长" are kept, while names anchored to a role,
    greeting, contact block or romanised signature are replaced.
    """
    if not isinstance(text, str) or not text:
        return text
    text = _PLACEHOLDER_ARTIFACT.sub(r"\1", text)
    text = _LEADING_PLACEHOLDER_ARTIFACT.sub(r"[\1]", text)
    text = _NAME_ROLE_SURNAME.sub(lambda match: _replace_name_role(match, counts), text)
    text = _NAME_ROLE_GENERIC.sub(lambda match: _replace_generic_name_role(match, counts), text)
    text = _ROLE_NAME.sub(
        lambda match: match.group("role") + match.group("sep") + "[身份]", text)
    text = _LABELED_NAME.sub(
        lambda match: match.group("label") + ": [身份]", text)
    text = _redact_signature_lines(text, counts)
    return _redact_greeting_lines(text, counts)
COMMON_DOMAINS = {'gmail','hotmail','outlook','yahoo','qq','163','126','sina','foxmail','yeah','live','msn','139','icloud','mail','email','com','net','org','cn','comcn'}
TOPICS = {
 '压合': r'压合|壓合|层压|層壓|laminat|pressing',
 '叠构与材料': r'叠构|疊構|叠板|疊板|铜箔|銅箔|半固化|prepreg|stack.?up|\bPP\b|树脂|樹脂|FR-?4|含胶量|含膠量|resin content',
 '温度与固化': r'升温|升溫|降温|降溫|固化|玻璃化|\bTg\b|curing|热压|熱壓|温度|溫度|temperature',
 '压力与真空': r'真空|压力|壓力|vacuum|pressure',
 '缺陷与可靠性': r'分层|分層|爆板|白斑|空洞|气泡|氣泡|滑板|皱折|皺折|板翘|板翹|翘曲|翹曲|delamination|void|warpage|CAF',
 '质量与验证': r'切片|剝離|剥离|热冲击|熱衝擊|耐热|耐熱|可靠性|DOE|SPC|CPK|microsection',
}


def decode(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    for encoding in ('utf-8-sig', 'gb18030', 'big5', 'cp1252'):
        try:
            return value.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError('undecodable')


def normalize(text):
    text = unicodedata.normalize('NFKC', html.unescape(text))
    text = ''.join(c for c in text if c in '\n\t\r' or unicodedata.category(c) not in ('Cf','Cc','Cs'))
    return text.replace('\r\n', '\n').replace('\r', '\n')


def header_value(value):
    try:
        return ''.join(part.decode(enc or 'utf-8', errors='strict') if isinstance(part, bytes) else part for part, enc in decode_header(value))
    except (UnicodeError, LookupError):
        return value


class IdentityIndex:
    """Raw identities live only in this object's memory, never on disk."""
    def __init__(self):
        self.terms = set()

    def add(self, value):
        value = normalize(header_value(decode(value))).strip(' \t\r\n<>\"\'')
        if 2 <= len(value) <= 100 and '\n' not in value and not value.isdigit():
            self.terms.add(value.casefold())

    def contact(self, value):
        value = header_value(decode(value))
        for name, address in getaddresses([value]):
            if name:
                self.add(name)
                for part in re.split(r'[\s/\\()（）<>_\-]+', name):
                    if CJK.search(part) or (part.isalpha() and len(part) >= 4):
                        self.add(part)
            if '@' in address:
                local, domain = address.rsplit('@', 1)
                for part in domain.lower().split('.'):
                    if len(part) >= 4 and part not in COMMON_DOMAINS:
                        self.add(part)
                # Distinctive user handles also occur in greetings and signatures.
                if len(local) >= 4 and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9._-]+', local):
                    self.add(local)

    def pattern(self):
        return IdentityMatcher(self.terms)


class IdentityMatcher:
    """Aho-Corasick lookup: linear scanning even with thousands of identities."""
    def __init__(self, terms):
        self.edges=[{}]
        self.fail=[0]
        self.outputs=[[]]
        for term in sorted(terms):
            state=0
            for character in term:
                if character not in self.edges[state]:
                    self.edges[state][character]=len(self.edges)
                    self.edges.append({})
                    self.fail.append(0)
                    self.outputs.append([])
                state=self.edges[state][character]
            self.outputs[state].append((len(term),not bool(CJK.search(term))))
        queue=collections.deque(self.edges[0].values())
        while queue:
            state=queue.popleft()
            for char,target in self.edges[state].items():
                queue.append(target)
                fallback=self.fail[state]
                while fallback and char not in self.edges[fallback]:
                    fallback=self.fail[fallback]
                self.fail[target]=self.edges[fallback].get(char,0)
                self.outputs[target].extend(self.outputs[self.fail[target]])

    def finditer(self,text):
        folded=text.casefold()
        offsets=None
        if len(folded)!=len(text):
            offsets=[]
            for i,char in enumerate(text):
                offsets.extend([i]*len(char.casefold()))
        state=0
        longest={}
        for i,char in enumerate(folded):
            while state and char not in self.edges[state]:
                state=self.fail[state]
            state=self.edges[state].get(char,0)
            for length,bounded in self.outputs[state]:
                start=i-length+1
                end=i+1
                if bounded:
                    if start>0 and folded[start-1].isascii() and folded[start-1].isalnum():
                        continue
                    if end<len(folded) and folded[end].isascii() and folded[end].isalnum():
                        continue
                if offsets is not None:
                    start,end=offsets[start],offsets[end-1]+1
                longest[start]=max(longest.get(start,0),end)
        previous_end=0
        for start,end in sorted(longest.items()):
            if start>=previous_end:
                yield start,end
                previous_end=end

    def subn(self,replacement,text):
        pieces=[]
        previous=0
        count=0
        for start,end in self.finditer(text):
            pieces.extend((text[previous:start],replacement))
            previous=end
            count+=1
        pieces.append(text[previous:])
        return ''.join(pieces),count


class Sanitizer:
    def __init__(self, identities, nlp_zh=None, nlp_en=None):
        self.identities = identities
        self.nlp_zh = nlp_zh
        self.nlp_en = nlp_en
        self.counts = collections.Counter()
        self.ner_cache = collections.OrderedDict()

    def sub(self, pattern, replacement, text, category):
        text, count = pattern.subn(replacement, text)
        self.counts[category] += count
        return text

    def lines(self, text):
        out=[]
        signature=False
        header_continuation=False
        for line in text.splitlines():
            if HEADER_LINE.match(line) or QUOTE_BOUNDARY.match(line):
                signature=False
                header_continuation=True
                self.counts['header_lines'] += 1
                continue
            if header_continuation and (not line.strip() or line.startswith(('\t','    '))):
                self.counts['header_continuations'] += 1
                continue
            header_continuation=False
            if SIGNOFF.match(line) or DISCLAIMER.match(line):
                signature=True
            if signature:
                self.counts['signature_lines'] += 1
                continue
            if CONTACT_LINE.match(line) or SALUTATION.match(line):
                self.counts['contact_lines'] += 1
                continue
            out.append(line)
        return '\n'.join(out)

    def rules(self, text):
        text=self.sub(CREDENTIAL, '[凭据]', text, 'credentials')
        text=self.sub(OBFUSCATED_EMAIL, '[邮箱]', text, 'obfuscated_emails')
        text=self.sub(EMAIL, '[邮箱]', text, 'emails')
        text=self.sub(URL, '[网址]', text, 'urls')
        text=self.sub(PATH, '[内部路径]', text, 'paths')
        text=self.sub(FILE, '[附件]', text, 'filenames')
        text=self.sub(DATE, '[日期]', text, 'dates')
        text=self.sub(EN_DATE, '[日期]', text, 'english_dates')
        text=self.sub(PHONE, '[电话]', text, 'phones')
        text=self.sub(IP, '[地址]', text, 'ip_addresses')
        text=self.sub(LONG_ID, '[编号]', text, 'long_ids')
        text=self.sub(BUSINESS, lambda m:m[1]+': [业务信息]', text, 'business_fields')
        text=self.sub(MONEY, '[金额]', text, 'money')
        text=self.sub(ADDRESS, '[地址]', text, 'addresses')
        text=self.sub(EN_ADDRESS, '[地址]', text, 'english_addresses')
        text=self.sub(ORG, '[实体]', text, 'organization_patterns')
        text=self.sub(MIXED_ID, lambda m:m[0] if SAFE_CODE.fullmatch(m[0]) else '[编号]', text, 'mixed_identifiers')
        text=self.sub(self.identities, '[身份]', text, 'known_identities')
        return text

    def ner(self, text):
        if not text.strip():
            return text
        # Fixed chunks with overlapping context avoid losing entities at boundaries.
        spans=[]
        for start in range(0, len(text), 1800):
            lo=max(0,start-120)
            hi=min(len(text),start+1920)
            chunk=text[lo:hi]
            digest=hashlib.sha256(chunk.encode()).digest()
            entities=self.ner_cache.get(digest)
            if entities is None:
                entities=[]
                models=[]
                if self.nlp_zh is not None and CJK.search(chunk):
                    models.append(self.nlp_zh)
                if self.nlp_en is not None and re.search(r'[a-zA-Z]{3,}',chunk):
                    models.append(self.nlp_en)
                for nlp in models:
                    for ent in nlp(chunk).ents:
                        if ent.label_ in {'PERSON','PER','ORG','GPE','LOC','FAC','NORP'} and not PLACEHOLDER.search(ent.text):
                            entities.append((ent.start_char,ent.end_char))
                self.ner_cache[digest]=entities
                if len(self.ner_cache)>2500:
                    self.ner_cache.popitem(last=False)
            spans.extend((lo+a,lo+b) for a,b in entities)
        merged=[]
        for a,b in sorted(set(spans)):
            if merged and a<=merged[-1][1]:
                merged[-1]=(merged[-1][0], max(b,merged[-1][1]))
            else:
                merged.append((a,b))
        self.counts['ner_entities']+=len(merged)
        for a,b in reversed(merged):
            text=text[:a]+'[实体]'+text[b:]
        return text

    def clean(self, text):
        text=self.lines(normalize(text))
        text=self.rules(text)
        text=self.ner(text)
        # Repeat deterministic rules after NER so changes cannot create a new match.
        text=self.rules(text)
        text=redact_residual_person_names(text, self.counts)
        text=re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', text)
        return text.strip()

    def residuals(self, text):
        # Exclude placeholders from the audit, but inspect all other output bytes.
        text=PLACEHOLDER.sub(' ',text)
        result={}
        for name,pattern in [('email',EMAIL),('obfuscated_email',OBFUSCATED_EMAIL),('url',URL),('phone',PHONE),('long_id',LONG_ID),('date',DATE),('english_date',EN_DATE),('path',PATH),('file',FILE),('known_identity',self.identities),('credential',CREDENTIAL),('address',ADDRESS),('english_address',EN_ADDRESS),('organization',ORG)]:
            count=sum(1 for _ in pattern.finditer(text))
            if count:
                result[name]=count
        return result


def opaque_id(key, source_id):
    return 'mail_'+hmac.new(key,str(source_id).encode(),hashlib.sha256).hexdigest()[:24]


def technical_topics(text):
    return [name for name,pattern in TOPICS.items() if re.search(pattern,text,re.I)]
