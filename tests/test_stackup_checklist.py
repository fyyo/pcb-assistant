"""Block checklists cannot borrow a unit, condition or value from another block."""
import copy
import unittest

from catalog_evidence import _excerpts_for_text
from pcb_selection import selection_conditions
from pcb_stackup_checklist import stackup_block_checks, block_check_lines

Q='如何选择2116半固化片PP？查叠构表格的材料、RC与介质厚度，铜厚2 oz。'
HEADER='行2 | 列1=層別 | 列2=材料 | 列3=布型 | 列4=RC | 列5=介厚 | 列12=層別 | 列13=材料 | 列14=布型 | 列15=RC(%) | 列16=介质厚度(mm) | 列17=铜厚(oz)'
ROW='行29 | 列1=基板 | 列2=Core 5mil | 列3=2116x1 | 列4=75 | 列5=5 | 列12=PP | 列13=1080_RC58_ZZ-170 | 列14=1080 | 列15=58 | 列16=0.20 | 列17=2'
TEXT=HEADER+'\n'+ROW


def citations(text=TEXT,question=Q,doc='d'):
    return [{**e,'doc_id':doc} for e in _excerpts_for_text(text,['2116','PP','RC'],'fixture_'+doc,
                selection=True,conditions=selection_conditions(question))]


class StackupChecklistTests(unittest.TestCase):
    def test_default_selects_one_matching_block_with_exact_cell_references(self):
        cs=citations();r=stackup_block_checks(Q,cs)
        self.assertEqual(len(r['checks']),1)
        c=r['checks'][0]
        self.assertEqual((c['first_column'],c['last_column']),(1,11))
        self.assertFalse(c['version_identity_verified'])
        self.assertFalse(c['column_semantics_verified'])
        self.assertFalse(c['engineering_conclusion_supported'])
        for column in c['columns']:
            self.assertLessEqual(column['column'],11)
            for part in ('heading','value'):
                ref=column[part]
                if ref:
                    quote=next(q for q in cs if q['excerpt_id']==ref['citation_id'])
                    self.assertEqual(ref['text'],TEXT[ref['start']:ref['end']])
                    self.assertTrue(quote['start']<=ref['start']<ref['end']<=quote['end'])

    def test_left_cannot_borrow_right_units_or_numeric_copper(self):
        check=stackup_block_checks(Q,citations())['checks'][0]
        fields={f['field']:f for f in check['fields']}
        self.assertEqual(fields['rc']['unit_status'],'unit_not_located_in_returned_block')
        self.assertEqual(fields['thickness']['unit_status'],'unit_not_located_in_returned_block')
        self.assertEqual(fields['copper']['status'],'heading_not_located_in_returned_block')
        copper=next(c for c in check['condition_mentions'] if c['kind']=='copper')
        self.assertFalse(copper['mentioned_in_block'])
        self.assertNotIn('1080_RC58',str(check))

    def test_requested_right_side_overrides_left_weave_hit_without_backfill(self):
        q=Q+'只核对右侧区块。'
        c=stackup_block_checks(q,citations(question=q))['checks'][0]
        self.assertEqual((c['first_column'],c['last_column']),(12,17))
        self.assertEqual(c['selection_basis'],'requested_right_block')
        self.assertFalse(next(x for x in c['condition_mentions'] if x['kind']=='glass')['mentioned_in_block'])
        self.assertTrue(all(x['column']>=12 for x in c['columns']))
        fields={f['field']:f for f in c['fields']}
        self.assertEqual(fields['rc']['unit_status'],'literal_unit_requires_review')
        lines='\n'.join(block_check_lines({'checks':[c]}))
        self.assertIn('不借用另一并列区块的值',lines)

    def test_left_right_combination_does_not_silently_select_right(self):
        c=stackup_block_checks(Q+'左右版本可以混用吗？',citations())['checks'][0]
        self.assertEqual(c['selection_basis'],'most_literal_condition_mentions_within_one_block')
        self.assertEqual(c['first_column'],1)

    def test_header_or_data_clipping_prevents_check(self):
        cs=citations()
        for which in (0,1):
            clipped=copy.deepcopy(cs)
            clipped[which]['start']+=1
            clipped[which]['text']=clipped[which]['text'][1:]
            self.assertEqual(stackup_block_checks(Q,clipped)['checks'],[])

    def test_header_and_row_from_different_sources_are_not_joined(self):
        cs=citations();cs[1]['doc_id']='other'
        self.assertEqual(stackup_block_checks(Q,cs)['checks'],[])

    def test_specification_or_damaged_row_is_not_inventory_data(self):
        cs=citations();cs[1]['table_row_roles'][0]['role']='specification'
        self.assertEqual(stackup_block_checks(Q,cs)['checks'],[])
        broken=TEXT.replace('列4=75','列3=75')
        self.assertEqual(stackup_block_checks(Q,citations(broken))['checks'],[])

    def test_duplicate_semantic_headings_in_same_block_remain_ambiguous(self):
        text=TEXT.replace('列5=介厚','列5=RC')
        fields=stackup_block_checks(Q,citations(text))['checks'][0]['fields']
        rc=next(f for f in fields if f['field']=='rc')
        self.assertEqual(rc['status'],'ambiguous_headings_require_review')
        self.assertEqual(rc['columns'],[4,5])

    def test_material_code_does_not_supply_rc_percentage_or_thickness_units(self):
        text='行2 | 列1=層別 | 列2=材料 | 列12=層別 | 列13=材料\n行29 | 列1=PP | 列2=2116_RC75_ZZ-170 | 列12=PP | 列13=1080_RC58_ZZ-170'
        q=Q+'只看左侧区块。'
        c=stackup_block_checks(q,citations(text,q))['checks'][0]
        fields={f['field']:f for f in c['fields']}
        self.assertEqual(fields['rc']['columns'],[])
        self.assertEqual(fields['rc']['unit_evidence'],[])
        self.assertEqual(fields['thickness']['unit_evidence'],[])

    def test_no_condition_or_side_does_not_choose_an_arbitrary_block(self):
        self.assertEqual(stackup_block_checks('如何选择PP，查叠构表格？',citations())['checks'],[])
        q='如何选择PP，查叠构表格？仅核对左侧区块。'
        self.assertEqual(stackup_block_checks(q,citations())['checks'][0]['first_column'],1)

    def test_missing_value_stays_missing_even_when_header_has_unit(self):
        text=TEXT.replace(' | 列15=58','')
        q=Q+'只看右侧区块。'
        fields=stackup_block_checks(q,citations(text,q))['checks'][0]['fields']
        rc=next(f for f in fields if f['field']=='rc')
        self.assertEqual(rc['status'],'value_not_present_in_returned_row')
        self.assertEqual(rc['unit_status'],'literal_unit_requires_review')
        self.assertFalse(rc['engineering_conclusion_supported'])

    def test_missing_heading_statement_is_limited_to_returned_row_and_block(self):
        result=stackup_block_checks(Q,citations())
        lines='\n'.join(block_check_lines(result))
        self.assertIn('缺口仅针对返回的表头与该行',lines)
        self.assertIn('版本身份',lines)
        self.assertIn('介质厚度单位（未定位）',lines)
        self.assertEqual(result,stackup_block_checks(Q,citations()))
