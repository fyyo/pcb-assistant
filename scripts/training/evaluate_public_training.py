"""Full-context public holdout evaluation plus unchanged historical checks."""
import argparse,gc
from pathlib import Path
from pcb_assistant.training.storage import read,fingerprint
from pcb_assistant.training.data import verify_data,rows
from pcb_assistant.training.evaluate import evaluate,decide
from pcb_assistant.training.common import ROOT,SYSTEM_PROMPT,digest_file,dump_json,emit

def public_evaluate(run,data,output):
 from pcb_assistant.training.inference import LearnedEngineer
 from pcb_assistant.training.train import instruction_tokens
 report=verify_data(data)
 signature=fingerprint({'model':digest_file(run/'report.json'),'inference':digest_file(run/'inference_config.json'),
                        'data':digest_file(data/'report.json'),'evaluator':digest_file(__file__)})
 cached=read(output)
 if cached and cached.get('signature')==signature:return cached
 engine=LearnedEngineer(run);torch=engine.torch
 if engine.device!='cuda':raise RuntimeError('public_evaluation_requires_gpu')
 engine.model.config.use_cache=False
 samples=rows(data/'public_evaluation_instruction.jsonl')
 total=0.;count=0;generations=[];loss_rows=[]
 for n,r in enumerate(samples,1):
  batch=instruction_tokens(engine.tokenizer,r,4096)
  ids=torch.tensor([batch['input_ids']],device=engine.device)
  labels=torch.tensor([batch['labels']],device=engine.device)
  targets=sum(x!=-100 for x in batch['labels'][1:])
  with torch.inference_mode():
   loss=engine.model(input_ids=ids,labels=labels,attention_mask=torch.ones_like(ids)).loss
  if not torch.isfinite(loss):raise ValueError('nonfinite_public_loss')
  total+=float(loss)*targets;count+=targets
  loss_rows.append({'id':r['id'],'loss':float(loss),'target_tokens':targets})
  messages=[{'role':'system','content':SYSTEM_PROMPT},{'role':'user','content':r['prompt'].strip()}]
  prefix=engine.tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True)
  if len(prefix)+224>4096:raise ValueError('evaluation_context_exceeds_limit')
  with torch.inference_mode():
   input_ids=torch.tensor([prefix],device=engine.device)
   generated=engine.model.generate(input_ids=input_ids,attention_mask=torch.ones_like(input_ids),max_new_tokens=224,
          do_sample=False,temperature=1.,top_p=1.,top_k=50,repetition_penalty=1.08,use_cache=True,pad_token_id=engine.tokenizer.eos_token_id)
  tokens=generated[0,len(prefix):]
  answer=engine.tokenizer.decode(tokens,skip_special_tokens=True)
  generations.append({'id':r['id'],'answer':answer,'generated_tokens':len(tokens),'hit_token_limit':len(tokens)==224,
                      'rubric':r['rubric'],'reference_answer':r['response']})
  if n%5==0 or n==len(samples):emit('public_evaluation_progress',run=run.name,completed=n,total=len(samples))
 result={'signature':signature,'run_name':run.name,'loss':total/count,'target_tokens':count,'tasks':len(samples),
         'per_task_loss':loss_rows,'mode':'provided_reference_open_book','full_prompt_used':True,
         'qualitative_scores':'pending_source_based_assessment; not keyword-only scores'}
 dump_json(output,result);dump_json(output.with_name(output.stem+'_answers.json'),generations)
 del engine,ids,labels,input_ids,generated,tokens,loss
 gc.collect();torch.cuda.empty_cache()
 return result

def compare(incumbent,candidate,data,foundation,output):
 before=evaluate(incumbent,foundation,data,output/'incumbent_evaluation.json')
 after=evaluate(candidate,foundation,data,output/'candidate_evaluation.json')
 old_public=public_evaluate(incumbent,data,output/'incumbent_public_evaluation.json')
 new_public=public_evaluate(candidate,data,output/'candidate_public_evaluation.json')
 before={**before,'losses':{**before['losses'],'new_public_instruction':old_public['loss']}}
 after={**after,'losses':{**after['losses'],'new_public_instruction':new_public['loss']}}
 decision=decide(before,after,read(ROOT/'continuous_policy.json'))
 decision.update(incumbent_run=incumbent.name,candidate_run=candidate.name,public_tasks=new_public['tasks'],
                 public_loss_before=old_public['loss'],public_loss_after=new_public['loss'],
                 public_generation_assessment_required_before_promotion=True)
 dump_json(output/'promotion_evaluation.json',decision);emit('public_candidate_compared',**decision)
 return decision
if __name__=='__main__':
 p=argparse.ArgumentParser()
 for arg in ['incumbent','candidate','data','foundation','output']:p.add_argument('--'+arg,type=Path,required=True)
 a=p.parse_args();compare(a.incumbent,a.candidate,a.data,a.foundation,a.output)
