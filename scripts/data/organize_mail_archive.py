#!/usr/bin/env python3
"""Build a local-only, full-content mail archive from one or more PST files.

The archive is intentionally separate from the sanitized Phase 3 catalog:
it keeps original bodies, headers and participants for local inspection,
while the MCP-facing catalog remains redacted and unchanged.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_privacy import decode, opaque_id


SCHEMA_VERSION = 1
ARCHIVE_NAME = 'mail_archive_v1'
MAX_BODY_CHARS = 2_000_000
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
SUBJECT_PREFIX = re.compile(
    r'^\s*(?:(?:re|fw|fwd|aw|sv|回复|回覆|转发|轉寄|轉發)\s*[:：]\s*)+',
    re.I,
)
GENERIC_SUBJECTS = {'re', 'fw', 'fwd', 'hi', 'hello', 'test', '通知', '公告', '問題', '问题'}
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def clean_text(value):
    return CONTROL_CHARS.sub('', value or '')


def safe_property(item, name, fallback=None):
    try:
        value = getattr(item, name)
        return fallback if value is None else value
    except (AttributeError, OSError, RuntimeError, ValueError):
        return fallback


def body_text(message):
    plain = safe_property(message, 'plain_text_body', b'')
    if plain and decode(plain).strip():
        return decode(plain), 'plain'
    raw_html = safe_property(message, 'html_body', b'')
    if raw_html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(decode(raw_html), 'html.parser')
        for node in soup(['script', 'style', 'img', 'object', 'iframe', 'embed', 'head', 'svg']):
            node.decompose()
        return soup.get_text(separator='\n'), 'html'
    return '', 'no_text'


def normalize_subject(value):
    text = re.sub(r'\s+', ' ', decode(value or '')).strip()
    while True:
        stripped = SUBJECT_PREFIX.sub('', text).strip()
        if stripped == text:
            break
        text = stripped
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def is_generic_subject(value):
    text = normalize_subject(value).casefold()
    return len(text) < 4 or text in GENERIC_SUBJECTS


def _looks_like_header_block(lines, index):
    if index >= len(lines):
        return False
    head = lines[index].strip()
    if not re.match(r'(?i)^(?:from|发件人|寄件者|寄件人)\s*[:：]', head):
        return False
    window = [line.strip() for line in lines[index + 1:index + 9] if line.strip()]
    markers = 0
    for line in window:
        if re.match(r'(?i)^(?:sent|date|to|cc|subject|发送时间|寄件日期|收件人|收件者|抄送|主旨|主题)\s*[:：]', line):
            markers += 1
    return markers >= 1


def _is_quote_start(lines, index):
    line = lines[index]
    stripped = line.strip()
    if re.match(r'^>', stripped):
        return True
    if re.match(r'^_{20,}\s*$', stripped) and index + 1 < len(lines):
        return _looks_like_header_block(lines, index + 1)
    if re.match(r'(?i)^-{2,}\s*(?:original message|forwarded message|原始邮件|原始郵件|转发邮件|轉寄郵件|轉發郵件)\s*-{2,}', stripped):
        return True
    if re.match(r'(?i)^on .{5,200}wrote:\s*$', stripped):
        return True
    if re.match(r'^在.{0,200}(?:写道|寫道)[：:]\s*$', stripped):
        return True
    if _looks_like_header_block(lines, index):
        return True
    return False


def split_quoted_body(text):
    """Return (latest body, quoted history) without discarding the full text."""
    if not text:
        return '', ''
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    for index, line in enumerate(lines):
        if _is_quote_start(lines, index):
            latest = '\n'.join(lines[:index]).strip()
            if len(latest) >= 20:
                return latest, '\n'.join(lines[index:]).strip()
    return text.strip(), ''


def decode_mime(value):
    if not value:
        return ''
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def parse_transport_headers(raw):
    from email.parser import HeaderParser
    parsed = HeaderParser().parsestr(decode(raw or ''))
    result = {}
    for name in ('From', 'To', 'Cc', 'Bcc', 'Reply-To', 'Sender', 'Subject',
                 'Date', 'Message-ID', 'In-Reply-To', 'References'):
        values = []
        for value in parsed.get_all(name, []):
            values.append(clean_text(decode_mime(value).strip()))
        result[name] = values
    return result


def extract_addresses(values):
    from email.utils import getaddresses
    result = []
    for name, address in getaddresses(values or []):
        name = clean_text(decode_mime(name).strip())
        address = clean_text((address or '').strip().lower())
        if address or name:
            result.append({'name': name, 'address': address})
    return result


def mapi_recipients(message):
    result = []
    recipients = safe_property(message, 'recipients', None)
    if not recipients:
        return result
    try:
        records = list(recipients.record_sets)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return result
    for record in records:
        name = ''
        address = ''
        try:
            entries = list(record.entries)
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
        for entry in entries:
            try:
                entry_type = entry.entry_type
                value_type = entry.value_type
                if value_type not in (0x1e, 0x1f):
                    continue
                value = entry.data_as_string
                if value is None:
                    continue
                value = decode(value).strip()
            except (AttributeError, OSError, RuntimeError, ValueError):
                continue
            if entry_type == 0x3001:
                name = value
            elif entry_type in (0x3003, 0x39fe, 0x39ff):
                address = value.lower()
        if name or address:
            result.append({'name': name, 'address': address})
    return result


def message_id_values(values):
    result = []
    for value in values or []:
        for match in re.findall(r'<([^<>]+)>', value):
            match = match.strip().lower()
            if match and match not in result:
                result.append(match)
    return result


def iso_datetime(value):
    if not value:
        return ''
    if isinstance(value, str):
        from email.utils import parsedate_to_datetime
        try:
            value = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return value.strip()
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    value = value.astimezone(LOCAL_TZ)
    return value.replace(tzinfo=None).isoformat(sep=' ', timespec='seconds')


def sort_datetime(value):
    if not value:
        return dt.datetime.min
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return dt.datetime.min


def stable_hash(value, prefix):
    return prefix + hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]


class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item):
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, left, right):
        left = self.find(left)
        right = self.find(right)
        if left == right:
            return
        rank = self.rank
        if rank[left] < rank[right]:
            left, right = right, left
        self.parent[right] = left
        if rank[left] == rank[right]:
            rank[left] += 1


def participant_set(message):
    values = set()
    address = (message.get('sender_address') or '').strip().lower()
    name = (message.get('sender_name') or '').strip().lower()
    if address:
        values.add(address)
    elif name:
        values.add(name)
    for key in ('to', 'cc', 'bcc'):
        for entry in message.get(key) or []:
            value = (entry.get('address') or entry.get('name') or '').strip().lower()
            if value:
                values.add(value)
    return values


def build_threads(messages):
    count = len(messages)
    thread_rows = []
    participant_rows = []
    if not count:
        return thread_rows, participant_rows
    union = UnionFind(count)
    by_message_id = {}
    for index, message in enumerate(messages):
        message_id = (message.get('message_id') or '').strip().lower()
        if message_id and message_id not in by_message_id:
            by_message_id[message_id] = index
    for index, message in enumerate(messages):
        references = []
        reply_to = (message.get('in_reply_to') or '').strip().lower()
        if reply_to:
            references.append(reply_to)
        references.extend(message.get('references') or [])
        for reference in references:
            target = by_message_id.get(reference)
            if target is not None:
                union.union(index, target)

    by_subject = collections.defaultdict(list)
    for index, message in enumerate(messages):
        subject = message.get('normalized_subject') or ''
        if not is_generic_subject(subject):
            by_subject[subject].append(index)
    for indices in by_subject.values():
        if len(indices) < 2:
            continue
        indices.sort(key=lambda i: sort_datetime(messages[i].get('sent_at')))
        for previous, current in zip(indices, indices[1:]):
            older = messages[previous]
            newer = messages[current]
            older_at = sort_datetime(older.get('sent_at'))
            newer_at = sort_datetime(newer.get('sent_at'))
            gap = (newer_at - older_at).days if older_at != dt.datetime.min and newer_at != dt.datetime.min else 0
            shared = participant_set(older) & participant_set(newer)
            same_sender = bool(older.get('sender_address')) and older.get('sender_address') == newer.get('sender_address')
            if gap <= 365 and (shared or same_sender):
                union.union(previous, current)
            elif not shared and not same_sender and gap <= 30:
                union.union(previous, current)

    groups = collections.defaultdict(list)
    for index in range(count):
        groups[union.find(index)].append(index)
    for indices in groups.values():
        indices.sort(key=lambda i: sort_datetime(messages[i].get('sent_at')))
        root_key = min(
            (messages[i].get('message_id') or messages[i].get('synthetic_id') or str(i))
            for i in indices
        )
        thread_id = stable_hash(root_key, 'thr_')
        subject = next((messages[i].get('subject') for i in indices if messages[i].get('subject')), '')
        dates = [messages[i].get('sent_at') for i in indices if messages[i].get('sent_at')]
        participants = collections.Counter()
        for index in indices:
            for participant in participant_set(messages[index]):
                participants[participant] += 1
            messages[index]['thread_id'] = thread_id
        thread_rows.append({
            'thread_id': thread_id,
            'subject': subject,
            'first_at': min(dates) if dates else '',
            'last_at': max(dates) if dates else '',
            'message_count': len(indices),
            'participant_count': len(participants),
            'root_message_id': root_key,
        })
        for participant, message_count in participants.most_common():
            participant_rows.append({
                'thread_id': thread_id,
                'participant': participant,
                'message_count': message_count,
            })
    return thread_rows, participant_rows

def walk_messages(folder, path, stats, limit=0):
    name = decode(safe_property(folder, 'name', '') or '').strip()
    current = path + ([name] if name else [])
    try:
        message_count = folder.number_of_sub_messages
    except (OSError, RuntimeError, ValueError):
        message_count = 0
        stats['folder_errors'] += 1
    for index in range(message_count):
        if limit and stats['messages_visited'] >= limit:
            return
        try:
            message = folder.get_sub_message(index)
        except (OSError, RuntimeError, ValueError):
            stats['unreadable_messages'] += 1
            continue
        stats['messages_visited'] += 1
        yield message, '/'.join(current)
    try:
        folder_count = folder.number_of_sub_folders
    except (OSError, RuntimeError, ValueError):
        folder_count = 0
        stats['folder_errors'] += 1
    for index in range(folder_count):
        try:
            subfolder = folder.get_sub_folder(index)
        except (OSError, RuntimeError, ValueError):
            stats['unreadable_folders'] += 1
            continue
        yield from walk_messages(subfolder, current, stats, limit)


def load_attachment_occurrences(path):
    mapping = {}
    if not path.exists():
        return mapping
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            parent = row.get('parent_source_id')
            ordinal = row.get('attachment_ordinal')
            document = row.get('document_id')
            if parent and isinstance(ordinal, int) and document:
                mapping[(parent, ordinal)] = document
    return mapping


def load_attachment_documents(path):
    documents = set()
    if not path.exists():
        return documents
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('document_id'):
                documents.add(row['document_id'])
    return documents


def attachment_document_id(key, digest):
    return 'att_' + opaque_id(key, 'attachment:' + digest)[5:]


def extract_message(message, folder_path, source_name, key, occurrences, known_documents,
                    hash_attachments, stats):
    identifier = safe_property(message, 'identifier', '')
    stable_id = opaque_id(key, identifier)
    subject = clean_text(decode(safe_property(message, 'subject', '')).strip())
    body, body_kind = body_text(message)
    body = clean_text(body)
    body_truncated = False
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        body_truncated = True
    latest, quoted = split_quoted_body(body)
    headers = parse_transport_headers(safe_property(message, 'transport_headers', ''))
    sender_name = clean_text(decode(safe_property(message, 'sender_name', '')).strip())
    from_entries = extract_addresses(headers.get('From') or [])
    sender_address = from_entries[0]['address'] if from_entries else ''
    if not sender_name and from_entries:
        sender_name = from_entries[0]['name']
    to_entries = extract_addresses(headers.get('To') or [])
    cc_entries = extract_addresses(headers.get('Cc') or [])
    bcc_entries = extract_addresses(headers.get('Bcc') or [])
    if not to_entries and not cc_entries and not bcc_entries:
        fallback = mapi_recipients(message)
        if fallback:
            to_entries = fallback

    message_ids = message_id_values(headers.get('Message-ID') or [])
    reply_ids = message_id_values(headers.get('In-Reply-To') or [])
    reference_ids = message_id_values(headers.get('References') or [])
    sent_at = iso_datetime(safe_property(message, 'delivery_time', None))
    if not sent_at:
        sent_at = iso_datetime((headers.get('Date') or [''])[0])
    created_at = iso_datetime(safe_property(message, 'creation_time', None))
    submitted_at = iso_datetime(safe_property(message, 'client_submit_time', None))

    attachment_rows = []
    attachment_count = 0
    try:
        attachment_count = int(safe_property(message, 'number_of_attachments', 0) or 0)
    except (TypeError, ValueError):
        attachment_count = 0
    for ordinal in range(attachment_count):
        row = {'ordinal': ordinal, 'filename': '', 'size_bytes': 0,
               'content_sha256': '', 'document_id': '', 'status': 'metadata_only'}
        linked = occurrences.get((stable_id, ordinal))
        if linked:
            row['document_id'] = linked
            row['status'] = 'linked_existing'
        try:
            attachment = message.get_attachment(ordinal)
            row['filename'] = clean_text(decode(safe_property(attachment, 'long_filename', '') or '').strip())
            row['size_bytes'] = int(safe_property(attachment, 'size', 0) or 0)
            if hash_attachments and not linked and 0 < row['size_bytes'] <= 64 * 1024 * 1024:
                content = attachment.read_buffer(row['size_bytes'])
                if len(content) == row['size_bytes']:
                    digest = hashlib.sha256(content).hexdigest()
                    row['content_sha256'] = digest
                    candidate = attachment_document_id(key, digest)
                    if candidate in known_documents:
                        row['document_id'] = candidate
                        row['status'] = 'linked_by_hash'
                    else:
                        row['status'] = 'hashed_unlinked'
                else:
                    row['status'] = 'read_incomplete'
        except (AttributeError, OSError, RuntimeError, ValueError):
            row['status'] = 'read_error'
            stats['attachment_read_errors'] += 1
        attachment_rows.append(row)

    stats['messages_ok'] += 1
    stats['body_chars'] += len(body)
    if body:
        stats['with_body'] += 1
    if headers.get('Message-ID'):
        stats['with_message_id'] += 1
    if sent_at:
        stats['with_date'] += 1
    if attachment_count:
        stats['with_attachments'] += 1
    stats['attachment_occurrences'] += attachment_count
    stats['linked_attachments'] += sum(1 for row in attachment_rows if row['document_id'])

    return {
        'stable_id': stable_id,
        'source_file': source_name,
        'source_message_id': str(identifier),
        'synthetic_id': source_name + ':' + str(identifier),
        'folder_path': clean_text(folder_path),
        'subject': subject,
        'normalized_subject': normalize_subject(subject),
        'sender_name': sender_name,
        'sender_address': sender_address,
        'to': to_entries,
        'cc': cc_entries,
        'bcc': bcc_entries,
        'sent_at': sent_at,
        'created_at': created_at,
        'submitted_at': submitted_at,
        'message_id': message_ids[0] if message_ids else '',
        'in_reply_to': reply_ids[0] if reply_ids else '',
        'references': reference_ids,
        'thread_id': '',
        'body_text': body,
        'body_latest': latest,
        'quoted_text': quoted,
        'body_sha256': hashlib.sha256(body.encode('utf-8', 'replace')).hexdigest() if body else '',
        'body_chars': len(body),
        'body_kind': body_kind,
        'body_truncated': body_truncated,
        'attachment_count': attachment_count,
        'attachments': attachment_rows,
        'transport_headers': clean_text(decode(safe_property(message, 'transport_headers', '') or '')),
    }

def source_entries(paths):
    entries = []
    for path in paths:
        source = Path(path)
        if not source.is_absolute():
            source = (ROOT / source).resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        stat = source.stat()
        entries.append({
            'path': source,
            'name': source.name,
            'size_bytes': stat.st_size,
            'modified_ns': stat.st_mtime_ns,
        })
    return entries


def load_catalog_mail_ids(path):
    if not path.exists():
        return set()
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return {row[0] for row in connection.execute(
            "select distinct source_id from documents where type='MAIL'"
        )}
    finally:
        connection.close()


def create_schema(connection):
    connection.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE messages (
            message_pk INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            folder_path TEXT,
            subject TEXT,
            normalized_subject TEXT,
            sender_name TEXT,
            sender_address TEXT,
            to_json TEXT,
            cc_json TEXT,
            bcc_json TEXT,
            sent_at TEXT,
            created_at TEXT,
            submitted_at TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            references_json TEXT,
            thread_id TEXT,
            body_text TEXT,
            body_latest TEXT,
            quoted_text TEXT,
            body_sha256 TEXT,
            body_chars INTEGER,
            body_kind TEXT,
            body_truncated INTEGER,
            attachment_count INTEGER,
            transport_headers TEXT,
            cross_source_duplicate INTEGER DEFAULT 0,
            UNIQUE(source_file, source_message_id)
        );
        CREATE INDEX messages_stable_id ON messages(stable_id);
        CREATE INDEX messages_thread_id ON messages(thread_id);
        CREATE INDEX messages_sent_at ON messages(sent_at);
        CREATE TABLE attachments (
            attachment_pk INTEGER PRIMARY KEY,
            message_pk INTEGER NOT NULL REFERENCES messages(message_pk),
            ordinal INTEGER NOT NULL,
            filename TEXT,
            size_bytes INTEGER,
            content_sha256 TEXT,
            document_id TEXT,
            status TEXT,
            UNIQUE(message_pk, ordinal)
        );
        CREATE INDEX attachments_document_id ON attachments(document_id);
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            first_at TEXT,
            last_at TEXT,
            message_count INTEGER,
            participant_count INTEGER,
            root_message_id TEXT
        );
        CREATE TABLE thread_participants (
            thread_id TEXT NOT NULL,
            participant TEXT NOT NULL,
            message_count INTEGER,
            PRIMARY KEY(thread_id, participant)
        );
    ''')
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "subject, body_text, content='messages', content_rowid='message_pk', tokenize='trigram')"
        )
        return 'trigram'
    except sqlite3.OperationalError:
        connection.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "subject, body_text, content='messages', content_rowid='message_pk', tokenize='unicode61')"
        )
        return 'unicode61'


def insert_messages(connection, messages):
    rows = []
    for message in messages:
        rows.append((
            message['stable_id'], message['source_file'], message['source_message_id'],
            message['folder_path'], message['subject'], message['normalized_subject'],
            message['sender_name'], message['sender_address'],
            json.dumps(message['to'], ensure_ascii=False),
            json.dumps(message['cc'], ensure_ascii=False),
            json.dumps(message['bcc'], ensure_ascii=False),
            message['sent_at'], message['created_at'], message['submitted_at'],
            message['message_id'], message['in_reply_to'],
            json.dumps(message['references'], ensure_ascii=False),
            message['thread_id'], message['body_text'], message['body_latest'],
            message['quoted_text'], message['body_sha256'], message['body_chars'],
            message['body_kind'], int(message['body_truncated']),
            message['attachment_count'], message['transport_headers'], 0,
        ))
    connection.executemany('''
        INSERT INTO messages (
            stable_id, source_file, source_message_id, folder_path, subject,
            normalized_subject, sender_name, sender_address, to_json, cc_json,
            bcc_json, sent_at, created_at, submitted_at, message_id,
            in_reply_to, references_json, thread_id, body_text, body_latest,
            quoted_text, body_sha256, body_chars, body_kind, body_truncated,
            attachment_count, transport_headers, cross_source_duplicate
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', rows)
    connection.execute('''
        INSERT INTO messages_fts(rowid, subject, body_text)
        SELECT message_pk, subject, body_text FROM messages
    ''')


def insert_attachments(connection, messages):
    id_by_key = {}
    for message_pk, source_file, source_message_id in connection.execute(
            'select message_pk, source_file, source_message_id from messages'):
        id_by_key[(source_file, source_message_id)] = message_pk
    rows = []
    for message in messages:
        message_pk = id_by_key[(message['source_file'], message['source_message_id'])]
        for attachment in message['attachments']:
            rows.append((
                message_pk, attachment['ordinal'], attachment['filename'],
                attachment['size_bytes'], attachment['content_sha256'],
                attachment['document_id'], attachment['status'],
            ))
    connection.executemany('''
        INSERT INTO attachments (
            message_pk, ordinal, filename, size_bytes, content_sha256,
            document_id, status
        ) VALUES (?,?,?,?,?,?,?)
    ''', rows)


def insert_threads(connection, thread_rows, participant_rows):
    connection.executemany('''
        INSERT INTO threads (
            thread_id, subject, first_at, last_at, message_count,
            participant_count, root_message_id
        ) VALUES (?,?,?,?,?,?,?)
    ''', [
        (row['thread_id'], row['subject'], row['first_at'], row['last_at'],
         row['message_count'], row['participant_count'], row['root_message_id'])
        for row in thread_rows
    ])
    connection.executemany('''
        INSERT INTO thread_participants (
            thread_id, participant, message_count
        ) VALUES (?,?,?)
    ''', [
        (row['thread_id'], row['participant'], row['message_count'])
        for row in participant_rows
    ])


def mark_cross_source_duplicates(connection):
    connection.execute('''
        UPDATE messages SET cross_source_duplicate = 1
        WHERE stable_id IN (
            SELECT stable_id FROM messages GROUP BY stable_id HAVING count(*) > 1
        )
    ''')


def validate_archive(connection, expected_messages, expected_attachments):
    counts = {
        'messages': connection.execute('select count(*) from messages').fetchone()[0],
        'threads': connection.execute('select count(*) from threads').fetchone()[0],
        'attachments': connection.execute('select count(*) from attachments').fetchone()[0],
        'fts_rows': connection.execute('select count(*) from messages_fts').fetchone()[0],
        'messages_without_thread': connection.execute(
            "select count(*) from messages where coalesce(thread_id,'')=''"
        ).fetchone()[0],
        'threads_without_messages': connection.execute(
            'select count(*) from threads t where not exists '
            '(select 1 from messages m where m.thread_id=t.thread_id)'
        ).fetchone()[0],
        'duplicate_source_keys': connection.execute(
            'select count(*) from (select source_file, source_message_id '
            'from messages group by source_file, source_message_id having count(*)>1)'
        ).fetchone()[0],
        'empty_bodies': connection.execute(
            "select count(*) from messages where coalesce(body_text,'')=''"
        ).fetchone()[0],
    }
    errors = []
    if counts['messages'] != expected_messages:
        errors.append('message_count_mismatch')
    if counts['attachments'] != expected_attachments:
        errors.append('attachment_count_mismatch')
    if counts['fts_rows'] != counts['messages']:
        errors.append('fts_count_mismatch')
    if counts['messages_without_thread']:
        errors.append('messages_without_thread')
    if counts['threads_without_messages']:
        errors.append('threads_without_messages')
    if counts['duplicate_source_keys']:
        errors.append('duplicate_source_keys')
    return {'status': 'passed' if not errors else 'failed', 'counts': counts, 'errors': errors}


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temp.replace(path)

def run(args):
    started = time.monotonic()
    sources = source_entries(args.source or [ROOT / '19924784-20240420.pst', ROOT / '20251029.pst'])
    if args.limit:
        sources = sources[:1]
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    if output.exists():
        if not args.overwrite:
            raise SystemExit(f'output already exists: {output}')
        import shutil
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=False)
    key_path = Path(args.pseudonym_key)
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    key = key_path.read_bytes()
    occurrences = {}
    for path in (args.attachment_occurrences or ['data/attachments_v2/occurrences.jsonl']):
        occurrences.update(load_attachment_occurrences(ROOT / path))
    known_documents = set()
    for path in (args.attachment_documents or ['data/attachments_v2/documents.jsonl']):
        known_documents.update(load_attachment_documents(ROOT / path))

    import pypff
    all_messages = []
    source_stats = {}
    for source in sources:
        stats = collections.Counter()
        pst = pypff.file()
        pst.open(str(source['path']), 'r')
        try:
            for message, folder_path in walk_messages(pst.root_folder, [], stats, args.limit):
                try:
                    all_messages.append(extract_message(
                        message, folder_path, source['name'], key, occurrences,
                        known_documents, args.hash_attachments, stats,
                    ))
                except (OSError, RuntimeError, ValueError, AttributeError) as error:
                    stats['message_errors'] += 1
                    stats['last_error'] = type(error).__name__
        finally:
            pst.close()
        source_stats[source['name']] = dict(stats)

    thread_rows, participant_rows = build_threads(all_messages)
    total_attachments = sum(len(message['attachments']) for message in all_messages)
    db_path = output / 'mail_archive.sqlite3'
    temp_path = output / 'mail_archive.sqlite3.tmp'
    if temp_path.exists():
        temp_path.unlink()
    connection = sqlite3.connect(temp_path)
    try:
        tokenizer = create_schema(connection)
        insert_messages(connection, all_messages)
        insert_attachments(connection, all_messages)
        insert_threads(connection, thread_rows, participant_rows)
        mark_cross_source_duplicates(connection)
        catalog_ids = load_catalog_mail_ids(ROOT / args.catalog)
        archive_ids = {message['stable_id'] for message in all_messages}
        validation = validate_archive(connection, len(all_messages), total_attachments)
        metadata = {
            'schema_version': SCHEMA_VERSION,
            'privacy_level': 'local_full',
            'external_services': False,
            'git_ignored': True,
            'mcp_exposed': False,
            'fts_tokenizer': tokenizer,
            'sources': [
                {'file': source['name'], 'size_bytes': source['size_bytes'],
                 'modified_ns': source['modified_ns']} for source in sources
            ],
            'catalog_mail_ids_matched': len(archive_ids & catalog_ids),
            'catalog_mail_ids_missing': len(archive_ids - catalog_ids),
            'catalog_mail_ids_total': len(catalog_ids),
        }
        connection.executemany(
            'INSERT INTO metadata(key, value) VALUES (?,?)',
            [(name, json.dumps(value, ensure_ascii=False)) for name, value in metadata.items()],
        )
        connection.commit()
        connection.execute('PRAGMA optimize')
    finally:
        connection.close()
    temp_path.replace(db_path)

    stable_counts = collections.Counter(message['stable_id'] for message in all_messages)
    report = {
        'schema': 'mail-archive-v1',
        'created_at': dt.datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(sep=' ', timespec='seconds'),
        'elapsed_seconds': round(time.monotonic() - started, 1),
        'privacy': {
            'level': 'local_full',
            'external_services': False,
            'git_ignored': True,
            'mcp_exposed': False,
            'note': '本地完整邮件档案，含原文、收发件人和邮件头；不得提交、导出或接入外部服务。',
        },
        'sources': [
            {'file': source['name'], **source_stats[source['name']], 'size_bytes': source['size_bytes']}
            for source in sources
        ],
        'counts': {
            'messages': len(all_messages),
            'threads': len(thread_rows),
            'attachments': total_attachments,
            'catalog_mail_ids_matched': len(archive_ids & catalog_ids),
            'cross_source_duplicate_messages': sum(
                count for count in stable_counts.values() if count > 1
            ),
        },
        'validation': validation,
    }
    write_json(output / 'report.json', report)
    write_json(output / 'validation.json', validation)
    (output / 'README.md').write_text(
        '# 本地邮件档案 v1\n\n'
        '本目录含两份 PST 的完整正文、收发件人、邮件时间和邮件头，'
        '仅限本机内部分析使用。\n\n'
        '- 不提交版本库；`data/` 已被忽略。\n'
        '- 不接入 MCP、不导出、不发送到外部服务。\n'
        '- 现有 `phase3_catalog_v3` 仍是脱敏检索层，二者相互独立。\n\n'
        '查询示例：\n\n'
        '```sql\n'
        "select sent_at, sender_name, subject from messages order by sent_at desc limit 20;\n"
        "select * from messages_fts where messages_fts match '白斑' limit 20;\n"
        '```\n',
        encoding='utf-8',
    )
    print(json.dumps(report['counts'], ensure_ascii=False))
    print(json.dumps(validation['counts'], ensure_ascii=False))
    if validation['status'] != 'passed':
        raise SystemExit('validation failed: ' + ', '.join(validation['errors']))


def main():
    parser = argparse.ArgumentParser(
        description='Build a local-only full-content mail archive from PST files'
    )
    parser.add_argument('--source', action='append', default=None,
                        help='PST file; repeat for both archives')
    parser.add_argument('--output', default=f'data/{ARCHIVE_NAME}')
    parser.add_argument('--attachment-occurrences', action='append', default=None,
                        help='attachment occurrence file; repeat to merge runs')
    parser.add_argument('--attachment-documents', action='append', default=None,
                        help='attachment document file; repeat to merge runs')
    parser.add_argument('--pseudonym-key', default='.private/pseudonym.key')
    parser.add_argument('--catalog', default='data/phase3_catalog_v3/catalog.sqlite3')
    parser.add_argument('--hash-attachments', action='store_true',
                        help='read attachment bytes to compute content hashes')
    parser.add_argument('--limit', type=int, default=0,
                        help='only process the first N messages from the first source')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('invalid limit')
    run(args)


if __name__ == '__main__':
    main()
