"""Table requirements and remarks must not become material data records."""
from catalog_chat import _table_context_lines
from catalog_evidence import _excerpts_for_text
from pcb_selection import selection_conditions
from pcb_table_context import classified_rows, table_records, table_row_context, parallel_column_groups
import unittest


HEADER='行4 | 列1=布种/型号 | 列2=玻布 | 列3=胶含量R/C(%) | 列5=凝胶时间G/T(s)'
SPEC='行27 | 列1=规格需符合要求: | 列2=1.PP的RC公差+/-极限≤2%'
WEAVE='行29 | 列2=3.胶片之纬纱歪斜量:2116 1080等薄布:<10%'
NOTE='行33 | 列1=备注:'


class TableRowRoleTests(unittest.TestCase):
    def test_requirement_section_marks_following_weave_rule_without_repeating_title(self):
        rows=classified_rows('\n'.join((HEADER,SPEC,WEAVE,NOTE)))
        self.assertEqual([r['role'] for r in rows],['header','specification','specification','remark'])
        self.assertEqual(rows[2]['row'],29)

    def test_percentage_requirement_cannot_become_rc_data_even_under_simple_header(self):
        text='行1 | 列1=布型 | 列2=RC(%)\n行2 | 列1=规格需符合要求: | 列2=58\n行3 | 列1=2116 | 列2=58'
        self.assertEqual(table_records(text),[])
        self.assertEqual(classified_rows(text)[2]['role'],'specification')

    def test_remark_signature_and_new_header_end_requirement_section(self):
        text='\n'.join((SPEC,WEAVE,NOTE,'行34 | 列1=供应商签名 | 列2=职务',
                        '行40 | 列1=布型 | 列2=RC(%)','行41 | 列1=2116 | 列2=58'))
        self.assertEqual([r['role'] for r in classified_rows(text)],
                         ['specification','specification','remark','remark','header','data_candidate'])
        self.assertEqual(table_records(text)[0]['data_row'],41)

    def test_repeated_row_numbers_reset_specification_scope(self):
        text=SPEC+'\n'+WEAVE+'\n行1 | 列1=2116 | 列2=58'
        self.assertEqual(classified_rows(text)[-1]['role'],'data_candidate')

    def test_material_codes_are_data_candidates_and_not_headers(self):
        text=('行2 | 列1=材料 | 列2=布型 | 列3=RC(%)\n'
              '行29 | 列1=PP | 列2=1035_RC75_ZZ-170 | 列3=5.52\n'
              '行32 | 列1=基板 | 列2=Core 5mil铜箔 | 列3=2116x1 | 列4=5')
        self.assertEqual([r['role'] for r in classified_rows(text)],
                         ['header','data_candidate','data_candidate'])
        damaged='行4 | 列1=2116 | 列2=58 | 列2=60'
        self.assertEqual(classified_rows(damaged)[0]['role'],'unresolved')

    def test_quote_roles_preserve_exact_original_spans_and_render_caution(self):
        text='\n'.join((HEADER,SPEC,WEAVE,NOTE))
        q='如何选择2116半固化片PP树脂含量，查进料检验表？'
        excerpts=_excerpts_for_text(text,['2116','PP','RC'],'row_roles',selection=True,
                                   conditions=selection_conditions(q))
        self.assertTrue(excerpts)
        self.assertTrue(any(r['role']=='specification' for e in excerpts for r in e['table_row_roles']))
        citations=[{**e,'doc_id':'d'} for e in excerpts]
        rendered='\n'.join(_table_context_lines(citations))
        self.assertIn('规格要求',rendered)
        self.assertIn('不能作为当前材料的实测值或选型数据',rendered)
        for excerpt in excerpts:
            for row in excerpt['table_row_roles']:
                self.assertTrue(text[row['start']:row['end']].startswith('行'))
                self.assertFalse(row['engineering_conclusion_supported'])

    def test_unmarked_flat_text_does_not_create_row_roles(self):
        self.assertEqual(table_row_context('RC 58%\n2116\n备注',0,17),[])

    def test_parallel_layer_blocks_keep_column_limits_without_inventing_versions(self):
        text='行2 | 列1=層別 | 列2=材料 | 列9=介厚 | 列12=層別 | 列13=材料 | 列20=介厚'
        group=parallel_column_groups(text)
        self.assertEqual(group['groups'],[{'first_column':1,'last_column':11},
                                         {'first_column':12,'last_column':20}])
        self.assertFalse(group['version_identity_verified'])
        citation={'doc_id':'d','excerpt_id':'EXC_x','parallel_table_groups':group,
                  'start':0,'end':len(text)}
        self.assertIn('不可跨区块拼接参数','\n'.join(_table_context_lines([citation])))

    def test_parallel_header_is_returned_and_cross_block_semantics_are_rejected(self):
        header='行1 | 列1=层别 | 列2=布型 | 列12=层别 | 列13=RC(%)'
        text=header+'\n'+'行2 | 列1=说明\n'*40+'行50 | 列2=2116 | 列13=58'
        excerpts=_excerpts_for_text(text,['2116'],'blocks',selection=True)
        self.assertEqual(table_records(text),[])
        headers=[e for e in excerpts if e.get('parallel_table_groups')]
        self.assertEqual(len(headers),1)
        self.assertEqual(headers[0]['text'],header)
        self.assertTrue(all(len(e['text'])<=360 for e in excerpts))
        hidden={**headers[0],'doc_id':'d','start':len(header)+1,'end':len(text)}
        self.assertNotIn('并列区块','\n'.join(_table_context_lines([hidden])))

    def test_clipped_rows_and_hidden_requirement_heading_are_not_cited_as_roles(self):
        text=SPEC+'\n行28 | 列1=2116 | 列2=58'
        lo=text.index('行28')
        roles=table_row_context(text,lo,len(text))
        c={'doc_id':'d','excerpt_id':'hit','start':lo,'end':len(text),'table_row_roles':roles}
        self.assertEqual(_table_context_lines([c]),[])
        heading={'doc_id':'d','excerpt_id':'heading','start':0,'end':len(SPEC)}
        rendered='\n'.join(_table_context_lines([c,heading]))
        self.assertIn('规格要求',rendered)
        self.assertIn('[hit, heading]',rendered)
        self.assertEqual(_table_context_lines([{**c,'start':lo+3},heading]),[])
