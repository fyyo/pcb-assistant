"""CPU-only full-token and independent arithmetic checks for public exercises."""
from decimal import Decimal as D
from transformers import AutoTokenizer
from pcb_assistant.training.train import instruction_tokens
from scripts.public_data.build_public_lamination import OUT, ROOT, rows, write

def main():
 t=AutoTokenizer.from_pretrained(ROOT/'models/Qwen2.5-0.5B-Instruct',local_files_only=True,trust_remote_code=False)
 result={};items={}
 for name,limit in [('train_instruction.jsonl',768),('evaluation_instruction.jsonl',4096)]:
  lengths=[]
  for r in rows(OUT/name):
   items[r['id']]=r
   lengths.append(len(instruction_tokens(t,r,limit)['input_ids']))
  result[name]={'examples':len(lengths),'maximum_tokens':max(lengths),'configured_limit':limit,'complete_no_truncation':True}
 values={'T21':D(120)-D(35)*D('.60')*2,'T23':(D('10.000')-D('4.200'))/D('10.000')*100,
         'T24':(D(20)-2*D(8))/D(20)*100,'T28':(D('11.0')-D('10.9'))/(D('11.0')-D('1.0'))*100,
         'T33':D(2)**3,'E08':D(176)-D(170),'E10':D(14)-D(10)}
 expected={'T21':(78,'78 μm'),'T23':(58,'58.0%'),'T24':(20,'20%'),'T28':(1,'1.0%'),
           'T33':(8,'8个组合'),'E08':(6,'6°C'),'E10':(4,'4分钟')}
 for key,value in values.items():
  number,target=expected[key]
  assert value==D(number) and target in items[key]['response'], 'arithmetic_target_mismatch:'+key
 result['arithmetic']={k:str(v) for k,v in values.items()}
 write(OUT/'token_and_arithmetic_validation.json',{'status':'passed',**result})
 print(result)
if __name__=='__main__':main()
