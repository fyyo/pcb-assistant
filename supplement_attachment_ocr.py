"""Offline, resumable OCR supplementation of immutable attachment candidates."""
from collections import Counter,defaultdict
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import concurrent.futures
import contextlib
import io
import importlib.metadata
import multiprocessing
import re

from catalog_common import ROOT,digest_file
from mail_privacy import opaque_id
from ocr_image_sources import ImageEnumerator,image_eligibility,image_fingerprint
from scripts.data.rewash_candidate_identities import read_rows,snapshot
from scripts.data.derive_mail_candidates import write_json
from candidate_privacy import ENGINEERING_EXPRESSION,_ENGINEERING_CODE
from mail_privacy import technical_topics
from ocr_candidate_privacy import OcrTextSanitizer,OcrPrivacyRejected,expression_hash
from scripts.data.rewash_candidate_identities import NUMBERS

SOURCE=ROOT/'20251029.pst'
BASE=ROOT/'data/attachments_v3'
CLEAN_BASE=ROOT/'data/attachments_v4_identity'
ARCHIVE=ROOT/'data/mail_archive_v1/mail_archive.sqlite3'
OCR_ENGINE=None


def emit(event,**counts):
    print(json.dumps({'event':event,**counts},ensure_ascii=False),flush=True)


def verified_sources(source=SOURCE,base=BASE):
    manifest=json.loads((base/'report.json').read_text(encoding='utf-8'))
    paths=[source,base/'report.json']
    for name,artifact in manifest['artifacts'].items():
        path=(base/name).resolve()
        if path.parent!=base.resolve() or digest_file(path)!=artifact['sha256']:
            raise ValueError('attachment_source_artifact_mismatch')
        paths.append(path)
    return snapshot(paths)


def verified_clean_base(base=CLEAN_BASE):
    report=json.loads((base/'report.json').read_text(encoding='utf-8'))
    paths=[base/'report.json']
    for name,artifact in report['artifacts'].items():
        path=(base/name).resolve()
        if path.parent!=base.resolve() or digest_file(path)!=artifact['sha256']:
            raise ValueError('clean_attachment_source_artifact_mismatch')
        paths.append(path)
    return snapshot(paths)


def source_documents(source=SOURCE,base=BASE,stats=None):
    """Read each retained document once; prove its original HMAC identity."""
    import pypff
    from sanitize_pst import iter_messages
    stats=stats if stats is not None else Counter()
    key=(ROOT/'.private/pseudonym.key').read_bytes()
    documents={r['document_id']:r for r in read_rows(base/'documents.jsonl')}
    occurrences={(r['parent_source_id'],r['attachment_ordinal']):r['document_id']
                 for r in read_rows(base/'occurrences.jsonl') if r.get('status')=='read'}
    seen=set()
    pst=pypff.file()
    pst.open(str(source),'r')
    try:
        for message in iter_messages(pst.root_folder,Counter()):
            parent=opaque_id(key,message.identifier)
            try:
                count=message.number_of_attachments
            except Exception:
                stats['attachment_list_errors']+=1
                continue
            for ordinal in range(count):
                document_id=occurrences.get((parent,ordinal))
                if document_id is None or document_id in seen:
                    continue
                attachment=message.get_attachment(ordinal)
                content=attachment.read_buffer(attachment.size)
                if len(content)!=attachment.size:
                    raise ValueError('incomplete_original_attachment')
                expected='att_'+opaque_id(key,'attachment:'+hashlib.sha256(content).hexdigest())[5:]
                if expected!=document_id:
                    raise ValueError('original_attachment_identity_mismatch')
                seen.add(document_id)
                stats['source_documents_verified']+=1
                yield documents[document_id],content
        if seen!=set(documents):
            raise ValueError('retained_attachment_source_missing')
    finally:
        pst.close()


