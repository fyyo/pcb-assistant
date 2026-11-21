"""Sparse native stackup sheets must survive a noisy bounded retrieval pool."""
from contextlib import redirect_stdout
import hashlib,io,json,tempfile,unittest
from pathlib import Path

from catalog_build import build
from catalog_chat import ChatSession,fingerprint_chat_result
from catalog_common import digest_file
from catalog_evidence import evidence,STACKUP_TABLE_POOL_LIMIT,_excerpts_for_text
from pcb_knowledge import term_groups
from pcb_selection import selection_conditions,condition_in_text
from tests.test_catalog import fixture

Q='如何选择2116半固化片PP树脂含量？查叠构表格的材料与介厚，左右版本可以混用吗？'
HEADER='行2 | 列1=層別 | 列2=材料 | 列9=介厚 | 列12=層別 | 列13=材料 | 列20=介厚'
HIT='行29 | 列1=基板 | 列2=Core 5mil | 列3=2116x2 | 列4=5 | 列12=PP | 列13=1080_RC65_ZZ-170 | 列14=3.7'
BODY='行1 | 列1=疊構\n'+HEADER+'\n'+'\n'.join('行{} | 列1=PP | 列3=1080x1 | 列4=3'.format(i) for i in range(3,29))+'\n'+HIT


class StackupRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.temp.cleanup)
        root=Path(cls.temp.name);fixture(root)
        path=root/'data/sanitized_v3/candidates.jsonl'
        rows=[json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()]
        for i in range(240):
            rows.append({'id':'noise_'+str(i),'subject':'PP 叠构树脂含量',
                         'text':'2116 半固化片PP树脂含量，叠构介厚，选择组合需试压。',
                         'topics':['压合'],'source_kind':'untrusted_email'})
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        report_path=path.parent/'report.json';report=json.loads(report_path.read_text(encoding='utf-8'))
        report['artifacts']['candidates.jsonl']['sha256']=digest_file(path)
        report_path.write_text(json.dumps(report),encoding='utf-8')
        base=root/'data/attachments_v2'
        for name,entry in (
            ('documents.jsonl',{'document_id':'parallel_sheet','parent_source_ids':['mail_a'],
                               'format':'xlsx','status':'processed','exported_units':1}),
            ('engineering_candidates.jsonl',{'id':'parallel_unit','document_id':'parallel_sheet',
               'parent_source_ids':['mail_a'],'subject':'XLSX 附件/工作表2','text':BODY,
               'topics':['压合'],'extraction_method':'native_text','source_kind':'untrusted_attachment',
               'independently_verified':False}),
            ('occurrences.jsonl',{'document_id':'parallel_sheet','parent_source_id':'mail_a',
                                 'attachment_ordinal':3,'status':'read'})):
            path=base/name
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
        report_path=base/'report.json';report=json.loads(report_path.read_text(encoding='utf-8'))
        for name in ('documents.jsonl','engineering_candidates.jsonl','occurrences.jsonl'):
            report['artifacts'][name]['sha256']=digest_file(base/name)
        report_path.write_text(json.dumps(report),encoding='utf-8')
        cls.catalog=root/'data/phase3_catalog_stackup'
        with redirect_stdout(io.StringIO()):build(root,cls.catalog)

    def test_stackup_query_keeps_its_own_context_and_inspection_is_explicit(self):
        groups=term_groups(Q)
        self.assertTrue(any('疊構' in g for g in groups))
        self.assertFalse(any('进料检验' in g for g in groups))
        self.assertTrue(any('进料检验' in g for g in term_groups('如何选择2116半固化片，查PP进料检验表？')))

    def test_weave_count_notation_is_literal_weave_mention_without_implied_rc(self):
        glass=selection_conditions(Q)[0]
        for text in ('2116','2116x1','2116X2','2116 × 3'):
            self.assertTrue(condition_in_text(glass,text),text)
        for text in ('21160x1','A2116x1','2116x0','2116x2A'):
            self.assertFalse(condition_in_text(glass,text),text)
        rc=next(c for c in selection_conditions('如何选择PP，RC 58%？') if c['kind']=='rc')
        self.assertFalse(condition_in_text(rc,HIT))

    def test_sparse_sheet_reaches_first_page_despite_over_two_hundred_full_coverage_mails(self):
        before=digest_file(self.catalog/'catalog.sqlite3')
        result=evidence(self.catalog,Q,limit=1,ranking_terms=term_groups(Q))
        self.assertEqual(result['results'][0]['source_id'],'parallel_unit')
        self.assertLessEqual(result['candidate_representations'],200)
        pool=result['candidate_pools']['stackup_table']
        self.assertLessEqual(pool['returned'],STACKUP_TABLE_POOL_LIMIT)
        self.assertFalse(pool['engineering_conclusion_supported'])
        self.assertEqual(before,digest_file(self.catalog/'catalog.sqlite3'))

    def test_filters_apply_to_the_reserved_pool(self):
        result=evidence(self.catalog,Q,source_type='mail',limit=2,ranking_terms=term_groups(Q))
        self.assertEqual(result['candidate_pools']['stackup_table']['returned'],0)
        self.assertTrue(all(r['source_id']!='parallel_unit' for r in result['results']))
        result=evidence(self.catalog,Q,source_version='missing_version',ranking_terms=term_groups(Q))
        self.assertEqual(result['results'],[])
        self.assertEqual(result['candidate_pools']['stackup_table']['returned'],0)

    def test_general_selection_does_not_reserve_a_stackup_table_pool(self):
        for q in ('如何选择2116半固化片PP树脂含量？','如何选择2116半固化片，查PP进料检验表？'):
            result=evidence(self.catalog,q,limit=1,ranking_terms=term_groups(q))
            self.assertNotIn('candidate_pools',result)

    def test_parallel_quotes_keep_full_header_and_matching_weave_row(self):
        excerpts=_excerpts_for_text(BODY,['2116','PP','RC'],'parallel',selection=True,conditions=selection_conditions(Q))
        self.assertEqual([e['text'] for e in excerpts],[HEADER,HIT])
        for e in excerpts:
            self.assertEqual(e['text'],BODY[e['start']:e['end']])
            self.assertEqual(e['sha256'],hashlib.sha256(e['text'].encode()).hexdigest())
            self.assertLessEqual(len(e['text']),360)
            self.assertNotIn('table_context',e)

    def test_answer_returns_parallel_warning_and_matching_row_without_semantic_records(self):
        r=ChatSession(self.catalog,generator=None,limit=1).ask(Q)
        self.assertIn('不可跨区块拼接参数',r['answer'])
        self.assertIn('2116x2','\n'.join(c['text'] for c in r['citations']))
        self.assertEqual(r['table_condition_coverage']['records'],[])
        self.assertFalse(r['selection_condition_coverage']['engineering_conclusion_supported'])
        self.assertIn('candidate_pools',r['retrieval'])
        self.assertEqual(r['citation_validation']['status'],'passed')
        self.assertEqual(r['result_fingerprint'],fingerprint_chat_result(r))
        self.assertEqual(len(r['stackup_block_checks']['checks']),1)
        self.assertEqual(r['stackup_block_checks']['checks'][0]['first_column'],1)
        self.assertIn('单一区块核对',r['answer'])

    def test_reserved_pool_is_deterministic_across_pages(self):
        a=evidence(self.catalog,Q,limit=1,ranking_terms=term_groups(Q))
        b=evidence(self.catalog,Q,limit=1,offset=1,ranking_terms=term_groups(Q))
        self.assertEqual(a['source_count_in_window'],b['source_count_in_window'])
        self.assertEqual(a['candidate_pools'],b['candidate_pools'])
        self.assertNotEqual(a['results'][0]['doc_id'],b['results'][0]['doc_id'])
