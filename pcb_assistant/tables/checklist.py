"""Review a single parallel block using only complete returned source rows."""
from __future__ import annotations

import re

from pcb_assistant.materials.selection import condition_in_text, selection_conditions
from pcb_assistant.tables.context import explicit_rows, parallel_column_groups, _header_kind

FIELD_LABELS = {'grade':'材料列文字', 'glass':'布型列', 'rc':'RC（树脂含量）列',
                'thickness':'介质厚度列', 'copper':'铜厚列', 'coverage':'残铜率列'}
_UNITS = {'rc':r'[%％]', 'coverage':r'[%％]',
          'thickness':r'(?<![a-z])(?:mm|mil|[μµu]m)(?![a-z])',
          'copper':r'(?<![a-z])(?:oz|mm|mil|[μµu]m)(?![a-z])'}


def _cell_reference(cell, base, citation_id):
    return {'text':cell['text'], 'start':base+cell['start'],
            'end':base+cell['end'], 'citation_id':citation_id}


def _complete_row(citation, start, end):
    lo, hi = citation.get('start'), citation.get('end')
    if lo is None or hi is None or not lo<=start<end<=hi:
        return None
    fragment=citation['text'][start-lo:end-lo]
    rows=explicit_rows(fragment)
    if len(rows)!=1 or not rows[0]['valid'] or rows[0]['start']!=0 or rows[0]['end']!=len(fragment):
        return None
    return rows[0]


def _requested_side(question):
    left=bool(re.search(r'左(?:侧|側|边|邊|区块|區塊|半表)|\bleft\s+(?:block|side)\b',question,re.I))
    right=bool(re.search(r'右(?:侧|側|边|邊|区块|區塊|半表)|\bright\s+(?:block|side)\b',question,re.I))
    return 'left' if left and not right else 'right' if right and not left else None


