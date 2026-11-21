"""Focused field review from one already scoped returned table block."""
import re

from pcb_assistant.materials.selection import RESIN_CONTENT, THICKNESS, COPPER, COVERAGE, has_alias
from pcb_assistant.tables.checklist import FIELD_LABELS

FIELD_ORDER=('grade','glass','rc','thickness','copper','coverage')
_FOCUS_WORDS=re.compile(r'只(?:看|查|核对|核對)|核对|核對|这个|這個|该|該|这一行|這一行|呢|'
                        r'多少|哪(?:个|個)?列|单位|單位|字段|表头|表頭|原文|对应|對應|含义|含義|\?')
_UNIT_FOLLOWUP=re.compile(r'单位|單位|版本|含义|含義|依据|依據|原文|这一项|這一項|补证|補證|'
                        r'补哪些|補哪些|需要什么资料|需要什麼資料|还缺什么|還缺什麼')
_FIELD_LOCATOR=re.compile(r'^字段定位：([a-z,]+)。$',re.M)


def requested_table_fields(question):
    """Recognize field names only; never parse RC digits from material codes."""
    # Codes are source text, not explicit field requests.
    clean=re.sub(r'(?<![a-z0-9])\d{3,4}[_ ]RC\d+[a-z0-9_.-]*',' ',question,flags=re.I)
    fields=[]
    for kind,aliases in (('rc',RESIN_CONTENT+('胶含量','膠含量','R/C')),
                         ('thickness',THICKNESS),('copper',COPPER),('coverage',COVERAGE)):
        if has_alias(clean,aliases):
            fields.append(kind)
    if re.search(r'材料牌号|材料牌號|材料列|牌号|牌號',clean):
        fields.append('grade')
    if re.search(r'布型|布种|布種|玻布|glass style',clean,re.I):
        fields.append('glass')
    return tuple(k for k in FIELD_ORDER if k in fields)


def field_followup_requested(question):
    return bool(requested_table_fields(question) and _FOCUS_WORDS.search(question))


def focused_fields(question, previous_assistant=''):
    fields=requested_table_fields(question)
    if fields:
        return fields
    if _UNIT_FOLLOWUP.search(question):
        match=_FIELD_LOCATOR.search(previous_assistant)
        if match:
            return tuple(k for k in FIELD_ORDER if k in match[1].split(','))
    return ()


def field_locator_line(fields):
    return '字段定位：'+','.join(fields)+'。'


def table_field_checks(block_checks, fields):
    """Project literal cells for requested fields; do not estimate missing data."""
    checks=[]
    for block in block_checks['checks']:
        by_column={c['column']:c for c in block['columns']}
        for kind in fields:
            field=next(f for f in block['fields'] if f['field']==kind)
            checks.append({
                'doc_id':block['doc_id'],'header_row':block['header_row'],'data_row':block['data_row'],
                'first_column':block['first_column'],'last_column':block['last_column'],
                **field,'cells':[by_column[n] for n in field['columns']],
                'citation_ids':list(block['citation_ids']),
                'version_identity_verified':False,'column_semantics_verified':False,
                'engineering_conclusion_supported':False})
    return {'basis':'requested_fields_in_single_scoped_returned_block',
            'requested_fields':list(fields),'checks':checks,'engineering_conclusion_supported':False}


def field_check_lines(result):
    lines=[]
    for check in result['checks']:
        refs=', '.join(check['citation_ids'])
        label=FIELD_LABELS[check['field']]
        lines.append('字段核对：{}，原表行{}、区块列{}–{}。 [{}]'.format(
            label,check['data_row'],check['first_column'],check['last_column'],refs))
        if not check['cells']:
            lines.append('返回表头中未定位该字段；未给该行其他数值指定含义。 [{}]'.format(refs))
        for cell in check['cells']:
            heading=cell['heading'];value=cell['value']
            lines.append('列{}表头原文：“{}”；当前行原文：{}。 [{}]'.format(
                cell['column'],heading['text'], '“'+value['text']+'”' if value else '未返回该列值',refs))
        if check['status']=='ambiguous_headings_require_review':
            lines.append('该字段有多个可见表头，列含义不唯一，未挑选其中一个值作为答案。 [{}]'.format(refs))
        if check['unit_status']=='unit_not_located_in_returned_block':
            lines.append('对应列未定位明确单位；未从材料代码、其他列或另一侧补入单位。 [{}]'.format(refs))
        elif check['unit_status']=='literal_unit_requires_review':
            units='、'.join('“'+ref['text']+'”' for ref in check['unit_evidence'])
            lines.append('对应列可见单位文字：{}；单位定义及适用口径仍待核对。 [{}]'.format(units,refs))
        lines.append('版本身份、列含义与适用性尚未验证；缺口仅针对已返回表头和该区块当前行。 [{}]'.format(refs))
    return lines
