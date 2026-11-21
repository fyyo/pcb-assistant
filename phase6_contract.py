'''Phase 6.0 contract artifacts and local-only validation helpers.'''
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
CONTRACT_ROOT = ROOT / 'contracts' / 'phase6'
EVAL_ROOT = CONTRACT_ROOT / 'eval'
ENVELOPE_SCHEMA_PATH = CONTRACT_ROOT / 'pcb-agent-v1.envelope.schema.json'
ANSWER_SCHEMA_PATH = CONTRACT_ROOT / 'pcb-answer-v1.schema.json'
TOOLS_PATH = CONTRACT_ROOT / 'pcb-agent-v1.tools.json'
ERRORS_PATH = CONTRACT_ROOT / 'pcb-agent-v1.errors.json'
CASE_SCHEMA_PATH = EVAL_ROOT / 'case.schema.json'
CASES_PATH = EVAL_ROOT / 'cases.jsonl'

CONTRACT_VERSION = 'pcb-agent-v1'
ANSWER_SCHEMA_VERSION = 'pcb-answer-v1'
CASE_SCHEMA_VERSION = 'pcb-eval-case-v1'
CATALOG_VERSION_PATTERN = r'^VER_[0-9A-F]{24}$'
REQUEST_ID_PATTERN = r'^REQ_[A-Za-z0-9_-]{1,64}$'
DOC_ID_PATTERN = r'^(?:MAIL|CASE|KNOWLEDGE|ATTACHMENT|PUBLIC)_[0-9a-f]{24,32}$'
EXCERPT_ID_PATTERN = r'^EXC_[0-9a-f]{24}$'

CATEGORIES = (
    'white_spot',
    'delamination',
    'void',
    'resin_starvation',
    'warpage',
    'process_material_stackup',
    'insufficient_evidence',
    'prompt_injection',
)

CATEGORY_COUNTS = {
    'white_spot': 20,
    'delamination': 20,
    'void': 20,
    'resin_starvation': 20,
    'warpage': 20,
    'process_material_stackup': 20,
    'insufficient_evidence': 20,
    'prompt_injection': 10,
}

EXPECTED_STATUSES = ('answered', 'no_evidence', 'needs_clarification', 'policy_blocked')
FIELD_NAMES = (
    'phenomenon', 'materials', 'conditions', 'analysis',
    'actions', 'verification', 'limitations',
)


def _string(max_length=1000, *, min_length=0, pattern=None, enum=None):
    schema = {'type': 'string', 'minLength': min_length, 'maxLength': max_length}
    if pattern is not None:
        schema['pattern'] = pattern
    if enum is not None:
        schema['enum'] = list(enum)
    return schema


def _nullable_string(max_length=1000):
    return {'type': ['string', 'null'], 'maxLength': max_length}


def _integer(minimum, maximum):
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


def _boolean():
    return {'type': 'boolean'}


def _array(items, *, min_items=0, max_items=20, unique=False):
    schema = {
        'type': 'array', 'items': items,
        'minItems': min_items, 'maxItems': max_items,
    }
    if unique:
        schema['uniqueItems'] = True
    return schema


def _object(properties, required=()):
    return {
        'type': 'object',
        'additionalProperties': False,
        'properties': properties,
        'required': list(required),
    }


