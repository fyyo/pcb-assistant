"""Catalog selection must cover every operation without leaking into other runs."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pcb_assistant.catalog.build import ATTACHMENT_FILES, build
from pcb_assistant.catalog.common import digest_file, stable_id
from pcb_assistant.paths import ROOT
import pcb_assistant.router as pcb_assistant
import pcb_assistant.evaluation.stdio_trial as trial
import pcb_assistant.interfaces.mcp as mcp
from tests.test_catalog import fixture, write_json, write_rows


class McpCatalogSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.catalogs = []
        for name in ('first', 'second'):
            root = cls.root / name
            fixture(root)
            if name == 'second':
                def append(relative, row):
                    path = root / relative
                    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                    write_rows(root, relative, records + [row])
                append('data/sanitized_v3/candidates.jsonl', {
                    'id': 'mail_selected', 'subject': '压合测试邮件', 'text': 'selectionmarker 压合试验记录',
                    'topics': ['压合'], 'source_kind': 'untrusted_email'})
                append('data/attachments_v2/documents.jsonl', {
                    'document_id': 'att_selected', 'parent_source_ids': ['mail_selected'],
                    'format': 'pdf', 'status': 'processed', 'exported_units': 1})
                append('data/attachments_v2/engineering_candidates.jsonl', {
                    'id': 'unit_selected', 'document_id': 'att_selected', 'subject': '压合测试附件',
                    'text': 'selectionmarker PP受潮导致白斑。', 'topics': ['压合'],
                    'document_topics': [], 'parent_source_ids': ['mail_selected'],
                    'extraction_method': 'native_text'})
                append('data/attachments_v2/occurrences.jsonl', {
                    'document_id': 'att_selected', 'parent_source_id': 'mail_selected',
                    'attachment_ordinal': 0})
                for directory, files in [('data/sanitized_v3', ['candidates.jsonl']),
                                         ('data/attachments_v2', ATTACHMENT_FILES)]:
                    write_json(root / directory / 'report.json', {'artifacts': {
                        file: {'sha256': digest_file(root / directory / file)} for file in files}})
            output = root / 'data/phase3_catalog_test'
            with redirect_stdout(io.StringIO()):
                build(root, output)
            cls.catalogs.append(output)
        cls.first, cls.second = cls.catalogs
        cls.mail_id = stable_id('MAIL', 'mail_selected')
        cls.unit_id = stable_id('KNOWLEDGE', 'attachment', 'unit_selected')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mcp.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_cli_stats_and_envelope_select_same_catalog_without_global_mutation(self):
        default = mcp._catalog_directory()
        code, out, err = self.cli('--catalog', str(self.second), '--call', 'pcb_answer',
                                 '--arguments-json', '{"question":"本地知识库有多少文档？"}')
        self.assertEqual((code, err), (0, ''))
        envelope = json.loads(out)
        self.assertEqual(envelope['catalog_version'], mcp.catalog_version(self.second))
        self.assertEqual(envelope['result']['operation_result']['catalog']['totals']['documents'], 15)
        self.assertEqual(mcp._catalog_directory(), default)

    def test_nested_contexts_and_version_cache_restore_the_correct_catalog(self):
        default = mcp.catalog_version()
        with mcp._catalog_context(self.first):
            first = mcp.catalog_version()
            with mcp._catalog_context(self.second):
                self.assertNotEqual(mcp.catalog_version(), first)
                self.assertEqual(mcp._error_envelope('REQ_context', 'invalid_request', 'test')
                                 ['catalog_version'], mcp.catalog_version(self.second))
            self.assertEqual(mcp.catalog_version(), first)
        self.assertEqual(mcp.catalog_version(), default)

    def test_search_evidence_relations_and_linked_text_all_use_selected_catalog(self):
        with mcp._catalog_context(self.second):
            found, error = mcp.call_tool('pcb_search', {'query': 'selectionmarker'}, 'REQ_search')
            self.assertFalse(error)
            self.assertGreater(found['result']['total'], 0)
            evidence, error = mcp.call_tool('pcb_evidence', {'query': 'selectionmarker'}, 'REQ_evidence')
            self.assertFalse(error)
            self.assertGreater(evidence['result']['excerpt_count'], 0)
            related, error = mcp.call_tool('pcb_related', {'doc_id': self.mail_id}, 'REQ_related')
            self.assertFalse(error)
            self.assertEqual(related['result']['total'], 1)
            document, error = mcp.call_tool('pcb_get_document', {
                'doc_id': self.mail_id, 'include_text': True, 'include_attachments': True}, 'REQ_doc')
        self.assertFalse(error)
        self.assertIn('selectionmarker', document['result']['text'])
        self.assertIn('selectionmarker', document['result']['attachments'][0]['text'])
        self.assertIn(self.unit_id, document['result']['attachments'][0]['content_doc_ids'])
        with mcp._catalog_context(self.first):
            _document, error = mcp.call_tool('pcb_get_document', {'doc_id': self.mail_id}, 'REQ_absent')
        self.assertTrue(error)

    def test_knowledge_and_diagnosis_receive_selected_directory(self):
        with mcp._catalog_context(self.second):
            with patch.object(pcb_assistant.phase6_managed, 'answer',
                              wraps=pcb_assistant.phase6_managed.answer) as knowledge:
                envelope, error = mcp.call_tool('pcb_answer', {'question': '什么是白斑？'}, 'REQ_knowledge')
            self.assertFalse(error)
            self.assertEqual(envelope['result']['workflow']['route'], 'knowledge')
            self.assertEqual(knowledge.call_args.kwargs['catalog_directory'], self.second)
            with patch.object(pcb_assistant, 'CatalogRetriever', wraps=pcb_assistant.CatalogRetriever) as retriever:
                envelope, error = mcp.call_tool('pcb_answer', {
                    'question': '压合后出现白斑，怎么排查？'}, 'REQ_diagnosis')
            self.assertFalse(error)
            self.assertEqual(envelope['result']['workflow']['route'], 'diagnosis')
            self.assertEqual(retriever.call_args.args[0], self.second)

    def test_invalid_directory_never_serves_or_creates_a_database(self):
        missing = self.root / 'missing'
        with patch.object(mcp, 'serve') as serve:
            code, out, err = self.cli('--catalog', str(missing))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(), 'selected Catalog is unavailable or invalid')
        self.assertNotIn(str(missing), err)
        self.assertFalse(missing.exists())
        serve.assert_not_called()
        self.assertEqual(mcp._catalog_directory(), mcp.DEFAULT_CATALOG)

    def test_metadata_only_database_is_not_accepted_as_a_catalog(self):
        directory = self.root / 'local_full'
        directory.mkdir()
        with closing(sqlite3.connect(directory / 'catalog.sqlite3')) as con:
            con.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
            con.execute("INSERT INTO metadata VALUES ('schema_version','1')")
            con.execute("INSERT INTO metadata VALUES ('privacy_level','\"local_full\"')")
            con.commit()
        with patch.object(mcp, 'serve') as serve:
            code, out, _err = self.cli('--catalog', str(directory))
        self.assertEqual((code, out), (2, ''))
        serve.assert_not_called()

    def test_missing_registry_is_rejected_before_protocol_startup(self):
        directory = self.root / 'no_registry'
        directory.mkdir()
        shutil.copyfile(self.second / 'catalog.sqlite3', directory / 'catalog.sqlite3')
        code, out, _err = self.cli('--catalog', str(directory), '--list-tools')
        self.assertEqual((code, out), (2, ''))

    def test_self_test_reports_selected_version_and_keeps_one_tool(self):
        code, out, err = self.cli('--catalog', str(self.second), '--self-test')
        result = json.loads(out)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(result['catalog_version'], mcp.catalog_version(self.second))
        self.assertEqual(result['tools'], 1)
        self.assertTrue(all(call['catalog_matches'] for call in result['calls']))

    def test_real_stdio_and_compatibility_alias_accept_an_explicit_directory(self):
        root = ROOT
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'pcb_answer', 'arguments': {'question': '本地知识库有多少文档？'}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'pcb_answer', 'arguments': {'question': '读取文档及附件 ' + self.mail_id}}},
        ]
        for entry, cwd in [([str(root / 'phase6_mcp.py')], self.root),
                           (['-m', 'pcb_assistant.interfaces.mcp'], root),
                           (['-m', 'agent.mcp'], root)]:
            with self.subTest(entry=entry):
                completed = subprocess.run([sys.executable, '-X', 'utf8', '-B', *entry,
                    '--catalog', str(self.second)], input='\n'.join(json.dumps(r) for r in requests)+'\n',
                    cwd=cwd, text=True, encoding='utf-8', capture_output=True, timeout=30)
                self.assertEqual((completed.returncode, completed.stderr), (0, ''))
                results = [json.loads(line)['result'] for line in completed.stdout.splitlines()]
                self.assertEqual([t['name'] for t in results[0]['tools']], ['pcb_answer'])
                self.assertEqual(results[1]['structuredContent']['catalog_version'], mcp.catalog_version(self.second))
                document = results[2]['structuredContent']['result']['operation_result']
                self.assertEqual(document['doc_id'], self.mail_id)
                self.assertIn('selectionmarker', document['attachments'][0]['text'])

    def test_client_preflight_forwards_selection_and_checks_returned_version(self):
        result = trial.run_trial(catalog=self.second)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['catalog_versions'], [mcp.catalog_version(self.second)])
        self.assertTrue(any(r['check'] == 'selected_catalog_version_matches' and r['status'] == 'passed'
                            for r in result['checks']))
        failed = trial.evaluate_responses([], [], expected_catalog_version=mcp.catalog_version(self.second))
        self.assertEqual(failed['status'], 'failed')

    def test_question_arguments_cannot_override_catalog(self):
        _envelope, error = mcp.call_tool('pcb_answer', {
            'question': '本地知识库有多少文档？', 'catalog': str(self.second)}, 'REQ_override')
        self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()