def inventory(output,source=SOURCE,base=BASE):
    output=Path(output)
    if output.exists():
        raise ValueError('output_already_exists')
    started=time.monotonic()
    before=verified_sources(source,base)
    output.mkdir(parents=True,exist_ok=False)
    stats,source_stats,formats=Counter(),Counter(),Counter()
    seen={}
    last=time.monotonic()
    with (output/'components.jsonl').open('w',encoding='utf-8',newline='\n') as stream:
        for document,content in source_documents(source,base,source_stats):
            enumerator=ImageEnumerator()
            for component in enumerator.enumerate(content,document['format']):
                reason=image_eligibility(component.image)
                digest=image_fingerprint(component.image)
                row={'document_id':document['document_id'],'format':document['format'],
                     'locator':component.locator,'pixel_sha256':digest,'width':component.image.width,
                     'height':component.image.height,'status':reason or 'eligible'}
                stats['components']+=1
                stats[row['status']]+=1
                formats[document['format']]+=1
                if not reason:
                    seen.setdefault(digest,(component.image.width,component.image.height))
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            stats.update(enumerator.counts)
            if time.monotonic()-last>=15:
                emit('ocr_inventory_progress',documents=source_stats['source_documents_verified'],components=stats['components'],unique_eligible_images=len(seen))
                last=time.monotonic()
    if verified_sources(source,base)!=before:
        raise ValueError('sources_changed_during_inventory')
    report={'schema':'attachment-ocr-inventory-v1','status':'completed','source_snapshots':before,
        'source_unchanged':True,'statistics':dict(stats),'source_statistics':dict(source_stats),
        'components_by_document_format':dict(formats),'unique_eligible_images':len(seen),
        'eligible_megapixels':round(sum(w*h for w,h in seen.values())/1_000_000,3),
        'artifacts':{'components.jsonl':{'sha256':digest_file(output/'components.jsonl')}},
        'external_services':False,'elapsed_seconds':round(time.monotonic()-started,1),
        'limits':['Small or oversized images are enumerated but skipped.',
                  'Vector media and unknown OLE image encodings remain unsupported.',
                  'GIF uses its first frame; TIFF pages are processed separately.',
                  'OCR does not interpret geometry or verify engineering numbers.']}
    write_json(output/'report.json',report)
    emit('ocr_inventory_completed',statistics=dict(stats),unique_eligible_images=len(seen),elapsed_seconds=report['elapsed_seconds'])
    return report


def _init_ocr_worker():
    global OCR_ENGINE
    import os
    os.environ['OMP_NUM_THREADS']='1'
    os.environ['OPENBLAS_NUM_THREADS']='1'
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from rapidocr_onnxruntime import RapidOCR
        OCR_ENGINE=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1,
                            det_use_cuda=False,cls_use_cuda=False,rec_use_cuda=False)


def recognize_image(job):
    digest,width,height,pixels=job
    started=time.monotonic()
    try:
        import numpy as np
        array=np.frombuffer(pixels,dtype=np.uint8).reshape((height,width,3))
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result,_=OCR_ENGINE(np.ascontiguousarray(array[:,:,::-1]))
        lines,confidence=[],[]
        rejected=0
        for row in result or []:
            score=float(row[2])
            if .65<=score<=1.000001 and isinstance(row[1],str) and row[1].strip():
                lines.append(row[1])
                confidence.append(score)
            else:
                rejected+=1
        return {'pixel_sha256':digest,'status':'recognized','text':'\n'.join(lines),
                'minimum_confidence':min(confidence) if confidence else None,
                'recognized_lines':len(lines),'low_confidence_lines_skipped':rejected,
                'seconds':round(time.monotonic()-started,3)}
    except Exception as exc:
        return {'pixel_sha256':digest,'status':'failed','error_type':type(exc).__name__,
                'seconds':round(time.monotonic()-started,3)}


def sanitized_result(raw,cleaner):
    """Raw OCR strings leave memory only after this succeeds."""
    result={key:raw[key] for key in ('pixel_sha256','status','minimum_confidence',
             'recognized_lines','low_confidence_lines_skipped','seconds','error_type') if key in raw}
    if raw['status']=='failed':
        return result
    if not raw['text'].strip():
        result['status']='ocr_empty'
        return result
    before=cleaner.counts.copy()
    try:
        text,audit=cleaner.sanitize(raw['text'])
        if any(len(line)>6000 for line in text.splitlines()):
            raise OcrPrivacyRejected('ocr_line_character_limit')
        if not text.strip():
            result['status']='empty_after_privacy'
        else:
            result.update(status='sanitized',text=text,audit=audit,topics=technical_topics(text))
    except OcrPrivacyRejected as exc:
        result.update(status='privacy_quarantined',reason=str(exc))
    result['privacy_operations']=dict(cleaner.counts-before)
    return result


def text_pieces(text):
    pieces,buffer=[],''
    for line in text.splitlines(keepends=True):
        if len(line)>6000:
            raise ValueError('ocr_line_exceeds_piece_limit')
        if buffer and len(buffer)+len(line)>6000:
            pieces.append(buffer)
            buffer=''
        buffer+=line
    if buffer:
        pieces.append(buffer)
    if ''.join(pieces)!=text:
        raise ValueError('ocr_piece_text_changed')
    return pieces


