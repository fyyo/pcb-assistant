"""Synthetic checks for quote selections, duplicate storage and case provenance."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from candidate_privacy import CandidateIdentityCleaner
from catalog_build import build, normalize_case_source, primary_inputs, source_snapshots
from catalog_common import CatalogError, connect_readonly, digest_file, digest_text, json_text, stable_id
from pcb_knowledge import tokens
from scripts.data.rewash_candidate_identities import read_rows
from scripts.data.rewash_case_identities import OffsetRepair, repair_cases
from tests.test_catalog import fixture, write_json, write_rows


class OffsetRepairTests(unittest.TestCase):
    def test_repeated_names_overlapping_quotes_and_mixed_line_endings(self):
        old = '陈欣瑶确认 180℃\r\n陈欣瑶确认 压力 0.5 MPa\n[身份] 结束\r\n'
        cleaner = CandidateIdentityCleaner({'陈欣瑶'})
        new = cleaner.clean(old)
        mapping = OffsetRepair(old,new)
        for start,end in [(0,len(old)),(1,2),(old.index('压力'),old.index('\n[身份]')),
                          (old.index('陈',2),old.index('确认',8)),(0,0)]:
            result = mapping.quote({'start':start,'end':end,'text':old[start:end]})
            self.assertEqual(new[result['start']:result['end']],result['text'])
            self.assertEqual(result['text'].count('['),result['text'].count(']'))
        self.assertEqual(mapping.quote({'start':1,'end':2,'text':old[1:2]})['text'],'[身份]')
        p = old.index('压力')
        self.assertEqual(mapping.quote({'start':p,'end':p+12,'text':old[p:p+12]})['text'],old[p:p+12])

    def test_empty_unchanged_and_invalid_original_quote(self):
        self.assertEqual(OffsetRepair('','').quote({'start':0,'end':0,'text':''})['text'],'')
        with self.assertRaisesRegex(ValueError,'source_case_quote_offset_mismatch'):
            OffsetRepair('180℃','180℃').quote({'start':0,'end':2,'text':'wrong'})
        with self.assertRaisesRegex(ValueError,'case_line_structure_changed'):
            OffsetRepair('a\nb','ab')

    def test_placeholder_characters_are_never_diffed_as_partial_names(self):
        old = '王身明确认 170℃'
        new = '[身份]确认 170℃'
        self.assertEqual(OffsetRepair(old,new).quote({'start':1,'end':2,'text':'身'})['text'],'[身份]')


class CaseRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.output = self.root/'source', self.root/'output'
        self.source.mkdir()
        self.cleaner = CandidateIdentityCleaner({'陈欣瑶'})
        self.body = '陈欣瑶确认\r\n压力 0.5 MPa，温度 180℃\n陈欣瑶回复'
        start,end = self.body.index('压力'),self.body.index('\n陈',8)
        self.fields = {'conditions':[{'start':start,'end':end,'text':self.body[start:end]}],
                       'analysis':[{'start':1,'end':2,'text':self.body[1:2]}]}
        self.row = {'source_id':'mail_synthetic','subject':'陈欣瑶合成记录','topics':['压合'],
                    'fields':self.fields,'manual_review_required':False}
        write_rows(self.root,'source/cases.jsonl',[self.row])
        with closing(sqlite3.connect(self.source/'knowledge.sqlite3')) as con:
            con.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version','1');
                CREATE TABLE cases(source_id TEXT PRIMARY KEY,subject TEXT,body TEXT,topics TEXT,fields TEXT);
                CREATE TABLE chunks(id TEXT PRIMARY KEY,source_id TEXT,start INTEGER,end INTEGER,text TEXT);
                CREATE VIRTUAL TABLE search USING fts5(title_tokens,body_tokens,chunk_id UNINDEXED);''')
            con.execute('INSERT INTO cases VALUES (?,?,?,?,?)',('mail_synthetic',self.row['subject'],self.body,
                json.dumps(self.row['topics']),json.dumps(self.fields)))
            con.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',('mail_synthetic:7','mail_synthetic',0,len(self.body),self.body))
            con.execute('INSERT INTO search VALUES (?,?,?)',('oldidentity','oldidentity','mail_synthetic:7'))
            con.commit()
        self.report()

    def report(self):
        write_json(self.source/'report.json',{'counts':{'cases':1,'passages':1},'artifacts':{
            name:{'sha256':digest_file(self.source/name)} for name in ('cases.jsonl','knowledge.sqlite3')}})

    def test_moves_offsets_preserves_ids_and_rebuilds_fts_without_old_pages(self):
        before = {p.name:digest_file(p) for p in self.source.iterdir()}
        result = repair_cases(self.source,self.output,self.cleaner)
        self.assertEqual(before,{p.name:digest_file(p) for p in self.source.iterdir()})
        row = next(read_rows(self.output/'cases.jsonl'))
        self.assertEqual(row['source_id'],self.row['source_id'])
        self.assertFalse(row['manual_review_required'])
        self.assertEqual(row['fields']['conditions'][0]['text'],self.fields['conditions'][0]['text'])
        self.assertEqual(row['fields']['analysis'][0]['text'],'[身份]')
        self.assertEqual(result['statistics']['cases_changed'],1)
        with closing(sqlite3.connect(self.output/'knowledge.sqlite3')) as con:
            subject,body = con.execute('SELECT subject,body FROM cases').fetchone()
            self.assertNotIn('陈欣瑶',subject+body)
            self.assertEqual(con.execute('SELECT id FROM chunks').fetchone()[0],'mail_synthetic:7')
            self.assertEqual(con.execute("SELECT count(*) FROM search WHERE search MATCH 'oldidentity'").fetchone()[0],0)
            search = con.execute('SELECT title_tokens,body_tokens FROM search').fetchone()
            self.assertEqual(search,(' '.join(tokens(subject)),' '.join(tokens(body))))
        self.assertNotIn('陈欣瑶'.encode(),(self.output/'knowledge.sqlite3').read_bytes())

    def test_rejects_json_sqlite_drift_before_success_report(self):
        with closing(sqlite3.connect(self.source/'knowledge.sqlite3')) as con:
            con.execute("UPDATE cases SET subject='不同标题'")
            con.commit()
        self.report()
        with self.assertRaisesRegex(ValueError,'case_json_sqlite_disagree'):
            repair_cases(self.source,self.output,self.cleaner)
        self.assertFalse((self.output/'report.json').exists())
        self.assertTrue((self.output/'REPAIR_FAILED.json').exists())

    def test_rejects_numbers_changed_by_cleaner_and_existing_outputs(self):
        class BadCleaner(CandidateIdentityCleaner):
            def clean(self,text):
                return text.replace('180','190')
        with self.assertRaisesRegex(ValueError,'case_text_audit_failed'):
            repair_cases(self.source,self.output,BadCleaner(set()))
        with self.assertRaisesRegex(ValueError,'output_already_exists'):
            repair_cases(self.source,self.output,self.cleaner)


class CatalogCaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)

    def test_explicit_case_directory_keeps_stable_ids_and_logical_version(self):
        baseline = self.root/'data/phase3_catalog_before'
        with redirect_stdout(io.StringIO()):
            build(self.root,baseline)
        source = self.root/'data/knowledge_cleaned'
        shutil.copytree(self.root/'data/knowledge_v1',source)
        row = next(read_rows(source/'cases.jsonl'))
        row['subject'] = '修复后的压合案例'
        prefix = '新上下文\n'
        for quotes in row['fields'].values():
            for q in quotes:
                q['start'] += len(prefix)
                q['end'] += len(prefix)
        write_rows(self.root,'data/knowledge_cleaned/cases.jsonl',[row])
        with closing(sqlite3.connect(source/'knowledge.sqlite3')) as con:
            con.execute('UPDATE cases SET body=? || body',(prefix,))
            con.commit()
        write_json(source/'report.json',{'artifacts':{name:{'sha256':digest_file(source/name)}
                   for name in ('cases.jsonl','knowledge.sqlite3')}})
        selected = {'base':'data/knowledge_cleaned','version':'knowledge_v1'}
        before = source_snapshots(self.root,case_source=selected)
        output = self.root/'data/phase3_catalog_after'
        with redirect_stdout(io.StringIO()):
            build(self.root,output,case_source=selected)
        self.assertEqual(source_snapshots(self.root,case_source=selected),before)
        self.assertNotIn('data/knowledge_v1/cases.jsonl',before)
        with closing(connect_readonly(baseline)) as a, closing(connect_readonly(output)) as b:
            self.assertEqual({r[0] for r in a.execute('SELECT doc_id FROM documents')},
                             {r[0] for r in b.execute('SELECT doc_id FROM documents')})
            row = b.execute('SELECT source_path,source_version,title FROM documents WHERE doc_id=?',
                            (stable_id('CASE','mail_a'),)).fetchone()
            self.assertEqual(tuple(row),('data/knowledge_cleaned/cases.jsonl','knowledge_v1','修复后的压合案例'))

    def test_defaults_and_invalid_source(self):
        self.assertEqual(normalize_case_source(),{'base':'data/knowledge_v1','version':'knowledge_v1'})
        self.assertIn('data/knowledge_v1/cases.jsonl',primary_inputs())
        for value in ([],{}, {'base':''},{'base':'a','version':2}):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                normalize_case_source(value)

    def test_excluded_historical_text_can_be_omitted_without_losing_provenance(self):
        output = self.root/'data/phase3_catalog_provenance'
        before = source_snapshots(self.root)
        with redirect_stdout(io.StringIO()):
            build(self.root,output,historical_exclusions='provenance_only')
        self.assertEqual(source_snapshots(self.root),before)
        old = list(read_rows(self.root/'data/attachments_v1/engineering_candidates.jsonl'))[-1]
        with closing(connect_readonly(output)) as con:
            record=json.loads(con.execute("SELECT original_record_json FROM excluded_records WHERE reason='historical_record_without_current_identity'").fetchone()[0])
        self.assertEqual(record,{'source_id':'obsolete_unit','record_representation':'provenance_only',
                                'source_record_sha256':digest_text(json_text(old)),'text_imported':False})
        with self.assertRaisesRegex(CatalogError,'invalid_historical_exclusions_mode'):
            build(self.root,self.root/'data/phase3_catalog_invalid',historical_exclusions='bad')


if __name__ == '__main__':
    unittest.main()
