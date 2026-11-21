"""Assemble an immutable instruction-only curriculum with separated holdouts."""
import json,random,shutil
from pcb_assistant.training.storage import ROOT,read,fingerprint,verify_pointer
from pcb_assistant.training.data import verify_data
from pcb_assistant.training.common import digest_file,dump_json,emit

def rows(path):
 return [json.loads(l) for l in path.open() if l.strip()]
def write_rows(path,rs):path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rs))

def validate_isolation(train,evaluation,manifests):
 groups={};doc_splits={}
 for m in manifests:
  assert groups.setdefault(m['source_group'],m['split'])==m['split'],'family_split_conflict'
  assert doc_splits.setdefault(m['sha256'],m['split'])==m['split'],'document_split_conflict'
 ids=set();eval_ids={r['id'] for r in evaluation}
 for r in train:
  assert r['id'] not in ids and r['id'] not in eval_ids,'task_leak';ids.add(r['id'])
  assert r['split']=='train' and groups[r['source_group']]=='train','training_uses_holdout'
  for e in r['evidence']:assert groups[e['source_group']]=='train','evidence_uses_holdout'
 for r in evaluation:
  assert r['split']=='evaluation' and groups[r['source_group']]=='evaluation'
 assert not {r['response'] for r in train}&{r['response'] for r in evaluation},'duplicate_target'

def prepare():
 from transformers import AutoTokenizer
 from pcb_assistant.training.train import instruction_tokens
 foundation=read(ROOT/'data/continuous/foundation.json')
 seed=ROOT/'data'/foundation['prepared'];verify_data(seed)
 assert digest_file(seed/'report.json')==foundation['prepared_report_sha256']
 parent=read(ROOT/'data/current_model.json');verify_pointer(parent)
 public=[];evaluation=[];manifests=[];inputs={}
 for version in ('public_lamination_v1','public_lamination_v2'):
  directory=ROOT/'data'/version
  for name in ('train_instruction.jsonl','evaluation_instruction.jsonl','manifest.json'):
   inputs[version+'/'+name]=digest_file(directory/name)
  public+=rows(directory/'train_instruction.jsonl');evaluation+=rows(directory/'evaluation_instruction.jsonl')
  ms=read(directory/'manifest.json');manifests+=ms
  for m in ms:assert digest_file(directory/m['raw_path'])==m['sha256'],'source_changed'
  report=read(directory/'report.json')
  for name,h in report['artifacts'].items():
   assert digest_file(directory/name)==(h['sha256'] if isinstance(h,dict) else h),'curriculum_artifact_changed'
 validate_isolation(public,evaluation,manifests)
 assert len(public)==67 and len(evaluation)==23
 recipe={'public_unique_tasks':67,'replay_tasks':134,'instruction_epochs':2,'instruction_learning_rate':5e-6,
         'accumulation':4,'sequence_length':768,'public_evaluation_length':4096,'replay_seed':20261009,
         'scope':'full_new_curriculum_with_sampled_historical_replay','validation_batches':64}
 ident=fingerprint({'inputs':inputs,'parent':parent,'recipe':recipe,'builder':digest_file(__file__)})[:12]
 output=ROOT/'data'/('training_data_public_'+ident)
 if (output/'report.json').exists():verify_data(output);return output,read(output/'report.json')
 output.mkdir(exist_ok=True)
 t=AutoTokenizer.from_pretrained(ROOT/'models/Qwen2.5-0.5B-Instruct',local_files_only=True,trust_remote_code=False)
 pool=rows(seed/'train_instruction.jsonl');random.Random(recipe['replay_seed']).shuffle(pool)
 replay=[];skipped=0;chosen_groups=set()
 # Diverse source groups first, then fill deterministically if needed.
 for pass_no in (0,1):
  for r in pool:
   group=r.get('group_id',r.get('source_id'))
   if pass_no==0 and group in chosen_groups:continue
   if any(x.get('source_id')==r.get('source_id') and x['prompt']==r['prompt'] for x in replay):continue
   row={**r,'id':'replay_'+fingerprint(r)[:16],'training_role':'historical_replay','preserve_complete_example':True}
   try:instruction_tokens(t,row,768)
   except ValueError:
    skipped+=1;continue
   replay.append(row);chosen_groups.add(group)
   if len(replay)==recipe['replay_tasks']:break
  if len(replay)==recipe['replay_tasks']:break
 assert len(replay)==134
 train=[{**r,'training_role':'public_curriculum','preserve_complete_example':True} for r in public]+replay
 token_stats={}
 for label,rs,limit in [('training',train,768),('public_evaluation',evaluation,4096)]:
  lens=[len(instruction_tokens(t,r,limit)['input_ids']) for r in rs]
  token_stats[label]={'tasks':len(rs),'maximum_tokens':max(lens),'no_truncation':True}
 write_rows(output/'train_instruction.jsonl',train);write_rows(output/'public_evaluation_instruction.jsonl',evaluation)
 (output/'train_domain.jsonl').write_text('')
 for name in ('validation_domain.jsonl','validation_instruction.jsonl'):
  shutil.copyfile(seed/name,output/name)
 assert {r.get('group_id') for r in replay}.isdisjoint({r.get('group_id') for r in rows(seed/'validation_instruction.jsonl')}),'historical_group_leak'
 report={'status':'completed','source_artifact_sha256':fingerprint(inputs),'parent_model':parent,
         'public_curriculum':recipe,'inputs':inputs,'token_validation':token_stats,
         'historical_replay':{'selected':len(replay),'source_groups':len(chosen_groups),'skipped_overlength':skipped,'pool':len(pool)},
         'foundation':foundation,'artifacts':{p.name:{'sha256':digest_file(p)} for p in output.glob('*.jsonl')}}
 dump_json(output/'report.json',report)
 return output,report
if __name__=='__main__':
 p,r=prepare();emit('public_training_data_ready',data=p.name,recipe=r['public_curriculum'],token_validation=r['token_validation'])