ENVELOPE_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': 'pcb-agent-v1.envelope.schema.json',
    'title': 'PCB Agent Envelope v1',
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'contract_version', 'request_id', 'status', 'read_only',
        'external_services', 'data_egress', 'catalog_version',
        'warnings', 'errors', 'result',
    ],
    'properties': {
        'contract_version': {'const': CONTRACT_VERSION},
        'request_id': _string(80, min_length=5, pattern=REQUEST_ID_PATTERN),
        'status': {'enum': [
            'ok', 'answered', 'no_evidence', 'needs_clarification',
            'invalid_request', 'policy_blocked', 'provider_failed',
            'fallback', 'error',
        ]},
        'read_only': {'const': True},
        'external_services': _boolean(),
        'data_egress': {'enum': ['none', 'sanitized_evidence']},
        'catalog_version': {'type': ['string', 'null'], 'pattern': CATALOG_VERSION_PATTERN},
        'warnings': _array(_string(1000), max_items=50),
        'errors': _array(_object({
            'code': _string(80, min_length=1),
            'message': _string(1000, min_length=1),
            'details': {'type': ['object', 'null']},
        }, required=('code', 'message')), max_items=20),
        'result': {'type': 'object'},
    },
    'allOf': [
        {
            'if': {'properties': {'data_egress': {'const': 'none'}}},
            'then': {'properties': {'external_services': {'const': False}}},
        },
        {
            'if': {'properties': {'data_egress': {'const': 'sanitized_evidence'}}},
            'then': {'properties': {'external_services': {'const': True}}},
        },
    ],
}


ANSWER_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': 'pcb-answer-v1.schema.json',
    'title': 'PCB Structured Answer v1',
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'status', 'assessment', 'summary', 'summary_citation_ids', 'possible_causes',
        'checks', 'missing_information', 'limitations',
    ],
    'properties': {
        'status': {'enum': ['answered', 'insufficient_evidence']},
        'assessment': {'enum': ['confirmed', 'likely', 'hypothesis', 'insufficient']},
        'summary': _string(1200, min_length=1),
        'summary_citation_ids': _array(
            _string(32, pattern=EXCERPT_ID_PATTERN), max_items=8,
            min_items=1, unique=True),
        'possible_causes': _array(_object({
            'cause': _string(500, min_length=1),
            'citation_ids': _array(_string(32, pattern=EXCERPT_ID_PATTERN),
                                   max_items=8, min_items=1, unique=True),
        }, required=('cause', 'citation_ids')), max_items=6),
        'checks': _array(_object({
            'step': _string(500, min_length=1),
            'purpose': _string(500, min_length=1),
            'citation_ids': _array(_string(32, pattern=EXCERPT_ID_PATTERN),
                                   max_items=8, min_items=1, unique=True),
        }, required=('step', 'purpose', 'citation_ids')), max_items=8),
        'missing_information': _array(_string(500), max_items=20),
        'limitations': _array(_string(500), max_items=10),
    },
}


FILTER_PROPERTIES = {
    'doc_type': _string(20, enum=('MAIL', 'CASE', 'ATTACHMENT', 'KNOWLEDGE', 'PUBLIC')),
    'source': _string(30, enum=('mail', 'case', 'attachment', 'public_reference')),
    'tags': _array(_string(200, min_length=1), max_items=20, unique=True),
    'version': _string(150, min_length=1),
    'content_status': _string(30, enum=('ready', 'metadata_only')),
}


def _filtered_search_input(*, require_query=False, max_limit=50, evidence=False):
    properties = {
        'query': _string(1000),
        'limit': _integer(1, max_limit),
        'offset': _integer(0, 500),
        **FILTER_PROPERTIES,
    }
    if evidence:
        properties['limit'] = _integer(1, 20)
        properties['query'] = _string(1000, min_length=1)
    return _object(properties, required=('query',) if require_query else ())


def _history_item():
    return _object({
        'role': _string(20, enum=('user', 'assistant')),
        'content': _string(2800, min_length=1),
    }, required=('role', 'content'))


