"""Read explicit row/column markers without reconstructing flattened OCR tables."""
from __future__ import annotations

import re

from pcb_selection import (
    COPPER, COVERAGE, RESIN_CONTENT, THICKNESS, condition_in_text, has_alias,
)

_ROWS = re.compile(r'^行(\d+)\s*\|', re.M)
_CELLS = re.compile(r'(?:\|\s*)列(\d+)=')
_UNIT = re.compile(r'[%％]|(?<![a-z])(?:oz|mm|mil|[μµu]m)(?![a-z])', re.I)
ROW_ROLE_LABELS = {
    'header':'表头', 'specification':'规格要求', 'remark':'备注／签核',
    'data_candidate':'数据候选行', 'unresolved':'内容待核对',
}


def row_role(row):
    """Describe explicit row wording; a data candidate is not validated data."""
    body = ' '.join(cell['text'] for cell in row['cells'])
    # These rows may contain glass styles and percentages but are requirements.
    if re.search(r'规格需符合|規格需符合|规格要求|規格要求|公差.*(?:极限|極限)|'
                 r'极限要求|極限要求|宽度规格|寬度規格|厂商依|廠商依|'
                 r'需符合|不得|不可低於|不可低于|要求.*[≤≦≥≧<>]|'
                 r'\b(?:requirement|tolerance|limit)\b',body,re.I):
        return 'specification'
    if re.search(r'备注|備註|签名|簽名|签属|簽屬|盖章|蓋章|职务|職務|'
                 r'\b(?:remark|signature)\b',body,re.I):
        return 'remark'
    kinds = [_header_kind(cell['text']) for cell in row['cells']]
    if sum(kind is not None for kind in kinds)>=2:
        return 'header'
    if not row['valid']:
        return 'unresolved'
    material_code = any(re.search(r'(?<![a-z0-9])\d{3,4}[_ ]RC\d+(?:[._ -]|$)',
                                  cell['text'],re.I) for cell in row['cells'])
    simple_values = sum(bool(re.fullmatch(r'\d+(?:\.\d+)?\s*(?:[%％]|oz|mm|mil|[μµu]m)?',
                                         cell['text'],re.I)) for cell in row['cells'])
    stackup_code = bool(re.search(r'\bCore\s+\d|(?<!\d)\d{3,4}x\d',body,re.I))
    if material_code or simple_values>=2 or (stackup_code and simple_values):
        return 'data_candidate'
    return 'unresolved'


def table_row_context(text,start,end):
    """Only intersecting explicit rows, with exact spans and no original values."""
    return [{'row':row['row'],'start':row['start'],'end':row['end'],
             'basis_start':row['basis_start'],'basis_end':row['basis_end'],
             'role':row['role'],'valid_column_markers':row['valid'],
             'engineering_conclusion_supported':False}
            for row in classified_rows(text) if row['start']<end and row['end']>start]


def parallel_column_groups(text):
    """Repeated explicit layer headings delimit parallel blocks, not versions."""
    for row in explicit_rows(text):
        if not row['valid']:
            continue
        anchors=[cell for cell in row['cells'] if re.fullmatch(
            r'層別|层别|层次|層次|layer',cell['text'],re.I)]
        if len(anchors)<2:
            continue
        last=max(cell['column'] for cell in row['cells'])
        return {'basis':'repeated_layer_headings_with_explicit_columns',
                'header_row':row['row'],'header_start':row['start'],'header_end':row['end'],
                'groups':[{'first_column':cell['column'],
                           'last_column':anchors[i+1]['column']-1 if i+1<len(anchors) else last}
                          for i,cell in enumerate(anchors)],
                'version_identity_verified':False,'engineering_conclusion_supported':False}
    return None


def is_table_question(text):
    return bool(re.search(r'进料检验表|進料檢驗表|表头|表頭|表格|工作表',text))


