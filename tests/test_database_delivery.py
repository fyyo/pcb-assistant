"""Detect drift after delivery, including private recovery material."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from pcb_assistant.catalog.common import digest_file
from scripts.catalog_backup import create_backup
from scripts.check_database_delivery import DATABASES, check_delivery


class DatabaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalog = self.root / 'data/phase3_catalog_v9'
        self.catalog.mkdir(parents=True)
        db = self.catalog / 'catalog.sqlite3'
        with closing(sqlite3.connect(db)) as con:
            con.execute('CREATE TABLE facts(value TEXT)')
            con.execute("INSERT INTO facts VALUES ('保留未验证状态')")
            con.commit()
        for relative in DATABASES.values():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path != db:
                shutil.copyfile(db, path)
        self.key = self.root / '.private/pseudonym.key'
        self.key.parent.mkdir()
        self.key.write_bytes(b'private-key-never-in-report')
        self.code = self.root / 'code.py'
        self.code.write_bytes(b'# delivered code\n')
        self.backup = self.root / '.private/backup.zip'
        receipt = create_backup(self.catalog, self.backup,
                                archive=self.root / DATABASES['archive'], identity_key=self.key)
        self.delivery = self.root / 'delivery.json'
        self.delivery.write_text(json.dumps({
            'schema': 'pcb-internal-database-delivery-v1', 'status': 'complete',
            'database_delivery_complete': True, 'default_catalog': 'data/phase3_catalog_v9',
            'database_hashes': {'after': {k: digest_file(self.root / p) for k, p in DATABASES.items()}},
            'changed_implementation_sha256': {'code.py': digest_file(self.code)},
            'backup_and_recovery': {**receipt, 'compressed_bytes': self.backup.stat().st_size,
                                    'backup_contents': receipt['scope']},
        }), encoding='utf-8')

    def check(self, **kwargs):
        return check_delivery(self.delivery, self.backup, root=self.root,
                              default_catalog=kwargs.get('default_catalog', self.catalog))

    def assert_failed(self, check, **kwargs):
        report = self.check(**kwargs)
        self.assertEqual(report['status'], 'failed')
        self.assertIn({'check': check, 'status': 'failed'}, report['checks'])
        return report

    def test_unchanged_delivery_passes_without_writes(self):
        before = {p.relative_to(self.root): digest_file(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.check()['status'], 'passed')
        after = {p.relative_to(self.root): digest_file(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_changed_default_fails(self):
        self.assert_failed('default_catalog', default_catalog=self.root / 'data/phase3_catalog_v3')

    def test_changed_rollback_database_fails(self):
        with closing(sqlite3.connect(self.root / DATABASES['v3'])) as con:
            con.execute("INSERT INTO facts VALUES ('changed')")
            con.commit()
        self.assert_failed('database_v3')

    def test_changed_key_fails_without_disclosing_private_data(self):
        self.key.write_bytes(b'changed-private-value')
        report = self.assert_failed('live_snapshot_matches_backup')
        output = json.dumps(report)
        for private in ('changed-private-value', 'pseudonym.key', str(self.root)):
            self.assertNotIn(private, output)

    def test_extra_catalog_file_and_active_archive_journal_fail(self):
        (self.catalog / 'extra.txt').write_bytes(b'new')
        Path(str(self.root / DATABASES['archive']) + '-wal').write_bytes(b'writer')
        self.assert_failed('catalog_file_set')
        self.assert_failed('no_active_database_journals')

    def test_changed_implementation_fails(self):
        self.code.write_bytes(b'# changed\n')
        self.assert_failed('delivered_implementation')

    def test_missing_or_corrupt_backup_fails_safely(self):
        self.backup.write_bytes(b'invalid zip')
        self.assert_failed('readable_and_valid_inputs')
        self.assertEqual(check_delivery(self.delivery, self.root / 'absent.zip', root=self.root,
                                       default_catalog=self.catalog)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
