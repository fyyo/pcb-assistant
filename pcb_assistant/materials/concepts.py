"""Literal material terminology signals, not validated process recommendations."""
from __future__ import annotations

import re


MATERIAL_TERMS = {
    'tg': ('tg', '玻璃化转变', '玻璃化轉變', '玻璃转化', '玻璃轉化', 'glass transition'),
    'td': ('td', '热分解温度', '熱分解溫度', '分解温度', '分解溫度', 'decomposition temperature'),
    'rf': ('rf', 'r/f', '树脂流动度', '樹脂流動度', '胶流量', '膠流量', 'resin flow'),
    'gt': ('gt', 'g/t', '凝胶时间', '凝膠時間', 'gel time'),
}
LABELS = {'tg': 'Tg（玻璃化转变温度）', 'td': 'Td（分解温度）',
          'rf': 'RF（树脂流动度）', 'gt': 'GT（凝胶时间）'}
_DESCRIPTIONS = {
    'tg': r'是指|指的是|定义为|定義為|由.{0,20}(?:转变|轉變)|从.{0,20}(?:转变|轉變)|'
          r'\b(?:temperature at which|changes? from|measur\w*|determining|defined as)\b',
    'td': r'是指|指的是|定义为|定義為|失重|重量变化|重量變化|开始分解|開始分解|'
          r'\b(?:temperature at which|weight (?:loss|change)|measur\w*|defined as)\b',
    'rf': r'是指|指的是|定义为|定義為|测定|測定|规定条件|規定條件|'
          r'\b(?:measur\w*|calculated|defined as)\b',
    'gt': r'是指|指的是|定义为|定義為|测定|測定|规定条件|規定條件|'
          r'\b(?:measur\w*|determining|defined as|elapsed time)\b',
}
_SCOPE_QUESTION = re.compile(
    r'能否|能不能|可否|是否|能.{0,12}(?:当作|當作|作为|作為|等同|代表|代替)|'
    r'适用|適用|等同|区别|區別|\b(?:can|equivalent|difference)\b', re.I)


def _matches(text, alias):
    pattern = (r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])'
               if alias.isascii() else re.escape(alias))
    for match in re.finditer(pattern, text, re.I):
        # TMA formula notation tD is a displacement variable, not Td.
        if alias == 'td' and match[0] == 'tD':
            continue
        yield match


def material_concepts(text):
    result = []
    for key, aliases in MATERIAL_TERMS.items():
        if not any(any(_matches(text, alias)) for alias in aliases):
            continue
        if key == 'rf' and re.search(r'射频|射頻|radio frequency|RF\s+coupon', text, re.I) and not re.search(
                r'树脂|樹脂|胶流|膠流|resin flow', text, re.I):
            continue
        result.append(key)
    return tuple(result)


def is_material_scope_question(text):
    return bool(material_concepts(text) and _SCOPE_QUESTION.search(text))


def material_ranking_keys(groups):
    return tuple(key for key, aliases in MATERIAL_TERMS.items() if aliases in groups)


def definition_keys(text, keys):
    """Require an explanatory clause near the named quantity, not just a heading."""
    if re.search(r'列\d+\s*=|(?:Typical\s+)?Values? Table', text, re.I):
        return ()
    table_headers = re.findall(r'^(?:Property|Typical Values?|Units|Test Method)\s*$', text, re.I | re.M)
    if len(table_headers) >= 2:
        return ()
    found = []
    for key in keys:
        for alias in MATERIAL_TERMS[key]:
            for match in _matches(text, alias):
                # Do not credit a definition belonging to a different paragraph.
                boundary = text.rfind('\n\n', 0, match.start())
                start = boundary + 2 if boundary >= 0 else 0
                end = text.find('\n\n', match.end())
                local = text[max(start, match.start() - 90):min(
                    end if end >= 0 else len(text), match.end() + 260)]
                semantic_name = any(any(_matches(local, name)) for name in MATERIAL_TERMS[key][1:]
                                    if name not in ('r/f', 'g/t'))
                if semantic_name and re.search(_DESCRIPTIONS[key], local, re.I) and not local.rstrip().endswith(('?', '？')):
                    found.append(key)
                    break
            if key in found:
                break
    return tuple(found)


def material_explanation_quality(text, keys):
    mentions = set(material_concepts(text)) & set(keys)
    return len(definition_keys(text, keys)), len(mentions)


def material_paragraph_window(text, hit_start, hit_end, max_chars):
    """Keep wrapped definition text after its heading/semicolon in one literal slice."""
    start = text.rfind('\n\n', 0, hit_start)
    start = start + 2 if start >= 0 else 0
    end = text.find('\n\n', hit_end)
    end = end if end >= 0 else len(text)
    if end - start <= max_chars:
        return start, end
    # Text extracted from PDF often has no blank lines. Keep the complete
    # sentence across wrapped lines, stopping at the next section heading.
    before = max(0, hit_start - 180)
    boundaries = list(re.finditer(r'[。！？!?]|\.(?=\s+[A-Z])|\n(?=\d+\.\d+\s)', text[before:hit_start]))
    start = before + boundaries[-1].end() if boundaries else before
    next_boundary = re.search(r'[。！？!?]|\.(?=\s|$)|\n(?=\d+\.\d+\s)', text[hit_end:])
    if next_boundary:
        end = hit_end + next_boundary.end()
        if start <= hit_start < hit_end <= end and end - start <= max_chars:
            return start, end
    return None


def material_gap_lines(question, citations):
    """State what the returned excerpts cannot establish, without adding facts."""
    keys = material_concepts(question)
    if not keys:
        return []
    definitions = {key for citation in citations for key in definition_keys(citation['text'], keys)}
    lines = []
    missing = [LABELS[key] for key in keys if key not in definitions]
    if missing:
        lines.append('当前摘录未提供完整定义或测试说明：' + '、'.join(missing) + '；指标名称或检验表头不能补足定义。')
    scopes = []
    if 'tg' in keys and re.search(r'卸[压壓]|release pressure|pressure release', question, re.I):
        scopes.append('Tg 能否作为通用卸压温度')
    if set(keys) & {'rf', 'gt'} and _SCOPE_QUESTION.search(question):
        if 'rf' in keys and re.search(r'流[胶膠]量|实板|實板|actual board', question, re.I):
            scopes.append('RF 与实板流胶量的对应关系')
        if 'gt' in keys and re.search(r'填充|填[胶膠]|可流动|可流動|filling', question, re.I):
            scopes.append('GT 与实板有效填充时间的对应关系')
    if scopes:
        # Definitions alone never establish a process mapping. Even an excerpt
        # mentioning that mapping needs its material/method/conditions checked.
        lines.append('尚未核实的适用性子问题：' + '；'.join(scopes) + '。')
        lines.append('不能仅凭上述指标名称、定义或实验数值确定实板工艺参数；需核对对应材料的测试方法、加工指南与实际料温／压力条件及验证记录。')
    return lines