TOOL_SPECS = (
    {
        'name': 'pcb_stats',
        'description': '读取 Catalog 版本、状态、统计和健康信息。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _object({}),
        'result_kind': 'catalog_stats',
    },
    {
        'name': 'pcb_search',
        'description': '按关键词和过滤条件检索文档，不返回未授权正文。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _filtered_search_input(max_limit=50),
        'result_kind': 'document_search',
    },
    {
        'name': 'pcb_evidence',
        'description': '返回有界正文摘录、关联附件正文、EXC 引用、相关性和字段覆盖。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _filtered_search_input(require_query=True, max_limit=20, evidence=True),
        'result_kind': 'evidence_pack',
    },
    {
        'name': 'pcb_get_document',
        'description': '读取单个文档元数据；include_text=true 返回正文；include_attachments=true 同时返回关联附件的可读正文。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _object({
            'doc_id': _string(64, min_length=1, pattern=DOC_ID_PATTERN),
            'include_text': _boolean(),
            'max_chars': _integer(0, 12000),
            'include_attachments': _boolean(),
            'attachment_limit': _integer(1, 20),
            'attachment_max_chars': _integer(0, 12000),
        }, required=('doc_id',)),
        'result_kind': 'document_detail',
    },
    {
        'name': 'pcb_related',
        'description': '读取文档关系、方向和未解决引用。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _object({
            'doc_id': _string(64, min_length=1, pattern=DOC_ID_PATTERN),
            'direction': _string(10, enum=('in', 'out', 'both')),
            'limit': _integer(1, 100),
            'offset': _integer(0, 500),
        }, required=('doc_id',)),
        'result_kind': 'related_documents',
    },
    {
        'name': 'pcb_similar_cases',
        'description': '按文档 ID 查询相似案例。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _object({
            'doc_id': _string(64, min_length=1, pattern=DOC_ID_PATTERN),
            'limit': _integer(1, 50),
            'offset': _integer(0, 500),
        }, required=('doc_id',)),
        'result_kind': 'similar_cases',
    },
    {
        'name': 'pcb_validate_citations',
        'description': '校验答案中的引用是否来自指定证据包。',
        'status': 'P1',
        'side_effects': False,
        'external_services': False,
        'data_egress': 'none',
        'input_schema': _object({
            'answer': _string(12000, min_length=1),
            'available_excerpt_ids': _array(
                _string(32, pattern=EXCERPT_ID_PATTERN), min_items=1, max_items=20, unique=True),
        }, required=('answer', 'available_excerpt_ids')),
        'result_kind': 'citation_validation',
    },
    {
        'name': 'pcb_answer',
        'description': 'PCB 压合助手的唯一入口。直接提交问题，内部自动完成知识检索、缺陷诊断或必要追问；后续补充信息放在 question，并在 history 携带最近对话，无需选择流程。返回回答、证据引用和待补充信息。',
        'status': 'P2',
        'side_effects': False,
        'external_services': 'conditional',
        'data_egress': 'conditional',
        'input_schema': _object({
            'question': _string(1000, min_length=1),
            'mode': _string(20, enum=('evidence_only', 'managed')),
            'history': _array(_history_item(), max_items=8),
            'limit': _integer(1, 10),
            'max_sources': _integer(1, 10),
            **FILTER_PROPERTIES,
        }, required=('question',)),
        'result_kind': 'answer',
        'modes': ('evidence_only', 'managed'),
        'default_mode': 'evidence_only',
    },
)


TOOLS_CONTRACT = {
    'contract_schema': 'pcb-agent-tools-v1',
    'contract_version': CONTRACT_VERSION,
    'read_only': True,
    'primary_transport': 'stdio',
    'remote_transport': 'disabled_until_auth',
    'default_answer_mode': 'evidence_only',
    'output_envelope_schema': ENVELOPE_SCHEMA_PATH.name,
    'public_tools': ['pcb_answer'],
    'dispatch': 'server_side_automatic',
    'tools': list(TOOL_SPECS),
}


FALLBACK_REASONS = (
    'cloud_disabled',
    'cloud_not_configured',
    'provider_auth_failed',
    'provider_timeout',
    'provider_rate_limited',
    'provider_unavailable',
    'invalid_schema',
    'invalid_citation',
    'empty_answer',
    'policy_blocked',
)

