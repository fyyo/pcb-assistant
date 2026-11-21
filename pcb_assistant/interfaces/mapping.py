'''Static mapping from Phase 6 MCP tools to existing project functions.'''
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from pcb_assistant.interfaces.contract import CONTRACT_ROOT, CONTRACT_VERSION, TOOL_SPECS


MAPPING_PATH = CONTRACT_ROOT / 'tool_mapping.json'

TOOL_MAPPING = {
    'contract_version': CONTRACT_VERSION,
    'phase': '6.2_managed_answer',
    'read_only': True,
    'mapping_status': 'static_reviewed',
    'transport': 'stdio',
    'tools': [
        {
            'name': 'pcb_stats',
            'primary': {'module': 'pcb_assistant.catalog.cli', 'symbol': 'status'},
            'secondary': [
                {'module': 'pcb_assistant.catalog.cli', 'symbol': 'statistics'},
            ],
            'notes': ['只读 Catalog 状态与统计。'],
        },
        {
            'name': 'pcb_search',
            'primary': {'module': 'pcb_assistant.catalog.cli', 'symbol': 'search'},
            'secondary': [],
            'notes': ['MCP 包装必须移除 source_path 等本地路径字段。'],
        },
        {
            'name': 'pcb_evidence',
            'primary': {'module': 'pcb_assistant.catalog.evidence', 'symbol': 'evidence'},
            'secondary': [],
            'notes': [
                'MCP 包装必须移除 source_path',
                '只返回有界摘要和 EXC 引用，不返回完整正文。',
            ],
        },
        {
            'name': 'pcb_get_document',
            'primary': {'module': 'pcb_assistant.catalog.cli', 'symbol': 'document'},
            'secondary': [
                {'module': 'pcb_assistant.catalog.cli', 'symbol': 'document_records'},
            ],
            'notes': [
                '默认只返回元数据',
                'include_text=false 时移除 original_text 和 normalized_text',
                'include_text=true 时按 max_chars 截断',
            ],
        },
        {
            'name': 'pcb_related',
            'primary': {'module': 'pcb_assistant.catalog.cli', 'symbol': 'related'},
            'secondary': [],
            'notes': ['包装必须移除关系和未解决引用中的本地路径字段。'],
        },
        {
            'name': 'pcb_similar_cases',
            'primary': {'module': 'pcb_assistant.catalog.insights', 'symbol': 'similar_cases'},
            'secondary': [],
            'notes': ['包装必须移除本地路径字段，只返回文档 ID 和安全元数据。'],
        },
        {
            'name': 'pcb_validate_citations',
            'primary': {'module': 'pcb_assistant.catalog.chat', 'symbol': '_citation_validation'},
            'secondary': [],
            'notes': [
                '当前为私有函数',
                'Phase 6.1 前必须增加公开、无副作用的包装函数。',
            ],
        },
        {
            'name': 'pcb_answer',
            'primary': {'module': 'pcb_assistant.router', 'symbol': 'answer'},
            'secondary': [
                {'module': 'agent.workflow', 'symbol': 'diagnose'},
                {'module': 'pcb_assistant.interfaces.managed', 'symbol': 'answer'},
                {'module': 'pcb_assistant.catalog.chat', 'symbol': 'ChatSession.ask'},
                {'module': 'pcb_assistant.catalog.agent', 'symbol': 'ask_agent'},
            ],
            'notes': [
                'Phase 6.2 默认 evidence_only；云端综合需请求显式选择 managed 且服务端开启出境。',
                'managed 只检索 public_reference 来源，脱敏通过后才发送结构化证据。',
                '不得调用本地生成模型；云端异常、隐私残留或引用校验失败时回退本地证据答案。',
            ],
        },
    ],
}

_TOOL_SPECS_BY_NAME = {item['name']: item for item in TOOL_SPECS}
for _tool in TOOL_MAPPING['tools']:
    _spec = _TOOL_SPECS_BY_NAME[_tool['name']]
    _tool.update({
        'status': _spec['status'],
        'data_egress': _spec['data_egress'],
        'external_services': _spec['external_services'],
        'side_effects': _spec['side_effects'],
        'phase6_1_exposed': _spec['status'] == 'P1',
        'phase6_2_exposed': True,
        'public_exposed': _tool['name'] == 'pcb_answer',
    })


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _resolve(module_name, symbol):
    module = importlib.import_module(module_name)
    value = module
    for part in symbol.split('.'):
        value = getattr(value, part)
    return value


