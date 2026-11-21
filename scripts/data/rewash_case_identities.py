"""Repair case bodies and move existing quote/chunk boundaries with the edits."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from difflib import SequenceMatcher
import json
from pathlib import Path
import sqlite3

from pcb_assistant.catalog.common import digest_file
from pcb_assistant.privacy.mail import PLACEHOLDER
from pcb_assistant.knowledge import tokens
from scripts.data.rewash_candidate_identities import NUMBERS, RELEASE, read_rows, snapshot, write_json


class OffsetRepair:
    """Map original selections through a line-preserving text repair.

    Placeholders are indivisible diff tokens. A boundary inside a replaced name
    expands to include the complete replacement, never half of a placeholder.
    Matching one line at a time avoids aligning repeated passages across lines.
    """
    def __init__(self, original, repaired):
        self.original, self.repaired = original, repaired
        self.left = [None] * (len(original) + 1)
        self.right = [None] * (len(original) + 1)
        old_lines = original.splitlines(keepends=True)
        new_lines = repaired.splitlines(keepends=True)
        if len(old_lines) != len(new_lines):
            raise ValueError('case_line_structure_changed')
        old_base = new_base = 0
        for old, new in zip(old_lines, new_lines):
            if old == new:
                for i in range(len(old)+1):
                    self._put(old_base+i, new_base+i, new_base+i)
            else:
                a, ap = self._tokens(old)
                b, bp = self._tokens(new)
                for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                    start, end = old_base+ap[i], old_base+ap[j]
                    target, finish = new_base+bp[k], new_base+bp[l]
                    self._put(start, target, target)
                    self._put(end, finish, finish)
                    if tag == 'equal':
                        for pos in range(start+1, end):
                            self._put(pos, target+pos-start, target+pos-start)
                    elif start == end:
                        self._put(start, target, finish)
                    else:
                        for pos in range(start+1, end):
                            self._put(pos, target, finish)
            old_base += len(old)
            new_base += len(new)
        if not original:
            self._put(0, 0, len(repaired))
        if any(x is None for x in self.left+self.right):
            raise ValueError('case_boundary_map_incomplete')

    @staticmethod
    def _tokens(text):
        parts, offsets, pos = [], [0], 0
        for match in PLACEHOLDER.finditer(text):
            for char in text[pos:match.start()]:
                parts.append(char)
                offsets.append(offsets[-1]+1)
            parts.append(match[0])
            offsets.append(match.end())
            pos = match.end()
        for char in text[pos:]:
            parts.append(char)
            offsets.append(offsets[-1]+1)
        return parts, offsets

    def _put(self, index, low, high):
        self.left[index] = low if self.left[index] is None else min(self.left[index], low)
        self.right[index] = high if self.right[index] is None else max(self.right[index], high)

    def quote(self, quote):
        start, end = quote['start'], quote['end']
        if (not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start <= end <= len(self.original)
                or self.original[start:end] != quote['text']):
            raise ValueError('source_case_quote_offset_mismatch')
        lo, hi = self.left[start], self.right[end]
        if start == end:
            hi = lo
        result = {**quote, 'start': lo, 'end': hi, 'text': self.repaired[lo:hi]}
        if NUMBERS.findall(quote['text']) != NUMBERS.findall(result['text']):
            raise ValueError('case_quote_numbers_changed')
        return result


def repair_cases(source, output, cleaner):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('output_already_exists')
    if output == source or source in output.parents:
        raise ValueError('output_overlaps_source')
    report = json.loads((source/'report.json').read_text(encoding='utf-8'))
    inputs = [source/'report.json']
    for name, expected in report['artifacts'].items():
        path = (source/name).resolve()
        if path.parent != source or digest_file(path) != expected['sha256']:
            raise ValueError('source_artifact_mismatch')
        inputs.append(path)
    before = snapshot(inputs)
    stats, operations_before = Counter(), cleaner.counts.copy()
    output.mkdir(parents=True, exist_ok=False)
    uri = (source/'knowledge.sqlite3').as_uri()+'?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True)) as old, closing(sqlite3.connect(output/'knowledge.sqlite3')) as new:
            old.row_factory = sqlite3.Row
            old.execute('PRAGMA query_only=ON')
            # A fresh database avoids retaining old identities in free pages or FTS.
            new.executescript('''
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE cases (source_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    body TEXT NOT NULL, topics TEXT NOT NULL, fields TEXT NOT NULL);
                CREATE TABLE chunks (id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES cases(source_id));
                CREATE INDEX chunks_source ON chunks(source_id);
                CREATE VIRTUAL TABLE search USING fts5(title_tokens, body_tokens, chunk_id UNINDEXED);
            ''')
            new.executemany('INSERT INTO metadata VALUES (?,?)',old.execute('SELECT * FROM metadata'))
            with (output/'cases.jsonl').open('w', encoding='utf-8', newline='\n') as stream:
                for original in read_rows(source/'cases.jsonl'):
                    row = deepcopy(original)
                    saved = old.execute('SELECT * FROM cases WHERE source_id=?',(row['source_id'],)).fetchone()
                    if (saved is None or saved['subject'] != row['subject']
                            or json.loads(saved['fields']) != row['fields']
                            or json.loads(saved['topics']) != row['topics']):
                        raise ValueError('case_json_sqlite_disagree')
                    subject, body = cleaner.clean(saved['subject']), cleaner.clean(saved['body'])
                    for a,b in ((saved['subject'],subject),(saved['body'],body)):
                        if NUMBERS.findall(a) != NUMBERS.findall(b) or cleaner.residuals(b):
                            raise ValueError('case_text_audit_failed')
                    offsets = OffsetRepair(saved['body'],body)
                    for field, quotes in row['fields'].items():
                        row['fields'][field] = [offsets.quote(q) for q in quotes]
                        stats['quotes_verified'] += len(quotes)
                        stats['quotes_changed'] += sum(a != b for a,b in zip(quotes,row['fields'][field]))
                        if any(cleaner.residuals(q['text']) for q in row['fields'][field]):
                            raise ValueError('case_quote_residual_audit_failed')
                    row['subject'] = subject
                    stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                    new.execute('INSERT INTO cases VALUES (?,?,?,?,?)',
                        (row['source_id'],subject,body,json.dumps(row['topics'],ensure_ascii=False),
                         json.dumps(row['fields'],ensure_ascii=False)))
                    for chunk in old.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY rowid',(row['source_id'],)):
                        updated = offsets.quote(dict(chunk))
                        if cleaner.residuals(updated['text']):
                            raise ValueError('case_chunk_residual_audit_failed')
                        new.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                            (chunk['id'],row['source_id'],updated['start'],updated['end'],updated['text']))
                        new.execute('INSERT INTO search VALUES (?,?,?)',
                            (' '.join(tokens(subject)),' '.join(tokens(updated['text'])),chunk['id']))
                        stats['chunks'] += 1
                        stats['chunks_changed'] += updated != dict(chunk)
                    stats['cases'] += 1
                    stats['cases_changed'] += saved['body'] != body or saved['subject'] != subject
            if stats['cases'] != old.execute('SELECT count(*) FROM cases').fetchone()[0]:
                raise ValueError('case_count_changed')
            if stats['chunks'] != old.execute('SELECT count(*) FROM chunks').fetchone()[0]:
                raise ValueError('case_chunk_count_changed')
            new.execute("INSERT INTO search(search) VALUES ('optimize')")
            new.commit()
            if new.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or new.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('case_database_integrity_failed')
        # Reopen and verify JSON, SQLite, chunks, quotes, and FTS independently.
        with closing(sqlite3.connect((output/'knowledge.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
            for row in read_rows(output/'cases.jsonl'):
                subject,body,fields = con.execute('SELECT subject,body,fields FROM cases WHERE source_id=?',(row['source_id'],)).fetchone()
                if subject != row['subject'] or json.loads(fields) != row['fields'] or cleaner.residuals(body):
                    raise ValueError('case_readback_mismatch')
                for quotes in row['fields'].values():
                    for q in quotes:
                        if body[q['start']:q['end']] != q['text']:
                            raise ValueError('repaired_case_quote_offset_mismatch')
                for start,end,text in con.execute('SELECT start,end,text FROM chunks WHERE source_id=?',(row['source_id'],)):
                    if body[start:end] != text:
                        raise ValueError('repaired_chunk_offset_mismatch')
            if con.execute('SELECT count(*) FROM search').fetchone()[0] != stats['chunks']:
                raise ValueError('case_fts_count_mismatch')
        if snapshot(inputs) != before:
            raise ValueError('source_changed_during_repair')
        write_json(output/'RELEASE_BLOCKED.json',RELEASE)
        rewash = {'source_run':source.name,'output_run':output.name,'kind':'case',
            'source_snapshots':before,'source_unchanged':True,'statistics':dict(stats),
            'operations':dict(cleaner.counts-operations_before),'ids_preserved':True,
            'numbers_preserved':True,'offsets_recomputed':True,'residual_audit':{}}
        result = {**report,'status':'completed_identity_repair_local_candidates','rewash':rewash,'release':RELEASE}
        result['artifacts'] = {p.name:{'sha256':digest_file(p),'size_bytes':p.stat().st_size}
                               for p in output.iterdir() if p.is_file()}
        write_json(output/'report.json',result)
        return {'source':source.name,'output':output.name,**rewash}
    except Exception as exc:
        write_json(output/'REPAIR_FAILED.json',{'status':'failed','error_type':type(exc).__name__,
            'error_code':str(exc) if isinstance(exc,ValueError) else 'case_repair_failed'})
        raise