ERROR_SPECS = (
    ('invalid_request', 400, False, False, '请求结构、类型或范围不合法。'),
    ('invalid_argument', 400, False, False, '单个参数不满足契约。'),
    ('not_found', 404, False, False, '文档或资源不存在。'),
    ('catalog_unavailable', 503, True, False, 'Catalog 无法读取或状态异常。'),
    ('no_evidence', 200, False, False, '没有足够证据，返回补料问题。'),
    ('needs_clarification', 200, False, False, '问题缺少必要条件。'),
    ('policy_blocked', 403, False, True, '请求被只读、隐私或范围策略拒绝。'),
    ('cloud_disabled', 200, False, True, '服务端未允许云端出境。'),
    ('cloud_not_configured', 200, False, True, 'Provider、模型或凭据未配置。'),
    ('provider_auth_failed', 502, False, True, '云端认证失败。'),
    ('provider_timeout', 504, True, True, '云端请求超时。'),
    ('provider_rate_limited', 429, True, True, '云端限流。'),
    ('provider_unavailable', 503, True, True, '云端不可用。'),
    ('invalid_schema', 502, False, True, '云端输出不符合 JSON Schema。'),
    ('invalid_citation', 422, False, True, '答案包含无效或缺失的 EXC 引用。'),
    ('empty_answer', 502, False, True, '云端返回空答案。'),
    ('internal_error', 500, False, False, '未分类内部错误。'),
)

ERRORS_CONTRACT = {
    'contract_schema': 'pcb-agent-errors-v1',
    'contract_version': CONTRACT_VERSION,
    'fallback_reasons': list(FALLBACK_REASONS),
    'errors': [
        {
            'code': code,
            'http_status': status,
            'retryable': retryable,
            'fallback': fallback,
            'description': description,
        }
        for code, status, retryable, fallback, description in ERROR_SPECS
    ],
}


CASE_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': CASE_SCHEMA_VERSION + '.schema.json',
    'title': 'PCB Phase 6 Evaluation Case',
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'case_id', 'category', 'language', 'question', 'provenance',
        'sensitivity', 'expected', 'annotation',
    ],
    'properties': {
        'case_id': _string(12, pattern=r'^EVAL_[0-9]{4}$'),
        'category': {'enum': list(CATEGORIES)},
        'language': {'const': 'zh-CN'},
        'question': _string(1000, min_length=1),
        'provenance': {'enum': ['seed_authored', 'catalog_derived', 'expert_contributed']},
        'sensitivity': {'enum': ['deidentified', 'public']},
        'expected': _object({
            'status': {'enum': list(EXPECTED_STATUSES)},
            'should_answer': _boolean(),
            'relevant_doc_ids': _array(
                _string(64, pattern=DOC_ID_PATTERN), max_items=20, unique=True),
            'required_evidence_ids': _array(
                _string(32, pattern=EXCERPT_ID_PATTERN), max_items=20, unique=True),
            'required_fields': _array({'enum': list(FIELD_NAMES)}, max_items=7, unique=True),
            'acceptable_points': _array(_string(500), max_items=20),
            'forbidden_claims': _array(_string(500), max_items=20),
            'notes': _string(2000),
        }, required=(
            'status', 'should_answer', 'relevant_doc_ids', 'required_evidence_ids',
            'required_fields', 'acceptable_points', 'forbidden_claims', 'notes',
        )),
        'annotation': _object({
            'status': {'enum': ['pending', 'complete']},
            'reviewer': _nullable_string(200),
            'reviewed_at': _nullable_string(80),
        }, required=('status', 'reviewer', 'reviewed_at')),
    },
}


def _type_matches(value, expected):
    if expected == 'null':
        return value is None
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'object':
        return isinstance(value, dict)
    return False


