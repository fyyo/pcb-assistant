"""Synthetic rejection checks for citation integrity and held-out isolation."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from scripts.public_data.validate_public_lamination import validate

def write(p,d):p.write_text(json.dumps(d))
def h(b):return hashlib.sha256(b).hexdigest()

class PublicDataIntegrity(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='pcb-public-test-');self.root=Path(self.tmp.name)
  (self.root/'raw').mkdir();self.manifest=[];self.pages=[];self.data={}
  for split,sid in [('train','a'),('evaluation','b')]:
   raw=('PDF '+sid).encode();(self.root/'raw'/sid).write_bytes(raw);text='A source statement '+sid
   self.manifest.append(dict(id=sid,raw_path='raw/'+sid,sha256=h(raw),source_group=sid,split=split))
   self.pages.append(dict(source_id=sid,page=1,text=text,sha256=h(text.encode())))
   evidence=dict(source_id=sid,page=1,document_sha256=h(raw),page_sha256=h(text.encode()),anchor_start=2,anchor_end=8,anchor_quote='source')
   r=dict(id=sid,split=split,source_group=sid,prompt='question '+sid,response='answer '+sid,rubric=['a','b','c'],preserve_complete_example=True,evidence=[evidence])
   if split=='evaluation':r.update(reference_context=text,prompt=text+' question',evaluation_mode='provided_reference_open_book')
   self.data[split]=[r]
  self.save()
 def tearDown(self):self.tmp.cleanup()
 def save(self):
  write(self.root/'manifest.json',self.manifest)
  (self.root/'pages.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in self.pages))
  for split,data in self.data.items():
   (self.root/(('train' if split=='train' else 'evaluation')+'_instruction.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in data))
  write(self.root/'report.json',{'train_tasks':1,'evaluation_tasks':1,'artifacts':{}})
 def test_valid_fixture(self):self.assertEqual(validate(self.root)['status'],'passed')
 def test_modified_original_rejected(self):
  (self.root/'raw/a').write_bytes(b'changed')
  with self.assertRaisesRegex(AssertionError,'raw_artifact_changed'):validate(self.root)
 def test_wrong_citation_offset_rejected(self):
  self.data['train'][0]['evidence'][0]['anchor_start']=3;self.save()
  with self.assertRaisesRegex(AssertionError,'citation_offset_mismatch'):validate(self.root)
 def test_shared_source_family_rejected(self):
  self.manifest[1]['source_group']='a';self.save()
  with self.assertRaisesRegex(AssertionError,'source_family_leak'):validate(self.root)
 def test_duplicate_target_rejected(self):
  self.data['evaluation'][0]['response']=self.data['train'][0]['response'];self.save()
  with self.assertRaisesRegex(AssertionError,'duplicate_target'):validate(self.root)
 def test_raw_context_cannot_enter_training(self):
  self.data['train'][0]['reference_context']='public document';self.save()
  with self.assertRaisesRegex(AssertionError,'raw_reference_in_training'):validate(self.root)

if __name__=='__main__':unittest.main()
