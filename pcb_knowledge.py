"""Build local, attributable engineering evidence without loading raw PST mail."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

from pcb_selection import is_selection_question, selection_term_groups
from pcb_material_concepts import (
    MATERIAL_TERMS, is_material_scope_question, material_concepts,
    material_explanation_quality, material_ranking_keys,
)

import jieba

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
EVIDENCE_STATUS = 'unverified_email_discussion'
PLACEHOLDERS = re.compile(r'\[(?:身份|实体|邮箱|网址|电话|编号|日期|地址|业务信息|附件|凭据|邮件头|签名|金额|内部路径|内容标识)\]')
_TOKEN_PATTERN = re.compile(r'[a-z0-9\u3400-\u9fff]+')
SYNONYMS = {
    'delamination': ('分层', '分層', '层间分离', '層間分離', 'delamination'),
    'blister': ('爆板', '起泡', '起泡分层', 'blister'),
    'whitespot': ('白斑', '白点', '白點', 'white spot', 'measling'),
    'void': ('空洞', '气泡', '氣泡', 'void'),
    'warpage': ('翘曲', '翹曲', '板翘', '板翹', '弯曲', 'warpage'),
    'lamination': ('压合', '壓合', '层压', '層壓', 'lamination', 'laminating', 'pressing'),
    'prepreg': ('半固化片', '半固化', 'prepreg', 'pp'),
    'resin': ('树脂', '樹脂', 'resin'),
    'copper': ('铜箔', '銅箔', 'copper foil'),
    'stackup': ('叠构', '疊構', '叠板', '疊板', 'stackup', 'stack-up'),
    'vacuum': ('真空', 'vacuum'),
    'pressure': ('压力', '壓力', 'pressure'),
    'temperature': ('温度', '溫度', 'temperature'),
    'heating': ('升温', '升溫', '升温速率', '升溫速率', 'heating rate'),
    'curing': ('固化', '保温', '保溫', 'curing', 'cure'),
    'microsection': ('切片', 'microsection'),
    'peel': ('剥离', '剝離', 'peel strength'),
    'reliability': ('可靠性', '热冲击', '熱衝擊', '耐热', '耐熱', 'reliability'),
}
STOPWORDS = set('如何 怎么 怎么办 为什么 是否 什么 哪些 多少 请问 帮我 帮忙 请 给出 关于 这个 那个 进行 需要 可以 相关 建议 分析 原因 措施 处理 排查 解决 方法 影响 发生 出现 导致 说明 问题 资料 邮件 依据 参数 条件 有关 你好 谢谢 情况 一下 我们 你们 已经 应该 可能 如果 因为 所以 但是 以及 或者 还是 的 了 和 是 在 与 对 后 前 中 下 上 用 有 无 不 未 吗 呢 吧 啊'.split())
GENERIC = {'压合', '壓合', '层压', '層壓', 'pcb', 'lamination', 'laminating', 'pressing', 'conceptlamination', '工艺', '工藝'}
FIELD_PATTERNS = {
    'phenomenon': r'分[层層]|爆板|白[斑点點]|空洞|[气氣]泡|滑板|[皱皺]折|[翘翹]曲|板[翘翹]|异常|異常|不良|缺陷|delaminat|void|warpage|blister',
    'materials': r'半固化|prepreg|\bPP\b|叠构|疊構|叠板|疊板|铜箔|銅箔|树脂|樹脂|材料|板厚|铜厚|銅厚|stack.?up|FR-?4|\bTg\b',
    'conditions': r'温度|溫度|压力|壓力|真空|保温|保溫|升温|升溫|降温|降溫|\d\s*(?:°C|℃|bar|MPa|kg/cm2|psi|min|分钟|分鐘|秒|μm|mm)',
    'analysis': r'原因|因为|因為|由于|由於|可能|怀疑|懷疑|机理|機理|分析|关联|關聯|because|cause|mechanism',
    'actions': r'建议|建議|改善|调整|調整|更换|更換|增加|降低|提高|控制|检查|檢查|清洁|清潔|排查|recommend|adjust|check',
    'verification': r'试验|試驗|测试|測試|验证|驗證|结果|結果|切片|合格|不良率|通过|通過|失效|test|result|verif',
    'limitations': r'仅适用|僅適用|限于|限於|不适用|不適用|尚未|未验证|未驗證|待验证|待驗證|待确认|待確認|不能确定|不能確定|需进一步|需進一步|unverified|not confirmed',
}
FIELD_LABELS = {
    'phenomenon': '问题现象', 'materials': '材料与叠构', 'conditions': '工艺条件',
    'analysis': '分析线索', 'actions': '处理措施线索', 'verification': '验证记录提及',
    'limitations': '适用边界与不确定性',
}
# What to supply next for a field that the returned excerpts leave empty. Keyed
# by the same analysis fields so the guidance derives from coverage instead of
# being hand-written per answer or per layer.
FIELD_INPUT_HINTS = {
    'phenomenon': '补充缺陷的现象、位置、比例与批次范围',
    'materials': '补充材料牌号、PP 规格与批次、叠构与板厚',
    'conditions': '补充本批温度、压力、真空曲线与设备实际记录',
    'analysis': '补充初步原因判断或机理分析（含怀疑点）',
    'actions': '补充已采取或拟采取的改善、调整措施',
    'verification': '补充切片、剥离、复现或对照批次的验证结果',
    'limitations': '补充适用边界、未验证项与不确定性说明',
}
_TOKENIZER = None
_TOKENIZER_LOCK = threading.Lock()


def dump_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def digest_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def tokenizer():
    """Return the shared tokenizer, initializing it at most once across threads."""
    global _TOKENIZER
    if _TOKENIZER is None:
        with _TOKENIZER_LOCK:
            if _TOKENIZER is None:
                jieba.setLogLevel(logging.ERROR)
                tokenizer_instance = jieba.Tokenizer()
                for terms in SYNONYMS.values():
                    for term in terms:
                        tokenizer_instance.add_word(term, freq=100000)
                _TOKENIZER = tokenizer_instance
    return _TOKENIZER


def warm_tokenizer_async():
    """Start tokenizer initialization without delaying readiness of the local retrieval layer."""
    if _TOKENIZER is not None:
        return None
    thread = threading.Thread(
        target=tokenizer, name="catalog-tokenizer-warmup", daemon=True)
    thread.start()
    return thread


_CONCEPT_PATTERNS = tuple(
    ('concept' + name, tuple(
        re.compile(
            r'(?<![a-z0-9])' + re.escape(term.casefold()) + r'(?![a-z0-9])'
            if term.isascii() else re.escape(term.casefold())
        )
        for term in terms
    ))
    for name, terms in SYNONYMS.items()
)


def concepts(text):
    text = text.casefold()
    return {name for name, patterns in _CONCEPT_PATTERNS
            if any(pattern.search(text) for pattern in patterns)}


def tokens(text):
    text = PLACEHOLDERS.sub(' ', text).casefold()
    words = [word.strip() for word in tokenizer().cut(text)]
    words = [word for word in words if len(word) >= 2 and word not in STOPWORDS
             and _TOKEN_PATTERN.fullmatch(word)]
    return words + sorted(concepts(text))


MAX_TERM_GROUPS = 6
MAX_QUERY_TERM_CHARS = 40
# Query-only aliases: keep existing FTS tokenization and immutable catalogs.
QUERY_CONTEXT_GROUPS = (
    ('厚铜', '厚銅', 'heavy copper', 'thick copper'),
    ('铜台阶', '銅台階', '铜阶', '銅階', 'copper step'),
    ('缺胶', '缺膠', '树脂不足', '樹脂不足', 'resin starvation'),
    ('填胶', '填膠', 'resin filling'),
)
EXPLANATION_TERMS = ('原理', '机理', '機理', '排出', '减少', '減少', '避免',
                     '防止', '因为', '因為', '由于', '由於', '用于', '用於',
                     'mechanism', 'remove', 'reduce', 'prevent', 'because')
_KNOWLEDGE_QUESTION = re.compile(
    r'什么是|什麼是|是什么(?!原因)|是什麼(?!原因)|定义|定義|含义|含義|区别|區別|'
    r'原理|机理|機理|解释|解釋|如何选择|怎么选|如何设置|怎么设置|测试方法|作用|'
    r'\b(?:what is|definition|difference|explain)\b', re.IGNORECASE)


def is_knowledge_question(text):
    return (is_selection_question(text) or bool(_KNOWLEDGE_QUESTION.search(text))
            or is_material_scope_question(text)) and not re.search(
        r'排查|诊断|診斷|怎么处理|怎麼處理|如何解决|原因|troubleshoot|diagnos', text, re.I)


def is_explanation_question(text):
    return (not is_selection_question(text) and is_knowledge_question(text)
            and (is_material_scope_question(text) or bool(re.search(
                r'什么是|什麼是|原理|机理|機理|作用|解释|解釋|定义|定義|区别|區別|含义|含義|what is|explain|difference', text, re.I))))


def query_context_groups(text):
    lowered = text.casefold()
    return tuple(group for group in QUERY_CONTEXT_GROUPS
                 if any(alias in lowered for alias in group))


def explanation_quality(text, ranking_terms):
    """Rank topical explanatory excerpts; this is not a source validity score."""
    material_keys = material_ranking_keys(ranking_terms)
    if material_keys:
        definitions, mentions = material_explanation_quality(text, material_keys)
        return definitions * (len(material_keys) + 1) + mentions, int(definitions > 0)
    body = text.casefold()
    topical = [group for group in ranking_terms if tuple(group) != EXPLANATION_TERMS
               and not ('lamination' in group and '压合' in group)]
    topic_hits = sum(any(term in body for term in group) for group in topical)
    complete = (len(body) >= 30 and topic_hits > 0
                and not body.rstrip().endswith(('?', '？'))
                and '是否' not in body
                and any(marker in body for marker in EXPLANATION_TERMS))
    return topic_hits, int(complete)


PROCESS_EQUIPMENT_TERMS = {
    'conceptvacuum': ('真空度', '抽真空', '预真空', '預真空', '排气', '排氣',
                      'pre-vacuum', 'vacuum pressure', 'vacuum level', 'vacuum loss'),
    'conceptpressure': ('压合压力', '壓合壓力', '层压压力', '層壓壓力',
                        '成型压力', 'lamination pressure', 'press pressure'),
}
NON_DISCRIMINATIVE = frozenset({
    # Corpus-wide wording: it matches almost every record, so it must not drive
    # a bounded retrieval window or the reranking of one engineering question.
    '不足', '造成', '缺陷', '不良', '异常', '異常', '现象', '現象',
    '情况', '情況', '相关', '相關', '常见', '常見', '严重', '嚴重',
    '会不会', '有没有', '能不能', '过高', '過高', '过低', '過低',
    '太大', '太小', '过多', '過多', '太少', '主要',
    # Question wrappers: they describe how the user wants evidence handled,
    # not which engineering record should be retrieved.
    '优先', '優先', '核对', '核對', '证据', '證據', '补充', '補充',
    '信息', '区分', '區分', '因素', '记录', '記錄',
    '作用', '附近',
})


def query_terms(text):
    """Return the discriminative query terms used for bounded retrieval."""
    if is_selection_question(text):
        return tuple(dict.fromkeys(term for group in term_groups(text)
                                   for term in group))
    if is_knowledge_question(text) and material_concepts(text):
        return tuple(dict.fromkeys(alias for key in material_concepts(text)
                                   for alias in MATERIAL_TERMS[key]))
    terms = []
    include_english = bool(re.search(r'[a-zA-Z]', text))
    for group in query_context_groups(text):
        for alias in group:
            if alias not in terms and (include_english or not alias.isascii()):
                terms.append(alias)
            if alias in text.casefold():
                text = re.sub(re.escape(alias), ' ', text, flags=re.IGNORECASE)
    for token in tokens(text):
        token = token.strip().casefold()
        if (not token or token.startswith('concept')
                or len(token) > MAX_QUERY_TERM_CHARS):
            continue
        if token in GENERIC or token in STOPWORDS or token in NON_DISCRIMINATIVE:
            continue
        if token not in terms:
            terms.append(token)
    return tuple(terms)


def retrieval_query(text):
    """Converge a question to its discriminative terms; keep the text if none remain."""
    terms = query_terms(text)
    return ' '.join(terms) if terms else text


def term_groups(text):
    """Concept synonym groups plus remaining discriminative words for reranking.

    The groups are the deterministic concept-coverage signal shared by the
    evidence agent and the chat agent. Lamination itself is excluded because it
    matches almost every catalog record. Equipment questions retain it when
    needed to distinguish lamination from other processes using vacuum/pressure.
    """
    if is_selection_question(text):
        return tuple((list(selection_term_groups(text)) + list(query_context_groups(text)))[:12])
    if is_knowledge_question(text) and material_concepts(text):
        return tuple(MATERIAL_TERMS[key] for key in material_concepts(text))
    context_groups = query_context_groups(text)
    remaining = text
    for group in context_groups:
        for alias in sorted(group, key=len, reverse=True):
            remaining = re.sub(re.escape(alias), ' ', remaining, flags=re.IGNORECASE)
    raw_tokens = tokens(remaining)
    matched_concepts = concepts(text)
    retain_process = (
        'conceptlamination' in matched_concepts
        and bool(matched_concepts & {'conceptvacuum', 'conceptpressure'})
        and not matched_concepts & {
            'conceptdelamination', 'conceptblister', 'conceptwhitespot',
            'conceptvoid', 'conceptwarpage',
        }
    )
    concept_groups = []
    covered = set(alias for group in context_groups for alias in group)
    for token in raw_tokens:
        token = token.casefold()
        if not token.startswith('concept'):
            continue
        concept_name = token[len('concept'):]
        if concept_name == 'lamination' and not retain_process:
            continue
        group = tuple(dict.fromkeys(
            term.strip().casefold() for term in SYNONYMS.get(concept_name, ())
            if term.strip() and (concept_name == 'lamination' or term.strip().casefold() not in GENERIC)
        ))
        if group and group not in concept_groups:
            concept_groups.append(group)
            covered.update(group)

    if retain_process:
        # "Vacuum" alone also finds cleaning machines. Prefer passages about
        # evacuation/pressing when the user explicitly asks about lamination.
        for name, group in PROCESS_EQUIPMENT_TERMS.items():
            if name in matched_concepts:
                concept_groups.append(group)
    if is_explanation_question(text):
        concept_groups.append(EXPLANATION_TERMS)
        if 'conceptvacuum' in matched_concepts:
            concept_groups.append(('空气', '空氣', '气体', '氣體', '水分', '挥发', '揮發',
                                   'air', 'moisture', 'gas', 'volatile'))

    word_groups = []
    for token in raw_tokens:
        token = token.strip().casefold()
        if (not token or token.startswith('concept') or token in GENERIC
                or token in STOPWORDS or token in NON_DISCRIMINATIVE
                or token in covered or len(token) > MAX_QUERY_TERM_CHARS):
            continue
        word_groups.append((token,))

    return tuple((list(context_groups) + concept_groups + word_groups)[:MAX_TERM_GROUPS])


def group_coverage(groups, texts):
    """Count how many term groups appear literally in the given texts."""
    haystack = ' '.join(str(text).casefold() for text in texts)
    return sum(
        1 for group in groups
        if any(term.casefold() in haystack for term in group))


def coverage_label(covered, total, *, has_hits=True):
    """Label a deterministic keyword-coverage ratio; never an engineering verdict."""
    if not total:
        return 'unrated'
    if not has_hits:
        return 'none'
    ratio = covered / total
    if ratio >= 0.6:
        return 'strong'
    if ratio >= 0.34:
        return 'partial'
    return 'weak'


def passages(text, max_chars=700):
    """Contiguous source slices; offsets always refer to the original sanitized text."""
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind('\n', start + max_chars // 2, end),
                           text.rfind('。', start + max_chars // 2, end),
                           text.rfind('；', start + max_chars // 2, end))
            if boundary >= 0:
                end = boundary + 1
        lo, hi = start, end
        while lo < hi and text[lo].isspace():
            lo += 1
        while hi > lo and text[hi - 1].isspace():
            hi -= 1
        if lo < hi:
            yield {'start': lo, 'end': hi, 'text': text[lo:hi]}
        start = end


def extract_fields(text, limit=8):
    fields = {key: [] for key in FIELD_PATTERNS}
    for match in re.finditer(r'[^\n。！？!?；;]+[。！？!?；;]?', text):
        raw = match.group()
        lo = match.start() + len(raw) - len(raw.lstrip())
        hi = match.end() - len(raw) + len(raw.rstrip())
        if hi - lo < 6:
            continue
        # Long email lines remain bounded while retaining exact source offsets.
        for piece in passages(text[lo:hi], max_chars=350):
            quote = piece['text']
            if len(re.sub(r'\W', '', PLACEHOLDERS.sub('', quote))) < 6:
                continue
            for key, pattern in FIELD_PATTERNS.items():
                if len(fields[key]) < limit and re.search(pattern, quote, re.I):
                    fields[key].append({'text': quote, 'start': lo + piece['start'],
                                        'end': lo + piece['end']})
    return fields


def field_hits(texts):
    """Return the analysis fields that the given texts literally mention.

    Shared by the chat layer so an answer can state which analysis fields the
    returned excerpts cover instead of implying that the evidence is complete.
    """
    haystack = '\n'.join(str(text) for text in texts)
    return tuple(key for key, pattern in FIELD_PATTERNS.items()
                 if re.search(pattern, haystack, re.I))


def field_coverage(texts):
    """Describe which analysis fields the given texts literally cover.

    One shared implementation for the evidence layer, the read-only agent and the
    chat answer, so every layer describes the same excerpts the same way.
    The same object also states what to supply for the fields it leaves empty.
    """
    covered = field_hits(texts)
    return {
        'basis': 'field_patterns_in_returned_excerpts',
        'covered': list(covered),
        'labels': [FIELD_LABELS[key] for key in covered],
        'missing_labels': [label for key, label in FIELD_LABELS.items()
                           if key not in covered],
        'recommended_inputs': [
            {'field': key, 'label': FIELD_LABELS[key], 'hint': FIELD_INPUT_HINTS[key]}
            for key in FIELD_LABELS if key not in covered
        ],
    }


def build(source_run, output, policy_path=ROOT / 'agent_policy.json'):
    source_run, output = Path(source_run), Path(output)
    policy = json.loads(Path(policy_path).read_text())
    if (policy.get('local_use_allowed') is not True or policy.get('external_services') is not False
            or policy.get('manual_review_required') is not False):
        raise ValueError('unsupported_local_use_policy')
    report = json.loads((source_run / 'report.json').read_text())
    progress = json.loads((source_run / 'progress.json').read_text())
    if report['status'] != 'completed_local_candidates_require_review' or progress['status'] != 'completed':
        raise ValueError('source_run_incomplete')
    source = source_run / 'engineering_candidates.jsonl'
    source_hash = digest_file(source)
    if source_hash != report['artifacts'][source.name]['sha256']:
        raise ValueError('source_checksum_mismatch')
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    counts = collections.Counter()
    coverage = collections.Counter()
    connection = sqlite3.connect(output / 'knowledge.sqlite3.partial')
    try:
        connection.executescript('''
            PRAGMA journal_mode=DELETE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE cases (source_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                body TEXT NOT NULL, topics TEXT NOT NULL, fields TEXT NOT NULL);
            CREATE TABLE chunks (id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES cases(source_id));
            CREATE INDEX chunks_source ON chunks(source_id);
            CREATE VIRTUAL TABLE search USING fts5(title_tokens, body_tokens, chunk_id UNINDEXED);
        ''')
        connection.execute('INSERT INTO metadata VALUES (?, ?)', ('schema_version', str(SCHEMA_VERSION)))
        with source.open(encoding='utf-8') as stream, (output / 'cases.jsonl.partial').open('w', encoding='utf-8') as cases_out:
            for line in stream:
                row = json.loads(line)
                if (not re.fullmatch(r'mail_[0-9a-f]{24}', row['id'])
                        or row['source_kind'] != 'untrusted_email'
                        or row['training_allowed'] is not False):
                    raise ValueError('invalid_source_record')
                fields = extract_fields(row['text'])
                case = {'source_id': row['id'], 'subject': row['subject'], 'topics': row['topics'],
                        'fields': fields, 'evidence_status': EVIDENCE_STATUS,
                        'local_use_allowed': True, 'manual_review_required': False,
                        'extraction_method': 'verbatim_rules', 'independently_verified': False}
                cases_out.write(json.dumps(case, ensure_ascii=False) + '\n')
                connection.execute('INSERT INTO cases VALUES (?, ?, ?, ?, ?)',
                                   (row['id'], row['subject'], row['text'], json.dumps(row['topics'], ensure_ascii=False),
                                    json.dumps(fields, ensure_ascii=False)))
                title_tokens = ' '.join(tokens(row['subject']))
                seen = set()
                for number, piece in enumerate(passages(row['text'])):
                    content_key = re.sub(r'\s+', '', piece['text']).casefold()
                    if content_key in seen:
                        continue
                    seen.add(content_key)
                    terms = tokens(piece['text'])
                    if not terms:
                        continue
                    chunk_id = row['id'] + ':' + str(number)
                    connection.execute('INSERT INTO chunks VALUES (?, ?, ?, ?, ?)',
                                       (chunk_id, row['id'], piece['start'], piece['end'], piece['text']))
                    connection.execute('INSERT INTO search VALUES (?, ?, ?)', (title_tokens, ' '.join(terms), chunk_id))
                    counts['passages'] += 1
                counts['cases'] += 1
                coverage.update(key for key, value in fields.items() if value)
                if counts['cases'] % 500 == 0:
                    dump_json(output / 'progress.json', {'status': 'building', 'counts': dict(counts)})
                    print(json.dumps({'event': 'indexing', **counts}), flush=True)
        if counts['cases'] != report['counts']['engineering_candidates']:
            raise ValueError('case_count_mismatch')
        connection.execute("INSERT INTO search(search) VALUES ('optimize')")
        connection.commit()
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('database_integrity_check_failed')
        if connection.execute('SELECT count(*) FROM search').fetchone()[0] != counts['passages']:
            raise ValueError('search_count_mismatch')
    finally:
        connection.close()
    if digest_file(source) != source_hash:
        raise ValueError('source_changed_during_build')
    artifacts = {}
    for name in ('cases.jsonl', 'knowledge.sqlite3'):
        (output / (name + '.partial')).replace(output / name)
        (output / name).chmod(0o600)
        artifacts[name] = {'sha256': digest_file(output / name), 'size_bytes': (output / name).stat().st_size}
    result = {'status': 'completed', 'schema_version': SCHEMA_VERSION,
              'source_run': source_run.name, 'source_artifact_sha256': source_hash,
              'counts': dict(counts), 'field_coverage': dict(coverage), 'policy': policy,
              'evidence_status': EVIDENCE_STATUS, 'artifacts': artifacts,
              'elapsed_seconds': round(time.monotonic() - started)}
    dump_json(output / 'report.json', result)
    dump_json(output / 'progress.json', {'status': 'completed', 'counts': dict(counts)})
    return result


def main():
    parser = argparse.ArgumentParser(description='从脱敏邮件建立本地工程资料库')
    parser.add_argument('--source-run', type=Path, default=ROOT / 'data/sanitized_v3')
    parser.add_argument('--run-name', default='knowledge_v1')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.run_name):
        parser.error('invalid run name')
    try:
        result = build(args.source_run, ROOT / 'data' / args.run_name)
        print(json.dumps({'event': 'completed', **result['counts'], 'elapsed_seconds': result['elapsed_seconds']}))
    except Exception as exc:
        print(json.dumps({'event': 'build_failed', 'error_type': type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