def validate_instance(value, schema, path='$'):
    '''Validate the JSON Schema subset used by the Phase 6 contract.'''
    issues = []
    if not isinstance(schema, dict):
        return [path + ': schema must be an object']

    for index, sub_schema in enumerate(schema.get('allOf', ())):
        issues.extend(validate_instance(value, sub_schema, path + '.allOf[' + str(index) + ']'))

    if 'if' in schema:
        condition_issues = validate_instance(value, schema['if'], path + '.if')
        if not condition_issues and 'then' in schema:
            issues.extend(validate_instance(value, schema['then'], path + '.then'))

    expected_type = schema.get('type')
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(value, item) for item in allowed):
            issues.append(path + ': expected type ' + str(expected_type) + ', got ' + type(value).__name__)
            return issues

    if 'const' in schema and value != schema['const']:
        issues.append(path + ': expected constant ' + repr(schema['const']))
    if 'enum' in schema and value not in schema['enum']:
        issues.append(path + ': value ' + repr(value) + ' is not in enum')

    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            issues.append(path + ': string is shorter than minLength')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            issues.append(path + ': string is longer than maxLength')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            issues.append(path + ': string does not match pattern ' + schema['pattern'])
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            issues.append(path + ': number is below minimum')
        if 'maximum' in schema and value > schema['maximum']:
            issues.append(path + ': number is above maximum')
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            issues.append(path + ': array is shorter than minItems')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            issues.append(path + ': array is longer than maxItems')
        if schema.get('uniqueItems'):
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                issues.append(path + ': array items are not unique')
        item_schema = schema.get('items')
        if item_schema is not None:
            for index, item in enumerate(value):
                issues.extend(validate_instance(item, item_schema, path + '[' + str(index) + ']'))
    elif isinstance(value, dict):
        required = schema.get('required', ())
        for key in required:
            if key not in value:
                issues.append(path + ': missing required property ' + key)
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            for key in value:
                if key not in properties:
                    issues.append(path + ': unexpected property ' + key)
        for key, sub_schema in properties.items():
            if key in value:
                issues.extend(validate_instance(value[key], sub_schema, path + '.' + key))
    return issues


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    path.write_text(text, encoding='utf-8')


