"""Field follow-ups preserve exact table scope without filling missing values."""
import json
import unittest
from unittest.mock import Mock

from catalog_chat import ChatSession,fingerprint_chat_result
from pcb_table_fields import requested_table_fields,focused_fields,table_field_checks,field_check_lines
from pcb_table_followup import resolve_table_scope
from pcb_stackup_checklist import stackup_block_checks
from tests import test_stackup_checklist as cells
from tests import test_stackup_retrieval as fixtures
import phase6_mcp as mcp

DOC='KNOWLEDGE_'+'a'*32
LOC='核对定位：来源 '+DOC+'；原表行29；区块列1–11。'


class TableFieldTests(unittest.TestCase):
    def test_field_names_handle_chinese_traditional_and_english_without_code_digits(self):
        for q in ('只核对RC','只核對樹脂含量','含胶量是多少？','R/C单位呢？'):
            self.assertEqual(requested_table_fields(q),('rc',))
        for q in ('介厚是多少？','介質厚度单位呢？','dielectric thickness?'):
            self.assertEqual(requested_table_fields(q),('thickness',))
        self.assertEqual(requested_table_fields('核对2116_RC75_ZZ-170材料代码'),())
        self.assertEqual(requested_table_fields('核对铜厚、残铜率与布型'),('glass','copper','coverage'))

    def test_units_followup_inherits_field_but_explicit_field_replaces_it(self):
        previous=LOC+'\n字段定位：rc。'
        self.assertEqual(focused_fields('单位呢？',previous),('rc',))
        self.assertEqual(focused_fields('只核对介厚。',previous),('thickness',))
        scope=resolve_table_scope('只核对RC。',[{'role':'assistant','content':LOC}])
        self.assertEqual(scope['fields'],['rc'])
        self.assertEqual(scope['row'],29)

    def test_new_question_drops_field_scope_and_does_not_use_material_code_as_field_request(self):
        h=[{'role':'assistant','content':LOC+'\n字段定位：rc。'}]
        self.assertIsNone(resolve_table_scope('新问题：如何选择PP树脂含量？',h))
        self.assertIsNone(resolve_table_scope('如何选择PP树脂含量？',h))
        self.assertIsNone(resolve_table_scope('核对2116_RC75_ZZ-170代码',h))

    def test_duplicate_field_headings_are_all_returned_without_choosing_a_value(self):
        text=cells.TEXT.replace('列5=介厚','列5=RC')
        checks=stackup_block_checks(cells.Q,cells.citations(text))
        result=table_field_checks(checks,('rc',))
        c=result['checks'][0]
        self.assertEqual([col['column'] for col in c['cells']],[4,5])
        self.assertEqual(c['status'],'ambiguous_headings_require_review')
        self.assertIn('未挑选其中一个值作为答案','\n'.join(field_check_lines(result)))
        self.assertFalse(c['engineering_conclusion_supported'])

    def test_missing_unit_does_not_borrow_from_another_block(self):
        checks=stackup_block_checks(cells.Q,cells.citations())
        result=table_field_checks(checks,('rc','thickness'))
        self.assertEqual(len(result['checks']),2)
        self.assertTrue(all(c['unit_status']=='unit_not_located_in_returned_block' for c in result['checks']))
        self.assertTrue(all(col['column']<=11 for c in result['checks'] for col in c['cells']))
        text='\n'.join(field_check_lines(result))
        self.assertIn('“75”',text)
        self.assertNotIn('75%',text)
        self.assertNotIn('0.20',text)

    def test_header_unit_is_literal_evidence_and_missing_value_stays_missing(self):
        text=cells.TEXT.replace(' | 列15=58','')
        q=cells.Q+'只核对右侧。'
        result=table_field_checks(stackup_block_checks(q,cells.citations(text,q)),('rc',))
        c=result['checks'][0]
        self.assertEqual(c['status'],'value_not_present_in_returned_row')
        self.assertEqual(c['unit_status'],'literal_unit_requires_review')
        rendered='\n'.join(field_check_lines(result))
        self.assertIn('未返回该列值',rendered)
        self.assertIn('“RC(%)”',rendered)
        self.assertFalse(c['column_semantics_verified'])

    def test_no_header_does_not_parse_rc_from_material_code(self):
        text='行2 | 列1=層別 | 列2=材料 | 列12=層別 | 列13=材料\n行29 | 列1=PP | 列2=2116_RC75_ZZ-170 | 列12=PP | 列13=1080_RC58_ZZ-170'
        q=cells.Q+'只核对左侧。'
        c=table_field_checks(stackup_block_checks(q,cells.citations(text,q)),('rc',))['checks'][0]
        self.assertEqual(c['cells'],[])
        self.assertEqual(c['status'],'heading_not_located_in_returned_block')
        self.assertEqual(c['unit_evidence'],[])


class FieldFollowupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackupRetrievalTests.setUpClass()
        cls.addClassCleanup(fixtures.StackupRetrievalTests.doClassCleanups)
        cls.catalog=fixtures.StackupRetrievalTests.catalog

    def session(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        initial=session.ask(fixtures.Q)
        return session,initial

    def test_stateful_field_then_units_then_another_field_preserves_scope(self):
        session,initial=self.session()
        doc=initial['citations'][0]['doc_id']
        rc=session.ask('只核对RC。')
        self.assertEqual(rc['table_field_checks']['requested_fields'],['rc'])
        self.assertEqual(rc['table_field_checks']['checks'][0]['cells'],[])
        units=session.ask('单位呢？')
        self.assertEqual(units['table_field_checks']['requested_fields'],['rc'])
        thickness=session.ask('只核对介厚。')
        self.assertEqual(thickness['table_field_checks']['requested_fields'],['thickness'])
        self.assertEqual({c['doc_id'] for c in thickness['citations']},{doc})
        self.assertIn('字段定位：thickness。',thickness['answer'][:400])
        self.assertEqual(thickness['citation_validation']['status'],'passed')
        self.assertEqual(thickness['result_fingerprint'],fingerprint_chat_result(thickness))

    def test_exact_right_field_shows_literal_unit_without_verified_numeric_answer(self):
        session,_=self.session()
        session.ask('继续核对右侧。')
        r=session.ask('只核对介厚。')
        check=r['table_field_checks']['checks'][0]
        self.assertEqual(check['first_column'],12)
        self.assertEqual(check['cells'][0]['column'],20)
        self.assertIn('表头原文：“介厚”',r['answer'])
        self.assertFalse(check['version_identity_verified'])

    def test_focused_response_skips_general_selection_body_and_optional_generator(self):
        session,_=self.session()
        generator=Mock(return_value='RC为99%')
        session.generator=generator
        r=session.ask('只核对RC和介厚。')
        generator.assert_not_called()
        self.assertFalse(r['generated_by_model'])
        self.assertEqual(r['table_field_checks']['requested_fields'],['rc','thickness'])
        self.assertIn('字段核对',r['answer'])
        self.assertNotIn('确定 PP 组合与树脂含量仍需',r['answer'])

    def test_stateless_tool_field_followup_survives_history_and_ordinal(self):
        with mcp._catalog_context(self.catalog):
            seed,error=mcp.call_tool('pcb_answer',{'question':fixtures.Q,'limit':1},'REQ_field_seed')
            history=[{'role':'user','content':fixtures.Q},{'role':'assistant','content':seed['result']['answer'][:2800]}]
            env,error=mcp.call_tool('pcb_answer',{'question':'只核对第1份右侧的RC。','history':history,'limit':1},'REQ_field')
        self.assertFalse(error)
        r=env['result']
        self.assertTrue(r['workflow']['context_used'])
        self.assertEqual(r['table_field_checks']['requested_fields'],['rc'])
        self.assertEqual(r['table_field_checks']['checks'][0]['first_column'],12)
        self.assertEqual(r['table_field_checks']['checks'][0]['cells'],[])

    def test_missing_row_keeps_field_request_without_other_evidence(self):
        session,_=self.session()
        r=session.ask('只核对原表行999的RC。')
        self.assertEqual(r['citations'],[])
        self.assertEqual(r['table_field_checks']['requested_fields'],['rc'])
        self.assertEqual(r['table_field_checks']['checks'],[])
        self.assertEqual(r['retrieval']['table_followup_scope']['status'],'row_missing_or_ambiguous')
        self.assertIn('没有改查其他来源或其他行',r['answer'])
