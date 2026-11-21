#!/usr/bin/env python3
"""Two-pass local PST sanitizer. Outputs candidates, never training-ready data."""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from pcb_assistant.paths import ROOT
import re
import secrets
import sys
import time
import warnings

import spacy

warnings.filterwarnings('ignore')  # Third-party warnings must not echo mail markup.

os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')
os.environ.setdefault('BLIS_NUM_THREADS', '1')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

from pcb_assistant.privacy.mail import (IdentityIndex, IdentityMatcher, Sanitizer, decode, opaque_id,
                          technical_topics, EMAIL)
from pcb_assistant.ingestion.mail_status import (MESSAGE_STATUS_SCHEMA_VERSION, MessageStatusLedger,
                         validate_message_status_rows)

ENGINE = None
MAX_BODY_CHARS = 250_000


def _init_worker(identity_terms):
    """Initialize an isolated worker for spawn-based platforms."""
    global ENGINE
    import spacy
    nlp_zh = spacy.load('zh_core_web_sm', disable=['tagger','parser','attribute_ruler','lemmatizer','senter'])
    nlp_en = spacy.load('en_core_web_sm', disable=['tagger','parser','attribute_ruler','lemmatizer','senter'])
    ENGINE = Sanitizer(IdentityMatcher(identity_terms), nlp_zh, nlp_en)


def emit(event, **stats):
    # Callers may provide counts/status enums ONLY. No raw mail or exceptions.
    print(json.dumps({'event':event, **stats}, ensure_ascii=False), flush=True)


def write_json(path, value):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
        f.write('\n')
    temp.chmod(0o600)
    temp.replace(path)


def safe_property(item, name, fallback=None):
    try:
        value=getattr(item,name)
        return fallback if value is None else value
    except (OSError,RuntimeError,ValueError):
        return fallback


def iter_messages(folder, stats):
    stats['folders']+=1
    for i in range(folder.number_of_sub_messages):
        stats['listed_messages']+=1
        try:
            message=folder.get_sub_message(i)
        except (OSError,RuntimeError,ValueError):
            stats['unreadable_messages']+=1
            continue
        yield message
    for i in range(folder.number_of_sub_folders):
        try:
            subfolder=folder.get_sub_folder(i)
        except (OSError,RuntimeError,ValueError):
            stats['unreadable_folders']+=1
            continue
        yield from iter_messages(subfolder,stats)


def body_text(message):
    plain=message.plain_text_body
    if plain and decode(plain).strip():
        return decode(plain), 'plain'
    raw_html=message.html_body
    if raw_html:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(decode(raw_html),'html.parser')
        for node in soup(['script','style','img','object','iframe','embed','head','svg']):
            node.decompose()
        return soup.get_text(separator='\n'), 'html'
    return '', 'no_text'


def collect_contacts(message, index):
    sender=safe_property(message,'sender_name','')
    if sender:
        index.add(sender)
        index.contact(sender)
    # MAPI identity properties only: never index subjects or message bodies as names.
    tags={0x0040,0x0042,0x0044,0x0064,0x0065,0x0c1a,0x0c1f,0x5d01,0x5d02,0x5d07,0x5d08}
    for record in message.record_sets:
        for entry in record.entries:
            if entry.entry_type in tags and entry.value_type in (0x1e,0x1f):
                value=entry.data_as_string
                if value:
                    index.contact(value)
                    if '@' not in value and not value.startswith('/'):
                        index.add(value)
    recipients=safe_property(message,'recipients')
    if recipients:
        for record in recipients.record_sets:
            for entry in record.entries:
                if entry.entry_type in (0x3001,0x3003,0x39fe,0x39ff) and entry.value_type in (0x1e,0x1f):
                    value=entry.data_as_string
                    if value:
                        index.contact(value)
                        if entry.entry_type==0x3001:
                            index.add(value)
    from email.parser import HeaderParser
    headers=HeaderParser().parsestr(decode(safe_property(message,'transport_headers','')))
    for name in ('From','To','Cc','Bcc','Reply-To','Sender'):
        for value in headers.get_all(name,[]):
            index.contact(value)
    body,_=body_text(message)
    # Forwarded names and mailbox aliases also form the in-memory lookup index.
    for line in body.splitlines():
        if re.match(r'(?i)^\s*[> ]*(?:from|to|cc|bcc|发件人|寄件者|寄件人|收件人|收件者|抄送|副本)\s*[:：]',line):
            index.contact(re.split(r'[:：]',line,maxsplit=1)[-1])
    for match in EMAIL.finditer(body):
        index.contact(match[0])


