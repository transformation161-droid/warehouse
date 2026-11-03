"""Offline PostgreSQL administration. No deployment/platform calls or automatic cutover."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import psycopg
from psycopg import sql
from .database import ROOT,Database
from .maintenance import snapshot_database
from .pg_transport import validate_transport

LEGACY_TABLES=('users','user_sites','records','audit','mutations','media','imports','decision_records')
from .decision_version_schema import TABLES as VERSION_TABLES,schema as version_schema
TABLES=LEGACY_TABLES+VERSION_TABLES
KEYS={'users':'id','user_sites':'user_id,site_id','records':'collection,id','audit':'seq','mutations':'user_id,id','media':'id','imports':'hash','decision_records':'id','decision_documents':'id','decision_versions':'id','decision_entries':'record_id','decision_retirements':'record_id'}


def canonical(row):
    # JSON text is intentionally preserved byte for byte, not reparsed/reformatted.
    return json.dumps([{'bytes':hashlib.sha256(bytes(v)).hexdigest(),'length':len(v)} if isinstance(v,(bytes,bytearray,memoryview)) else v for v in row],ensure_ascii=False,separators=(',',':')).encode()


def fingerprints(connection,postgres=False,tables=None):
    result={}
    for table in tables or TABLES:
        order=KEYS[table]
        if postgres and table!='audit':order=','.join(column+' COLLATE "C"' for column in order.split(','))
        query='select * from '+('ksu.' if postgres else '')+table+' order by '+order
        if postgres:
            cursor=connection.cursor(name='verify_'+table);cursor.itersize=1 if table in ('media','imports') else 64;cursor.execute(query)
        else:cursor=connection.execute(query)
        digest=hashlib.sha256();count=0
        for row in cursor:
            digest.update(canonical(row));digest.update(b'\n');count+=1
        cursor.close();result[table]={'count':count,'sha256':digest.hexdigest()}
    return result


def grant_runtime(connection,role):
    ident=sql.Identifier(role)
    properties=connection.execute('select rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication from pg_roles where rolname=%s',(role,)).fetchone()
    if not properties or any(properties):raise ValueError('Runtime role must exist: NOSUPERUSER NOBYPASSRLS NOINHERIT')
    if connection.execute('select exists(select 1 from pg_auth_members m join pg_roles r on r.oid=m.member where r.rolname=%s)',(role,)).fetchone()[0]:raise ValueError('Runtime must not inherit or SET ROLE to other roles')
    connection.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA ksu FROM {}').format(ident))
    connection.execute(sql.SQL('GRANT USAGE ON SCHEMA ksu TO {}').format(ident))
    connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA ksu TO {}').format(ident))
    connection.execute(sql.SQL('GRANT INSERT,UPDATE ON ksu.records TO {}').format(ident))
    connection.execute(sql.SQL('GRANT INSERT ON ksu.media,ksu.imports,ksu.decision_records,ksu.mutations TO {}').format(ident))
    connection.execute(sql.SQL('GRANT INSERT ON ksu.decision_documents,ksu.decision_versions,ksu.decision_entries,ksu.decision_retirements TO {}').format(ident))
    connection.execute(sql.SQL('GRANT INSERT,UPDATE,DELETE ON ksu.sessions,ksu.attempts TO {}').format(ident))
    for function in ('actor_id()','actor_role()','accessible(text)','json_extract(text,text)','import_exists(text)','import_styles(text)','state_revision()','import_created_at(text)'):
        connection.execute(sql.SQL('GRANT EXECUTE ON FUNCTION ksu.'+function+' TO {}').format(ident))


def grant_backup(connection,role):
    ident=sql.Identifier(role)
    properties=connection.execute('select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole from pg_roles where rolname=%s',(role,)).fetchone()
    if not properties or properties!=(False,True,False,False):raise ValueError('Backup role requires only BYPASSRLS, no administrator permissions')
    connection.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA ksu FROM {}').format(ident))
    connection.execute(sql.SQL('GRANT USAGE ON SCHEMA ksu TO {}').format(ident))
    connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA ksu TO {}').format(ident))
    connection.execute(sql.SQL('GRANT SELECT ON ALL SEQUENCES IN SCHEMA ksu TO {}').format(ident))


def upgrade_schema(connection):
    connection.execute('select pg_advisory_xact_lock(170009)')
    connection.execute("set local search_path='ksu','pg_catalog'")
    version=connection.execute('select max(version) from ksu.schema_version').fetchone()[0]
    if version==1:connection.execute(version_schema(True))
    elif version!=2:raise ValueError('Unsupported schema version')


def upgrade(dsn,role,backup_role=None):
    validate_transport(dsn)
    with psycopg.connect(dsn) as connection:
        before=fingerprints(connection,True,LEGACY_TABLES)
        upgrade_schema(connection)
        if fingerprints(connection,True,LEGACY_TABLES)!=before:raise ValueError('Existing business data changed during upgrade')
        grant_runtime(connection,role)
        if backup_role:grant_backup(connection,backup_role)
        return {'schema_version':2,'existing_data_preserved':True,'tables':fingerprints(connection,True)}


def initialize(dsn,role,backup_role=None):
    validate_transport(dsn)
    with psycopg.connect(dsn) as connection:
        if connection.execute("select to_regnamespace('ksu')").fetchone()[0]:raise ValueError('Destination schema exists; refusing overwrite')
        connection.execute((ROOT/'server/postgres/001_initial.sql').read_text())
        upgrade_schema(connection)
        grant_runtime(connection,role)
        if backup_role:grant_backup(connection,backup_role)


def migrate_sqlite(source,dsn):
    """Read-only SQLite snapshot; one transaction into a verified empty target schema."""
    validate_transport(dsn)
    with tempfile.TemporaryDirectory(prefix='.ksu-migrate-',dir=os.getenv('KSU_PG_MIGRATION_DIR',str(Path(source).resolve().parent))) as directory:
        snapshot=Path(directory)/'synthetic-or-approved-source.sqlite3'
        snapshot_database(source,snapshot)
        if snapshot.stat().st_size>int(os.getenv('KSU_DB_MAX_BYTES','268435456')):raise ValueError('Source exceeds the established SQLite cap')
        Database(snapshot)  # Upgrade only the private copy, never the source pilot.
        with closing(sqlite3.connect(snapshot)) as old,psycopg.connect(dsn) as target:
            known=set(TABLES)|{'sessions','attempts','sqlite_sequence'}
            if {r[0] for r in old.execute("select name from sqlite_master where type='table'")}-known:raise ValueError('Unsupported source tables; refusing to omit data')
            expected=fingerprints(old)
            target.execute('select pg_advisory_xact_lock(170009)')
            if target.execute('select coalesce(max(version),0) from ksu.schema_version').fetchone()[0]!=2:raise ValueError('Unsupported target schema version')
            for table in TABLES+('sessions','attempts'):
                if target.execute(sql.SQL('select exists(select 1 from ksu.{})').format(sql.Identifier(table))).fetchone()[0]:
                    raise ValueError('Destination is not empty; migration cancelled')
            for table in TABLES:
                target.execute(sql.SQL('ALTER TABLE ksu.{} DISABLE TRIGGER USER').format(sql.Identifier(table)))
            for table in TABLES:
                with target.cursor().copy(sql.SQL('COPY ksu.{} FROM STDIN').format(sql.Identifier(table))) as copy:
                    for row in old.execute('select * from '+table):copy.write_row(tuple(row))
            target.execute('SET CONSTRAINTS ALL IMMEDIATE')
            target.execute("select setval(pg_get_serial_sequence('ksu.audit','seq'),coalesce(max(seq),1),max(seq) is not null) from ksu.audit")
            for table in TABLES:
                target.execute(sql.SQL('ALTER TABLE ksu.{} ENABLE TRIGGER USER').format(sql.Identifier(table)))
            maximum=int(os.getenv('KSU_PG_DATABASE_MAX_BYTES',os.getenv('KSU_DB_MAX_BYTES','268435456')))
            if maximum<=0 or target.execute('select pg_database_size(current_database())').fetchone()[0]>maximum:raise ValueError('Destination exceeds the established database cap')
            actual=fingerprints(target,True)
            if actual!=expected:raise ValueError('Migration checksums differ; transaction rolled back: '+','.join(k for k in expected if expected[k]!=actual[k]))
            return {'tables':actual,'sessions_revoked':True,'source_read_only':True}


def provision_roles(dsn,runtime_role,backup_role=None):
    validate_transport(dsn)
    password=os.environ['KSU_PG_RUNTIME_PASSWORD']
    if len(password)<20:raise ValueError('Use a generated service password of at least 20 characters')
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Identifier(runtime_role),sql.Literal(password)))
        if backup_role:
            backup_password=os.environ['KSU_PG_BACKUP_PASSWORD']
            if len(backup_password)<20:raise ValueError('Use a generated backup service password')
            connection.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS PASSWORD {}').format(sql.Identifier(backup_role),sql.Literal(backup_password)))


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Offline PostgreSQL tools; credentials only via environment')
    sub=parser.add_subparsers(dest='command',required=True)
    provision=sub.add_parser('provision-roles');provision.add_argument('--runtime-role',required=True);provision.add_argument('--backup-role')
    init=sub.add_parser('init');init.add_argument('--runtime-role',required=True);init.add_argument('--backup-role')
    update=sub.add_parser('upgrade');update.add_argument('--runtime-role',required=True);update.add_argument('--backup-role')
    migrate=sub.add_parser('migrate-sqlite');migrate.add_argument('source')
    backup=sub.add_parser('backup');backup.add_argument('destination')
    restore=sub.add_parser('restore');restore.add_argument('source');restore.add_argument('--runtime-role',required=True);restore.add_argument('--backup-role')
    args=parser.parse_args()
    try:
        dsn=os.environ['KSU_PG_ADMIN_DSN']
        if args.command=='provision-roles':provision_roles(dsn,args.runtime_role,args.backup_role);result={'roles_created':True}
        elif args.command=='init':initialize(dsn,args.runtime_role,args.backup_role);result={'schema_version':2}
        elif args.command=='upgrade':result=upgrade(dsn,args.runtime_role,args.backup_role)
        elif args.command=='migrate-sqlite':result=migrate_sqlite(args.source,dsn)
        elif args.command=='backup':
            from .pg_backup import backup_database
            result=backup_database(os.environ.get('KSU_PG_BACKUP_DSN',dsn),args.destination)
        else:
            from .pg_backup import restore_database
            result=restore_database(args.source,dsn,args.runtime_role,args.backup_role)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as error:
        # Driver/tool errors can contain DSNs/passwords: never echo them from CLI.
        parser.exit(1,'PostgreSQL operation failed: '+type(error).__name__+'. Destination must be inspected locally.\n')


if __name__=='__main__':main()