def stackup_block_checks(question, citations, *, table_scope=None):
    """Literal inventory, not a semantic material record or cross-block join.

    Each check selects one block in one header/data pair from one catalog
    document. Requested left/right sides take priority over a weave hit on the
    other side. Without a side, only a block mentioning a recognized explicit
    condition is selected. Missing values remain missing in that block.
    """
    conditions=selection_conditions(question)
    side=table_scope.get('side') if table_scope else _requested_side(question)
    checks=[];seen=set()
    for heading_citation in citations:
        group=heading_citation.get('parallel_table_groups')
        if not group:
            continue
        header=_complete_row(heading_citation,group['header_start'],group['header_end'])
        if not header:
            continue
        fragment=heading_citation['text'][group['header_start']-heading_citation['start']:
                                          group['header_end']-heading_citation['start']]
        actual=parallel_column_groups(fragment)
        if not actual or actual['groups']!=group['groups']:
            continue
        doc_id=heading_citation['doc_id']
        candidates=[]
        for citation in citations:
            if citation['doc_id']!=doc_id:
                continue
            for role in citation.get('table_row_roles',[]):
                if role['role']!='data_candidate' or role['start']<=group['header_end']:
                    continue
                row=_complete_row(citation,role['start'],role['end'])
                if not row or row['row']<=header['row']:
                    continue
                bounds=group['groups']
                indices=([0] if side=='left' else [len(bounds)-1] if side=='right' else range(len(bounds)))
                if table_scope and 'first_column' in table_scope:
                    indices=[i for i,b in enumerate(bounds) if b['first_column']==table_scope['first_column']
                             and b['last_column']==table_scope['last_column']]
                for index in indices:
                    block=bounds[index]
                    cells=[c for c in row['cells'] if block['first_column']<=c['column']<=block['last_column']]
                    body='\n'.join(c['text'] for c in cells)
                    hits=[dict(c,mentioned_in_block=condition_in_text(c,body)) for c in conditions]
                    count=sum(c['mentioned_in_block'] for c in hits)
                    if not side and not count and not (table_scope and 'first_column' in table_scope):
                        continue
                    candidates.append((count,-role['start'],-index,row,role,citation,block,cells,hits))
        if not candidates:
            continue
        _,_,_,row,role,citation,block,cells,hits=max(candidates,key=lambda v:v[:3])
        key=(doc_id,group['header_start'],role['start'],block['first_column'])
        if key in seen:
            continue
        seen.add(key)
        headings={c['column']:c for c in header['cells']
                  if block['first_column']<=c['column']<=block['last_column']}
        values={c['column']:c for c in cells}
        columns=[]
        for column in sorted(headings.keys()|values.keys()):
            columns.append({'column':column,
                'heading':_cell_reference(headings[column],group['header_start'],heading_citation['excerpt_id']) if column in headings else None,
                'value':_cell_reference(values[column],role['start'],citation['excerpt_id']) if column in values else None})
        fields=[]
        for kind in FIELD_LABELS:
            located=[c for c in columns if c['heading'] and _header_kind(c['heading']['text'])==kind]
            complete=[c for c in located if c['value']]
            unit_refs=[]
            for c in located:
                for part in ('heading','value'):
                    if c[part] and kind in _UNITS and re.search(_UNITS[kind],c[part]['text'],re.I):
                        unit_refs.append(c[part])
            status=('heading_not_located_in_returned_block' if not located else
                    'ambiguous_headings_require_review' if len(located)>1 else
                    'value_not_present_in_returned_row' if not complete else 'visible_text_requires_review')
            fields.append({'field':kind,'status':status,'columns':[c['column'] for c in located],
                           'unit_status':('not_applicable' if kind not in _UNITS else
                                          'literal_unit_requires_review' if unit_refs else 'unit_not_located_in_returned_block'),
                           'unit_evidence':unit_refs,'engineering_conclusion_supported':False})
        checks.append({'doc_id':doc_id,'basis':'complete_returned_header_and_row_within_single_parallel_block',
                       'header_row':header['row'],'data_row':row['row'],
                       'first_column':block['first_column'],'last_column':block['last_column'],
                       'selection_basis':('requested_'+side+'_block' if side else
                                          'inherited_column_block' if table_scope and 'first_column' in table_scope else
                                          'most_literal_condition_mentions_within_one_block'),
                       'condition_mentions':hits,'columns':columns,'fields':fields,
                       'version_identity_verified':False,'column_semantics_verified':False,
                       'engineering_conclusion_supported':False,
                       'citation_ids':list(dict.fromkeys([heading_citation['excerpt_id'],citation['excerpt_id']]))})
    return {'basis':'returned_quotes_only_one_block_per_source_header',
            'scope':'visible_header_and_single_returned_data_row_not_full_sheet',
            'checks':checks,'engineering_conclusion_supported':False}


def block_check_lines(result):
    lines=[]
    for check in result['checks']:
        refs=', '.join(check['citation_ids'])
        lines.append('单一区块核对：原表头行{}／命中行{}，仅列{}–{}，版本身份与列含义待核对。 [{}]'.format(
            check['header_row'],check['data_row'],check['first_column'],check['last_column'],refs))
        visible=[];missing=[]
        for field in check['fields']:
            label=FIELD_LABELS[field['field']]
            if field['status']=='visible_text_requires_review':
                visible.append(label+'（列{}，文字待核对）'.format(field['columns'][0]))
            else:
                missing.append(label+'（'+('未定位表头' if not field['columns'] else '对应待核对')+'）')
            if field['field'] in ('rc','thickness') and field['unit_status']=='unit_not_located_in_returned_block':
                missing.append(('RC 百分比单位' if field['field']=='rc' else '介质厚度单位')+'（未定位）')
        if visible:
            lines.append('该区块可见：'+'、'.join(visible)+'。 [{}]'.format(refs))
        lines.append('该区块待补证：'+'、'.join(['版本身份',*missing])+'；缺口仅针对返回的表头与该行，不代表整份附件不存在这些信息。 [{}]'.format(refs))
        missing_conditions=[c['label']+'“'+c['value']+'”' for c in check['condition_mentions'] if not c['mentioned_in_block']]
        if missing_conditions:
            lines.append('该区块当前行未提及：'+'、'.join(missing_conditions)+'；不借用另一并列区块的值。 [{}]'.format(refs))
    return lines
