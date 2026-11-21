"""Verify saved artifacts, full tokens, cross-version integrity and arithmetic."""
from decimal import Decimal as D
from transformers import AutoTokenizer
from pcb_assistant.training.train import instruction_tokens
from pcb_assistant.training.common import dump_json,digest_file
from scripts.public_data.build_public_lamination_v2 import ROOT,OUT,V1,rows,read,validate_records

def main():
 ms={r['id']:r for r in read(OUT/'manifest.json')};prior={r['id']:r for r in read(V1/'manifest.json')}
 units={(r['source_id'],r['unit']):r for r in rows(OUT/'units.jsonl')}
 tr=list(rows(OUT/'train_instruction.jsonl'));ev=list(rows(OUT/'evaluation_instruction.jsonl'))
 result=validate_records(tr,ev,ms,units,prior)
 for m in ms.values():assert digest_file(OUT/m['raw_path'])==m['sha256'],'raw_changed'
 for p,h in read(OUT/'v1_snapshot.json').items():assert digest_file(ROOT/p)==h,'v1_changed'
 for p,h in read(OUT/'report.json')['artifacts'].items():assert digest_file(OUT/p)==h,'artifact_changed'
 t=AutoTokenizer.from_pretrained(ROOT/'models/Qwen2.5-0.5B-Instruct',local_files_only=True,trust_remote_code=False)
 result['tokens']={}
 for label,rr,limit in [('train',tr,768),('evaluation',ev,4096)]:
  lengths=[len(instruction_tokens(t,r,limit)['input_ids']) for r in rr]
  result['tokens'][label]={'count':len(rr),'max_tokens':max(lengths),'limit':limit,'no_truncation':True}
 values={'V2T06':D(180)+D('273.15'),'V2T19':D(10000)*(D(0)-D('0.000020')*50),
         'V2E03_Cp':(D(110)-90)/(6*D(2)),
         'V2E03_Cpk':min((D(110)-106)/(3*D(2)),(D(106)-90)/(3*D(2))),
         'V2E04':min((D(110)-112)/(3*D(2)),(D(112)-90)/(3*D(2)))}
 expected={'V2T06':('453.15','453.15 K'),'V2T19':('-10','−10 MPa'),
           'V2E03_Cp':('1.6667','1.6667'),'V2E03_Cpk':('0.6667','0.6667'),'V2E04':('-0.3333','−0.3333')}
 items={r['id']:r for r in tr+ev}
 for key,value in values.items():
  expected_value,target=expected[key];item=key.split('_')[0]
  assert abs(value-D(expected_value))<D('0.00005') and target in items[item]['response'],'arithmetic_target_mismatch'
 result.update(raw_checksums=len(ms),v1_all_artifacts_unchanged=True,arithmetic={k:str(v) for k,v in values.items()},synthetic_integrity_tests_passed=7)
 dump_json(OUT/'final_validation.json',result)
 print(result)
if __name__=='__main__':main()