def validate_checkpoint_result(row):
    if row['status']!='sanitized':
        if 'text' in row:
            raise ValueError('unsafe_text_in_rejected_checkpoint')
        return
    text=row['text']
    for pattern,key in ((NUMBERS,'output_numeric_sequence_sha256'),
                        (_ENGINEERING_CODE,'output_codes_sha256'),
                        (ENGINEERING_EXPRESSION,'engineering_expression_sha256')):
        if expression_hash(pattern,text)!=row['audit'][key]:
            raise ValueError('checkpoint_engineering_hash_mismatch')
    if not row['audit']['engineering_expression_preserved'] or not .65<=row['minimum_confidence']<=1.000001:
        raise ValueError('invalid_ocr_candidate_audit')


def publish_supplement(output,inventory_dir,connection,cleaner,sources_before,signature,stats,
                       clean_before=None,archive_before=None):
    components=defaultdict(list)
    for row in read_rows(inventory_dir/'components.jsonl'):
        if row['status']=='eligible':
            components[(row['document_id'],row['pixel_sha256'])].append(row['locator'])
    current=CLEAN_BASE
    documents={r['document_id']:r for r in read_rows(current/'documents.jsonl')}
    topics=defaultdict(set)
    for row in read_rows(current/'engineering_candidates.jsonl'):
        topics[row['document_id']].update(row.get('document_topics',row['topics']))
    results={r[0]:json.loads(r[1]) for r in connection.execute('SELECT pixel_sha256,result_json FROM images')}
    expected={digest for _,digest in components}
    if set(results)!=expected:
        raise ValueError('ocr_unique_image_coverage_incomplete')
    for (doc_id,digest),locators in components.items():
        if results[digest]['status']=='sanitized':
            topics[doc_id].update(results[digest]['topics'])
    numbers=Counter()
    counts=Counter()
    operations=Counter()
    with (output/'records.jsonl').open('w',encoding='utf-8',newline='\n') as stream, (output/'coverage.jsonl').open('w',encoding='utf-8',newline='\n') as coverage:
        for (doc_id,digest),locators in components.items():
            image=results[digest]
            validate_checkpoint_result(image)
            counts['document_image_pairs']+=1
            counts[image['status']+'_document_images']+=1
            exported=0
            if image['status']=='sanitized' and topics[doc_id]:
                if cleaner.residuals(image['text']):
                    raise ValueError('ocr_export_residual_audit_failed')
                for part,text in enumerate(text_pieces(image['text'])):
                    numbers[doc_id]+=1
                    row={'entity_type':'KNOWLEDGE','source_id':f'{doc_id}_ocr_{digest[:24]}_{part}',
                        'title':f"{documents[doc_id]['format'].upper()} OCR 补充 {numbers[doc_id]}",
                        'text':text,'topics':sorted(topics[doc_id]),'file_format':documents[doc_id]['format'],
                        'extraction_method':'local_ocr','evidence_status':'untrusted_attachment_ocr',
                        'independently_verified':False,'parent_source_ids':[doc_id],'parent_target_type':'ATTACHMENT',
                        'metadata':{'document_id':doc_id,'pixel_sha256':digest,'component_locators':sorted(set(locators)),
                            'ocr_min_confidence':image['minimum_confidence'],'ocr_backend':'CPUExecutionProvider',
                            'ocr_audit':image['audit'],'source_coverage':'ocr_supplement',
                            'training_allowed':False,'manual_review_required':False,'external_transmission_allowed':False,
                            'mail_parent_source_ids':documents[doc_id]['parent_source_ids'],
                            'original_unit_ids_untouched':True,'part':part}}
                    stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                    counts['exported_units']+=1
                    exported+=1
            coverage.write(json.dumps({'document_id':doc_id,'pixel_sha256':digest,'component_locators':sorted(set(locators)),
                'status':image['status'],'exported_units':exported,'reason':image.get('reason'),
                'engineering_document':bool(topics[doc_id])})+'\n')
    for image in results.values():
        operations.update(image.get('privacy_operations',{}))
        counts['unique_images_'+image['status']]+=1
        counts['ocr_lines_accepted']+=image.get('recognized_lines',0)
        counts['low_confidence_lines_skipped']+=image.get('low_confidence_lines_skipped',0)
    if verified_sources()!=sources_before:
        raise ValueError('original_sources_changed_during_ocr')
    if clean_before is not None and verified_clean_base()!=clean_before:
        raise ValueError('clean_attachment_sources_changed_during_ocr')
    if archive_before is not None and snapshot([ARCHIVE])!=archive_before:
        raise ValueError('archive_changed_during_ocr')
    connection.commit()
    if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        raise ValueError('ocr_checkpoint_integrity_failed')
    report={'schema':'attachment-ocr-supplement-v1','status':'completed_local_candidates',
        'statistics':dict(counts),'scan_statistics':dict(stats),'privacy_operations':dict(operations),
        'source_snapshots':sources_before,'clean_source_snapshots':clean_before,
        'archive_snapshot':archive_before,'source_unchanged':True,'signature':signature,
        'inventory_report_sha256':digest_file(inventory_dir/'report.json'),
        'external_services':False,'original_candidates_unchanged':True,
        'limits':['OCR numbers are recognized strings, not independently verified engineering measurements.',
                  'Low-confidence lines are excluded; spaced digits are preserved without guessing.',
                  'Privacy conflicts with process expressions quarantine the image text.',
                  'Small images, vector media, decode failures and resource limits remain explicit coverage gaps.']}
    report['artifacts']={p.name:{'sha256':digest_file(p),'size_bytes':p.stat().st_size}
                         for p in output.iterdir() if p.is_file() and p.name not in ('report.json','progress.json','failure.json')}
    write_json(output/'manifest.json',{'source_name':'second_pst_ocr_supplement','source_type':'attachment',
        'source_version':output.name,'records':'records.jsonl','fingerprint':digest_file(output/'records.jsonl')})
    report['artifacts']['manifest.json']={'sha256':digest_file(output/'manifest.json'),'size_bytes':(output/'manifest.json').stat().st_size}
    write_json(output/'report.json',report)
    emit('ocr_supplement_completed',statistics=dict(counts))
    return report


