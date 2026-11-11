"""Explicit PostgreSQL adapter; qmark parameters only, no data/schema changes at startup."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from .database import Database
from .pg_transport import validate_transport,connect
from .storage import StorageFull


class Row(dict):
    def __getitem__(self,key):
        return list(self.values())[key] if isinstance(key,int) else super().__getitem__(key)


class Cursor:
    def __init__(self,cursor):self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone();return Row(row) if row is not None else None
    def fetchall(self):return [Row(row) for row in self.cursor.fetchall()]
    def __iter__(self):
        for row in self.cursor:yield Row(row)


class Connection:
    def __init__(self,connection):self.raw=connection
    @staticmethod
    def parameters(query):
        pieces=re.split("('(?:''|[^'])*')",query)
        return ''.join(piece.replace('%','%%').replace('?', '%s') if i%2==0 else piece.replace('%','%%') for i,piece in enumerate(pieces))

    def execute(self,query,params=()):
        if query.lower().strip()=='begin immediate':
            # Preserve SQLite's atomic write/version/idempotency contract across workers.
            return Cursor(self.raw.execute('select pg_advisory_xact_lock(170009)'))
        if query.lower().strip()=='begin':return Cursor(self.raw.execute('select 1'))
        # Static application SQL uses qmark parameters; never interpolate parameter values.
        return Cursor(self.raw.execute(self.parameters(query),params))

    def stream(self,query,params=()):
        with self.raw.cursor(name="ksu_stream_"+uuid4().hex) as cursor:
            cursor.itersize=64
            cursor.execute(self.parameters(query),params)
            for row in cursor:yield Row(row)


class PostgresDatabase:
    backend='postgresql'
    decision_order='import_created_at(d.import_hash)'
    user=Database.user
    snapshot=Database.snapshot

    def __init__(self,dsn):
        validate_transport(dsn)
        self.dsn=dsn
        self.locator='postgresql'  # Never persist credentials in import metadata.
        self.path=Path(os.environ.get('KSU_WORK_DIR','var/postgres-work')).resolve()/'staging'
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as con:
            role=con.execute('select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolinherit from pg_roles where rolname=current_user').fetchone()
            owner=con.execute("select pg_get_userbyid(nspowner)=current_user from pg_namespace where nspname='ksu'").fetchone()
            if any(role.values()) or owner and owner[0]:raise ValueError('PostgreSQL runtime must be a non-owner without BYPASSRLS/superuser')
            if con.execute('select exists(select 1 from pg_auth_members where member=(select oid from pg_roles where rolname=current_user))').fetchone()[0]:raise ValueError('Runtime role memberships are forbidden')
            if con.execute("select exists(select 1 from pg_class where relnamespace='ksu'::regnamespace and relowner=(select oid from pg_roles where rolname=current_user))").fetchone()[0]:raise ValueError('Runtime must not own PostgreSQL objects')
            if con.execute("select count(*) from pg_class where relnamespace='ksu'::regnamespace and relname in ('records','media','decision_records','audit','user_sites','mutations','imports','decision_documents','decision_versions','decision_entries','decision_retirements') and relrowsecurity").fetchone()[0]!=11:raise ValueError('Required RLS policies are not enabled')
            if con.execute("select count(*) from pg_policy where polname='viewer_restriction' and not polpermissive and polrelid in (select oid from pg_class where relnamespace='ksu'::regnamespace)").fetchone()[0]!=15:raise ValueError('Complete viewer restrictions required')
            self.viewer_read_capable=bool(con.execute("select to_regprocedure('ksu.viewer_read_version()')").fetchone()[0])
            if self.viewer_read_capable:self.viewer_read_capable=con.execute('select viewer_read_version()').fetchone()[0]==2
            if con.execute("select has_table_privilege(current_user,'ksu.users','UPDATE')").fetchone()[0]:raise ValueError('Runtime cannot administer user roles')
            if con.execute('select coalesce(max(version),0) from schema_version').fetchone()[0]!=4:raise ValueError('PostgreSQL schema v4 required; run the offline upgrade before starting')
            if con.execute("select count(*) from pg_class where relnamespace='ksu'::regnamespace and relname in ('checklist_versions','operation_receipts','media_keys','offline_packages') and relrowsecurity").fetchone()[0]!=4:raise ValueError('Offline RLS policies are required')

    @contextmanager
    def connect(self,actor=None):
        with connect(self.dsn,connect_timeout=10,row_factory=dict_row) as connection:
            connection.execute("select set_config('search_path','ksu,pg_catalog',true)")
            connection.execute("select set_config('ksu.actor',%s,true)",(actor['id'] if actor else '',))
            connection.execute("set local statement_timeout='120s'")
            connection.execute("set local lock_timeout='15s'")
            yield Connection(connection)

    def iterate(self,con,query,params=()):return con.stream(query,params)

    def check_capacity(self,con,estimate=65536):
        maximum=int(os.getenv('KSU_PG_DATABASE_MAX_BYTES',os.getenv('KSU_DB_MAX_BYTES','268435456')))
        if maximum<=0:raise ValueError('PostgreSQL database cap must be positive')
        if con.execute('select pg_database_size(current_database())').fetchone()[0]+estimate>maximum:
            raise StorageFull('PostgreSQL database cap reached; no automatic growth or paid action')

    def revision(self,con):return con.execute('select state_revision()').fetchone()[0]

    def import_exists(self,con,signature):return con.execute('select import_exists(?)',(signature,)).fetchone()[0]
    def import_metadata(self,con,signature):return con.execute('select import_styles(?)',(signature,)).fetchone()[0]