def process_record(record):
    ident, subject, body, attachment_count=record
    try:
        ENGINE.counts.clear()
        subject=ENGINE.clean(subject)
        text=ENGINE.clean(body)
        residuals=ENGINE.residuals(subject+'\n'+text)
        if residuals:
            return {'status':'residual_quarantine','id':ident,'residuals':residuals}
        if not text.strip():
            return {'status':'empty_after_redaction','id':ident}
        topics=technical_topics(subject+'\n'+text)
        candidate={'id':ident,'subject':subject,'text':text,'topics':topics,
                   'excluded_attachment_count':attachment_count,'review_required':True,
                   'training_allowed':False,'source_kind':'untrusted_email'}
        return {'status':'candidate','record':candidate,'redactions':dict(ENGINE.counts)}
    except Exception as exc:
        return {'status':'processing_error','id':ident,'error_type':type(exc).__name__}


def hash_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def run(args):
    global ENGINE
    os.umask(0o077)
    source=Path(args.source).resolve()
    workspace=ROOT
    output=workspace/'data'/args.run_name
    private=workspace/'.private'
    private.mkdir(exist_ok=True,mode=0o700)
    private.chmod(0o700)
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    output.chmod(0o700)
    key_path=private/'pseudonym.key'
    if not key_path.exists():
        with key_path.open('xb') as f:
            f.write(secrets.token_bytes(32))
        key_path.chmod(0o600)
    key=key_path.read_bytes()
    policy=json.loads((workspace/'config/privacy.json').read_text())
    # Fail closed if configuration requests an unsupported or less private mode.
    required={'mode':'local_review_candidates','preserve_process_parameters':True,
              'extract_attachments':False,'export_exact_dates':False,'export_headers':False,
              'export_folder_names':False,'export_source_filename':False,
              'external_services':False,'training_release_requires_review':True}
    if any(policy.get(k)!=v for k,v in required.items()):
        raise ValueError('unsupported_policy')
    started=time.monotonic()
    before=source.stat()
    def stage_progress(status, **counts):
        # Persist aggregate state only, including stages before body processing.
        write_json(output/'progress.json',{'status':status,**counts,
                   'elapsed_seconds':round(time.monotonic()-started)})

    write_json(output/'RELEASE_BLOCKED.json',{
        'training_allowed':False,'published':False,'external_mail_transmission':False,
        'reason':'处理尚未完成；完整输出也必须经过本地复核。'})
    stage_progress('loading_local_models')
    emit('loading_local_models')
    import pypff
    pst=pypff.file()
    pst.open(str(source),'r')
    scan=collections.Counter()
    index=IdentityIndex()
    failed_contacts=set()
    stage_progress('contact_scan',inventory=dict(scan))
    emit('contact_scan_started')
    for message in iter_messages(pst.root_folder,scan):
        scan['read_messages']+=1
        try:
            collect_contacts(message,index)
        except Exception:
            scan['contact_scan_errors']+=1
            failed_contacts.add(message.identifier)
        if scan['read_messages']%500==0:
            stage_progress('contact_scan',inventory=dict(scan),identity_terms_in_memory=len(index.terms))
            emit('contact_scan',messages=scan['read_messages'],identity_terms=len(index.terms),errors=scan['contact_scan_errors'])
    scan['orphan_items']=pst.number_of_orphan_items
    terms_path=(workspace/policy['sensitive_terms_file']).resolve()
    if terms_path.parent != private.resolve():
        raise ValueError('sensitive_terms_location')
    if terms_path.exists():
        for line in terms_path.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.startswith('#'):
                index.add(line)
    else:
        terms_path.write_text('# 仅本地保存；每行一个需要去除的公司简称、人名、客户代号或内部标识。\n',encoding='utf-8')
        terms_path.chmod(0o600)
    emit('contact_scan_complete',messages=scan['read_messages'],identity_terms=len(index.terms),errors=scan['contact_scan_errors'])
    stage_progress('processing',inventory=dict(scan),stats={})
    matcher=index.pattern()
    try:
        process_context=multiprocessing.get_context('fork')
    except ValueError:
        process_context=multiprocessing.get_context('spawn')
    if process_context.get_start_method()=='fork':
        nlp_zh=spacy.load('zh_core_web_sm',disable=['tagger','parser','attribute_ruler','lemmatizer','senter'])
        nlp_en=spacy.load('en_core_web_sm',disable=['tagger','parser','attribute_ruler','lemmatizer','senter'])
        ENGINE=Sanitizer(matcher,nlp_zh,nlp_en)
        worker_kwargs={}
    else:
        # Windows has no fork; each worker loads the local models in isolation.
        worker_kwargs={'initializer':_init_worker,'initargs':(frozenset(index.terms),)}
    stats=collections.Counter()
    redactions=collections.Counter()
    residuals=collections.Counter()
    topics=collections.Counter()
    ledger=MessageStatusLedger()
    second_pass=collections.Counter()
    output_files=['candidates.jsonl','engineering_candidates.jsonl','quarantine_manifest.jsonl','message_status.jsonl']
    paths={name:output/(name+'.partial') for name in output_files}
    with paths['candidates.jsonl'].open('w',encoding='utf-8') as all_out, paths['engineering_candidates.jsonl'].open('w',encoding='utf-8') as eng_out, paths['quarantine_manifest.jsonl'].open('w',encoding='utf-8') as quarantine, paths['message_status.jsonl'].open('w',encoding='utf-8') as status_out:
        def reject(sequence,ident,reason):
            stats[reason]+=1
            quarantine.write(json.dumps({'id':ident,'reason':reason},ensure_ascii=False)+'\n')
            ledger.record_reject(sequence,ident,reason)

        def records():
            sequence=0
            for message in iter_messages(pst.root_folder,second_pass):
                sequence+=1
                stats['messages_visited']+=1
                ident=opaque_id(key,message.identifier)
                if message.identifier in failed_contacts:
                    reject(sequence,ident,'contact_read_error')
                    continue
                try:
                    attachment_count=message.number_of_attachments
                    stats['attachments_excluded']+=attachment_count
                    body,kind=body_text(message)
                    stats['body_'+kind]+=1
                    if not body.strip():
                        reject(sequence,ident,'no_text_body')
                        continue
                    subject=decode(message.subject)
                    if len(body)+len(subject)>MAX_BODY_CHARS:
                        reject(sequence,ident,'oversize_body')
                        continue
                    yield sequence,ident,subject,body,attachment_count
                except Exception:
                    reject(sequence,ident,'body_read_error')

        def consume(result,sequence,ident):
            stats['processed']+=1
            if result['status']!='candidate':
                reject(sequence,ident,result['status'])
                residuals.update(result.get('residuals',{}))
                return
            record=result['record']
            if record['id']!=ident:
                raise ValueError('worker_message_id_mismatch')
            redactions.update(result['redactions'])
            fingerprint=hashlib.sha256((record['subject']+'\n'+record['text']).encode()).digest()
            _,is_duplicate=ledger.record_candidate(sequence,ident,fingerprint)
            if is_duplicate:
                stats['duplicate_candidates']+=1
                return
            line=json.dumps(record,ensure_ascii=False)+'\n'
            all_out.write(line)
            stats['unique_candidates']+=1
            if record['topics']:
                eng_out.write(line)
                stats['engineering_candidates']+=1
                topics.update(record['topics'])

        def progress():
            emit('sanitizing',processed=stats['processed'],candidates=stats['unique_candidates'],engineering_candidates=stats['engineering_candidates'],elapsed_seconds=round(time.monotonic()-started))
            write_json(output/'progress.json',{'status':'processing','stats':dict(stats),'elapsed_seconds':round(time.monotonic()-started)})

        # Bounded submissions: source bodies are passed through local memory only.
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers,mp_context=process_context,**worker_kwargs) as pool:
            pending={}
            last_progress=time.monotonic()
            for sequence,ident,subject,body,attachment_count in records():
                future=pool.submit(process_record,(ident,subject,body,attachment_count))
                pending[future]=(sequence,ident)
                if len(pending)>=args.workers*3:
                    # Consume in scan order so duplicate links are deterministic.
                    future=next(iter(pending))
                    concurrent.futures.wait([future],return_when=concurrent.futures.ALL_COMPLETED)
                    sequence,ident=pending.pop(future)
                    consume(future.result(),sequence,ident)
                if time.monotonic()-last_progress>=20:
                    progress()
                    last_progress=time.monotonic()
            while pending:
                future=next(iter(pending))
                done,_=concurrent.futures.wait([future],timeout=20,return_when=concurrent.futures.ALL_COMPLETED)
                if done:
                    sequence,ident=pending.pop(future)
                    consume(future.result(),sequence,ident)
                if time.monotonic()-last_progress>=20:
                    progress()
                    last_progress=time.monotonic()
        for row in ledger.rows():
            status_out.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
        for handle in (all_out,eng_out,quarantine,status_out):
            handle.flush()
            os.fsync(handle.fileno())
    pst.close()
    stage_progress('auditing_outputs',stats=dict(stats))
    emit('auditing_outputs',candidates=stats['unique_candidates'])
    if ENGINE is None:
        # Spawn workers initialize models in child processes; audit needs one here.
        _init_worker(frozenset(index.terms))
    audit=collections.Counter()
    # Independent full read-back: do not assume a worker check proves file safety.
    for name in ['candidates.jsonl','engineering_candidates.jsonl']:
        with paths[name].open(encoding='utf-8') as handle:
            for line in handle:
                record=json.loads(line)
                if set(record)!={'id','subject','text','topics','excluded_attachment_count','review_required','training_allowed','source_kind'}:
                    raise ValueError('unexpected_output_schema')
                if record['training_allowed'] is not False or record['review_required'] is not True:
                    raise ValueError('unsafe_release_flag')
                audit.update(ENGINE.residuals(record['subject']+'\n'+record['text']))
                audit['records_read_back']+=1
    if any(count for name,count in audit.items() if name!='records_read_back'):
        raise ValueError('read_back_audit_failed')
    status_rows=[]
    with paths['message_status.jsonl'].open(encoding='utf-8') as handle:
        for line in handle:
            status_rows.append(json.loads(line))
    status_counts=validate_message_status_rows(status_rows,stats)
    after=source.stat()
    unchanged=(before.st_size==after.st_size and before.st_mtime_ns==after.st_mtime_ns)
    if not unchanged:
        raise ValueError('source_changed_during_processing')
    # Keep archive fingerprint private, and never output the filename or checksum.
    stage_progress('fingerprinting_source',stats=dict(stats))
    emit('fingerprinting_source')
    source_hash=hash_file(source)
    final_stat=source.stat()
    if (before.st_size,before.st_mtime_ns)!=(final_stat.st_size,final_stat.st_mtime_ns):
        raise ValueError('source_changed_during_fingerprinting')
    write_json(private/(args.run_name+'_source_fingerprint.json'),{'sha256':source_hash,'size_bytes':before.st_size,'mtime_ns':before.st_mtime_ns})
    artifacts={}
    for name,path in paths.items():
        destination=output/name
        path.replace(destination)
        artifacts[name]={'sha256':hash_file(destination),'size_bytes':destination.stat().st_size}
    report={'status':'completed_local_candidates_require_review',
            'source_size_bytes':before.st_size,'source_opened_read_only':True,'source_metadata_unchanged':unchanged,
            'inventory':dict(scan),'second_pass_inventory':dict(second_pass),'counts':dict(stats),
            'message_status':{'artifact':'message_status.jsonl','schema_version':MESSAGE_STATUS_SCHEMA_VERSION,
                              'rows':len(status_rows),'counts':status_counts},
            'identity_terms_in_memory':len(index.terms),'rule_match_operations':dict(redactions),
            'quarantined_residual_detections':dict(residuals),'output_audit':dict(audit),
            'topic_counts':dict(topics),'elapsed_seconds':round(time.monotonic()-started),
            'models':{'zh_core_web_sm':ENGINE.nlp_zh.meta['version'],'en_core_web_sm':ENGINE.nlp_en.meta['version'],'spacy':spacy.__version__,'libpff':pypff.get_version()},
            'artifacts':artifacts,'release':{'training_allowed':False,'published':False,'external_mail_transmission':False,
            'reason':'自动规则与实体识别存在漏检/误删，须本地复核身份、简称、业务背景及工艺机密。'},
            'limits':['附件、图片、嵌入邮件及仅有 RTF 的正文未导出；附件数量为邮件容器报告值。',
                      '孤立项目只计数，未恢复导出；读取失败项目只记录匿名编号和原因。',
                      '所有匹配表仅在内存；无明文联系人映射落盘；原始 PST 仍敏感。',
                      '逐邮件状态清单只记录匿名 ID、处理状态和去重关联；不含正文、主题、身份、源路径或内容指纹。',
                      '工艺参数尽量保留，但无法自动判断配方是否为商业秘密；NER 可能误删技术词。',
                      '主题分类仅关键词筛选，不代表该邮件已形成工程知识或有效操作规范。']}
    write_json(output/'report.json',report)
    write_json(output/'progress.json',{'status':'completed','stats':dict(stats),'elapsed_seconds':report['elapsed_seconds']})
    write_json(output/'RELEASE_BLOCKED.json',report['release'])
    emit('completed',**dict(stats),elapsed_seconds=report['elapsed_seconds'])


def main():
    parser=argparse.ArgumentParser(description='本地 PST 脱敏；仅生成待复核候选数据。')
    parser.add_argument('source')
    parser.add_argument('--run-name',default='sanitized_v1',type=lambda v:v if re.fullmatch(r'[a-zA-Z0-9_-]+',v) else parser.error('invalid run name'))
    parser.add_argument('--workers',type=int,default=6)
    args=parser.parse_args()
    if not 1<=args.workers<=12:
        parser.error('workers must be between 1 and 12')
    try:
        run(args)
    except KeyboardInterrupt:
        emit('interrupted_outputs_incomplete')
        return 130
    except Exception as exc:
        # Exception strings/tracebacks may contain a subject, address or body.
        emit('failed_outputs_incomplete',error_type=type(exc).__name__)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