def _header_kind(value):
    if len(value) > 45 or '\n' in value or re.search(r'\d',value):
        return None
    if has_alias(value, RESIN_CONTENT) or re.search(r'胶含量|膠含量|R/C', value, re.I):
        return 'rc'
    if has_alias(value, COVERAGE):
        return 'coverage'
    if has_alias(value, THICKNESS):
        return 'thickness'
    if has_alias(value, COPPER):
        return 'copper'
    if re.search(r'布型|布种|布種|玻布|glass style', value, re.I):
        return 'glass'
    if re.fullmatch(r'(?:材料|材料牌号|材料牌號|牌号|牌號|material|material grade|grade)', value.strip(), re.I):
        return 'grade'
    return None


def explicit_rows(text):
    """Each cell keeps exact offsets. Damaged/repeated columns invalidate a row."""
    matches = list(_ROWS.finditer(text))
    rows = []
    for index, match in enumerate(matches):
        end = matches[index+1].start() if index+1 < len(matches) else len(text)
        while end > match.start() and text[end-1].isspace():
            end -= 1
        markers = list(_CELLS.finditer(text, match.start(), end))
        columns = [int(m[1]) for m in markers]
        valid = (bool(columns) and columns == sorted(set(columns))
                 and len(list(re.finditer(r'\|\s*列\d+',text[match.start():end])))==len(markers))
        cells = []
        for i, marker in enumerate(markers):
            start = marker.end()
            stop = markers[i+1].start() if i+1 < len(markers) else end
            while start < stop and text[start].isspace():
                start += 1
            while stop > start and text[stop-1].isspace():
                stop -= 1
            cells.append({'column':int(marker[1]), 'start':start, 'end':stop,
                          'text':text[start:stop]})
        rows.append({'row':int(match[1]), 'start':match.start(), 'end':end,
                     'cells':cells, 'valid':valid})
    return rows


def classified_rows(text):
    rows = explicit_rows(text)
    specification_section = None
    previous = 0
    for row in rows:
        if row['row']<=previous:
            specification_section = None
        previous = row['row']
        role = row_role(row)
        body = ' '.join(cell['text'] for cell in row['cells'])
        if role in ('header','remark'):
            specification_section = None
        if re.search(r'规格需符合要求|規格需符合要求|^规格要求[:：]|^規格要求[:：]',body):
            specification_section = (row['start'],row['end'])
        row['basis_start'],row['basis_end'] = row['start'],row['end']
        if specification_section and role not in ('header','remark'):
            role = 'specification'
            row['basis_start'],row['basis_end'] = specification_section
        row['role'] = role
    return rows


def table_records(text):
    # Parallel blocks require independently verified column/version mapping.
    if parallel_column_groups(text):
        return []
    header = None
    records = []
    previous = 0
    for row in classified_rows(text):
        if row['row'] <= previous:
            header = None
        previous = row['row']
        if not row['valid']:
            header = None
            continue
        if row['role'] in ('specification','remark'):
            header = None
            continue
        fields = [(cell, _header_kind(cell['text'])) for cell in row['cells']]
        fields = [(cell, kind) for cell, kind in fields if kind]
        kinds = [kind for _, kind in fields]
        if len(set(kinds)) >= 2 and set(kinds) & {'rc','glass'}:
            # Repeated semantic columns and merged/multilevel headings need review.
            header = (row, fields) if len(set(kinds)) == len(kinds) else None
            continue
        if fields:
            header = None
            continue
        if not header:
            continue
        header_row, headings = header
        values = {cell['column']:cell for cell in row['cells']}
        pairs = []
        for cell, kind in headings:
            value = values.get(cell['column'])
            if not value:
                continue
            unit = _UNIT.search(cell['text'])
            pairs.append({'kind':kind, 'header':dict(cell), 'value':dict(value),
                          'unit':unit[0] if unit else None})
        if len(pairs) >= 2:
            records.append({'basis':'explicit_row_column_markers',
                            'header_row':header_row['row'], 'data_row':row['row'],
                            'header_start':header_row['start'], 'header_end':header_row['end'],
                            'row_start':row['start'], 'row_end':row['end'], 'fields':pairs,
                            'engineering_conclusion_supported':False})
    return records


