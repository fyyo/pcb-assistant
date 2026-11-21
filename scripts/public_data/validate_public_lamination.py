"""Fail-closed provenance and split validation for the public study dataset."""
import hashlib
import json
from scripts.public_data.build_public_lamination import OUT, digest, read, rows, norm

def validate(out=OUT):
 manifest={r['id']:r for r in read(out/'manifest.json')}
 pages={(p['source_id'],p['page']):p for p in rows(out/'pages.jsonl')}
 train=list(rows(out/'train_instruction.jsonl'));evaluation=list(rows(out/'evaluation_instruction.jsonl'))
 assert train and evaluation
 groups={};ids=set();answers=set()
 for m in manifest.values():
  assert digest(out/m['raw_path'])==m['sha256'],'raw_artifact_changed'
  previous=groups.setdefault(m['source_group'],m['split'])
  assert previous==m['split'],'source_family_leak'
 for split,data in [('train',train),('evaluation',evaluation)]:
  for r in data:
   assert r['id'] not in ids,'duplicate_id';ids.add(r['id'])
   assert r['split']==split,'row_split_mismatch'
   assert norm(r['response']) not in answers,'duplicate_target';answers.add(norm(r['response']))
   assert r['preserve_complete_example'] is True
   assert len(r['rubric'])>=3
   for e in r['evidence']:
    p=pages[(e['source_id'],e['page'])];m=manifest[e['source_id']]
    assert m['split']==split and r['source_group']==m['source_group'],'evidence_split_leak'
    assert e['document_sha256']==m['sha256']
    assert e['page_sha256']==p['sha256']==hashlib.sha256(p['text'].encode()).hexdigest(),'page_changed'
    assert p['text'][e['anchor_start']:e['anchor_end']]==e['anchor_quote'],'citation_offset_mismatch'
    assert p['text'].strip(),'blank_source'
   if split=='train':
    assert 'reference_context' not in r and '参考资料（' not in r['prompt'],'raw_reference_in_training'
   else:
    assert r['reference_context'] in r['prompt'] and r['evaluation_mode']=='provided_reference_open_book'
    # E11 includes the adjacent page explicitly; all context is source-identical.
    assert all(pages[(e['source_id'],e['page'])]['text'] in r['reference_context'] for e in r['evidence'])
 report=read(out/'report.json')
 for name,s in report['artifacts'].items():assert digest(out/name)==s['sha256'],'output_artifact_changed'
 assert report['train_tasks']==len(train) and report['evaluation_tasks']==len(evaluation)
 return {'status':'passed','training_tasks':len(train),'evaluation_tasks':len(evaluation),'verified_citations':len(train)+len(evaluation),
         'raw_checksums':len(manifest),'training_source_groups':len({r['source_group'] for r in train}),
         'evaluation_source_groups':len({r['source_group'] for r in evaluation}),
         'page_spans_verified':True,'family_separation_verified':True,'historical_semantic_contamination_ruled_out':False}

if __name__=='__main__':
 result=validate()
 (OUT/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(result))
