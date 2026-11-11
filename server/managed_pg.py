"""Owner-local managed PostgreSQL operations. No provider API, deployment or implicit cutover.

DSNs/passwords are supplied through private env files/secret bindings, never argv or logs.
Only known closed SQLite copies and new empty PostgreSQL schemas are accepted.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import tempfile

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from .backup_job import atomic_json
from .pg_admin import TABLES,fingerprints,initialize,migrate_sqlite
from .pg_backup import backup_database,restore_database,validate_schema
from .pg_transport import validate_transport,connect,session_transport
from .release_schema import checksum,prepare_sqlite

ROLES=('ksu_admin','ksu_runtime','ksu_backup')


def binding(name):
    direct=os.getenv(name);file=os.getenv(name+'_FILE')
    if bool(direct)==bool(file):raise ValueError('Exactly one explicit secret binding required')
    return Path(file).read_text().strip() if file else direct


def transport(dsn):
    return validate_transport(dsn,managed=True)


# Compatibility for owner tooling from PR37/38; use transport() in new code.
tls=transport


def role_properties(con,role):
    row=con.execute('select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit,rolreplication from pg_roles where rolname=%s',(role,)).fetchone()
    if not row or any(row):raise ValueError('Dedicated non-superuser NOBYPASSRLS NOINHERIT role required')
    if con.execute('select exists(select 1 from pg_auth_members where member=(select oid from pg_roles where rolname=%s))',(role,)).fetchone()[0]:raise ValueError('Service role memberships forbidden')


def preflight(dsn,kind='runtime'):
    info=transport(dsn)
    if kind not in ('runtime','admin','backup') or info['user']!='ksu_'+kind:raise ValueError('Explicit service role binding required')
    with connect(dsn,connect_timeout=10) as con:
        con.execute('set transaction isolation level repeatable read read only')
        observed=session_transport(con)
        role_properties(con,info['user'])
        version=con.info.server_version
        if not 170000<=version<180000:raise ValueError('This candidate is validated for PostgreSQL 17; approve another major separately')
        validate_schema(con)
        owner=con.execute("select pg_get_userbyid(nspowner) from pg_namespace where nspname='ksu'").fetchone()[0]
        if owner!='ksu_admin':raise ValueError('Schema must be owned by offline admin, never runtime/backup')
        if kind!='admin':
            if con.execute("select exists(select 1 from pg_class where relnamespace='ksu'::regnamespace and relowner=(select oid from pg_roles where rolname=current_user))").fetchone()[0]:raise ValueError('Service owns database objects')
            if con.execute("select has_schema_privilege(current_user,'ksu','CREATE') or has_schema_privilege(current_user,'public','CREATE') or has_database_privilege(current_user,current_database(),'CREATE')").fetchone()[0]:raise ValueError('Service DDL privileges forbidden')
        if kind=='backup':
            for table in TABLES:
                if con.execute('select has_table_privilege(current_user,%s,%s)',('ksu.'+table,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')).fetchone()[0]:raise ValueError('Backup must be SELECT-only')
            missing=con.execute("""select exists(select 1 from pg_class c where c.relnamespace='ksu'::regnamespace and c.relrowsecurity
                and not exists(select 1 from pg_policy p where p.polrelid=c.oid and p.polname='managed_backup_read'
                and p.polcmd='r' and p.polpermissive and pg_get_expr(p.polqual,p.polrelid)='true'
                and p.polroles=array[(select oid from pg_roles where rolname=current_user)]))""").fetchone()[0]
            if missing:raise ValueError('Complete backup SELECT policies required')
            # Read-only consistent fingerprints exercise every per-role RLS policy.
            fingerprints(con,True)
        return {'backend':'postgresql','major':17,'role':kind,'schema':4,**observed}


def bootstrap(dsn,passwords):
    transport(dsn)
    if set(passwords)!=set(ROLES) or any(not isinstance(p,str) or len(p)<20 for p in passwords.values()):raise ValueError('Three generated service passwords required')
    with connect(dsn,connect_timeout=10) as con:
        # This is for the owner's newly approved database only; never retrofit a live DB.
        if con.execute("select exists(select 1 from pg_namespace where nspname not in ('public','information_schema') and nspname not like 'pg_%')").fetchone()[0] or con.execute("select exists(select 1 from pg_class where relnamespace='public'::regnamespace and relkind in ('r','p','v','m','S'))").fetchone()[0]:raise ValueError('New empty target database required')
        for role in ROLES:
            con.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Identifier(role),sql.Literal(passwords[role])))
        database=sql.Identifier(con.info.dbname)
        con.execute(sql.SQL('REVOKE CONNECT,CREATE,TEMPORARY ON DATABASE {} FROM PUBLIC').format(database))
        con.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        for role in ROLES:con.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(database,sql.Identifier(role)))
        con.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO ksu_admin').format(database))
    return {'roles_created':3,'superuser_in_application':False,'target_empty':True}


def migrate(source,dsn,output):
    transport(dsn)
    if conninfo_to_dict(dsn)['user']!='ksu_admin':raise ValueError('Offline schema admin required')
    if os.getenv('KSU_OFFLINE_MODE') is not None:raise ValueError('Run migration outside application runtime settings')
    source=Path(source);original=checksum(source);output=Path(output)
    if output.exists() or output.is_symlink():raise FileExistsError('New private report path required')
    # The release image defaults to verification only. Explicit owner copy migration
    # changes that setting in this one-off process, never in a running application.
    old_mode=os.environ.get('KSU_SCHEMA_MODE');os.environ['KSU_SCHEMA_MODE']='explicit-copy-migration'
    try:
        with tempfile.TemporaryDirectory(prefix='ksu-private-pg-migrate-',dir=os.getenv('KSU_PG_MIGRATION_DIR','/tmp')) as folder:
            prepared=Path(folder)/'v4';report=prepare_sqlite(source,prepared,closed_backup=True)
            initialize(dsn,'ksu_runtime','ksu_backup')
            # This is our newly created private working copy. Its read-only schema
            # inspection can leave empty WAL-index sidecars; snapshot it normally.
            # Only prepare_sqlite ever receives the owner's closed standalone input.
            result=migrate_sqlite(prepared/'ksu.sqlite3',dsn)
            if result['tables']!=report['tables'] or checksum(source)!=original:raise ValueError('Source/migrated fingerprints differ')
            report.update(result,source_sha256=original,source_unchanged=True,backend='postgresql',schema_after=4)
            preflight(dsn,'admin')
            atomic_json(output,report,exclusive=True)
            return {'migrated':True,'business_tables':len(TABLES),'source_unchanged':True,'schema':4,'ready_for_writes':False}
    finally:
        if old_mode is None:os.environ.pop('KSU_SCHEMA_MODE',None)
        else:os.environ['KSU_SCHEMA_MODE']=old_mode


def verify_report(dsn,path):
    transport(dsn);report=json.loads(Path(path).read_text())
    fresh=report.get('format')=='ksu-production-initialization-v1' and report.get('initialized') is True
    if not (fresh or report.get('source_unchanged')) or not report.get('sessions_revoked') or set(report.get('tables',{}))!=set(TABLES):raise ValueError('Complete private setup/migration report required')
    with connect(dsn,connect_timeout=10) as con:
        con.execute('set transaction isolation level repeatable read read only')
        validate_schema(con)
        if fingerprints(con,True)!=report['tables']:raise ValueError('Target differs from migrated source')
        if con.execute('select count(*) from ksu.sessions').fetchone()[0] or con.execute('select count(*) from ksu.attempts').fetchone()[0]:raise ValueError('Migrated sessions must be empty before activation')
    return {'verified':True,'business_tables':len(TABLES),'passwords_media_history_receipts_identical':True}


def main():
    import argparse,sys
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=('bootstrap','init-schema','init-production','template','probe','migrate','verify','preflight','backup','restore'))
    parser.add_argument('--source');parser.add_argument('--output');parser.add_argument('--report');parser.add_argument('--role',choices=('runtime','admin','backup','bootstrap'),default='runtime')
    args=parser.parse_args()
    try:
        if args.operation=='template':
            from .production_init import template
            atomic_json(Path(args.output),template(),exclusive=True)
            print(json.dumps({'template_written':True,'owner_approval_required':True}));return
        key={'bootstrap':'KSU_PG_BOOTSTRAP_DSN','init-schema':'KSU_PG_ADMIN_DSN','init-production':'KSU_PG_ADMIN_DSN','backup':'KSU_PG_BACKUP_DSN','verify':'KSU_PG_BACKUP_DSN','restore':'KSU_PG_ADMIN_DSN','migrate':'KSU_PG_ADMIN_DSN'}.get(args.operation,{'runtime':'KSU_DATABASE_URL','admin':'KSU_PG_ADMIN_DSN','backup':'KSU_PG_BACKUP_DSN','bootstrap':'KSU_PG_BOOTSTRAP_DSN'}[args.role])
        dsn=binding(key);transport(dsn)
        if args.operation=='probe':
            with connect(dsn,connect_timeout=10) as con:result={'connected':True,'server_major':con.info.server_version//10000,**session_transport(con)}
        elif args.operation=='bootstrap':result=bootstrap(dsn,json.load(sys.stdin))
        elif args.operation=='init-schema':
            if conninfo_to_dict(dsn)['user']!='ksu_admin':raise ValueError('Offline schema admin required')
            initialize(dsn,'ksu_runtime','ksu_backup');result={'schema':4,'business_data_created':False}
        elif args.operation=='init-production':
            from .production_init import initialize as setup
            result=setup(dsn,json.loads(Path(args.source).read_text(encoding='utf-8-sig')),args.output)
        elif args.operation=='migrate':
            if os.getenv('KSU_DEPLOYMENT_PROFILE')=='production':raise ValueError('Clean production pilot forbids SQLite migration')
            result=migrate(args.source,dsn,args.output)
        elif args.operation=='verify':result=verify_report(dsn,args.report)
        elif args.operation=='preflight':result=preflight(dsn,args.role)
        elif args.operation=='backup':
            preflight(dsn,'backup');result=backup_database(dsn,args.output)
            result={k:v for k,v in result.items() if k!='tables'};result['independent_restore_required']=True
        else:
            if conninfo_to_dict(dsn)['user']!='ksu_admin':raise ValueError('Offline admin required')
            result=restore_database(args.source,dsn,'ksu_runtime','ksu_backup');preflight(dsn,'admin')
            if args.output:atomic_json(Path(args.output),result,exclusive=True)
            result={k:v for k,v in result.items() if k!='tables'}
        print(json.dumps(result))
    except Exception as error:raise SystemExit('Managed PostgreSQL operation refused: '+type(error).__name__+'. Inspect private inputs/target; no automatic retry or overwrite.')


if __name__=='__main__':main()
