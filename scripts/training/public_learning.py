"""Run the authorized public curriculum through training and isolated evaluation."""
from pcb_assistant.paths import python_executable
import argparse,subprocess,threading,time,os
from pcb_assistant.training.storage import ROOT,lock,read,verify_pointer,training_gpu,fingerprint
from pcb_assistant.training.worker import update_state
from pcb_assistant.training.common import dump_json,digest_file,emit

JOB=ROOT/'data/continuous/public_training_job.json'
def execute(command):
 with (ROOT/'data/continuous/public_training.log').open('ab') as log:
  subprocess.run(command,cwd=ROOT,stdout=log,stderr=log,check=True)

def run():
 from scripts.training.prepare_public_training import prepare
 with lock('public_curriculum_runner'),lock('learning_cycle',blocking=True):
  if read(ROOT/'continuous_policy.json').get('paused_by_user'):raise RuntimeError('paused_by_user')
  stop=threading.Event()
  def beat():
   while not stop.wait(30):update_state()
  thread=threading.Thread(target=beat,daemon=True);thread.start()
  try:
   update_state(state='preparing_public_curriculum',active_task='public_curriculum')
   data,report=prepare();parent=report['parent_model'];parent_run=verify_pointer(parent)
   ident=fingerprint({'data':digest_file(data/'report.json'),'parent':parent,'trainer':digest_file(ROOT / 'pcb_assistant/training/train.py')})[:12]
   output=ROOT/'data'/('model_training_public_'+ident)
   prior=read(JOB,{})
   if prior.get('run_name')==output.name and prior.get('state') in ('rejected','promoted','awaiting_generation_assessment'):
    emit('public_training_already_finished',**prior);return
   job={'run_name':output.name,'data_run':data.name,'parent':parent,'state':'prepared','recipe':report['public_curriculum'],'started_at':time.time()}
   dump_json(JOB,job)
   if read(ROOT/'data/current_model.json')!=parent:raise ValueError('parent_changed')
   py=python_executable('.venv-train', ROOT)
   with training_gpu():
    if not (output/'report.json').exists():
     job['state']='training';dump_json(JOB,job)
     update_state(state='training',active_task='public_curriculum',candidate_run=output.name)
     command=[py,'-m', 'pcb_assistant.training.train','--data',str(data),'--run-name',output.name,'--initial-run',str(parent_run),
              '--domain-steps','0','--instruction-epochs','2','--sequence-length','768','--accumulation','4','--validation-batches','64','--instruction-learning-rate','0.000005']
     if (output/'run_config.json').exists():command.append('--resume')
     execute(command)
    result=read(output/'report.json')
    assert result['optimizer_updates']>0 and result['changed_parameter_tensors']>0
    job['state']='comparing';dump_json(JOB,job);update_state(state='comparing',active_task='public_curriculum')
    execute([py,'-m','scripts.training.evaluate_public_training','--incumbent',str(parent_run),'--candidate',str(output),
             '--data',str(data),'--foundation',str(ROOT/'data'/report['foundation']['prepared']),'--output',str(output)])
   decision=read(output/'promotion_evaluation.json')
   job.update(state='awaiting_generation_assessment' if decision['accepted'] else 'rejected',decision=decision,
              completed_at=time.time(),optimizer_updates=result['optimizer_updates'],trained_tokens=result['trained_tokens'])
   dump_json(JOB,job)
   update_state(state='waiting_for_new_data',active_task=None,public_curriculum_result=job['state'])
   emit('public_training_finished',state=job['state'],run_name=output.name)
  except Exception as exc:
   job=read(JOB,{})
   job.update(state='error',error_type=type(exc).__name__);dump_json(JOB,job)
   update_state(state='public_curriculum_error',error_type=type(exc).__name__,active_task=None)
   raise
  finally:stop.set();thread.join(timeout=2)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['run','status']);a=p.parse_args();os.umask(0o077)
 if a.command=='run':run()
 else:
  j=read(JOB,{});emit('public_training_status',job=j,progress=read(ROOT/'data'/j.get('run_name','missing')/'progress.json',{}))
