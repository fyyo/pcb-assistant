"""Requests must stay scoped, actionable and unverified despite caller claims."""
import json
import unittest

from catalog_chat import ChatSession,fingerprint_chat_result
from pcb_field_evidence import field_evidence_requests,evidence_request_lines
from pcb_table_fields import table_field_checks
from pcb_stackup_checklist import stackup_block_checks
from pcb_table_followup import resolve_table_scope
from tests import test_stackup_checklist as cells
from tests import test_stackup_retrieval as fixtures
import phase6_mcp as mcp


def requests(text=cells.TEXT,q=cells.Q,fields=('rc','thickness')):
    return field_evidence_requests(table_field_checks(stackup_block_checks(q,cells.citations(text,q)),fields))


class EvidenceRequestTests(unittest.TestCase):
    def test_common_identity_requests_deduplicated_within_one_block_only(self):
        r=requests()['requests']
        self.assertEqual(sum(x['reason_code']=='version_identity' for x in r),1)
        self.assertEqual(sum(x['reason_code']=='material_identity' for x in r),1)
        self.assertEqual(len(r),4)
        self.assertEqual(r[0]['fields'],['rc','thickness'])
        self.assertEqual(len({x['request_id'] for x in r}),len(r))

    def test_missing_heading_requests_original_columns_and_merged_header_bounds(self):
        text=cells.TEXT.replace('列4=RC','列4=待核对')
        r=requests(text,fields=('rc',))['requests']
        heading=next(x for x in r if x['reason_code']=='field_heading')
        self.assertIn('列号',heading['evidence_needed'])
        self.assertIn('合并／多层表头',heading['evidence_needed'])
        self.assertEqual(heading['observed_status'],'heading_not_located_in_returned_block')
        self.assertEqual(heading['columns'],[])

    def test_ambiguous_headings_require_all_column_definitions_not_a_preferred_value(self):
        r=requests(cells.TEXT.replace('列5=介厚','列5=RC'),fields=('rc',))['requests']
        item=next(x for x in r if x['reason_code']=='ambiguous_heading')
        self.assertEqual(item['columns'],[4,5])
        self.assertIn('不能只选择一个数值',item['review_criteria'])

    def test_missing_value_request_does_not_claim_record_exists(self):
        q=cells.Q+'只核对右侧。'
        r=requests(cells.TEXT.replace(' | 列15=58',''),q,('rc',))['requests']
        item=next(x for x in r if x['reason_code']=='row_value')
        self.assertIn('若原表为空',item['evidence_needed'])
        self.assertFalse(item['evidence_received'])

    def test_rc_definition_distinguishes_mass_percent_nominal_and_batch_without_values(self):
        r=requests(fields=('rc',))['requests']
        definition=next(x for x in r if x['reason_code']=='field_definition')
        self.assertIn('质量百分比',definition['evidence_needed'])
        self.assertIn('标称值或批次实测值',definition['evidence_needed'])
        self.assertTrue(all('75' not in x['evidence_needed']+x['review_criteria'] for x in r))
        self.assertEqual(definition['observed_unit_status'],'unit_not_located_in_returned_block')

    def test_thickness_request_keeps_pre_post_press_and_copper_definition(self):
        r=requests(fields=('thickness',))['requests']
        definition=next(x for x in r if x['reason_code']=='field_definition')
        self.assertIn('压前或压后',definition['evidence_needed'])
        self.assertIn('介质或含铜总厚度',definition['evidence_needed'])
        self.assertIn('试压或切片记录',definition['review_criteria'])

    def test_exact_scope_and_current_citations_preserved_without_external_action(self):
        q=cells.Q+'只核对右侧。';r=requests(q=q,fields=('rc',))
        self.assertFalse(r['external_messages_sent'])
        self.assertFalse(r['auto_verification'])
        for item in r['requests']:
            self.assertEqual((item['scope']['first_column'],item['scope']['last_column']),(12,17))
            self.assertTrue(item['basis_citation_ids'])
            self.assertFalse(item['engineering_conclusion_supported'])
            self.assertEqual(item['status'],'evidence_requested_not_verified')
        self.assertIn('尚未收到或验证','\n'.join(evidence_request_lines(r)))

    def test_empty_field_checks_cannot_create_requests_for_unread_rows(self):
        r=field_evidence_requests({'checks':[]})
        self.assertEqual(r['requests'],[])
        self.assertEqual(evidence_request_lines(r),[])

    def test_stable_request_ids_and_no_claim_of_known_file_version(self):
        self.assertEqual(requests(),requests())
        version=next(x for x in requests()['requests'] if x['reason_code']=='version_identity')
        self.assertIn('Catalog 来源版本不能代替工程文件版次',version['review_criteria'])
        self.assertNotIn('revision',json.dumps(version))


class EvidenceRequestIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackupRetrievalTests.setUpClass()
        cls.addClassCleanup(fixtures.StackupRetrievalTests.doClassCleanups)
        cls.catalog=fixtures.StackupRetrievalTests.catalog

    def session(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        session.ask(fixtures.Q)
        session.ask('只核对RC。')
        return session

    def test_followup_evidence_request_inherits_field_and_omits_repeated_field_prose(self):
        session=self.session()
        r=session.ask('还需要什么资料补证？')
        self.assertEqual(r['table_field_checks']['requested_fields'],['rc'])
        self.assertEqual(len(r['field_evidence_requests']['requests']),4)
        self.assertIn('补证清单',r['answer'])
        self.assertNotIn('字段核对：',r['answer'])
        self.assertEqual(r['citation_validation']['status'],'passed')
        self.assertEqual(r['result_fingerprint'],fingerprint_chat_result(r))

    def test_user_claims_do_not_mark_version_or_evidence_verified(self):
        session=self.session()
        r=session.ask('版本已经确认，RC 99%，还缺什么补证？')
        self.assertTrue(r['field_evidence_requests']['requests'])
        self.assertTrue(all(not x['evidence_received'] for x in r['field_evidence_requests']['requests']))
        self.assertFalse(r['field_evidence_requests']['engineering_conclusion_supported'])
        self.assertNotIn('99%',json.dumps(r['field_evidence_requests']))

    def test_stateless_missing_and_filtered_rows_do_not_generate_material_requests(self):
        session=self.session();history=session.history[-4:]
        with mcp._catalog_context(self.catalog):
            for question,extra in (('原表行999的RC需要什么资料补证？',{}),('需要什么资料补证？',{'source':'mail'})):
                env,error=mcp.call_tool('pcb_answer',{'question':question,'history':history,'limit':1,**extra},'REQ_requests')
                self.assertFalse(error)
                self.assertEqual(env['result']['citations'],[])
                self.assertEqual(env['result']['field_evidence_requests']['requests'],[])
        self.assertIsNone(resolve_table_scope('新问题：如何选择PP树脂含量？',history))