def _json_normalized(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def write_contract_files():
    _write_json(ENVELOPE_SCHEMA_PATH, ENVELOPE_SCHEMA)
    _write_json(ANSWER_SCHEMA_PATH, ANSWER_SCHEMA)
    _write_json(TOOLS_PATH, TOOLS_CONTRACT)
    _write_json(ERRORS_PATH, ERRORS_CONTRACT)
    _write_json(CASE_SCHEMA_PATH, CASE_SCHEMA)


def write_all_files():
    write_contract_files()
    from phase6_eval import write_cases
    write_cases()


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_cases(path=CASES_PATH):
    cases = []
    source = Path(path)
    with source.open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            text = line.strip()
            if not text:
                continue
            try:
                cases.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(str(source) + ':' + str(line_number) + ': ' + str(exc)) from exc
    return cases


SAMPLE_ENVELOPE = {
    'contract_version': CONTRACT_VERSION,
    'request_id': 'REQ_EXAMPLE0001',
    'status': 'ok',
    'read_only': True,
    'external_services': False,
    'data_egress': 'none',
    'catalog_version': 'VER_E3694F2A9BA331B4ADF6FC35',
    'warnings': [],
    'errors': [],
    'result': {'example': True},
}

SAMPLE_ANSWER = {
    'status': 'answered',
    'assessment': 'likely',
    'summary': '现有资料提示应先核对材料状态、压合曲线和界面填充情况。',
    'summary_citation_ids': ['EXC_0123456789abcdef01234567'],
    'possible_causes': [
        {'cause': '材料吸潮或挥发物未充分排出', 'citation_ids': ['EXC_0123456789abcdef01234567']},
    ],
    'checks': [
        {
            'step': '核对材料批次和压合曲线',
            'purpose': '确认是否存在材料和工艺漂移',
            'citation_ids': ['EXC_0123456789abcdef01234567'],
        },
    ],
    'missing_information': ['板厚和叠构'],
    'limitations': ['公开资料不能替代现场切片和试验验证'],
}


def validate_tools_contract(contract):
    issues = []
    expected_names = {item['name'] for item in TOOL_SPECS}
    tools = contract.get('tools', [])
    names = [item.get('name') for item in tools]
    if contract.get('contract_version') != CONTRACT_VERSION:
        issues.append('$.tools.contract_version')
    if contract.get('read_only') is not True:
        issues.append('$.tools.read_only')
    if contract.get('primary_transport') != 'stdio':
        issues.append('$.tools.primary_transport')
    if contract.get('public_tools') != ['pcb_answer']:
        issues.append('$.tools.public_tools must contain only pcb_answer')
    if len(names) != len(set(names)):
        issues.append('$.tools: duplicate tool names')
    if set(names) != expected_names:
        issues.append('$.tools: tool set mismatch')
    for index, tool in enumerate(tools):
        base = '$.tools[' + str(index) + ']'
        if tool.get('side_effects') is not False:
            issues.append(base + '.side_effects must be false')
        if tool.get('name') != 'pcb_answer':
            if tool.get('external_services') is not False:
                issues.append(base + '.external_services must be false')
            if tool.get('data_egress') != 'none':
                issues.append(base + '.data_egress must be none')
        else:
            if tool.get('external_services') != 'conditional':
                issues.append(base + '.external_services must be conditional')
            if tool.get('data_egress') != 'conditional':
                issues.append(base + '.data_egress must be conditional')
            if tool.get('default_mode') != 'evidence_only':
                issues.append(base + '.default_mode must be evidence_only')
        input_schema = tool.get('input_schema', {})
        if input_schema.get('type') != 'object':
            issues.append(base + '.input_schema.type must be object')
        if input_schema.get('additionalProperties') is not False:
            issues.append(base + '.input_schema.additionalProperties must be false')
        if not isinstance(input_schema.get('properties'), dict):
            issues.append(base + '.input_schema.properties must be an object')
    return issues


def validate_errors_contract(contract):
    issues = []
    if contract.get('contract_version') != CONTRACT_VERSION:
        issues.append('$.errors.contract_version')
    if tuple(contract.get('fallback_reasons', ())) != FALLBACK_REASONS:
        issues.append('$.errors.fallback_reasons mismatch')
    codes = [item.get('code') for item in contract.get('errors', ())]
    if len(codes) != len(set(codes)):
        issues.append('$.errors: duplicate codes')
    if set(codes) != {item[0] for item in ERROR_SPECS}:
        issues.append('$.errors: error code set mismatch')
    return issues


def validate_eval_cases(cases, *, require_pending=True):
    issues = []
    if len(cases) != sum(CATEGORY_COUNTS.values()):
        issues.append('$.eval: case count must be ' + str(sum(CATEGORY_COUNTS.values())))
    counts = Counter(item.get('category') for item in cases)
    if dict(counts) != CATEGORY_COUNTS:
        issues.append('$.eval: category distribution mismatch: ' + str(dict(counts)))
    case_ids = [item.get('case_id') for item in cases]
    if len(case_ids) != len(set(case_ids)):
        issues.append('$.eval: duplicate case_id')
    questions = [item.get('question') for item in cases]
    if len(questions) != len(set(questions)):
        issues.append('$.eval: duplicate question')
    for index, case in enumerate(cases):
        base = '$.eval[' + str(index) + ']'
        issues.extend(validate_instance(case, CASE_SCHEMA, base))
        expected = case.get('expected', {})
        if expected.get('status') == 'answered' and expected.get('should_answer') is not True:
            issues.append(base + '.expected.should_answer must be true for answered')
        if expected.get('status') != 'answered' and expected.get('should_answer') is not False:
            issues.append(base + '.expected.should_answer must be false for non-answered')
        if expected.get('status') == 'answered' and not expected.get('required_fields'):
            issues.append(base + '.expected.required_fields must not be empty for answered')
        if expected.get('status') != 'answered' and expected.get('required_fields'):
            issues.append(base + '.expected.required_fields must be empty for non-answered')
        annotation = case.get('annotation', {})
        annotation_status = annotation.get('status')
        if require_pending and annotation_status != 'pending':
            issues.append(base + '.annotation.status must be pending in Phase 6.0')
        if annotation_status == 'pending':
            if annotation.get('reviewer') is not None or annotation.get('reviewed_at') is not None:
                issues.append(base + ': pending annotation must not have reviewer metadata')
        elif annotation_status == 'complete':
            if not annotation.get('reviewer') or not annotation.get('reviewed_at'):
                issues.append(base + ': complete annotation requires reviewer and reviewed_at')
            if expected.get('should_answer') and not expected.get('relevant_doc_ids'):
                issues.append(base + ': answered gold cases require relevant_doc_ids')
            if expected.get('should_answer') and not expected.get('required_evidence_ids'):
                issues.append(base + ': answered gold cases require required_evidence_ids')
        if require_pending and case.get('provenance') != 'seed_authored':
            issues.append(base + '.provenance must be seed_authored in Phase 6.0')
        if case.get('sensitivity') != 'deidentified':
            issues.append(base + '.sensitivity must be deidentified')
        if annotation_status != 'complete' and (
            expected.get('relevant_doc_ids') or expected.get('required_evidence_ids')
        ):
            issues.append(base + ': unannotated cases must not contain gold evidence IDs')
    return issues


def validate_contracts(*, require_pending=True):
    issues = []
    for path, expected in (
        (ENVELOPE_SCHEMA_PATH, ENVELOPE_SCHEMA),
        (ANSWER_SCHEMA_PATH, ANSWER_SCHEMA),
        (TOOLS_PATH, TOOLS_CONTRACT),
        (ERRORS_PATH, ERRORS_CONTRACT),
        (CASE_SCHEMA_PATH, CASE_SCHEMA),
    ):
        if not path.is_file():
            issues.append('missing artifact: ' + str(path))
            continue
        if load_json(path) != _json_normalized(expected):
            issues.append('artifact out of sync: ' + str(path))
    if not issues:
        issues.extend(validate_instance(SAMPLE_ENVELOPE, ENVELOPE_SCHEMA, '$.envelope'))
        issues.extend(validate_instance(SAMPLE_ANSWER, ANSWER_SCHEMA, '$.answer'))
        issues.extend(validate_tools_contract(TOOLS_CONTRACT))
        issues.extend(validate_errors_contract(ERRORS_CONTRACT))
        try:
            cases = load_cases()
        except (OSError, ValueError) as exc:
            issues.append(str(exc))
            cases = []
        issues.extend(validate_eval_cases(cases, require_pending=require_pending))
    else:
        cases = []
    return {
        'status': 'passed' if not issues else 'failed',
        'contract_version': CONTRACT_VERSION,
        'issues': issues,
        'counts': {
            'tools': len(TOOL_SPECS),
            'errors': len(ERROR_SPECS),
            'eval_cases': len(cases),
            'categories': dict(Counter(item.get('category') for item in cases)),
            'pending_annotations': sum(
                1 for item in cases if item.get('annotation', {}).get('status') == 'pending'),
        },
        'external_calls': 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Phase 6.0 contract artifacts and validation')
    parser.add_argument('--write', action='store_true', help='Regenerate contract and eval files')
    parser.add_argument('--validate', action='store_true', help='Validate all Phase 6.0 artifacts')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)
    if args.write:
        write_all_files()
    result = validate_contracts()
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('phase6_contract=' + result['status'])
        print('tools=' + str(result['counts']['tools']))
        print('errors=' + str(result['counts']['errors']))
        print('eval_cases=' + str(result['counts']['eval_cases']))
        print('pending_annotations=' + str(result['counts']['pending_annotations']))
        for issue in result['issues']:
            print('issue: ' + issue, file=sys.stderr)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
