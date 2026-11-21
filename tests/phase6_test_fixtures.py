"""Small synthetic Catalog for protocol tests, independent of local delivery data."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pcb_assistant.catalog.chat as catalog_chat
import pcb_assistant.interfaces.mcp as phase6_mcp
from pcb_assistant.catalog.build import PRIMARY_INPUTS, build
from pcb_assistant.catalog.common import digest_file, digest_text, stable_id
from tests.test_catalog import fixture, write_json, write_rows

DOC_ID = stable_id('KNOWLEDGE', 'case', 'mail_a')
MAIL_ID = stable_id('MAIL', 'mail_a')


class SyntheticCatalogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fixture(root)
        text = ('合成软件记录：压合后出现白斑、分层和板弯翘，建议核对 PP 受潮、真空、压力、'
                '板弯翘改善记录和固化条件。温度 180℃，压力 0.5 MPa，保温 60 min。'
                '这些数字仅为合成测试输入，不是生产工艺建议。')
        for relative in ('data/sanitized_v3/candidates.jsonl',
                         'data/attachments_v2/engineering_candidates.jsonl'):
            rows = [json.loads(line) for line in (root/relative).read_text(encoding='utf-8').splitlines()]
            for row in rows:
                row['text'] = text
                row['subject'] = '白斑分层板弯翘改善合成测试'
            write_rows(root, relative, rows)
        cases = [{'source_id': name, 'subject': '白斑分层合成案例', 'topics': ['压合'],
                  'fields': {'phenomenon': [{'text': text, 'start': 0, 'end': len(text)}]},
                  'evidence_status': 'synthetic', 'local_use_allowed': True,
                  'manual_review_required': False, 'independently_verified': False}
                 for name in ('mail_a', 'mail_b')]
        write_rows(root, 'data/knowledge_v1/cases.jsonl', cases)
        with closing(sqlite3.connect(root/'data/knowledge_v1/knowledge.sqlite3')) as con:
            con.execute('UPDATE cases SET body=?', (text,))
            con.execute('INSERT INTO cases VALUES (?,?)', ('mail_b', text))
            con.commit()
        for version, ordinal, filename in (('v1','page','pages.jsonl'), ('v2','unit','units.jsonl')):
            write_rows(root, f'data/public_lamination_{version}/{filename}', [
                {'source_id': 'public_source', ordinal: 1, 'text': text, 'sha256': digest_text(text)}])
        reports = {}
        for relative in PRIMARY_INPUTS:
            path = root/relative
            reports.setdefault(path.parent, {'artifacts': {}})['artifacts'][path.name] = {'sha256': digest_file(path)}
        for parent, report in reports.items():
            write_json(parent/'report.json', report)
        cls.catalog = root/'data/phase3_catalog_test'
        with redirect_stdout(io.StringIO()):
            build(root, cls.catalog)
        cls.catalog_version = phase6_mcp.catalog_version(cls.catalog)
        patcher = patch.object(catalog_chat, 'DEFAULT_CATALOG', cls.catalog)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        context = phase6_mcp._catalog_context(cls.catalog)
        context.__enter__()
        cls.addClassCleanup(context.__exit__, None, None, None)


class SyntheticManagedCatalogTestCase(SyntheticCatalogTestCase):
    """Use real privacy rules with authored NER fixtures, without model downloads."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import spacy
        import pcb_assistant.interfaces.managed as managed
        from pcb_assistant.privacy.mail import IdentityIndex, Sanitizer

        zh = spacy.blank('zh')
        en = spacy.blank('en')
        zh.add_pipe('entity_ruler').add_patterns([{'label': 'PERSON', 'pattern': '测试小甲'}])
        en.add_pipe('entity_ruler').add_patterns([{'label': 'PERSON', 'pattern': 'Fixture Person'}])
        patcher = patch.object(managed, '_SANITIZER_COMPONENTS',
                               (IdentityIndex().pattern(), Sanitizer, zh, en))
        patcher.start()
        cls.addClassCleanup(patcher.stop)
