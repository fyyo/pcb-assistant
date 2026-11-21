"""PP retrieval keeps literal product conditions without inventing a recipe."""
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_evidence
from catalog_build import build
from catalog_common import connect_readonly, digest_file
from catalog_chat import ChatSession, fingerprint_chat_result
from catalog_evidence import MAX_EXCERPT_CHARS, _excerpts_for_text, evidence
from pcb_knowledge import is_knowledge_question, term_groups, tokens
from pcb_selection import (
    condition_in_text, is_selection_question, selection_conditions, selection_coverage,
)
import pcb_assistant
from tests.test_catalog import fixture


class PPSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        fixture(root)
        path = root/'data/sanitized_v3/candidates.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        rows.extend([
            {'id': 'selection', 'subject': 'PP选型试压资料', 'text':
             '材料ZZ-170GTBS，2116半固化片PP。\n铜厚2 oz\n残铜率70%\n目标介质厚度0.20 mm\n'
             '树脂含量RC 58%。选择PP组合时应同时核对压后厚度与局部填胶，使用同材料组合试压记录，不能直接采用历史配方。'},
            {'id': 'inspection', 'subject': 'PP进料检验规格', 'text':
             'PP进料检验规格表\n2116半固化片\n树脂含量检验\n凝胶时间\n厂商选择测试方法\n'*12},
            {'id': 'wrong_grade', 'subject': '其他牌号试压', 'text':
             '材料ZZ-170GTBSX，2116半固化片PP，铜厚12 oz，残铜率17%，目标介质厚度0.73 mm。'
             '树脂含量RC 58%。选择PP组合需核对叠构和试压厚度，不能仅按布型决定材料。'},
        ])
        for row in rows:
            row.setdefault('topics', ['压合'])
            row.setdefault('source_kind', 'untrusted_email')
        path.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')
        report_path = path.parent/'report.json'
        report = json.loads(report_path.read_text(encoding='utf-8'))
        report['artifacts']['candidates.jsonl']['sha256'] = digest_file(path)
        report_path.write_text(json.dumps(report), encoding='utf-8')
        cls.catalog = root/'data/phase3_catalog_selection_test'
        with redirect_stdout(io.StringIO()):
            build(root, cls.catalog)
        cls.question = 'ZZ-170GTBS材料，2116半固化片，铜厚2 oz，残铜率70%，目标介质厚度0.20 mm，如何选择PP树脂含量？'

    def test_ranked_query_reuses_body_work_without_sharing_source_metadata(self):
        parsed = {}
        extract = catalog_evidence._excerpts_for_text

        def capture(text, terms, content_id, **kwargs):
            self.assertNotIn(content_id, parsed, 'A body was extracted twice in one query')
            parsed[content_id] = extract(text, terms, content_id, **kwargs)
            return parsed[content_id]

        with patch.object(catalog_evidence, '_excerpts_for_text', side_effect=capture), \
                patch.object(catalog_evidence, '_load_texts', wraps=catalog_evidence._load_texts) as load:
            result = evidence(self.catalog, self.question, ranking_terms=term_groups(self.question), limit=3)
        self.assertEqual(load.call_count, 1)
        self.assertTrue(result['results'])
        self.assertTrue(any(source['excerpts'] for source in result['results']))
        for source in result['results']:
            for excerpt in source['excerpts']:
                self.assertIn('source_location', excerpt)
                original = next(e for items in parsed.values() for e in items
                                if e['excerpt_id'] == excerpt['excerpt_id'])
                self.assertNotIn('source_location', original)
                before = list(original['matched_terms'])
                excerpt['matched_terms'].append('changed by caller')
                self.assertEqual(original['matched_terms'], before)

    def test_empty_ranked_excerpts_are_reused_and_new_queries_recompute(self):
        with patch.object(catalog_evidence, '_excerpts_for_text', return_value=[]) as extract:
            first = evidence(self.catalog, self.question, limit=3)
            count = extract.call_count
            self.assertGreater(count, 0)
            self.assertTrue(first['results'])
            self.assertEqual(first['excerpt_count'], 0)
            self.assertEqual(len({call.args[2] for call in extract.call_args_list}), count)
            second = evidence(self.catalog, self.question, limit=3)
            self.assertEqual(extract.call_count, count * 2)
            self.assertEqual(first, second)

    def test_conditions_preserve_whole_grade_and_all_dimensions_without_index_changes(self):
        before = tokens(self.question)
        conditions = selection_conditions(self.question)
        self.assertEqual([c['value'] for c in conditions],
                         ['zz-170gtbs', '2116', '2 oz', '70%', '0.20 mm'])
        groups = term_groups(self.question)
        for value in ('zz-170gtbs', '2116', '2 oz', '70%', '0.20 mm'):
            self.assertIn((value,), groups)
        self.assertFalse(any('固化' in g or '保温' in g for g in groups))
        self.assertEqual(tokens(self.question), before)
        self.assertLessEqual(len(groups), 12)

    def test_selection_intent_handles_chinese_pp_and_does_not_take_over_diagnosis(self):
        for q in ('铜厚2 oz，如何选择PP？', '半固化片PP选型资料',
                  '如何選擇半固化片含膠量？', 'How to select prepreg resin content?'):
            self.assertTrue(is_selection_question(q), q)
            self.assertTrue(is_knowledge_question(q), q)
        self.assertFalse(is_selection_question('半固化片导致缺胶怎么排查原因？'))
        case, inherited = pcb_assistant._case_context('铜厚2 oz，如何选择PP？', [
            {'role':'user', 'content':'厚铜板压合后缺胶，怎么排查？'}])
        self.assertIsNone(case)
        self.assertFalse(inherited)

    def test_values_need_field_correspondence_and_complete_numeric_boundaries(self):
        conditions = selection_conditions(self.question)
        text = '材料ZZ-170GTBSX，2116，铜厚12 oz，残铜率170%，介厚0.200 mm'
        self.assertEqual([condition_in_text(c, text) for c in conditions],
                         [False, True, False, False, True])
        self.assertFalse(condition_in_text(conditions[2], '铜厚待确认，另外总厚度2 oz'))
        self.assertFalse(condition_in_text(conditions[0], '材料ZZ-170GTBS-2'))
        self.assertFalse(condition_in_text(conditions[3], '树脂含量70%'))
        self.assertFalse(condition_in_text(conditions[4], '介厚0.20 mil'))

    def test_adjacent_parameter_lines_are_exact_bounded_source_slices(self):
        text = '资料开头\n材料ZZ-170GTBS\n2116半固化片PP\n铜厚2 oz\n残铜率70%\n介厚0.20 mm\n树脂含量58%\n选择组合需试压。\n结尾'
        result = _excerpts_for_text(text, ['2116', 'PP'], 'selection_test',
                                   ranking_terms=term_groups(self.question), selection=True,
                                   conditions=selection_conditions(self.question))
        self.assertTrue(any('铜厚2 oz' in e['text'] and '介厚0.20 mm' in e['text'] for e in result))
        for e in result:
            self.assertEqual(e['text'], text[e['start']:e['end']])
            self.assertLessEqual(len(e['text']), MAX_EXCERPT_CHARS)
            self.assertEqual(e['sha256'], hashlib.sha256(e['text'].encode()).hexdigest())

    def test_real_retrieval_prefers_matching_selection_over_inspection_and_wrong_grade(self):
        before = digest_file(self.catalog/'catalog.sqlite3')
        result = evidence(self.catalog, self.question, ranking_terms=term_groups(self.question), limit=1)
        self.assertEqual(result['results'][0]['source_id'], 'selection')
        with closing(connect_readonly(self.catalog)) as con:
            body = con.execute('SELECT COALESCE(c.normalized_text,c.original_text) FROM documents d '
                               'JOIN contents c USING(content_id) WHERE d.doc_id=?',
                               (result['results'][0]['doc_id'],)).fetchone()[0]
        for e in result['results'][0]['excerpts']:
            self.assertEqual(e['text'], body[e['start']:e['end']])
        self.assertEqual(digest_file(self.catalog/'catalog.sqlite3'), before)
        general = ChatSession(self.catalog, generator=None, limit=1).ask('如何选择半固化片PP树脂含量？')
        self.assertNotEqual(general['sources'][0]['source_id'], 'inspection')

    def test_missing_condition_is_visible_even_when_related_material_is_found(self):
        q = self.question.replace('ZZ-170GTBS', 'ZXQ9999').replace('0.20 mm', '0.73 mm')
        result = ChatSession(self.catalog, generator=None, limit=1).ask(q)
        self.assertTrue(result['citations'])
        self.assertIn('zxq9999', result['answer'])
        self.assertIn('未直接对应', result['answer'])
        self.assertIn('不能拼接不同资料', result['answer'])
        self.assertFalse(result['selection_condition_coverage']['all_conditions_in_one_excerpt'])
        self.assertEqual(result['clarifying_questions'], [])
        self.assertEqual(result['citation_validation']['status'], 'passed')
        self.assertEqual(result['result_fingerprint'], fingerprint_chat_result(result))

    def test_disconnected_excerpts_do_not_imply_joint_support(self):
        coverage = selection_coverage(self.question, ['ZZ-170GTBS 2116',
            '铜厚2 oz，残铜率70%', '目标介质厚度0.20 mm'])
        self.assertEqual(coverage['missing'], [])
        self.assertFalse(coverage['all_conditions_in_one_excerpt'])
        self.assertFalse(coverage['engineering_conclusion_supported'])

    def test_followup_keeps_selection_intent_and_explicit_missing_dimensions(self):
        session = ChatSession(self.catalog, generator=None, limit=1)
        session.ask('如何选择半固化片PP树脂含量？')
        result = session.ask('补充：铜厚9 oz。')
        self.assertIn('9 oz', result['answer'])
        self.assertIn('选型资料', result['answer'])
        self.assertEqual(result['clarifying_questions'], [])
        self.assertTrue(result['selection_condition_coverage']['missing'])
