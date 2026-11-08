"""Independent, bounded, consistent pg_dump packages. Trusted administrator restore only."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import zipfile

import psycopg
from psycopg.conninfo import conninfo_to_dict
from .pg_admin import fingerprints, grant_runtime, grant_backup, TABLES, LEGACY_TABLES, upgrade_schema, SCHEMA_VERSION, tables_for_version
from .pg_transport import validate_transport


class BackupError(Exception):pass


def tool_environment(dsn):
    info=validate_transport(dsn)
    # Remote backups must validate both CA and hostname; localhost is the local test path.
    if info.get('host','') not in ('','localhost','127.0.0.1','::1') and not info.get('host','').startswith('/') and info.get('sslmode')!='verify-full':
        raise BackupError('Remote PostgreSQL requires sslmode=verify-full')
    names={'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD','dbname':'PGDATABASE',
           'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY'}
    env=dict(os.environ)  # Preserve injected password/passfile/CA bindings; explicit DSN wins.
    env.update({names[key]:value for key,value in info.items() if key in names});return env


def checksum(path):
    with Path(path).open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


def run_tool(name,args,dsn,output=None,maximum=None):
    command=[os.getenv('KSU_'+name.upper()+'_BIN',name),*args]
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen(command,env=tool_environment(dsn),stdout=log,stderr=log)
        deadline=time.monotonic()+int(os.getenv('KSU_PG_BACKUP_TIMEOUT','600'))
        try:
            while process.poll() is None:
                if time.monotonic()>deadline:raise BackupError('PostgreSQL tool deadline exceeded')
                if output and Path(output).exists() and maximum and Path(output).stat().st_size>maximum:raise BackupError('Backup exceeds the configured byte cap')
                if output and shutil.disk_usage(Path(output).parent).free<64*1024*1024:raise BackupError('Insufficient backup disk space')
                time.sleep(.1)
            if process.returncode:raise BackupError(name+' failed; inspect private administrator diagnostics')
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:process.kill();process.wait()


def validate_schema(connection,version=SCHEMA_VERSION):
    tables={row[0] for row in connection.execute("select tablename from pg_tables where schemaname='ksu'")}
    if tables!=set(tables_for_version(version))|{'schema_version','sessions','attempts'}:raise BackupError('Unsupported PostgreSQL schema; no table may be silently omitted')
    if connection.execute('select coalesce(max(version),0) from ksu.schema_version').fetchone()[0]!=version:raise BackupError('Unsupported PostgreSQL schema version')


def backup_database(dsn,destination):
    target=Path(destination)
    if target.exists() or target.is_symlink():raise FileExistsError('Backup already exists')
    target=target.resolve()
    maximum=int(os.getenv('KSU_PG_BACKUP_MAX_BYTES',str(512*1024*1024)))
    if maximum<=0:raise BackupError('Backup cap must be positive')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if shutil.disk_usage(target.parent).free<maximum*2+64*1024*1024:raise BackupError('Need room for dump and package within the fixed byte cap')
    with tempfile.TemporaryDirectory(prefix='.ksu-pg-backup-',dir=target.parent) as folder:
        dump=Path(folder)/'database.dump';bundle=Path(folder)/'backup.ksubackup'
        with psycopg.connect(dsn,connect_timeout=10) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            if connection.execute("select has_table_privilege(current_user,'ksu.records','INSERT,UPDATE,DELETE')").fetchone()[0]:raise BackupError('Backup requires a dedicated read-only database role')
            if not connection.execute('select pg_try_advisory_lock(140014)').fetchone()[0]:raise BackupError('Another database backup is active')
            validate_schema(connection)
            snapshot=connection.execute('select pg_export_snapshot()').fetchone()[0]
            run_tool('pg_dump',['--format=custom','--compress=0','--no-owner','--no-acl','--schema=ksu',
                '--exclude-table-data=ksu.sessions','--exclude-table-data=ksu.attempts','--snapshot='+snapshot,
                '--lock-wait-timeout=15000','--file='+str(dump)],dsn,dump,maximum)
            manifest={'format':'ksu-pg-backup-v1','schema_version':SCHEMA_VERSION,'dump_sha256':checksum(dump),
                      'tables':fingerprints(connection,True),'sessions_revoked':True}
        os.chmod(dump,0o600)
        with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            archive.write(dump,'database.dump');archive.writestr('manifest.json',json.dumps(manifest,separators=(',',':')))
        if bundle.stat().st_size>maximum:raise BackupError('Package exceeds fixed byte cap')
        os.chmod(bundle,0o600)
        with bundle.open('rb') as handle:os.fsync(handle.fileno())
        digest=checksum(bundle);size=bundle.stat().st_size
        os.link(bundle,target)  # Atomic no-overwrite publication into a private directory.
        directory=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return {'sha256':digest,'bytes':size,'tables':manifest['tables'],'sessions_revoked':True}


def restore_database(source,dsn,runtime_role,backup_role=None):
    source=Path(source).resolve(strict=True)
    with psycopg.connect(dsn,connect_timeout=10) as connection:
        if connection.execute("select to_regnamespace('ksu')").fetchone()[0]:raise BackupError('Destination schema exists; refusing overwrite')
    maximum=int(os.getenv('KSU_PG_BACKUP_MAX_BYTES',str(512*1024*1024)))
    if source.stat().st_size>maximum:raise BackupError('Restore package exceeds fixed cap')
    with tempfile.TemporaryDirectory(prefix='.ksu-restore-',dir=os.getenv('KSU_PG_RESTORE_DIR',str(source.parent))) as folder,zipfile.ZipFile(source) as archive:
        if archive.namelist()!=['database.dump','manifest.json']:raise BackupError('Unexpected backup members')
        if any(item.compress_type!=zipfile.ZIP_STORED for item in archive.infolist()):raise BackupError('Compressed/untrusted package')
        if archive.getinfo('manifest.json').file_size>65536:raise BackupError('Manifest too large')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!='ksu-pg-backup-v1' or manifest.get('schema_version') not in (1,2,3):raise BackupError('Unsupported backup format')
        expected_tables=tables_for_version(manifest['schema_version'])
        if set(manifest.get('tables',{}))!=set(expected_tables):raise BackupError('Unexpected manifest tables')
        dump=Path(folder)/'database.dump'
        with archive.open('database.dump') as src,dump.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
        os.chmod(dump,0o600)
        if checksum(dump)!=manifest['dump_sha256']:raise BackupError('Backup checksum mismatch')
        # pg_restore executes SQL: only restore packages from a trusted administrator.
        run_tool('pg_restore',['--single-transaction','--exit-on-error','--no-owner','--no-acl','--dbname='+conninfo_to_dict(dsn).get('dbname','postgres'),str(dump)],dsn)
        with psycopg.connect(dsn,connect_timeout=10) as connection:
            validate_schema(connection,manifest['schema_version'])
            actual=fingerprints(connection,True,expected_tables)
            if actual!=manifest['tables']:raise BackupError('Restored table checksums differ; do not start the app')
            if connection.execute('select count(*) from ksu.sessions').fetchone()[0] or connection.execute('select count(*) from ksu.attempts').fetchone()[0]:
                raise BackupError('Restored sessions must be empty')
            if manifest['schema_version']<SCHEMA_VERSION:
                upgrade_schema(connection)
                if fingerprints(connection,True,expected_tables)!=actual:raise BackupError('Legacy data changed during restore upgrade')
                actual=fingerprints(connection,True)
            grant_runtime(connection,runtime_role)
            if backup_role:grant_backup(connection,backup_role)
            if connection.execute("select count(*) from pg_constraint where connamespace='ksu'::regnamespace and not convalidated").fetchone()[0]:raise BackupError('Unvalidated constraints')
        return {'verified':True,'tables':actual,'sessions_revoked':True,'package_sha256':checksum(source)}