def record_matches(condition, record):
    for field in record['fields']:
        if field['kind'] != condition['kind']:
            continue
        value = field['value']['text']
        if condition['kind'] in ('grade','glass'):
            if value.strip().casefold()==condition['value']:
                return True
        else:
            if not re.fullmatch(r'\d+(?:\.\d+)?\s*(?:[%％]|oz|mm|mil|[μµu]m)?',value,re.I):
                continue
            # Unit inheritance is allowed only from this explicit column header.
            if not _UNIT.search(value) and field['unit']:
                value += ' ' + field['unit']
            labels = {'copper':'铜厚', 'coverage':'残铜率', 'thickness':'介质厚度', 'rc':'树脂含量'}
            if condition_in_text(condition, labels[condition['kind']] + value):
                return True
    return False


def returned_table_coverage(conditions, citations):
    records, seen = [], set()
    for item in citations:
        record = item.get('table_context')
        if not record:
            continue
        key = (item['doc_id'],record['header_start'],record['row_start'])
        if key in seen:
            continue
        related = [c for c in citations if c['doc_id']==item['doc_id']]
        # Both header and data must actually be present in returned quotes.
        if not all(any(c['start'] <= start and c['end'] >= end for c in related)
                   for start,end in ((record['header_start'],record['header_end']),
                                     (record['row_start'],record['row_end']))):
            continue
        seen.add(key)
        records.append({'doc_id':item['doc_id'], 'header_row':record['header_row'],
                        'data_row':record['data_row'],
                        'matched_conditions':[dict(c) for c in conditions if record_matches(c,record)],
                        'all_recognized_conditions_same_row':bool(conditions) and all(
                            record_matches(c,record) for c in conditions),
                        'citation_ids':[c['excerpt_id'] for c in related if c.get('table_context')==record]})
    return {'basis':'explicit_same_column_header_and_single_data_row_in_returned_quotes',
            'records':records, 'engineering_conclusion_supported':False}


def table_excerpt_pair(text, terms, conditions, max_chars):
    candidates = []
    for record in table_records(text):
        if (record['header_end']-record['header_start'] > max_chars
                or record['row_end']-record['row_start'] > max_chars):
            continue
        row = text[record['row_start']:record['row_end']]
        hits = sum(has_alias(row.casefold(),(term.casefold(),)) for term in terms if term)
        matched = sum(record_matches(c,record) for c in conditions)
        if not hits or (conditions and not matched):
            continue
        candidates.append((matched,hits,-record['row_start'],record))
    if not candidates:
        return None
    record = max(candidates,key=lambda item:item[:3])[3]
    if record['row_end']-record['header_start'] <= max_chars:
        spans = [(record['header_start'],record['row_end'])]
    else:
        spans = [(record['header_start'],record['header_end']),
                 (record['row_start'],record['row_end'])]
    return spans, record


def parallel_excerpt_spans(text, terms, conditions, max_chars):
    """Return an intact parallel header and hit row, without mapping fields."""
    group=parallel_column_groups(text)
    if not group or group['header_end']-group['header_start']>max_chars:
        return None
    candidates=[]
    for row in classified_rows(text):
        if (not row['valid'] or row['start']<=group['header_start']
                or row['end']-row['start']>max_chars or row['role']!='data_candidate'):
            continue
        body=text[row['start']:row['end']]
        matched=sum(condition_in_text(c,body) for c in conditions)
        hits=sum(has_alias(body,(term,)) for term in terms if term)
        if matched or hits:
            candidates.append((matched,hits,-row['start'],row))
    if not candidates:
        return None
    row=max(candidates,key=lambda v:v[:3])[3]
    return [(group['header_start'],group['header_end']),(row['start'],row['end'])]


