"""Synthetic checks for cross-version source protection and honest citations."""
import copy,hashlib,unittest
from scripts.public_data.build_public_lamination_v2 import locate,validate_records
class IntegrityV2(unittest.TestCase):
 def setUp(self):
  self.manifest={};self.units={};self.data={}
  for sid,split in [('a','train'),('b','evaluation')]:
   text='Some\n  exact evidence '+sid;sha=hashlib.sha256(text.encode()).hexdigest();a,b=locate(text,'exact evidence')
   self.manifest[sid]={'source_group':sid,'split':split,'sha256':'raw-'+sid}
   self.units[(sid,1)]={'source_group':sid,'split':split,'sha256':sha,'text':text}
   evidence={'source_id':sid,'source_group':sid,'unit':1,'document_sha256':'raw-'+sid,'unit_sha256':sha,'anchor_start':a,'anchor_end':b,'anchor_quote':text[a:b]}
   r={'id':sid,'split':split,'source_group':sid,'prompt':'q','response':'answer '+sid,'rubric':['a','b','c'],'evidence':[evidence]}
   if split=='evaluation':r.update(context_span=[0,len(text)],reference_context=text,prompt=text+' q')
   self.data[split]=[r]
  self.prior={'old':{'source_group':'protected','split':'evaluation','sha256':'oldhash'}}
 def check(self):return validate_records(self.data['train'],self.data['evaluation'],self.manifest,self.units,self.prior)
 def test_valid(self):self.assertEqual(self.check()['status'],'passed')
 def test_original_whitespace_offsets(self):
  text='first\n  second';a,b=locate(text,'first second');self.assertEqual(text[a:b],text)
 def test_prior_heldout_family_cannot_become_training(self):
  self.manifest['a']['source_group']='protected'
  with self.assertRaisesRegex(AssertionError,'cross_version_source_family_leak'):self.check()
 def test_prior_heldout_bytes_cannot_be_renamed_into_training(self):
  self.manifest['a']['sha256']='oldhash'
  with self.assertRaisesRegex(AssertionError,'cross_version_document_leak'):self.check()
 def test_citation_tampering_rejected(self):
  self.data['train'][0]['evidence'][0]['anchor_quote']='invented'
  with self.assertRaisesRegex(AssertionError,'citation_changed'):self.check()
 def test_evaluation_context_tampering_rejected(self):
  self.data['evaluation'][0]['reference_context']='invented'
  with self.assertRaisesRegex(AssertionError,'context_changed'):self.check()
 def test_wrong_row_family_rejected(self):
  self.data['train'][0]['source_group']='wrong'
  with self.assertRaisesRegex(AssertionError,'row_group_mismatch'):self.check()
if __name__=='__main__':unittest.main()
