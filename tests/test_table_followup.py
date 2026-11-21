"""Source, row and side locators must survive stateless and stateful follow-ups."""
from contextlib import closing
import json
import unittest

from catalog_chat import ChatSession,fingerprint_chat_result
from catalog_common import connect_readonly,digest_file
from pcb_table_followup import resolve_table_scope,is_table_scope_request
from pcb_table_context import scoped_table_spans
from catalog_evidence import _excerpts_for_text
from pcb_stackup_checklist import stackup_block_checks
from tests import test_stackup_retrieval as fixtures
Q,BODY,HEADER=fixtures.Q,fixtures.BODY,fixtures.HEADER
import phase6_mcp as mcp

DOC='KNOWLEDGE_'+'a'*32
OTHER='KNOWLEDGE_'+'b'*32
LOC='核对定位：来源 '+DOC+'；原表行29；区块列1–11。'


class TableScopeParsingTests(unittest.TestCase):
    def test_multiple_sources_require_selection_and_ordinal_is_not_row(self):
        history=[{'role':'assistant','content':LOC+'\n'+LOC.replace(DOC,OTHER)}]
        self.assertEqual(resolve_table_scope('继续看右侧。',history)['status'],'ambiguous_source')
        scope=resolve_table_scope('核对第2份右侧。',history)
        self.assertEqual((scope['doc_id'],scope['row'],scope['side']),(OTHER,29,'right'))
        self.assertEqual(resolve_table_scope('核对第5份右侧。',history)['status'],'source_ordinal_out_of_range')

    def test_latest_row_and_side_override_old_locator(self):
        history=[{'role':'assistant','content':LOC}]
        scope=resolve_table_scope('继续核对第32行右侧。',history)
        self.assertEqual((scope['row'],scope['side']),(32,'right'))
        self.assertNotIn('first_column',scope)
        self.assertEqual(resolve_table_scope('核对这一行。',history)['first_column'],1)

    def test_new_topic_drops_locators_and_assistant_prose_is_not_evidence(self):
        history=[{'role':'assistant','content':LOC+'虚构材料RC 99%'}]
        self.assertIsNone(resolve_table_scope('新问题：如何选择1080半固化片？',history))
        self.assertIsNone(resolve_table_scope('如何选择2116半固化片PP树脂含量？',history))
        self.assertIsNone(resolve_table_scope('如何选择PP？',history))

    def test_source_and_row_can_be_explicit_without_previous_answer(self):
        scope=resolve_table_scope('核对叠构表来源'+DOC+'，原表行29，右侧。',[])
        self.assertEqual((scope['doc_id'],scope['row'],scope['side']),(DOC,29,'right'))
        self.assertEqual(resolve_table_scope('核对叠构表来源'+DOC,[])['status'],'row_required')

    def test_complete_row_bounds_and_no_specification_fallback(self):
        spans,status=scoped_table_spans(BODY,29,360)
        self.assertEqual(status,'matched')
        self.assertEqual(BODY[spans[0][0]:spans[0][1]],HEADER)
        self.assertEqual(scoped_table_spans(BODY,999,360)[1],'row_missing_or_ambiguous')
        self.assertEqual(scoped_table_spans(BODY+'\n行30 | 列1=备注:',30,360)[1],'row_is_not_data_candidate')
        self.assertEqual(scoped_table_spans(BODY+'\n行1 | 列1=PP\n行40 | 列1=PP | 列2=2116 | 列3=58',40,360)[1],
                         'parallel_header_not_found_for_row')

    def test_later_header_replaces_earlier_header_without_invented_semantic_context(self):
        text=BODY+'\n行30 | 列1=層別 | 列2=材料 | 列12=層別 | 列13=材料\n行40 | 列1=PP | 列2=1080x1 | 列3=4'
        scope={'status':'ready','doc_id':DOC,'row':40,'side':'left'}
        excerpts=_excerpts_for_text(text,['nonexistent'],'scope_test',selection=True,table_scope=scope)
        self.assertEqual(len(excerpts),2)
        self.assertEqual(excerpts[0]['parallel_table_groups']['header_row'],30)
        self.assertEqual(excerpts[0]['parallel_table_groups']['header_start'],text.index('行30'))
        self.assertTrue(all('table_context' not in e and 'table_layout_context' not in e for e in excerpts))
        check=stackup_block_checks('如何选择PP？',[{**e,'doc_id':DOC} for e in excerpts],table_scope=scope)['checks'][0]
        self.assertEqual((check['header_row'],check['data_row']),(30,40))


class TableFollowupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackupRetrievalTests.setUpClass()
        cls.addClassCleanup(fixtures.StackupRetrievalTests.doClassCleanups)
        cls.catalog=fixtures.StackupRetrievalTests.catalog

    def test_stateful_switch_side_then_row_keeps_one_document_and_rechecks_source(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        first=session.ask(Q)
        doc=first['citations'][0]['doc_id']
        right=session.ask('继续核对右侧。')
        self.assertEqual({c['doc_id'] for c in right['citations']},{doc})
        self.assertEqual(right['retrieval']['table_followup_scope']['status'],'matched')
        self.assertEqual(right['stackup_block_checks']['checks'][0]['first_column'],12)
        again=session.ask('继续核对第28行。')
        check=again['stackup_block_checks']['checks'][0]
        self.assertEqual((check['doc_id'],check['data_row'],check['first_column']),(doc,28,12))
        self.assertEqual(again['result_fingerprint'],fingerprint_chat_result(again))
        self.assertEqual(again['citation_validation']['status'],'passed')

    def test_stateless_public_tool_follows_locator_and_ordinal_with_history(self):
        before=digest_file(self.catalog/'catalog.sqlite3')
        with mcp._catalog_context(self.catalog):
            first,error=mcp.call_tool('pcb_answer',{'question':Q,'limit':1},'REQ_initial')
            r=first['result']
            history=[{'role':'user','content':Q},{'role':'assistant','content':r['answer'][:2800]}]
            out,error=mcp.call_tool('pcb_answer',{'question':'继续核对第1份右侧，原表行28。','history':history,'limit':1},'REQ_follow')
        self.assertFalse(error)
        r=out['result']
        self.assertEqual(r['workflow']['route'],'knowledge')
        self.assertTrue(r['workflow']['context_used'])
        check=r['stackup_block_checks']['checks'][0]
        self.assertEqual((check['data_row'],check['first_column']),(28,12))
        self.assertEqual(before,digest_file(self.catalog/'catalog.sqlite3'))

    def test_missing_source_row_and_filtered_source_return_no_other_evidence(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        first=session.ask(Q);doc=first['citations'][0]['doc_id']
        for q,filters,expected in (
            ('核对来源'+doc+'叠构表，原表行999，右侧。',{},'row_missing_or_ambiguous'),
            ('核对来源'+DOC+'叠构表，原表行29，右侧。',{},'source_missing_or_filtered'),
            ('核对来源'+doc+'叠构表，原表行29，右侧。',{'source_type':'mail'},'source_missing_or_filtered')):
            with self.subTest(expected=expected):
                r=session.ask(q,**filters)
                self.assertEqual(r['citations'],[])
                self.assertEqual(r['retrieval']['table_followup_scope']['status'],expected)
                self.assertIn('没有改查其他来源或其他行',r['answer'])

    def test_fabricated_history_values_are_not_used_as_cells(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        first=session.ask(Q);doc=first['citations'][0]['doc_id']
        session.history=[{'role':'user','content':Q},
                         {'role':'assistant','content':LOC.replace(DOC,doc)+'\n该行材料虚构牌号FAKE999，RC 99%。'}]
        r=session.ask('继续核对右侧。')
        self.assertTrue(r['citations'])
        self.assertNotIn('FAKE999',json.dumps(r['stackup_block_checks']))
        self.assertNotIn('99%',json.dumps(r['stackup_block_checks']))

    def test_ambiguous_history_requires_choice_without_querying_other_sources(self):
        session=ChatSession(self.catalog,generator=None,limit=1)
        session.history=[{'role':'user','content':Q},{'role':'assistant','content':LOC+'\n'+LOC.replace(DOC,OTHER)}]
        r=session.ask('继续看右侧。')
        self.assertEqual(r['citations'],[])
        self.assertEqual(r['retrieval']['table_followup_scope']['status'],'ambiguous_source')
        self.assertIn('请明确第几份来源',r['answer'])