def scoped_table_spans(text, target_row, max_chars):
    """Re-read one requested explicit row and its own parallel header."""
    rows=classified_rows(text)
    targets=[r for r in rows if r['row']==target_row]
    if len(targets)!=1:
        return [], 'row_missing_or_ambiguous'
    row=targets[0]
    if not row['valid'] or row['role']!='data_candidate':
        return [], 'row_is_not_data_candidate'
    # A row-number reset ends the earlier header's scope.
    preceding=[]
    previous=0
    for r in rows:
        if r['row']<=previous:
            preceding=[]
        previous=r['row']
        if r['start']==row['start']:
            break
        if parallel_column_groups(text[r['start']:r['end']]):
            preceding.append(r)
    if not preceding:
        return [], 'parallel_header_not_found_for_row'
    header=preceding[-1]
    if any(r['end']-r['start']>max_chars for r in (header,row)):
        return [], 'complete_header_or_row_exceeds_excerpt_limit'
    return [(header['start'],header['end']),(row['start'],row['end'])], 'matched'


def table_layout_pair(text, terms, max_chars, conditions=()):
    """Keep raw headings with a matching row when semantic headings are ambiguous."""
    headers, candidates = [], []
    for row in classified_rows(text):
        if not row['valid'] or row['end']-row['start'] > max_chars:
            continue
        kinds = [_header_kind(cell['text']) for cell in row['cells']]
        if sum(kind is not None for kind in kinds)>=2 and set(kinds) & {'rc','glass'}:
            headers.append(row)
            continue
        preceding = [h for h in headers if h['row'] < row['row']]
        if not preceding:
            continue
        body = text[row['start']:row['end']].casefold()
        hits = sum(has_alias(body,(term.casefold(),)) for term in terms if term)
        if hits:
            values = sum(has_alias(body,(c['value'],)) for c in conditions)
            candidates.append((values,hits,-row['start'],preceding[-1],row))
    if not candidates:
        return None
    _,_,_,header,row = max(candidates,key=lambda v:v[:3])
    quote_start = row['start']
    if row['end']-row['basis_start']<=max_chars:
        quote_start = row['basis_start']
    return [(header['start'],header['end']),(quote_start,row['end'])], {
        'basis':'explicit_rows_with_unresolved_header_semantics',
        'header_row':header['row'],'data_row':row['row'],
        'header_start':header['start'],'header_end':header['end'],
        'row_start':row['start'],'row_end':row['end'],
        'status':'column_correspondence_requires_review',
        'row_role':row['role'],
        'engineering_conclusion_supported':False}


def source_location(title, locator, text, start, end):
    location = {'text_line_start':text.count('\n',0,start)+1,
                'text_line_end':text.count('\n',0,max(start,end-1))+1}
    if locator.get('unit_id'):
        location['unit_id'] = locator['unit_id']
    for pattern, key in ((r'(?:^|/)第(\d+)页(?:/|$)','page'),
                         (r'(?:^|/)工作表(\d+)(?:/|$)','sheet'),
                         (r'(?:^|/)幻灯片(\d+)(?:/|$)','slide'),
                         (r'(?:^|/)表格(\d+)(?:/|$)','table')):
        match = re.search(pattern,title)
        if match:
            location[key] = int(match[1])
    rows = [row for row in explicit_rows(text) if row['start'] < end and row['end'] > start]
    if rows:
        location['rows'] = [row['row'] for row in rows]
        location['columns'] = sorted({cell['column'] for row in rows for cell in row['cells']
                                      if cell['start'] < end and cell['end'] > start})
    location['basis'] = 'source_title_and_explicit_text_markers'
    return location


def location_label(location):
    labels = []
    for key, label in (('page','页'),('sheet','工作表'),('slide','幻灯片'),('table','表格')):
        if key in location:
            labels.append(label + str(location[key]))
    if location.get('rows'):
        labels.append('原表行' + ','.join(map(str,location['rows'])))
    if location.get('columns'):
        labels.append('列' + ','.join(map(str,location['columns'])))
    labels.append('正文行{}–{}'.format(location['text_line_start'],location['text_line_end']))
    return ' / '.join(labels)
