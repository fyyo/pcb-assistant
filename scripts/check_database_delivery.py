"""Check a completed local delivery against its recorded hashes, without writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_common import DEFAULT_CATALOG, digest_file
from scripts.catalog_backup import MANIFEST, verify_backup

DELIVERY = ROOT / '.private/project_records/database_maintenance_baseline.json'
BACKUP = ROOT / '.private/database_delivery_20261116/database_backup.zip'
DATABASES = {
    'v3': 'data/phase3_catalog_v3/catalog.sqlite3',
    'v8': 'data/phase3_catalog_v8/catalog.sqlite3',
    'v9': 'data/phase3_catalog_v9/catalog.sqlite3',
    'archive': 'data/mail_archive_v1/mail_archive.sqlite3',
}


def _local(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError('path_outside_workspace')
    return path


def check_delivery(delivery=DELIVERY, backup=BACKUP, *, root=ROOT, default_catalog=None):
    root = Path(root).resolve()
    checks = []

    def record(name, passed):
        checks.append({'check': name, 'status': 'passed' if passed else 'failed'})

    def result():
        return {
            'schema': 'pcb-database-maintenance-check-v1',
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'status': 'passed' if all(c['status'] == 'passed' for c in checks) else 'failed',
            'read_only': True,
            'scope': 'recorded_delivery_and_current_backup_consistency',
            'checks': checks,
            'limits': 'Does not repeat business queries or validate new engineering evidence; '
                      'does not establish off-device backup or external release readiness.',
        }

    try:
        baseline = json.loads(Path(delivery).read_text(encoding='utf-8'))
        valid = (baseline['schema'] == 'pcb-internal-database-delivery-v1'
                 and baseline['status'] == 'complete'
                 and baseline['database_delivery_complete'] is True
                 and set(baseline['database_hashes']['after']) == set(DATABASES)
                 and bool(baseline['changed_implementation_sha256']))
        record('completed_delivery_baseline', valid)
        if not valid:
            return result()
        catalog = _local(root, baseline['default_catalog'])
        record('default_catalog', Path(default_catalog or DEFAULT_CATALOG).resolve() == catalog)
        # Reuse hashes when the live snapshot also refers to the checked databases.
        hashes = {}

        def matches(path, expected):
            path = Path(path)
            if not path.is_file():
                return False
            if path not in hashes:
                hashes[path] = digest_file(path)
            return hashes[path] == expected

        for name, relative in DATABASES.items():
            record('database_' + name, matches(_local(root, relative), baseline['database_hashes']['after'][name]))
        implementation = [matches(_local(root, name), expected)
                          for name, expected in baseline['changed_implementation_sha256'].items()]
        record('delivered_implementation', all(implementation))
        receipt = verify_backup(Path(backup))
        expected = baseline['backup_and_recovery']
        record('backup_matches_delivery', all((
            receipt['backup_sha256'] == expected['backup_sha256'],
            receipt['files'] == expected['files'],
            receipt['uncompressed_bytes'] == expected['uncompressed_bytes'],
            Path(backup).stat().st_size == expected['compressed_bytes'],
            receipt['scope'] == expected['backup_contents'],
        )))
        # Verification above checks every ZIP member before its manifest is used.
        with zipfile.ZipFile(backup) as bundle:
            files = json.loads(bundle.read(MANIFEST))['files']
        special = {'archive/mail_archive.sqlite3': _local(root, DATABASES['archive']),
                   'private/pseudonym.key': _local(root, '.private/pseudonym.key')}
        expected_catalog = {name.removeprefix('catalog/') for name in files if name.startswith('catalog/')}
        actual_catalog = {p.relative_to(catalog).as_posix() for p in catalog.rglob('*') if p.is_file()}
        record('catalog_file_set', actual_catalog == expected_catalog)
        current = []
        for name, metadata in files.items():
            path = (_local(root, baseline['default_catalog'] + '/' + name.removeprefix('catalog/'))
                    if name.startswith('catalog/') else special[name])
            current.append(matches(path, metadata['sha256']) and path.stat().st_size == metadata['bytes'])
        record('live_snapshot_matches_backup', all(current))
        journal_free = all(not Path(str(_local(root, relative)) + suffix).exists()
                           for relative in DATABASES.values() for suffix in ('-wal', '-shm', '-journal'))
        record('no_active_database_journals', journal_free)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, RuntimeError):
        # Neither source values nor private filesystem paths belong in public output.
        record('readable_and_valid_inputs', False)
    return result()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', type=Path, default=DELIVERY)
    parser.add_argument('--backup', type=Path, default=BACKUP)
    args = parser.parse_args(argv)
    report = check_delivery(args.delivery, args.backup)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
