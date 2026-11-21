"""Product conditions and learning intent must survive bounded retrieval."""
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
import re
from pathlib import Path
import tempfile
import unittest

from agent.gate import extract_facts
from agent.models import CaseInput
from agent.workflow import diagnose
from catalog_build import build
from catalog_chat import ChatSession, fingerprint_chat_result
from catalog_common import connect_readonly, digest_file
from catalog_evidence import MAX_EXCERPT_CHARS, _excerpts_for_text, evidence, fingerprint_result
import pcb_assistant
from pcb_knowledge import (
    is_explanation_question, is_knowledge_question, query_terms, term_groups, tokens,
)
from tests.test_agent_workflow import FakeRetriever, evidence_item
from tests.test_catalog import fixture
from pcb_material_concepts import definition_keys, material_concepts, material_gap_lines


class ContextualRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        path = cls.root / 'data/sanitized_v3/candidates.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        rows.extend([
            {'id': 'heavy_fill', 'subject': '厚铜填胶试用',
             'text': '厚铜设计的压合测试，采用半固化片。\n树脂流动不足，无法互相填胶，导致缺胶，需要核对材料组合。'},
            {'id': 'generic_fill', 'subject': '一般缺胶',
             'text': '树脂流动不足，无法互相填胶，导致缺胶，需要核对材料组合。' * 8},
            {'id': 'vacuum_heading', 'subject': '压合真空流程',
             'text': '压合抽真空\n真空度\n抽真空\n真空度\n失真空处理流程'},
            {'id': 'vacuum_explanation', 'subject': '压合真空原理',
             'text': '层压开始抽真空可排出层间空气并减少截留水分，避免气体滞留影响树脂填充。'},
            {'id': 'unrelated_explanation', 'subject': '真空层压机说明',
             'text': '机器保养原理是及时更换零件，因为减少磨损可以防止设备停机影响工作安排。'},
            {'id': 'tg_td_heading', 'subject': 'Tg Td 检验表',
             'text': '二、常板特性测试---TD&TG\nTD测试 TG测试\n结果合格。' * 5},
            {'id': 'tg_td_definition', 'subject': '材料温度术语',
             'text': 'TG – Glass Transition Temperature; The temperature at which the resin changes from a glass-\n'
                     'like state to an amorphous state changing its mechanical behavior.\n\n'
                     'TD – Decomposition Temperature; The temperature at which the resin begins to decompose,\n'
                     'measured by a weight change in the resin sample.'},
            {'id': 'td_formula', 'subject': 'TMA 位移计算',
             'text': 'Percent TE = tD – tA\ntD\nThe displacement at the end of the test.'},
            {'id': 'rf_gt_heading', 'subject': 'RF GT 检验项目',
             'text': 'RF(胶流量)\nGT(凝胶时间)\n检验合格。' * 5},
            {'id': 'rf_definition', 'subject': '树脂流动试验',
             'text': '1.0 Scope\nThis test method is designed to measure the\n'
                     'Resin Flow Percent by weight in prepreg.\n2.0 Applicable Documents\nNone.'},
            {'id': 'gt_definition', 'subject': '凝胶试验',
             'text': '1.0 Scope\nThe purpose of this test method is to provide a proce-\n'
                     'dure for determining the gel time of resin preimpregnated B\nStage glass fabric.\n'
                     '2.0 Applicable Documents\nNone.'},
            {'id': 'rf_radio', 'subject': 'RF 射频测量',
             'text': '客户要求测量 RF 射频 coupon，radio frequency measurement，电性测试站确认。'},
        ])
        for row in rows:
            row.setdefault('topics', ['压合'])
            row.setdefault('source_kind', 'untrusted_email')
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
        report_path = path.parent / 'report.json'
        report = json.loads(report_path.read_text(encoding='utf-8'))
        report['artifacts']['candidates.jsonl']['sha256'] = digest_file(path)
        report_path.write_text(json.dumps(report), encoding='utf-8')
        cls.catalog = cls.root / 'data/phase3_catalog_context_test'
        with redirect_stdout(io.StringIO()):
            build(cls.root, cls.catalog)

    def test_query_aliases_preserve_compounds_without_changing_index_tokens(self):
        question = '厚铜板压合后铜台阶附近出现缺胶，怎么排查？'
        before = tokens(question)
        groups = term_groups(question)
        self.assertEqual([g[0] for g in groups], ['厚铜', '铜台阶', '缺胶'])
        terms = query_terms(question)
        self.assertIn('厚铜', terms)
        self.assertIn('缺膠', terms)
        self.assertNotIn('附近', terms)
        self.assertEqual(tokens(question), before)
        self.assertEqual(term_groups('厚銅板壓合後缺膠怎麼排查？')[0], groups[0])

    def test_condition_and_fill_statement_remain_one_exact_bounded_source_slice(self):
        text = '本页说明\n厚铜设计的压合测试。\n树脂流动不足，无法互相填胶，造成缺胶风险。\n末尾说明'
        groups = term_groups('厚铜板缺胶')
        excerpts = _excerpts_for_text(text, ['缺胶'], 'synthetic', ranking_terms=groups)
        self.assertTrue(excerpts)
        quote = excerpts[0]
        self.assertIn('厚铜', quote['text'])
        self.assertIn('缺胶', quote['text'])
        self.assertEqual(quote['text'], text[quote['start']:quote['end']])
        self.assertLessEqual(len(quote['text']), MAX_EXCERPT_CHARS)
        self.assertEqual(quote['sha256'], hashlib.sha256(quote['text'].encode()).hexdigest())
        oversized = '厚铜' + '甲' * 500 + '\n缺胶需要核对填胶。'
        result = _excerpts_for_text(oversized, ['缺胶'], 'large', ranking_terms=groups)
        self.assertTrue(all(len(q['text']) <= MAX_EXCERPT_CHARS for q in result))

    def test_diagnosis_preserves_product_conditions_and_reports_uncovered_step(self):
        item = evidence_item('EXC_fill', '厚铜结构的树脂流动不足会造成缺胶。')
        fake = FakeRetriever((('树脂流动', [item]),))
        result = diagnose({'question': '厚铜板铜台阶附近缺胶怎么排查？'}, retriever=fake)
        self.assertTrue(fake.queries)
        self.assertTrue(all('厚铜' in query and '铜台阶' in query for query in fake.queries))
        self.assertEqual(result['retrieval']['context_coverage']['uncovered'], ['铜台阶'])
        self.assertIn('未直接覆盖“铜台阶”', result['answer'])

    def test_product_conditions_survive_followup_and_clear_for_new_batch(self):
        history = [{'role': 'user', 'content': '厚铜板铜台阶出现缺胶，怎么排查？'}]
        case, inherited = pcb_assistant._case_context('补充：FR-4，8层板。', history)
        self.assertTrue(inherited)
        facts = extract_facts(CaseInput.from_mapping(case))
        self.assertIn('厚铜', facts['product_context'])
        case, inherited = pcb_assistant._case_context('另一批板出现缺胶。', history)
        self.assertFalse(inherited)
        self.assertNotIn('厚铜', extract_facts(CaseInput.from_mapping(case))['product_context'])

    def test_real_catalog_returns_heavy_fill_body_with_original_hashes(self):
        before = digest_file(self.catalog / 'catalog.sqlite3')
        question = '厚铜板压合后铜台阶附近出现缺胶，怎么排查？'
        result = evidence(self.catalog, question, ranking_terms=term_groups(question), limit=1)
        self.assertEqual(result['results'][0]['source_id'], 'heavy_fill')
        self.assertEqual(result['result_fingerprint'], fingerprint_result(result))
        quote = result['results'][0]['excerpts'][0]
        with closing(connect_readonly(self.catalog)) as con:
            body = con.execute('SELECT COALESCE(c.normalized_text,c.original_text) FROM documents d '
                               'JOIN contents c USING(content_id) WHERE d.doc_id=?',
                               (result['results'][0]['doc_id'],)).fetchone()[0]
        self.assertEqual(quote['text'], body[quote['start']:quote['end']])
        self.assertIn('厚铜', quote['text'])
        self.assertIn('缺胶', quote['text'])
        self.assertEqual(digest_file(self.catalog / 'catalog.sqlite3'), before)

    def test_learning_request_prefers_explanation_and_omits_case_questionnaire(self):
        question = '真空在压合中的作用是什么？'
        result = ChatSession(self.catalog, generator=None, limit=1).ask(question)
        self.assertEqual(result['sources'][0]['source_id'], 'vacuum_explanation')
        self.assertIn('排出层间空气', result['answer'])
        self.assertEqual(result['clarifying_questions'], [])
        self.assertNotIn('具体缺陷', result['answer'])
        self.assertNotIn('可能原因与排查方向', result['answer'])
        self.assertIn('不作为通用工艺设定', result['answer'])
        self.assertEqual(result['citation_validation']['status'], 'passed')
        self.assertEqual(result['result_fingerprint'], fingerprint_chat_result(result))
        self.assertEqual(result['retrieval_ranking']['basis'],
                         'topical_explanatory_excerpt_then_coverage_bm25')

    def test_learning_intent_does_not_convert_diagnosis_or_selection_to_mechanism_search(self):
        self.assertFalse(is_knowledge_question('压合后白斑是什么原因，怎么排查？'))
        self.assertTrue(is_knowledge_question('压合如何选择半固化片？'))
        self.assertFalse(is_explanation_question('压合如何选择半固化片？'))
        missing = ChatSession(self.catalog, generator=None).ask('什么是不存在的专用术语？')
        self.assertEqual(missing['citations'], [])
        self.assertNotIn('具体缺陷', missing['answer'])
        self.assertIn('没有返回', missing['answer'])

    def test_material_comparison_keeps_both_complete_definitions_and_excludes_formula(self):
        before = digest_file(self.catalog / 'catalog.sqlite3')
        question = 'Tg与Td有什么区别？Tg能当作通用卸压温度吗？'
        result = ChatSession(self.catalog, generator=None, limit=1).ask(question)
        self.assertEqual(result['sources'][0]['source_id'], 'tg_td_definition')
        body = result['answer'].split('## 可追溯依据')[0]
        self.assertIn('changes from a glass-', body)
        self.assertIn('begins to decompose', body)
        self.assertNotIn('Percent TE', result['answer'])
        self.assertNotIn('具体缺陷', result['answer'])
        self.assertIn('尚未核实的适用性子问题：Tg 能否作为通用卸压温度', body)
        self.assertEqual(result['clarifying_questions'], [])
        self.assertEqual(result['citation_validation']['status'], 'passed')
        self.assertEqual(result['result_fingerprint'], fingerprint_chat_result(result))
        with closing(connect_readonly(self.catalog)) as con:
            for citation in result['citations']:
                text = con.execute('SELECT COALESCE(c.normalized_text,c.original_text) '
                                   'FROM documents d JOIN contents c USING(content_id) WHERE doc_id=?',
                                   (citation['doc_id'],)).fetchone()[0]
                self.assertEqual(citation['text'], text[citation['start']:citation['end']])
                self.assertEqual(citation['sha256'], hashlib.sha256(citation['text'].encode()).hexdigest())
                self.assertLessEqual(len(citation['text']), MAX_EXCERPT_CHARS)
        self.assertEqual(digest_file(self.catalog / 'catalog.sqlite3'), before)

    def test_rf_gt_applicability_prefers_test_descriptions_without_defect_questionnaire(self):
        question = '树脂流动度RF和凝胶时间GT，能直接当作实板流胶量和填充时间吗？'
        result = ChatSession(self.catalog, generator=None, limit=2).ask(question)
        self.assertEqual({s['source_id'] for s in result['sources']}, {'rf_definition', 'gt_definition'})
        self.assertIn('Resin Flow Percent by weight in prepreg.', result['answer'])
        self.assertIn('gel time of resin preimpregnated B', result['answer'])
        self.assertNotIn('RF 射频', result['answer'])
        self.assertNotIn('可能原因与排查方向', result['answer'])
        self.assertNotIn('具体缺陷', result['answer'])
        self.assertIn('RF 与实板流胶量的对应关系', result['answer'])
        self.assertIn('GT 与实板有效填充时间的对应关系', result['answer'])
        self.assertIn('不能仅凭', result['answer'])
        self.assertEqual(result['clarifying_questions'], [])
        self.assertEqual(result['citation_validation']['status'], 'passed')
        self.assertEqual(result['result_fingerprint'], fingerprint_chat_result(result))

    def test_material_scope_resets_previous_defect_and_preserves_explicit_diagnosis(self):
        question = 'RF和GT能直接当作实板流胶量和填充时间吗？'
        case, _ = pcb_assistant._case_context(question, [{'role': 'user', 'content': '压合后出现白斑怎么排查？'}])
        self.assertIsNone(case)
        outcome = pcb_assistant.answer({'question': question, 'limit': 2,
                                       'history': [{'role': 'user', 'content': '压合后出现白斑怎么排查？'}]},
                                      'REQ_material_scope', catalog_directory=self.catalog)
        self.assertEqual(outcome.result['workflow']['route'], 'knowledge')
        self.assertNotIn('白斑', outcome.result['answer'])
        self.assertFalse(outcome.external_services)
        self.assertEqual(outcome.result['citation_validation']['status'], 'passed')
        for text in ('RF与GT有什么区别？', 'RF與GT能直接當作實板流膠量和填充時間嗎？',
                     'Can resin flow and gel time represent actual board filling time?'):
            self.assertTrue(is_knowledge_question(text))
            self.assertTrue(is_explanation_question(text))
        diagnostic = '压合后缺胶，RF和GT异常怎么排查？'
        self.assertFalse(is_knowledge_question(diagnostic))
        self.assertIsNotNone(pcb_assistant._case_context(diagnostic, [])[0])

    def test_material_name_and_test_heading_are_not_complete_definitions(self):
        self.assertEqual(material_concepts('Percent TE = tD - tA'), ())
        self.assertEqual(material_concepts('RF 射频 coupon 测量'), ())
        self.assertEqual(definition_keys('TG – Glass Transition Temperature;\nTD – Decomposition Temperature;', ('tg', 'td')), ())
        self.assertEqual(definition_keys('Typical Values Table\nGlass Transition Temperature (Tg) by DSC\nDecomposition Temperature (Td) by TGA @ 5% weight loss', ('tg', 'td')), ())
        self.assertEqual(definition_keys('Table\nProperty\nTypical Value\nUnits\nTest Method\nGlass Transition Temperature (Tg) by DSC\n180\nDecomposition Temperature (Td) by TGA @ 5% weight loss\n340', ('tg', 'td')), ())
        missing = ChatSession(self.catalog, generator=None, limit=1).ask(
            'RF和GT能直接当作实板流胶量和填充时间吗？', doc_type='PUBLIC')
        self.assertEqual(missing['citations'], [])
        self.assertIn('未提供完整定义或测试说明', missing['answer'])
        self.assertIn('尚未核实的适用性子问题', missing['answer'])
        self.assertNotIn('具体缺陷', missing['answer'])
        partial = material_gap_lines('Tg和Td有什么区别？', [{'text': 'Tg 是指玻璃化转变温度。'}])
        self.assertTrue(any('Td（分解温度）' in line for line in partial))
        self.assertEqual(material_gap_lines('压合为何需要卸压？', []), [])
        gt_only = '\n'.join(material_gap_lines('GT能当作实板填充时间吗？', []))
        self.assertNotIn('RF 与实板流胶量', gt_only)

    def test_local_model_knowledge_prompt_and_answer_keep_applicability_gaps(self):
        def generator(prompt, history):
            self.assertIn('逐项说明资料支持与未覆盖的子问题', prompt)
            citation = re.search(r'EXC_[0-9a-f]{24}', prompt)[0]
            return '资料提供了材料指标说明。 [' + citation + ']'
        result = ChatSession(self.catalog, generator=generator, limit=1).ask(
            'Tg与Td有什么区别？Tg能当作通用卸压温度吗？')
        self.assertTrue(result['generated_by_model'])
        self.assertIn('尚未核实的适用性子问题', result['answer'])
        self.assertNotIn('补充缺陷的现象', result['answer'])
        self.assertEqual(result['clarifying_questions'], [])
        self.assertEqual(result['citation_validation']['status'], 'passed')
