"""Database recovery checks with real SQLite files and deliberately damaged bundles."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from scripts.catalog_backup import create_backup, restore_backup, verify_backup


class CatalogBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'catalog'
        self.catalog.mkdir()
        self.db = self.catalog / 'catalog.sqlite3'
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('CREATE TABLE facts(value TEXT)')
            con.execute('INSERT INTO facts VALUES (?)', ('厚度未验证',))
            con.commit()
        (self.catalog / 'versions').mkdir()
        (self.catalog / 'versions/registry.json').write_text('{}', encoding='utf-8')
        self.bundle = self.root / 'backup.zip'

    def test_restore_database_archive_and_identity_key(self):
        key = self.root / 'key'
        key.write_bytes(b'local-recovery-key')
        before = self.db.read_bytes()
        create_backup(self.catalog, self.bundle, archive=self.db, identity_key=key)
        receipt = restore_backup(self.bundle, self.root / 'restored')
        self.assertTrue(receipt['sqlite_integrity_passed'])
        self.assertEqual((self.root / 'restored/catalog/catalog.sqlite3').read_bytes(), before)
        self.assertEqual((self.root / 'restored/private/pseudonym.key').read_bytes(), key.read_bytes())
        self.assertEqual((self.root / 'restored/archive/mail_archive.sqlite3').read_bytes(), before)
        self.assertEqual(self.db.read_bytes(), before)

    def test_existing_backup_refused(self):
        self.bundle.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'backup_output_exists'):
            create_backup(self.catalog, self.bundle)
        self.assertEqual(self.bundle.read_bytes(), b'preserve')

    def test_existing_restore_directory_refused(self):
        create_backup(self.catalog, self.bundle)
        with self.assertRaisesRegex(ValueError, 'restore_destination_exists'):
            restore_backup(self.bundle, self.catalog)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT value FROM facts').fetchone()[0], '厚度未验证')

    def test_tampered_payload_rejected_before_restore(self):
        create_backup(self.catalog, self.bundle)
        damaged = self.root / 'damaged.zip'
        with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(damaged, 'w') as target:
            for name in source.namelist():
                data = source.read(name)
                if name == 'versions/registry.json':  # the actual path includes the catalog prefix
                    self.fail('unscoped member')
                if name == 'catalog/versions/registry.json':
                    data = b'[]'
                target.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'backup_checksum_mismatch'):
            restore_backup(damaged, self.root / 'bad-restore')
        self.assertFalse((self.root / 'bad-restore').exists())

    def test_traversal_member_rejected(self):
        create_backup(self.catalog, self.bundle)
        with zipfile.ZipFile(self.bundle, 'a') as target:
            target.writestr('../outside', b'x')
        with self.assertRaisesRegex(ValueError, 'unsafe_archive_path'):
            verify_backup(self.bundle)

    def test_unlisted_member_rejected(self):
        create_backup(self.catalog, self.bundle)
        with zipfile.ZipFile(self.bundle, 'a') as target:
            target.writestr('catalog/unlisted.txt', b'x')
        with self.assertRaisesRegex(ValueError, 'backup_member_set_mismatch'):
            verify_backup(self.bundle)

    def test_live_journal_refused(self):
        Path(str(self.db) + '-wal').write_bytes(b'writer')
        with self.assertRaisesRegex(ValueError, 'database_has_live_journal'):
            create_backup(self.catalog, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_backup_inside_catalog_refused(self):
        with self.assertRaisesRegex(ValueError, 'invalid_backup_location'):
            create_backup(self.catalog, self.catalog / 'self.zip')


if __name__ == '__main__':
    unittest.main()
