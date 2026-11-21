'''Private feedback ledger for corrections made during internal pilot use.

The ledger is deliberately outside the catalog and outside the training data
pipeline. It records what a user says was wrong, links the note to the prior
answer fingerprint and citations, and never rewrites the answer automatically.
'''
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import time

try:
    import fcntl
except ImportError:  # Native Windows has no fcntl.
    fcntl = None
try:
    import msvcrt
except ImportError:  # Unix uses fcntl instead.
    msvcrt = None

from pcb_assistant.catalog.common import CatalogError


from pcb_assistant.paths import ROOT
DEFAULT_FEEDBACK_ROOT = ROOT / '.private' / 'internal_pilot_feedback'
FEEDBACK_SCHEMA = 'pcb-internal-pilot-feedback-v1'
STATUS_SCHEMA = 'pcb-internal-pilot-feedback-status-v1'
LIST_SCHEMA = 'pcb-internal-pilot-feedback-list-v1'
DETAIL_SCHEMA = 'pcb-internal-pilot-feedback-detail-v1'
FEEDBACK_KINDS = (
    'wrong_citation',
    'wrong_retrieval',
    'wrong_conclusion',
    'missing_context',
    'other',
)
MAX_FEEDBACK_CHARS = 4000
MAX_RESOLUTION_CHARS = 1000
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_EXCERPT = re.compile(r'^EXC_[0-9a-f]{24}$')
_FEEDBACK = re.compile(r'^FBK_[0-9a-f]{24}$')


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _feedback_id(seed):
    digest = hashlib.sha256(
        (_canonical(seed) + '|' + str(time.time_ns())).encode('utf-8')).hexdigest()
    return 'FBK_' + digest[:24]


def _validate_feedback_id(value):
    if not isinstance(value, str) or not _FEEDBACK.fullmatch(value):
        raise CatalogError('invalid_feedback_id')
    return value


def _read_rows(path, *, strict=False):
    if not path.is_file():
        return [], 0
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError('feedback_ledger_unreadable') from exc
    rows = []
    invalid = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if (not isinstance(row, dict)
                or row.get('schema') != FEEDBACK_SCHEMA
                or not isinstance(row.get('feedback_id'), str)
                or not _FEEDBACK.fullmatch(row['feedback_id'])):
            invalid += 1
            continue
        rows.append(row)
    if strict and invalid:
        raise CatalogError('feedback_ledger_invalid')
    return rows, invalid


def _turn_citation_ids(turn):
    citations = turn.get('citations')
    if not isinstance(citations, list):
        return []
    ids = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        excerpt_id = citation.get('excerpt_id')
        if isinstance(excerpt_id, str) and _EXCERPT.fullmatch(excerpt_id):
            ids.append(excerpt_id)
    return ids


def _normalize_citations(turn, citation_ids):
    available = _turn_citation_ids(turn)
    if citation_ids is None:
        return available
    if isinstance(citation_ids, str):
        citation_ids = [citation_ids]
    if not isinstance(citation_ids, (list, tuple)):
        raise CatalogError('invalid_feedback_citation_ids')
    selected = []
    for item in citation_ids:
        if not isinstance(item, str) or not _EXCERPT.fullmatch(item):
            raise CatalogError('invalid_feedback_citation')
        if item not in available:
            raise CatalogError('feedback_citation_not_in_turn')
        if item not in selected:
            selected.append(item)
    return selected


@contextmanager
def _ledger_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / '.lock'
    with lock_path.open('a+b') as stream:
        if os.name == 'nt':
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_feedback(turn, text, *, kind='other', citation_ids=None,
                    root=DEFAULT_FEEDBACK_ROOT, created_at=None):
    '''Append one private correction linked to a completed chat turn.'''
    if not isinstance(turn, dict):
        raise CatalogError('feedback_requires_previous_turn')
    fingerprint = turn.get('result_fingerprint')
    if not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint):
        raise CatalogError('feedback_turn_fingerprint_invalid')
    if kind not in FEEDBACK_KINDS:
        raise CatalogError('invalid_feedback_kind')
    if not isinstance(text, str):
        raise CatalogError('invalid_feedback_text')
    text = text.strip()
    if not text or len(text) > MAX_FEEDBACK_CHARS:
        raise CatalogError('invalid_feedback_text')
    selected = _normalize_citations(turn, citation_ids)
    created_at = created_at or _now()
    if not isinstance(created_at, str) or not created_at.strip():
        raise CatalogError('invalid_feedback_created_at')
    base = {
        'schema': FEEDBACK_SCHEMA,
        'created_at': created_at,
        'kind': kind,
        'text': text,
        'status': 'open',
        'turn_fingerprint': fingerprint,
        'result_fingerprint': fingerprint,
        'citation_ids': selected,
        'answer_mode': turn.get('answer_mode'),
        'evidence_status': turn.get('evidence_status'),
        'catalog_write': False,
        'external_services': False,
    }
    record = {'feedback_id': _feedback_id(base), **base}
    root = Path(root)
    with _ledger_lock(root):
        with (root / 'feedback.jsonl').open(
                'a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return {
        'status': 'recorded',
        'schema': FEEDBACK_SCHEMA,
        'feedback_id': record['feedback_id'],
        'kind': kind,
        'turn_fingerprint': fingerprint,
        'citation_ids': list(selected),
        'storage': 'private_local',
        'catalog_write': False,
        'external_services': False,
    }


