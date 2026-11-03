"""Explicit PostgreSQL adapter; qmark parameters only, no data/schema changes at startup."""
from contextlib import contextmanager
import os
from pathlib import Path
import re

import psycopg
from psycopg.rows import dict_row
from .database import Database
from .pg_transport import validate_transport
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
    def execute(self,query,params=()):
        if query.lower().strip()=='begin immediate':
            # Preserve SQLite's atomic write/version/idempotency contract across workers.
            return Cursor(self.raw.execute('select pg_advisory_xact_lock(170009)'))
        if query.lower().strip()=='begin':return Cursor(self.raw.execute('select 1'))
        # Static application SQL uses qmark parameters; never interpolate parameter values.
        pieces=re.split("('(?:''|[^'])*')",query)
        query=''.join(piece.replace('%','%%').replace('?', '%s') if i%2==0 else piece.replace('%','%%') for i,piece in enumerate(pieces))
        return Cursor(self.raw.execute(query,params))


class PostgresDatabase:
    backend='postgresql'
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
            if con.execute("select count(*) from pg_class where relnamespace='ksu'::regnamespace and relname in ('records','media','decision_records','audit','user_sites','mutations','imports') and relrowsecurity").fetchone()[0]!=7:raise ValueError('Required RLS policies are not enabled')
            if con.execute("select has_table_privilege(current_user,'ksu.users','UPDATE')").fetchone()[0]:raise ValueError('Runtime cannot administer user roles')
            if con.execute('select coalesce(max(version),0) from schema_version').fetchone()[0]!=1:raise ValueError('Unsupported PostgreSQL schema version')

    @contextmanager
    def connect(self,actor=None):
        with psycopg.connect(self.dsn,connect_timeout=10,row_factory=dict_row) as connection:
            connection.execute("select set_config('search_path','ksu,pg_catalog',true)")
            connection.execute("select set_config('ksu.actor',%s,true)",(actor['id'] if actor else '',))
            connection.execute("set local statement_timeout='120s'")
            connection.execute("set local lock_timeout='15s'")
            yield Connection(connection)

    def check_capacity(self,con,estimate=65536):
        maximum=int(os.getenv('KSU_PG_DATABASE_MAX_BYTES',os.getenv('KSU_DB_MAX_BYTES','268435456')))
        if maximum<=0:raise ValueError('PostgreSQL database cap must be positive')
        if con.execute('select pg_database_size(current_database())').fetchone()[0]+estimate>maximum:
            raise StorageFull('PostgreSQL database cap reached; no automatic growth or paid action')

    def revision(self,con):return con.execute('select state_revision()').fetchone()[0]

    def import_exists(self,con,signature):return con.execute('select import_exists(?)',(signature,)).fetchone()[0]
    def import_metadata(self,con,signature):return con.execute('select import_styles(?)',(signature,)).fetchone()[0]
