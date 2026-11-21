"""Evidence requests describe unresolved scoped fields, never verified facts."""
import hashlib
import json
import re

from pcb_assistant.tables.checklist import FIELD_LABELS

_REQUEST=re.compile(r'补证|補證|补哪些|補哪些|需要什么资料|需要什麼資料|还缺什么|還缺什麼')
_DEFINITIONS={
    'rc':('对应材料牌号的树脂含量定义及测试方法，说明是否为质量百分比、标称值或批次实测值',
          '定义、测试法及数值类型与该材料行相符；材料代码中的 RC 数字不能代替检验依据'),
    'thickness':('该材料／组合的厚度定义与单位，说明压前或压后、介质或含铜总厚度',
                 '厚度口径、单位和材料组合一致；若作为压后选型依据，另需对应组合的试压或切片记录'),
    'copper':('该铜层的铜厚定义与单位，说明基铜或成品铜厚及测量依据',
              '层别、厚度口径、单位及测量／标称属性与该行一致'),
    'coverage':('该层残铜率／铜覆盖率的定义、百分比单位、计算区域和数据来源',
                '层别、区域及计算口径一致，不能以另一层或整体平均值替代'),
    'glass':('该材料规格中的玻纤布型定义与布型×张数记法说明',
             '布型及张数分别确认，不能仅凭布型推断树脂含量或压后厚度'),
    'grade':('该行材料名称、完整牌号及芯板／PP 角色的对应依据',
             '完整牌号、材料角色与原表该行一致，不能用类似代码或其他版本替代'),
}


def is_evidence_request(question):
    return bool(_REQUEST.search(question))


def field_evidence_requests(field_checks):
    """Create actionable requests from observed statuses in current returned quotes.

    Common version/material requests are grouped only within the same document,
    header row, data row and block. Evidence is not requested for a nonexistent
    row, and future documents are never named as if already located or verified.
    """
    groups={}
    for check in field_checks['checks']:
        key=tuple(check[k] for k in ('doc_id','header_row','data_row','first_column','last_column'))
        groups.setdefault(key,[]).append(check)
    requests=[]
    for key,checks in groups.items():
        scope=dict(zip(('doc_id','header_row','data_row','first_column','last_column'),key))
        refs=list(dict.fromkeys(id for c in checks for id in c['citation_ids']))
        fields=list(dict.fromkeys(c['field'] for c in checks))
        def add(code,needed,acceptance,check=None):
            affected=[check['field']] if check else fields
            identity=json.dumps([*key,code,affected],ensure_ascii=False,separators=(',',':'))
            requests.append({
                'request_id':'EVD_'+hashlib.sha256(identity.encode()).hexdigest()[:24],
                'scope':dict(scope),'fields':affected,'reason_code':code,
                'observed_status':check['status'] if check else 'identity_not_verified',
                'observed_unit_status':check['unit_status'] if check else 'not_applicable',
                'columns':list(check['columns']) if check else [],
                'evidence_needed':needed,'review_criteria':acceptance,
                'basis_citation_ids':list(refs),'basis':'current_returned_field_status_not_future_document_evidence',
                'status':'evidence_requested_not_verified','evidence_received':False,
                'engineering_conclusion_supported':False})
        add('version_identity',
            '该来源叠构表的受控文件编号、修订版次／日期，以及所选区块属于该版次的说明',
            '文件编号、版次和区块身份可与该来源及原表行逐项对应；Catalog 来源版本不能代替工程文件版次')
        add('material_identity',
            '所选行的完整材料牌号、芯板／PP 角色及对应材料资料的文件编号和修订版次',
            '材料资料明确适用于该行牌号及角色；供应商、牌号或文件版次缺失时继续保留待核对')
        for c in checks:
            label=FIELD_LABELS[c['field']]
            if c['status']=='heading_not_located_in_returned_block':
                add('field_heading',
                    label+'的完整原表表头、列号与该行对应位置；如有合并／多层表头，需同时给出覆盖范围',
                    '能在同一版次、同一区块内唯一定位字段及该行；不能给未命名的数字列直接指定含义',c)
            elif c['status']=='ambiguous_headings_require_review':
                add('ambiguous_heading',
                    label+'的重复表头释义、各列用途及合并／分组范围说明',
                    '各候选列的含义可分别确认；不能只选择一个数值来消除歧义',c)
            elif c['status']=='value_not_present_in_returned_row':
                add('row_value',
                    label+'在该版次、该区块和该行的实际记录及其表头；若原表为空，需明确为空',
                    '值有该行记录支持；其他行／区块的值不能补作该行数据，空值继续保留缺口',c)
            needed,criteria=_DEFINITIONS[c['field']]
            add('field_definition',needed,criteria,c)
    return {'basis':'unverified_returned_field_statuses','scope':'one_source_row_and_block_per_request',
            'requests':requests,'engineering_conclusion_supported':False,
            'external_messages_sent':False,'auto_verification':False}


def evidence_request_lines(result):
    if not result['requests']:
        return []
    lines=['补证清单（以下为待取得资料及核对条件，尚未收到或验证）：']
    for item in result['requests']:
        fields='／'.join(FIELD_LABELS[k] for k in item['fields'])
        columns='，候选列'+','.join(map(str,item['columns'])) if item['columns'] else ''
        scope=item['scope']
        lines.append('- {}（原表行{}、区块列{}–{}{}）：需提供{}。核对条件：{}。当前缺口依据 [{}]'.format(
            fields,scope['data_row'],scope['first_column'],scope['last_column'],columns,
            item['evidence_needed'],item['review_criteria'],', '.join(item['basis_citation_ids'])))
    return lines