def run(args):
    output,inventory_dir=Path(args.output).resolve(),Path(args.inventory).resolve()
    if not 1<=args.workers<=6:
        raise ValueError('invalid_ocr_worker_count')
    if output.exists() and (not args.resume or (output/'report.json').exists()):
        raise ValueError('output_already_exists_or_completed')
    inv=json.loads((inventory_dir/'report.json').read_text(encoding='utf-8'))
    if inv['status']!='completed' or digest_file(inventory_dir/'components.jsonl')!=inv['artifacts']['components.jsonl']['sha256']:
        raise ValueError('ocr_inventory_artifact_mismatch')
    before=verified_sources()
    if before!=inv['source_snapshots']:
        raise ValueError('ocr_inventory_sources_changed')
    clean_before=verified_clean_base()
    from scripts.data.derive_mail_candidates import build_identity_index,connect_archive
    archive_before=snapshot([ARCHIVE])
    emit('ocr_loading_memory_only_contact_index')
    with closing(connect_archive(ARCHIVE)) as con:
        identities=build_identity_index(con,ROOT/'.private/sensitive_terms.txt')
    import spacy
    disabled=['tagger','parser','attribute_ruler','lemmatizer','senter']
    models=[spacy.load(name,disable=disabled) for name in ('zh_core_web_sm','en_core_web_sm')]
    cleaner=OcrTextSanitizer(identities.terms,models)
    from rapidocr_onnxruntime import __file__ as rapidocr_file
    models_dir=Path(rapidocr_file).parent/'models'
    signature={'implementation':{name:digest_file(ROOT/name) for name in
        ('supplement_attachment_ocr.py','ocr_image_sources.py','ocr_candidate_privacy.py','candidate_privacy.py',
         'mail_privacy.py','scripts/data/rewash_candidate_identities.py','scripts/data/derive_mail_candidates.py')},
        'inventory_sha256':digest_file(inventory_dir/'components.jsonl'),
        'archive_sha256':archive_before[str(ARCHIVE)]['sha256'],
        'clean_source_snapshots':clean_before,
        'models':{p.name:digest_file(p) for p in models_dir.glob('*.onnx')},
        'packages':{name:importlib.metadata.version(name) for name in
                    ('rapidocr-onnxruntime','onnxruntime','spacy','zh-core-web-sm','en-core-web-sm')},
        'confidence_threshold':.65,'backend':'CPUExecutionProvider'}
    if len(signature['models'])!=3:
        raise ValueError('expected_local_ocr_models_missing')
    output.mkdir(parents=True,exist_ok=args.resume)
    con=sqlite3.connect(output/'checkpoint.sqlite3')
    con.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS images(pixel_sha256 TEXT PRIMARY KEY,result_json TEXT NOT NULL);')
    previous=con.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
    if previous and json.loads(previous[0])!=signature:
        con.close()
        raise ValueError('ocr_resume_signature_mismatch')
    con.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('signature',json.dumps(signature,sort_keys=True)))
    con.commit()
    inventory_map={(r['document_id'],r['locator']):r for r in read_rows(inventory_dir/'components.jsonl')}
    done={row[0] for row in con.execute('SELECT pixel_sha256 FROM images')}
    seen_components=set()
    pending={}
    active=set()
    stats=Counter()
    last=time.monotonic()
    started=time.monotonic()
    def progress():
        value={'status':'processing','unique_completed':len(done),'unique_total':inv['unique_eligible_images'],
            'pending':len(pending),'statistics':dict(stats),'elapsed_seconds':round(time.monotonic()-started)}
        write_json(output/'progress.json',value)
        emit('ocr_supplement_progress',**{k:v for k,v in value.items() if k!='statistics'})
    def consume(future):
        digest=pending.pop(future)
        raw=future.result()
        if raw['pixel_sha256']!=digest:
            raise ValueError('ocr_worker_identity_mismatch')
        image=sanitized_result(raw,cleaner)
        validate_checkpoint_result(image)
        con.execute('INSERT INTO images VALUES (?,?)',(digest,json.dumps(image,ensure_ascii=False)))
        con.commit()
        done.add(digest)
        active.remove(digest)
        stats['completed_'+image['status']]+=1
    try:
        progress()
        context=multiprocessing.get_context('spawn')
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers,mp_context=context,initializer=_init_ocr_worker) as pool:
            for document,content in source_documents(stats=stats):
                enumerator=ImageEnumerator()
                for component in enumerator.enumerate(content,document['format']):
                    key=(document['document_id'],component.locator)
                    expected=inventory_map.get(key)
                    digest=image_fingerprint(component.image)
                    if expected is None or expected['pixel_sha256']!=digest:
                        raise ValueError('source_image_changed_since_inventory')
                    seen_components.add(key)
                    if expected['status']!='eligible':
                        continue
                    if digest in done or digest in active:
                        stats['deduplicated_or_resumed_components']+=1
                        continue
                    pending[pool.submit(recognize_image,(digest,component.image.width,component.image.height,component.image.tobytes()))]=digest
                    active.add(digest)
                    while len(pending)>=args.workers*2:
                        complete,_=concurrent.futures.wait(pending,timeout=10,return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in complete:
                            consume(future)
                        if time.monotonic()-last>=15:
                            progress()
                            last=time.monotonic()
                if time.monotonic()-last>=15:
                    progress()
                    last=time.monotonic()
            while pending:
                complete,_=concurrent.futures.wait(pending,timeout=10,return_when=concurrent.futures.FIRST_COMPLETED)
                for future in complete:
                    consume(future)
                if time.monotonic()-last>=15:
                    progress()
                    last=time.monotonic()
        if seen_components!=set(inventory_map):
            raise ValueError('image_component_coverage_changed')
        if snapshot([ARCHIVE])!=archive_before:
            raise ValueError('archive_changed_during_ocr')
        report=publish_supplement(output,inventory_dir,con,cleaner,before,signature,stats,
                                  clean_before,archive_before)
        if (output/'failure.json').exists():
            write_json(output/'failure.json',{'status':'resolved_by_completed_run'})
        write_json(output/'progress.json',{'status':'completed','unique_completed':len(done),
            'unique_total':inv['unique_eligible_images'],'elapsed_seconds':round(time.monotonic()-started)})
        return report
    except Exception as exc:
        write_json(output/'failure.json',{'status':'incomplete_resumable','error_type':type(exc).__name__,
            'error_code':str(exc) if isinstance(exc,ValueError) else 'ocr_processing_failed'})
        raise
    finally:
        con.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['inventory','run'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inventory',type=Path,default=ROOT/'data/ocr_inventory_v1')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args()
    if args.command=='inventory':
        inventory(args.output)
    else:
        run(args)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        emit('ocr_supplement_failed',error_type=type(exc).__name__,
             error_code=str(exc) if isinstance(exc,ValueError) else 'processing_failed')
        raise SystemExit(1)
