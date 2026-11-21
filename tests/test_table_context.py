"""Traceable table header/data pairs and exact-content source deduplication."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from pcb_assistant.catalog.build import build
from pcb_assistant.catalog.chat import ChatSession, fingerprint_chat_result
from pcb_assistant.catalog.common import digest_file
from pcb_assistant.catalog.evidence import _excerpts_for_text, evidence, fingerprint_result
from pcb_assistant.knowledge import term_groups
from pcb_assistant.materials.selection import selection_conditions
from pcb_assistant.tables.context import (
    explicit_rows, location_label, record_matches, returned_table_coverage,
    source_location, table_records, table_layout_pair,
)
from tests.test_catalog import fixture

HEADER='行1 | 列1=材料牌号 | 列2=布型 | 列3=RC(%) | 列4=铜厚(oz) | 列5=残铜率(%) | 列6=介质厚度(mm)'
DATA='行20 | 列1=ZZ-170GTBS | 列2=2116 | 列3=58 | 列4=2 | 列5=70 | 列6=0.20'
QUESTION='ZZ-170GTBS材料，2116半固化片，RC 58%，铜厚2 oz，残铜率70%，目标介质厚度0.20 mm，如何选择PP？'
BODY=HEADER+'\n'+'\n'.join('行{} | 列1=其他材料 | 列2=1080 | 列3=60'.format(i) for i in range(2,20))+'\n'+DATA


class TableContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root=Path(cls.temp.name)
        fixture(root)
        base=root/'data/attachments_v2'
        documents=[json.loads(l) for l in (base/'documents.jsonl').read_text(encoding='utf-8').splitlines()]
        units=[json.loads(l) for l in (base/'engineering_candidates.jsonl').read_text(encoding='utf-8').splitlines()]
        occurrences=[json.loads(l) for l in (base/'occurrences.jsonl').read_text(encoding='utf-8').splitlines()]
        for i,body in enumerate((BODY,BODY,BODY.replace('列3=58 | 列4=2','列3=59 | 列4=2'))):
            doc_id='table_'+str(i)
            documents.append({'document_id':doc_id,'parent_source_ids':['mail_a'],
                              'format':'xlsx','status':'processed','exported_units':1})
            units.append({'id':doc_id+'_unit_0','document_id':doc_id,
                'parent_source_ids':['mail_a'],'subject':'XLSX 附件/工作表2/表格1',
                'text':body,'topics':['压合'],'extraction_method':'native_text',
                'source_kind':'untrusted_attachment','independently_verified':False})
            occurrences.append({'document_id':doc_id,'parent_source_id':'mail_a',
                                'attachment_ordinal':i+3,'status':'read'})
        for filename,rows in (('documents.jsonl',documents),('engineering_candidates.jsonl',units),
                              ('occurrences.jsonl',occurrences)):
            path=base/filename
            path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
        report_path=base/'report.json'
        report=json.loads(report_path.read_text(encoding='utf-8'))
        for filename in ('documents.jsonl','engineering_candidates.jsonl','occurrences.jsonl'):
            report['artifacts'][filename]['sha256']=digest_file(base/filename)
        report_path.write_text(json.dumps(report),encoding='utf-8')
        cls.catalog=root/'data/phase3_catalog_table_test'
        with redirect_stdout(io.StringIO()):build(root,cls.catalog)

    def test_only_explicit_columns_allow_same_row_correspondence_and_header_units(self):
        records=table_records(HEADER+'\n'+DATA)
        record=records[0]
        self.assertEqual((record['header_row'],record['data_row']),(1,20))
        self.assertTrue(all(record_matches(c,record) for c in selection_conditions(QUESTION)))
        wrong=selection_conditions('铜厚12 oz，如何选择PP？')[0]
        self.assertFalse(record_matches(wrong,record))
        text=HEADER+'\n'+DATA
        for field in record['fields']:
            for key in ('header','value'):
                part=field[key]
                self.assertEqual(part['text'],text[part['start']:part['end']])

    def test_damaged_repeated_or_ambiguous_columns_do_not_create_matches(self):
        damaged=DATA.replace('列3=58','列2=58')
        self.assertFalse(explicit_rows(damaged)[0]['valid'])
        self.assertEqual(table_records(HEADER+'\n'+damaged),[])
        ambiguous=HEADER.replace('列2=布型','列2=材料')
        self.assertEqual(table_records(ambiguous+'\n'+DATA),[])
        self.assertEqual(table_records('材料\nRC\n58\n2116\n2 oz'),[])
        unitless=HEADER.replace('铜厚(oz)','铜厚')+'\n'+DATA
        copper=next(c for c in selection_conditions(QUESTION) if c['kind']=='copper')
        self.assertFalse(record_matches(copper,table_records(unitless)[0]))
        self.assertFalse(record_matches(copper,table_records(HEADER+'\n'+DATA.replace('列4=2','列4=2 或 3'))[0]))
        narrative='行1 | 列1=material qualification anomaly | 列2=We suggested to use less RC for trial'
        self.assertEqual(table_records(narrative+'\n'+DATA),[])
        interrupted=HEADER+'\n行2 | 列1=材料 | 列2=铜厚\n'+DATA
        self.assertEqual(table_records(interrupted),[])

    def test_ambiguous_header_keeps_raw_context_without_semantic_mapping(self):
        ambiguous=HEADER.replace('列1=材料牌号','列1=布种').replace('列2=布型','列2=玻布')
        body=ambiguous+'\n'+DATA
        self.assertEqual(table_records(body),[])
        pair=table_layout_pair(body,['2116'],360)
        self.assertIsNotNone(pair)
        self.assertEqual(pair[1]['status'],'column_correspondence_requires_review')
        self.assertNotIn('fields',pair[1])
        non_pp='行1 | 列1=銅箔 | 列2=材料\n行2 | 列1=铜箔检验 | 列2=7天'
        self.assertIsNone(table_layout_pair(non_pp,['检验'],360))

    def test_far_header_and_data_remain_two_exact_bounded_quotes(self):
        excerpts=_excerpts_for_text(BODY,['ZZ-170GTBS','2116','RC'],'table_test',
            ranking_terms=term_groups(QUESTION),selection=True,conditions=selection_conditions(QUESTION))
        self.assertEqual(len(excerpts),2)
        self.assertEqual(excerpts[0]['text'],HEADER)
        self.assertEqual(excerpts[1]['text'],DATA)
        for excerpt in excerpts:
            self.assertLessEqual(len(excerpt['text']),360)
            self.assertEqual(excerpt['text'],BODY[excerpt['start']:excerpt['end']])
            self.assertEqual(excerpt['sha256'],hashlib.sha256(excerpt['text'].encode()).hexdigest())

    def test_page_sheet_and_table_location_preserve_explicit_row_column_identity(self):
        start=BODY.index(DATA)
        location=source_location('XLSX 附件/工作表2/表格1',{'unit_id':'u'},BODY,start,len(BODY))
        self.assertEqual(location['sheet'],2)
        self.assertEqual(location['table'],1)
        self.assertEqual(location['rows'],[20])
        self.assertEqual(location['columns'],[1,2,3,4,5,6])
        self.assertIn('原表行20',location_label(location))
        page=source_location('PDF 附件/第7页',{},'原文',0,2)
        self.assertEqual(page['page'],7)
        self.assertNotIn('sheet',page)

    def test_missing_header_quote_cannot_establish_table_condition_coverage(self):
        record=table_records(HEADER+'\n'+DATA)[0]
        citation={'doc_id':'d','excerpt_id':'x','start':record['row_start'],
                  'end':record['row_end'],'table_context':record}
        self.assertEqual(returned_table_coverage(selection_conditions(QUESTION),[citation])['records'],[])

    def test_real_catalog_deduplicates_identical_bodies_and_preserves_variants(self):
        before=digest_file(self.catalog/'catalog.sqlite3')
        result=evidence(self.catalog,QUESTION,limit=6,ranking_terms=term_groups(QUESTION))
        self.assertGreaterEqual(result['deduplication']['merged_source_groups'],1)
        table_sources=[r for r in result['results'] if r['source_id'].startswith('table_')]
        self.assertEqual(len(table_sources),2)
        merged=next(r for r in table_sources if r['representation_count_in_window']==2)
        self.assertEqual(len({r['source_id'] for r in merged['representations']}),2)
        self.assertEqual(result['result_fingerprint'],fingerprint_result(result))
        self.assertEqual(digest_file(self.catalog/'catalog.sqlite3'),before)

    def test_answer_exposes_header_data_pair_locations_and_support_boundaries(self):
        result=ChatSession(self.catalog,generator=None,limit=1).ask(QUESTION)
        self.assertIn('同列对应（未验证）',result['answer'])
        self.assertIn('原表行20',result['answer'])
        self.assertIn('工作表2',result['answer'])
        records=result['table_condition_coverage']['records']
        self.assertEqual(len(records),1)
        self.assertTrue(records[0]['all_recognized_conditions_same_row'])
        self.assertFalse(result['table_condition_coverage']['engineering_conclusion_supported'])
        self.assertEqual(result['citation_validation']['status'],'passed')
        self.assertEqual(len(result['sources'][0]['identical_content_sources']),2)
        self.assertEqual(result['result_fingerprint'],fingerprint_chat_result(result))

    def test_pagination_deduplicates_before_slicing_pages(self):
        first=evidence(self.catalog,QUESTION,limit=1,ranking_terms=term_groups(QUESTION))
        second=evidence(self.catalog,QUESTION,limit=1,offset=1,ranking_terms=term_groups(QUESTION))
        self.assertNotEqual(first['results'][0]['excerpt_text_sha256'],second['results'][0]['excerpt_text_sha256'])
        self.assertEqual(first['source_count_in_window'],second['source_count_in_window'])
