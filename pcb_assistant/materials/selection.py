"""Literal PP selection conditions and excerpt signals; no engineering model."""
from __future__ import annotations

from decimal import Decimal
import re

PREPREG = ('半固化片', '半固化', 'prepreg', 'pp')
RESIN_CONTENT = ('树脂含量', '樹脂含量', '含胶量', '含膠量', 'resin content', 'rc')
COPPER = ('铜厚', '銅厚', 'copper thickness', '铜箔', '銅箔')
COVERAGE = ('残铜率', '殘銅率', '铜覆盖率', '銅覆蓋率', 'copper coverage')
THICKNESS = ('介质厚度', '介質厚度', '介厚', 'dielectric thickness', '压后厚度', '壓後厚度')
STACKUP = ('叠构', '疊構', '叠层', '疊層', 'stackup', 'stack-up')
FIELDS = (RESIN_CONTENT, COPPER, COVERAGE, THICKNESS)
_CHOICE = re.compile(r'选择|選擇|选型|選型|怎么选|怎麼選|选用|選用|搭配|组合|組合|'
                     r'\b(?:select\w*|choos\w*)\b', re.I)
_MATERIAL = re.compile(r'半固化|\b(?:pp|prepreg|rc)\b|树脂|樹脂|resin', re.I)
_TROUBLE = re.compile(r'排查|诊断|診斷|原因|怎么处理|如何解决|troubleshoot|diagnos', re.I)
_NUMBER = r'(?<![\d.])\d+(?:\.\d+)?'
_UNITS = r'(?:oz|[μµu]m|mm|mil|盎司|微米|毫米)'
_GLASS = re.compile(r'(?<![a-z0-9])(?:1067|1078|1080|2113|2116|2313|3313|1506|7628|106)(?![a-z0-9])', re.I)
_CODE = re.compile(r'(?<![a-z0-9])(?:[a-z]+-?\d+[a-z0-9]*(?:-[a-z0-9]+)*)(?![a-z0-9])', re.I)


def is_selection_question(text):
    return bool(_CHOICE.search(text) and (_MATERIAL.search(text) or has_alias(text, PREPREG))
                and not _TROUBLE.search(text))


def has_alias(text, aliases):
    return any(re.search(r'(?<![a-z0-9])' + re.escape(a) + r'(?![a-z0-9])', text, re.I)
               if a.isascii() else a in text for a in aliases)


def selection_conditions(question):
    """Keep explicit values; never infer a grade, convert units or estimate RC."""
    result = []
    def add(kind, label, value):
        item = {'kind': kind, 'label': label, 'value': value.casefold()}
        if item not in result and len(result) < 8:
            result.append(item)
    for match in _CODE.finditer(question):
        if len(match[0]) <= 40 and not re.fullmatch(r'(?:rc|l)\d+', match[0], re.I):
            add('grade', '材料牌号', match[0])
    for match in _GLASS.finditer(question):
        add('glass', '玻纤布型号', match[0])
    for kind, label, aliases, unit in (
        ('copper', '铜厚', COPPER, _UNITS),
        ('coverage', '残铜率／铜覆盖率', COVERAGE, r'[%％]'),
        ('thickness', '目标介质厚度', THICKNESS, _UNITS),
        ('rc', '树脂含量', RESIN_CONTENT, r'[%％]'),
    ):
        marker = '|'.join(re.escape(a) for a in aliases)
        pattern = r'(?:' + marker + r')[\s:：=约約为為]*(?:为|為)?\s*(' + _NUMBER + r'\s*' + unit + r')'
        for match in re.finditer(pattern, question, re.I):
            add(kind, label, match[1])
    return tuple(result)


def condition_in_text(condition, text):
    value = condition['value']
    if condition['kind'] == 'grade':
        return bool(re.search(r'(?<![a-z0-9-])' + re.escape(value) + r'(?![a-z0-9-])', text, re.I))
    if condition['kind'] == 'glass':
        # A weave/count notation literally mentions the weave, not a validated
        # layup, compressed thickness or resin amount.
        return bool(re.search(r'(?<![a-z0-9])'+re.escape(value)+
                              r'(?:\s*[x×]\s*[1-9]\d*)?(?![a-z0-9])',text,re.I))
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(.*)', value)
    number = format(Decimal(match[1]).normalize(), 'f')
    numeric = re.escape(number) + (r'0*' if '.' in number else r'(?:\.0+)?')
    unit = r'[%％]' if match[2] in ('%', '％') else re.escape(match[2])
    quantity = r'(?<![\d.])' + numeric + r'\s*' + unit + r'(?![a-z0-9])'
    aliases = {'copper': COPPER, 'coverage': COVERAGE, 'thickness': THICKNESS, 'rc': RESIN_CONTENT}[condition['kind']]
    # A number elsewhere in a table cannot establish this field's correspondence.
    marker = '(?:' + '|'.join(re.escape(a) for a in aliases) + ')'
    return bool(re.search(marker + r'[\s:：=约約为為]*' + quantity, text, re.I))


def selection_term_groups(question):
    conditions = selection_conditions(question)
    groups = [(c['value'],) for c in conditions]
    groups.extend((PREPREG, RESIN_CONTENT))
    for aliases in (COPPER, COVERAGE, THICKNESS):
        if has_alias(question, aliases):
            groups.append(aliases)
    if has_alias(question, STACKUP):
        groups.append(STACKUP + ('层别', '層別'))
    if re.search(r'进料检验|進料檢驗',question):
        groups.append(('进料检验', '進料檢驗', '胶含量', '膠含量'))
    return tuple(groups[:12])


def selection_quality(text, conditions=()):
    """Signals describe literal excerpts, never correctness or applicability."""
    topical = has_alias(text, PREPREG) or has_alias(text, RESIN_CONTENT)
    dimensions = sum(has_alias(text, group) for group in FIELDS)
    decision = bool(re.search(r'选|選|搭配|组合|組合|填胶|填膠|试压|試壓|压后|壓後|'
                              r'select|choos|fill', text, re.I))
    numerical = bool(re.search(r'\d+(?:\.\d+)?\s*(?:%|％|oz|mm|mil|[μµ]m)|\bRC\s*\d+', text, re.I))
    inspection = bool(re.search(r'进料检验|進料檢驗|检验规格|檢驗規格|凝胶时间|凝膠時間', text))
    connected = topical and decision and any(len(line.strip()) >= 40 for line in text.splitlines())
    return (sum(condition_in_text(c, text) for c in conditions),
            int(connected), int(topical and (decision or dimensions >= 2)), int(topical),
            dimensions, int(numerical), -int(inspection))


def selection_coverage(question, texts):
    conditions = selection_conditions(question)
    checks = [{**c, 'mentioned_in_excerpt': any(condition_in_text(c, text) for text in texts)}
              for c in conditions]
    return {'basis': 'literal_conditions_in_individual_returned_excerpts',
            'scope': 'recognized_explicit_conditions_only',
            'conditions': checks,
            'missing': [c for c in checks if not c['mentioned_in_excerpt']],
            'all_conditions_in_one_excerpt': bool(conditions) and any(
                all(condition_in_text(c, text) for c in conditions) for text in texts),
            'engineering_conclusion_supported': False}
