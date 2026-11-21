"""Synthetic source isolation checks before real public-curriculum optimization."""
import copy,unittest
from scripts.training.prepare_public_training import validate_isolation
class PublicTrainingIsolation(unittest.TestCase):
 def setUp(self):
  self.manifests=[dict(source_group='teach',split='train',sha256='a'),dict(source_group='hold',split='evaluation',sha256='b')]
  self.train=[dict(id='T',split='train',source_group='teach',response='grounded answer',evidence=[dict(source_group='teach')])]
  self.evaluation=[dict(id='E',split='evaluation',source_group='hold',response='held-out answer')]
 def check(self):validate_isolation(self.train,self.evaluation,self.manifests)
 def test_isolated_sources_accepted(self):self.check()
 def test_holdout_evidence_cannot_be_trained(self):
  self.train[0]['evidence'][0]['source_group']='hold'
  with self.assertRaisesRegex(AssertionError,'evidence_uses_holdout'):self.check()
 def test_duplicate_task_rejected(self):
  self.train[0]['id']='E'
  with self.assertRaisesRegex(AssertionError,'task_leak'):self.check()
 def test_relabelled_document_rejected(self):
  self.manifests[1]['sha256']='a'
  with self.assertRaisesRegex(AssertionError,'document_split_conflict'):self.check()
 def test_copied_evaluation_target_rejected(self):
  self.train[0]['response']='held-out answer'
  with self.assertRaisesRegex(AssertionError,'duplicate_target'):self.check()
if __name__=='__main__':unittest.main()