def _contains_forbidden_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'source_path' or key.endswith('_path'):
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def validate_mapping(value=None):
    mapping = TOOL_MAPPING if value is None else value
    issues = []
    if mapping.get('contract_version') != CONTRACT_VERSION:
        issues.append('$.mapping.contract_version')
    if mapping.get('read_only') is not True:
        issues.append('$.mapping.read_only')
    if mapping.get('transport') != 'stdio':
        issues.append('$.mapping.transport')
    expected = {item['name'] for item in TOOL_SPECS}
    names = [item.get('name') for item in mapping.get('tools', [])]
    if len(names) != len(set(names)):
        issues.append('$.mapping: duplicate tool names')
    if set(names) != expected:
        issues.append('$.mapping: tool set mismatch')
    exposed = [item.get('name') for item in mapping.get('tools', [])
               if item.get('phase6_1_exposed')]
    if len(exposed) != 7:
        issues.append('$.mapping: Phase 6.1 must expose exactly seven P1 tools')
    if 'pcb_answer' in exposed:
        issues.append('$.mapping: pcb_answer must remain P2 in Phase 6.1')
    phase62_exposed = [item.get('name') for item in mapping.get('tools', [])
                       if item.get('phase6_2_exposed')]
    if (len(phase62_exposed) != len(expected)
            or set(phase62_exposed) != expected):
        issues.append('$.mapping: Phase 6.2 must expose all eight read-only tools')
    if [item.get('name') for item in mapping.get('tools', [])
            if item.get('public_exposed')] != ['pcb_answer']:
        issues.append('$.mapping: current public entry must be pcb_answer only')
    for index, item in enumerate(mapping.get('tools', [])):
        base = '$.mapping.tools[' + str(index) + ']'
        spec = _TOOL_SPECS_BY_NAME.get(item.get('name'))
        if spec is None:
            continue
        for key in ('status', 'data_egress', 'external_services', 'side_effects'):
            if item.get(key) != spec[key]:
                issues.append(base + ': ' + key + ' differs from the tool contract')
        if item.get('external_services') is True and item.get('name') != 'pcb_answer':
            issues.append(base + ': only pcb_answer may use external services')
        if item.get('side_effects') is not False:
            issues.append(base + ': mapping targets must be read-only')
        targets = [item.get('primary')] + list(item.get('secondary', []))
        for target in targets:
            if not target:
                issues.append(base + ': empty target')
                continue
            try:
                _resolve(target['module'], target['symbol'])
            except (ImportError, AttributeError) as exc:
                issues.append(base + ': cannot resolve ' + target['module'] + '.' + target['symbol'] + ': ' + str(exc))
    if _contains_forbidden_key(mapping):
        issues.append('$.mapping: must not carry path-valued fields')
    return issues


def write_mapping(path=MAPPING_PATH):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(TOOL_MAPPING, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return target


def validate_mapping_file(path=MAPPING_PATH):
    target = Path(path)
    if not target.is_file():
        return ['missing mapping file: ' + str(target)]
    try:
        value = _read_json(target)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    issues = validate_mapping(value)
    if value != json.loads(json.dumps(TOOL_MAPPING, ensure_ascii=False)):
        issues.append('mapping file is out of sync with pcb_assistant.interfaces.mapping')
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description='Phase 6 tool mapping audit')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)
    if args.write:
        write_mapping()
    issues = validate_mapping_file()
    result = {
        'status': 'passed' if not issues else 'failed',
        'issues': issues,
        'tools': len(TOOL_MAPPING['tools']),
        'mapping_status': TOOL_MAPPING['mapping_status'],
        'external_calls': 0,
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('phase6_mapping=' + result['status'])
        for issue in issues:
            print('issue: ' + issue)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
