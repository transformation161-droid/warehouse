"""Read-only schema inventory and no-overwrite upgrade of an independent closed backup."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from functools import lru_cache

from .pg_admin import TABLES,V2_TABLES,LEGACY_TABLES,fingerprints


@lru_cache(maxsize=1)
def expected_release_schema():
    # Trusted DDL is compiled only in memory, never against the supplied database.
    from .database import SCHEMA
    from .decision_version_schema import schema as decisions
    from .offline_schema import schema as offline
    with closing(sqlite3.connect(':memory:')) as con:
        con.create_function('sha256',1,lambda value:hashlib.sha256(str(value).encode()).hexdigest(),deterministic=True)
        con.executescript(SCHEMA);con.executescript(decisions());con.executescript(offline())
        return {(kind,name):' '.join(sql.split()).lower() for kind,name,sql in con.execute(
            "select type,name,sql from sqlite_master where type in ('table','trigger') and sql is not null")}


def checksum(path):
    with Path(path).open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


def inspect_sqlite(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing regular SQLite file required')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as con:
        if con.execute('pragma integrity_check').fetchone()[0]!='ok' or con.execute('pragma foreign_key_check').fetchone():raise ValueError('SQLite integrity/FK check failed')
        tables={r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if not {'sessions','attempts'}<=tables:raise ValueError('Unsupported authentication schema')
        business=tables-{'sessions','attempts','sqlite_sequence'}
        versions={frozenset(set(LEGACY_TABLES)-{'decision_records'}):0,frozenset(LEGACY_TABLES):1,frozenset(V2_TABLES):2,frozenset(TABLES):3}
        if frozenset(business) not in versions:raise ValueError('Unknown/partial schema; no table may be silently omitted')
        if not con.execute('select 1 from users where active=1').fetchone():raise ValueError('No existing active users')
        version=versions[frozenset(business)]
        if version==3:
            actual={(kind,name):' '.join(sql.split()).lower() for kind,name,sql in con.execute(
                "select type,name,sql from sqlite_master where type in ('table','trigger') and sql is not null")}
            if actual!=expected_release_schema():raise ValueError('Unexpected table constraints/triggers; explicit review required')
        return {'version':version,'tables':tuple(t for t in TABLES if t in business)}


def prepare_sqlite(source,destination,closed_backup=False):
    if not closed_backup:raise ValueError('Explicit closed-backup contract required')
    source=Path(source).resolve(strict=True);destination=Path(destination).absolute()
    if any(Path(str(source)+suffix).exists() for suffix in ('-wal','-shm','-journal')):raise ValueError('Use a closed, independent standalone backup, never a live main file')
    if destination.exists() or destination.is_symlink():raise FileExistsError('New destination directory required')
    source_hash=checksum(source)
    if source.stat().st_size>268435456:raise ValueError('Established SQLite cap exceeded; do not silently increase it')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(destination.parent).free<source.stat().st_size*4+128*1024*1024:raise ValueError('Insufficient staging disk reserve')
    destination.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.private-upgrade-',dir=destination) as private:
        copy=Path(private)/'copy.sqlite3'
        with source.open('rb') as incoming,copy.open('xb') as output:shutil.copyfileobj(incoming,output,1024*1024)
        copy.chmod(0o600);inventory=inspect_sqlite(copy)
        with closing(sqlite3.connect(copy)) as con:before=fingerprints(con,tables=inventory['tables'])
        from .database import Database
        # This command is an explicit migration. Runtime release startup never executes DDL.
        if os.getenv('KSU_OFFLINE_MODE') is not None or os.getenv('KSU_SCHEMA_MODE')=='verified':raise ValueError('Run migration separately from release runtime configuration')
        Database(copy)
        with closing(sqlite3.connect(copy)) as con:
            if fingerprints(con,tables=inventory['tables'])!=before:raise ValueError('Existing records/users/audit changed during migration')
            after=fingerprints(con)
        from .maintenance import snapshot_database
        target=destination/'ksu.sqlite3';snapshot_database(copy,target)
        if inspect_sqlite(target)['version']!=3:raise ValueError('Final migrated copy failed release schema verification')
        if checksum(source)!=source_hash or any(Path(str(source)+suffix).exists() for suffix in ('-wal','-shm','-journal')):raise ValueError('Source changed during preparation; do not start staging')
        report={'source_sha256':source_hash,'schema_before':inventory['version'],'schema_after':3,'source_unchanged':True,
                'all_original_tables_preserved':True,'sessions_revoked':True,'tables':after}
        descriptor=os.open(destination/'migration-report.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'w') as handle:json.dump(report,handle,indent=2)
        return report


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    probe=sub.add_parser('inspect');probe.add_argument('source')
    prepare=sub.add_parser('prepare-copy');prepare.add_argument('source');prepare.add_argument('--destination',required=True);prepare.add_argument('--closed-backup',action='store_true',required=True)
    args=parser.parse_args()
    if args.action=='inspect':
        result=inspect_sqlite(args.source);print(json.dumps({'schema_version':result['version'],'table_names':result['tables']}))
    else:
        result=prepare_sqlite(args.source,args.destination,closed_backup=args.closed_backup)
        print(json.dumps({key:result[key] for key in ('schema_before','schema_after','source_unchanged','all_original_tables_preserved','sessions_revoked')}))


if __name__=='__main__':main()
