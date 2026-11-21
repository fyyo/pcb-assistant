"""Read-only verification of local OCR coverage, privacy and derivative catalogs."""
from collections import Counter,defaultdict
from contextlib import closing
import argparse
import json
from pathlib import Path
import sqlite3

from pcb_assistant.catalog.common import ROOT,digest_file,stable_id,connect_readonly
from scripts.data.derive_mail_candidates import build_identity_index,connect_archive,write_json
from pcb_assistant.privacy.ocr import OcrTextSanitizer
from scripts.data.rewash_candidate_identities import read_rows,snapshot
from pcb_assistant.ingestion.ocr import ARCHIVE,validate_checkpoint_result,text_pieces,emit


def require(condition,code):
    if not condition:
        raise ValueError(code)


def verify(output,inventory,catalog=None,base=None):
    report=json.loads((output/'report.json').read_text(encoding='utf-8'))
    require(report['status']=='completed_local_candidates','ocr_run_not_completed')
    for name,artifact in report['artifacts'].items():
        require(digest_file(output/name)==artifact['sha256'],'ocr_artifact_mismatch')
    for key in ('source_snapshots','clean_source_snapshots','archive_snapshot'):
        expected=report[key]
        require(snapshot([Path(p) for p in expected])==expected,'ocr_sources_changed')
    baseline_path=ROOT/'docs/OCR_BASELINE_SNAPSHOTS_20261115.json'
    if not baseline_path.exists():
        baseline_path=ROOT/'.private/project_records/docs/OCR_BASELINE_SNAPSHOTS_20261115.json'
    baseline=json.loads(baseline_path.read_text(encoding='utf-8'))['snapshots']
    require(snapshot([Path(p) for p in baseline])==baseline,'original_catalog_files_changed')
    inv=json.loads((inventory/'report.json').read_text(encoding='utf-8'))
    require(digest_file(inventory/'report.json')==report['inventory_report_sha256'],'inventory_report_changed')
    require(digest_file(inventory/'components.jsonl')==inv['artifacts']['components.jsonl']['sha256'],
            'inventory_components_changed')
    components=defaultdict(set)
    total=0
    for row in read_rows(inventory/'components.jsonl'):
        total+=1
        if row['status']=='eligible':
            components[(row['document_id'],row['pixel_sha256'])].add(row['locator'])
    with closing(sqlite3.connect((output/'checkpoint.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
        con.execute('PRAGMA query_only=ON')
        require(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok','checkpoint_integrity_failed')
        images={r[0]:json.loads(r[1]) for r in con.execute('SELECT pixel_sha256,result_json FROM images')}
    require(set(images)=={digest for _,digest in components},'unique_coverage_mismatch')
    require(len(images)==inv['unique_eligible_images'],'unique_image_count_mismatch')
    emit('ocr_verification_loading_contact_index',unique_images=len(images))
    with closing(connect_archive(ARCHIVE)) as con:
        index=build_identity_index(con,ROOT/'.private/sensitive_terms.txt')
    cleaner=OcrTextSanitizer(index.terms)
    statuses=Counter()
    for digest,image in images.items():
        require(image['pixel_sha256']==digest,'checkpoint_image_identity_mismatch')
        validate_checkpoint_result(image)
        statuses[image['status']]+=1
        if image['status']=='sanitized':
            require(not cleaner.residuals(image['text']),'checkpoint_identity_residual')
            require(image['audit']['ocr_values_independently_verified'] is False,'ocr_values_overclaimed')
        else:
            require('text' not in image,'rejected_image_text_persisted')
    records=list(read_rows(output/'records.jsonl'))
    grouped=defaultdict(list)
    ids=set()
    for row in records:
        meta=row['metadata']
        pair=(meta['document_id'],meta['pixel_sha256'])
        require(pair in components,'exported_image_not_in_inventory')
        require(row['source_id'] not in ids,'duplicate_ocr_source_id')
        ids.add(row['source_id'])
        require(row['parent_source_ids']==[pair[0]] and row['parent_target_type']=='ATTACHMENT',
                'ocr_attachment_parent_mismatch')
        require(set(meta['component_locators'])==components[pair],'ocr_locator_coverage_mismatch')
        require(row['extraction_method']=='local_ocr' and row['independently_verified'] is False,
                'invalid_ocr_evidence_status')
        require(meta['training_allowed'] is False and meta['external_transmission_allowed'] is False,
                'invalid_ocr_release_policy')
        require(meta['ocr_audit']==images[pair[1]]['audit'],'ocr_record_audit_mismatch')
        grouped[pair].append(row)
    coverage=list(read_rows(output/'coverage.jsonl'))
    require(len(coverage)==len(components),'document_image_pair_count_mismatch')
    require({(r['document_id'],r['pixel_sha256']) for r in coverage}==set(components),
            'document_image_pair_coverage_mismatch')
    expected_records=0
    for row in coverage:
        pair=(row['document_id'],row['pixel_sha256'])
        image=images[pair[1]]
        require(row['status']==image['status'],'coverage_status_mismatch')
        require(set(row['component_locators'])==components[pair],'coverage_locator_mismatch')
        exported=sorted(grouped.get(pair,[]),key=lambda r:r['metadata']['part'])
        expected=text_pieces(image['text']) if image['status']=='sanitized' and row['engineering_document'] else []
        require([r['text'] for r in exported]==expected,'ocr_exported_text_or_numbers_changed')
        require(len(exported)==row['exported_units'],'coverage_export_count_mismatch')
        expected_records+=len(expected)
    require(expected_records==len(records)==report['statistics']['exported_units'],'ocr_export_count_mismatch')
    result={'schema':'ocr-independent-verification-v1','status':'passed',
        'components':total,'unique_images':len(images),'unique_image_statuses':dict(statuses),
        'document_image_pairs':len(components),'exported_units':len(records),
        'exported_attachment_documents':len({p[0] for p in grouped}),
        'identity_rule_residuals':0,'numeric_and_code_hashes_verified':True,
        'ocr_values_independently_verified':False,'all_original_catalog_files_unchanged':True,
        'original_source_files_unchanged':True,'external_services':False,
        'inventory_gaps':inv['statistics'],'catalog':None}
    if catalog:
        from pcb_assistant.catalog.cli import validate,growth
        validation=validate(catalog)
        require(validation['status']=='passed','candidate_catalog_validation_failed')
        with closing(connect_readonly(catalog)) as con:
            con.execute('ATTACH DATABASE ? AS previous',(f'file:{(base/"catalog.sqlite3").as_posix()}?mode=ro',))
            preserved={}
            for table in ('documents','contents','relationships','source_map','unresolved_references','excluded_records'):
                # All old fields must remain byte-for-byte identical, not just IDs.
                missing=con.execute(f'SELECT COUNT(*) FROM (SELECT * FROM previous.{table} EXCEPT SELECT * FROM main.{table})').fetchone()[0]
                require(missing==0,'previous_catalog_rows_changed')
                preserved[table]=con.execute(f'SELECT COUNT(*) FROM previous.{table}').fetchone()[0]
            expected_doc_ids={stable_id('KNOWLEDGE','attachment',r['source_id']) for r in records}
            added={r[0] for r in con.execute('SELECT doc_id FROM main.documents EXCEPT SELECT doc_id FROM previous.documents')}
            require(added==expected_doc_ids,'new_catalog_document_id_mismatch')
            mapped={r['source_id']:r['doc_id'] for r in con.execute(
                'SELECT source_id,doc_id FROM source_map WHERE source_version=?',(output.name,))}
            for row in records:
                doc_id=stable_id('KNOWLEDGE','attachment',row['source_id'])
                require(mapped.get(row['source_id'])==doc_id,'ocr_catalog_source_map_mismatch')
                actual=con.execute('SELECT d.*,c.original_text FROM documents d JOIN contents c USING(content_id) WHERE doc_id=?',
                                   (doc_id,)).fetchone()
                require(actual['original_text']==row['text'] and actual['original_title']==row['title'],
                        'ocr_catalog_text_changed')
                parent=stable_id('ATTACHMENT',row['parent_source_ids'][0])
                require(con.execute('SELECT COUNT(*) FROM relationships WHERE from_id=? AND to_id=? AND relation=?',
                                    (doc_id,parent,'EXTRACTED_FROM')).fetchone()[0]==1,'ocr_catalog_parent_link_missing')
            counts={t:con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in preserved}
            require(counts['relationships']==preserved['relationships']+len(records),'unexpected_catalog_relations')
            require(counts['unresolved_references']==preserved['unresolved_references'],'new_missing_parent_references')
        reconciliation=growth(catalog)
        require(reconciliation['reconciliation_status']=='matched','catalog_growth_reconciliation_failed')
        ingestion=json.loads((catalog/'ingestion.json').read_text(encoding='utf-8'))
        result['catalog']={'validation':validation,'preserved_rows':preserved,'totals':counts,
                           'version':ingestion['version'],'batch':ingestion['batch'],
                           'growth_reconciliation':'matched'}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'data/attachments_ocr_v1')
    parser.add_argument('--inventory',type=Path,default=ROOT/'data/ocr_inventory_v1')
    parser.add_argument('--catalog',type=Path)
    parser.add_argument('--base',type=Path,default=ROOT/'data/phase3_catalog_v8')
    parser.add_argument('--report',type=Path,default=ROOT/'docs/OCR_SUPPLEMENT_VERIFICATION_20261115.json')
    args=parser.parse_args()
    result=verify(args.source.resolve(),args.inventory.resolve(),args.catalog.resolve() if args.catalog else None,
                  args.base.resolve())
    write_json(args.report,result)
    emit('ocr_independent_verification_passed',exported_units=result['exported_units'],unique_images=result['unique_images'])


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        emit('ocr_independent_verification_failed',error_type=type(exc).__name__,
             error_code=str(exc) if isinstance(exc,ValueError) else 'verification_failed')
        raise SystemExit(1)