def feedback_status(root=DEFAULT_FEEDBACK_ROOT):
    '''Summarize the private ledger without returning feedback text.'''
    root = Path(root)
    path = root / 'feedback.jsonl'
    total = open_count = resolved_count = invalid = 0
    kinds = Counter()
    latest = None
    if path.is_file():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as exc:
            raise CatalogError('feedback_ledger_unreadable') from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if (not isinstance(row, dict)
                    or row.get('schema') != FEEDBACK_SCHEMA
                    or not isinstance(row.get('feedback_id'), str)):
                invalid += 1
                continue
            total += 1
            kind = row.get('kind')
            if kind in FEEDBACK_KINDS:
                kinds[kind] += 1
            if row.get('status') == 'open':
                open_count += 1
            else:
                resolved_count += 1
            created_at = row.get('created_at')
            if isinstance(created_at, str) and (latest is None or created_at > latest):
                latest = created_at
    return {
        'schema': STATUS_SCHEMA,
        'status': 'ok',
        'storage': 'private_local',
        'ledger_present': path.is_file(),
        'total_feedback': total,
        'open_feedback': open_count,
        'resolved_feedback': resolved_count,
        'invalid_records': invalid,
        'kinds': {name: kinds[name] for name in FEEDBACK_KINDS if kinds[name]},
        'latest_created_at': latest,
        'text_returned': False,
        'catalog_write': False,
        'external_services': False,
    }


def list_feedback(root=DEFAULT_FEEDBACK_ROOT, *, status='open', limit=20):
    '''List feedback metadata newest first without returning feedback text.'''
    if status not in {'open', 'resolved', 'all'}:
        raise CatalogError('invalid_feedback_filter')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CatalogError('invalid_feedback_limit')
    root = Path(root)
    rows, invalid = _read_rows(root / 'feedback.jsonl')
    if status != 'all':
        rows = [row for row in rows if row.get('status') == status]
    rows = list(reversed(rows))
    items = []
    for row in rows[:limit]:
        fingerprint = row.get('turn_fingerprint')
        citation_ids = row.get('citation_ids')
        items.append({
            'feedback_id': row['feedback_id'],
            'created_at': row.get('created_at'),
            'kind': row.get('kind'),
            'status': row.get('status'),
            'turn_fingerprint_prefix': (
                fingerprint[:12] if isinstance(fingerprint, str) else None),
            'citation_ids': list(citation_ids) if isinstance(citation_ids, list) else [],
            'resolved_at': row.get('resolved_at'),
            'resolution_present': bool(row.get('resolution')),
        })
    return {
        'schema': LIST_SCHEMA,
        'status': 'ok',
        'filter': status,
        'total_matched': len(rows),
        'returned': len(items),
        'feedback': items,
        'invalid_records': invalid,
        'storage': 'private_local',
        'text_returned': False,
        'catalog_write': False,
        'external_services': False,
    }


def show_feedback(feedback_id, root=DEFAULT_FEEDBACK_ROOT):
    '''Return one full feedback record for local review.'''
    feedback_id = _validate_feedback_id(feedback_id)
    rows, invalid = _read_rows(Path(root) / 'feedback.jsonl')
    for row in rows:
        if row['feedback_id'] == feedback_id:
            return {
                'schema': DETAIL_SCHEMA,
                'status': 'found',
                'feedback': row,
                'invalid_records': invalid,
                'storage': 'private_local',
                'text_returned': True,
                'catalog_write': False,
                'external_services': False,
            }
    raise CatalogError('feedback_not_found')


def resolve_feedback(feedback_id, resolution=None, *, root=DEFAULT_FEEDBACK_ROOT,
                     resolved_at=None):
    '''Mark one feedback record resolved and rewrite the ledger atomically.'''
    feedback_id = _validate_feedback_id(feedback_id)
    if resolution is not None:
        if not isinstance(resolution, str):
            raise CatalogError('invalid_feedback_resolution')
        resolution = resolution.strip() or None
        if resolution is not None and len(resolution) > MAX_RESOLUTION_CHARS:
            raise CatalogError('invalid_feedback_resolution')
    root = Path(root)
    path = root / 'feedback.jsonl'
    with _ledger_lock(root):
        rows, _ = _read_rows(path, strict=True)
        target = None
        for row in rows:
            if row['feedback_id'] == feedback_id:
                target = row
                break
        if target is None:
            raise CatalogError('feedback_not_found')
        if target.get('status') == 'resolved':
            return {
                'status': 'already_resolved',
                'feedback_id': feedback_id,
                'resolved_at': target.get('resolved_at'),
                'storage': 'private_local',
                'catalog_write': False,
                'external_services': False,
            }
        resolved_at = resolved_at or _now()
        if not isinstance(resolved_at, str) or not resolved_at.strip():
            raise CatalogError('invalid_feedback_resolved_at')
        target['status'] = 'resolved'
        target['resolved_at'] = resolved_at
        target['resolution'] = resolution
        temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as stream:
                for row in rows:
                    stream.write(json.dumps(
                        row, ensure_ascii=False, sort_keys=True) + '\n')
            os.replace(temporary, path)
        except OSError as exc:
            raise CatalogError('feedback_resolve_failed') from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return {
        'status': 'resolved',
        'feedback_id': feedback_id,
        'resolved_at': resolved_at,
        'resolution_present': bool(resolution),
        'storage': 'private_local',
        'catalog_write': False,
        'external_services': False,
    }
