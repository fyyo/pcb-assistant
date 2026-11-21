import datetime as dt
import sqlite3
import unittest

from scripts.data.organize_mail_archive import (
    build_threads,
    create_schema,
    extract_addresses,
    iso_datetime,
    message_id_values,
    normalize_subject,
    parse_transport_headers,
    split_quoted_body,
    validate_archive,
)


def message(**overrides):
    value = {
        'message_id': '',
        'in_reply_to': '',
        'references': [],
        'normalized_subject': '',
        'subject': '',
        'sent_at': '',
        'sender_address': '',
        'sender_name': '',
        'to': [],
        'cc': [],
        'bcc': [],
        'synthetic_id': '',
    }
    value.update(overrides)
    return value


class MailArchiveTests(unittest.TestCase):
    def test_normalize_subject_removes_repeated_prefixes(self):
        self.assertEqual(normalize_subject('RE: Re:  FWD: 压合白斑'), '压合白斑')
        self.assertEqual(normalize_subject('回复：转发：分层'), '分层')
        self.assertEqual(normalize_subject('  正常主题  '), '正常主题')

    def test_split_quoted_body_keeps_latest_and_history(self):
        text = (
            '请确认今天下午的压合参数。\n'
            '温度 180 度，压力 20 kg。\n\n'
            'From: 张三\n'
            'Sent: 2026-01-01 10:00\n'
            'To: 李四\n'
            'Subject: 压合参数\n\n'
            '旧内容\n'
        )
        latest, quoted = split_quoted_body(text)
        self.assertIn('请确认今天下午', latest)
        self.assertNotIn('旧内容', latest)
        self.assertTrue(quoted.startswith('From:'))

    def test_split_quoted_body_keeps_short_text(self):
        latest, quoted = split_quoted_body('好的')
        self.assertEqual(latest, '好的')
        self.assertEqual(quoted, '')

    def test_parse_transport_headers_and_addresses(self):
        raw = (
            'From: Alice <alice@example.com>\r\n'
            'To: Bob <bob@example.com>, carol@example.com\r\n'
            'Cc: Dave <dave@example.com>\r\n'
            'Date: Sat, 3 Jan 2026 20:34:46 +0800\r\n'
            'Message-ID: <ABC@example.com>\r\n'
            'In-Reply-To: <ROOT@example.com>\r\n'
            'References: <ROOT@example.com> <MID@example.com>\r\n'
        )
        headers = parse_transport_headers(raw)
        self.assertEqual(headers['Message-ID'], ['<ABC@example.com>'])
        to = extract_addresses(headers['To'])
        self.assertEqual(to[0]['address'], 'bob@example.com')
        self.assertEqual(to[1]['address'], 'carol@example.com')
        self.assertEqual(
            message_id_values(headers['References']),
            ['root@example.com', 'mid@example.com'],
        )

    def test_iso_datetime_uses_local_timezone(self):
        self.assertEqual(
            iso_datetime(dt.datetime(2026, 1, 3, 12, 34, 46)),
            '2026-01-03 12:34:46',
        )
        self.assertEqual(
            iso_datetime('Sat, 3 Jan 2026 20:34:46 +0800'),
            '2026-01-03 20:34:46',
        )

    def test_threads_follow_message_id_links(self):
        messages = [
            message(message_id='a@x', subject='白斑', normalized_subject='白斑',
                    sent_at='2026-01-01 10:00:00', sender_address='a@x'),
            message(message_id='b@x', in_reply_to='a@x', subject='RE: 白斑',
                    normalized_subject='白斑', sent_at='2026-01-01 11:00:00',
                    sender_address='b@x'),
        ]
        threads, participants = build_threads(messages)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]['message_count'], 2)
        self.assertEqual(messages[0]['thread_id'], messages[1]['thread_id'])
        self.assertEqual(len(participants), 2)

    def test_threads_fall_back_to_subject_and_participants(self):
        messages = [
            message(message_id='c@x', subject='压合参数', normalized_subject='压合参数',
                    sent_at='2026-01-01 10:00:00', sender_address='a@x'),
            message(message_id='d@x', subject='RE: 压合参数', normalized_subject='压合参数',
                    sent_at='2026-01-02 10:00:00', sender_address='a@x'),
        ]
        threads, _ = build_threads(messages)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]['message_count'], 2)

    def test_schema_and_empty_validation(self):
        connection = sqlite3.connect(':memory:')
        tokenizer = create_schema(connection)
        self.assertIn(tokenizer, ('trigram', 'unicode61'))
        validation = validate_archive(connection, 0, 0)
        self.assertEqual(validation['status'], 'passed')
        connection.close()


if __name__ == '__main__':
    unittest.main()
