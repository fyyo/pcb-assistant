"""Create, verify and restore a local database snapshot without overwriting live data."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from catalog_common import DEFAULT_CATALOG, digest_file

SCHEMA = 'pcb-local-database-backup-v1'
MANIFEST = 'backup_manifest.json'


def _name(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('unsafe_archive_path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ('', '.', '..') for p in path.parts):
        raise ValueError('unsafe_archive_path')
    if path.as_posix() != value:
        raise ValueError('unsafe_archive_path')
    return value


def _sqlite_check(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.execute('PRAGMA query_only=ON')
        if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database_integrity_failed')
        if con.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('database_foreign_keys_failed')


def _members(archive):
    infos = archive.infolist()
    names = [_name(i.filename) for i in infos]
    if len(names) != len(set(names)) or MANIFEST not in names:
        raise ValueError('invalid_backup_members')
    if any(i.is_dir() or ((i.external_attr >> 16) & 0o170000) == 0o120000 for i in infos):
        raise ValueError('unsupported_backup_member')
    if archive.getinfo(MANIFEST).file_size > 8 * 1024 * 1024:
        raise ValueError('backup_manifest_too_large')
    manifest = json.loads(archive.read(MANIFEST))
    if manifest.get('schema') != SCHEMA or not isinstance(manifest.get('files'), dict):
        raise ValueError('invalid_backup_manifest')
    if set(names) != set(manifest['files']) | {MANIFEST}:
        raise ValueError('backup_member_set_mismatch')
    for name, expected in manifest['files'].items():
        _name(name)
        if (not name.startswith(('catalog/', 'archive/', 'private/'))
                or not isinstance(expected.get('bytes'), int) or expected['bytes'] < 0
                or archive.getinfo(name).file_size != expected['bytes']):
            raise ValueError('invalid_backup_file')
    if 'catalog/catalog.sqlite3' not in manifest['files']:
        raise ValueError('backup_catalog_missing')
    return manifest


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        manifest = _members(archive)
        for name, expected in manifest['files'].items():
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != expected['sha256']:
                raise ValueError('backup_checksum_mismatch')
    return {'status': 'passed', 'files': len(manifest['files']),
            'uncompressed_bytes': sum(r['bytes'] for r in manifest['files'].values()),
            'backup_sha256': digest_file(Path(path)), 'scope': manifest['scope']}


def create_backup(catalog, output, *, archive=None, identity_key=None):
    catalog, output = Path(catalog).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('backup_output_exists')
    if not catalog.is_dir() or output.is_relative_to(catalog):
        raise ValueError('invalid_backup_location')
    sources = {}
    for source in sorted(catalog.rglob('*')):
        if source.is_symlink():
            raise ValueError('source_symlink_not_supported')
        if source.is_file():
            if source.name.endswith(('-wal', '-shm', '-journal')):
                raise ValueError('database_has_live_journal')
            sources['catalog/' + source.relative_to(catalog).as_posix()] = source
    if 'catalog/catalog.sqlite3' not in sources:
        raise ValueError('catalog_database_missing')
    _sqlite_check(sources['catalog/catalog.sqlite3'])
    if archive is not None:
        source = Path(archive).resolve()
        _sqlite_check(source)
        if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('database_has_live_journal')
        sources['archive/mail_archive.sqlite3'] = source
    if identity_key is not None:
        sources['private/pseudonym.key'] = Path(identity_key).resolve()
    before = {name: {'bytes': p.stat().st_size, 'sha256': digest_file(p)} for name, p in sources.items()}
    manifest = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': before,
                'scope': {'catalog': True, 'mail_archive': archive is not None, 'identity_key': identity_key is not None,
                          'raw_pst_included': False, 'source_runs_included': False, 'application_code_included': False},
                'restore_policy': 'new_directory_only_never_overwrite_live_database'}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a concurrent run from overwriting a backup.
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as bundle:
        for name, source in sources.items():
            bundle.write(source, name)
        bundle.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    if any(p.stat().st_size != before[name]['bytes'] or digest_file(p) != before[name]['sha256']
           for name, p in sources.items()):
        raise ValueError('source_changed_during_backup')
    return verify_backup(output)


def restore_backup(backup, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('restore_destination_exists')
    receipt = verify_backup(backup)
    # Create only after the whole archive has passed path, membership and hash checks.
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(backup) as archive:
        manifest = _members(archive)
        for name, expected in manifest['files'].items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError('unsafe_restore_path')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('xb') as stream:
                shutil.copyfileobj(source, stream)
            if digest_file(target) != expected['sha256']:
                raise ValueError('restored_checksum_mismatch')
    _sqlite_check(destination / 'catalog/catalog.sqlite3')
    if (destination / 'archive/mail_archive.sqlite3').is_file():
        _sqlite_check(destination / 'archive/mail_archive.sqlite3')
    return {**receipt, 'restored_files_verified': True, 'sqlite_integrity_passed': True,
            'live_data_overwritten': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--archive', type=Path)
    create.add_argument('--identity-key', type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('backup', type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            result = create_backup(args.catalog, args.output, archive=args.archive, identity_key=args.identity_key)
        elif args.command == 'verify':
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.destination)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError):
        print(json.dumps({'status': 'failed', 'operation': args.command}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
