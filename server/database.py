"""Database boundary: SQLite remains the default; PostgreSQL is explicitly selected."""
import hashlib
import json
import os
import secrets
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + key


def password_valid(password, encoded):
    return secrets.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


SCHEMA = """
pragma journal_mode=WAL;
begin immediate;
create table if not exists users(
 id text primary key, username text not null unique, name text not null,
 role text not null check(role in ('manager','inspector')), password text not null, active integer not null default 1);
create table if not exists user_sites(user_id text references users(id), site_id text, primary key(user_id,site_id));
create table if not exists sessions(token_hash text primary key, user_id text references users(id), csrf text not null, expires real not null);
create table if not exists attempts(key text primary key, count integer not null, until real not null);
create table if not exists records(
 collection text not null check(collection in ('sites','questions','visits','inspections','findings','inventories','inventoryLines')),
 id text not null, data text not null check(json_valid(data)),
 author_id text not null references users(id), site_id text,
 version integer not null check(version>0), created_at text not null, updated_at text not null,
 primary key(collection,id));
create index if not exists records_site on records(site_id,collection);
create table if not exists audit(
 seq integer primary key autoincrement, at text not null, actor_id text not null,
 collection text not null, record_id text not null, operation text not null, before_json text, after_json text);
create table if not exists mutations(user_id text references users(id), id text not null, body_hash text not null,
 primary key(user_id,id));
create table if not exists media(id text primary key, site_id text not null, author_id text references users(id),
 content blob not null, thumbnail blob not null, created_at text not null);
create table if not exists imports(hash text primary key, actor_id text not null, raw_json text not null, created_at text not null);
-- Replace permissions atomically on existing pilot databases; never reseed records.
drop trigger if exists guard_insert;
drop trigger if exists guard_update;
drop trigger if exists guard_visit_insert;
drop trigger if exists guard_visit_update;
drop trigger if exists log_insert;
drop trigger if exists log_update;
create table if not exists decision_records(
 id text primary key, import_hash text not null references imports(hash), file_sha text not null,
 sheet text not null, source_row integer not null, site_id text not null,
 author_id text not null references users(id), data text not null check(json_valid(data)),
 unique(file_sha,sheet,source_row));
create index if not exists decisions_site on decision_records(site_id,source_row);
create trigger if not exists decision_guard before insert on decision_records begin
 select case when actor_role() not in ('manager','inspector') or new.author_id!=actor_id() or
 not exists(select 1 from records where collection='sites' and id=new.site_id) or
 (actor_role()='inspector' and not exists(select 1 from user_sites where user_id=actor_id() and site_id=new.site_id))
 then raise(abort,'forbidden') end;
end;
create trigger if not exists decision_no_update before update on decision_records begin select raise(abort,'immutable import'); end;
create trigger if not exists decision_no_delete before delete on decision_records begin select raise(abort,'immutable import'); end;
create trigger if not exists decision_audit after insert on decision_records begin
 insert into audit(at,actor_id,collection,record_id,operation,after_json)
 values(strftime('%Y-%m-%dT%H:%M:%fZ','now'),actor_id(),'decisions',new.id,'import',new.data);
end;
create trigger guard_insert before insert on records begin
 select case when actor_role() not in ('manager','inspector') then raise(abort,'forbidden') end;
 select case when new.author_id != actor_id() then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and (new.collection in ('sites','questions') or
 not exists(select 1 from user_sites where user_id=actor_id() and site_id=new.site_id))
 then raise(abort,'forbidden') end;
 select case when new.collection='inventories' and json_extract(new.data,'$.stage')!='primary' then raise(abort,'forbidden') end;
end;
create trigger guard_update before update on records begin
 select case when actor_role() not in ('manager','inspector') or new.author_id!=old.author_id or
 new.collection!=old.collection or new.id!=old.id or new.created_at!=old.created_at or new.version!=old.version+1
 then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and new.collection='inventories' and
 (json_extract(new.data,'$.stage')!=json_extract(old.data,'$.stage') or json_extract(new.data,'$.decision')!=json_extract(old.data,'$.decision'))
 then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and (
 new.collection in ('sites','questions','inspections') or (new.collection!='visits' and new.site_id is not old.site_id) or
 not exists(select 1 from user_sites where user_id=actor_id() and site_id=new.site_id) or
 not exists(select 1 from user_sites where user_id=actor_id() and site_id=old.site_id) or
 (new.collection!='visits' and old.author_id!=actor_id()) or
 (new.collection='visits' and (json_extract(old.data,'$.assignedTo') is not actor_id() or
 json_extract(new.data,'$.assignedTo') is not actor_id())))
 then raise(abort,'forbidden') end;
end;
create trigger guard_visit_insert before insert on records when new.collection='visits' begin
 select case when json_extract(new.data,'$.status') is not 'planned' or
 json_extract(new.data,'$.siteId') is not new.site_id or
 json_extract(new.data,'$.inspectionId') is not null or
 coalesce(json_extract(new.data,'$.cancellationReason'),'')!='' or
 json_extract(new.data,'$.cancelledBy') is not null or json_extract(new.data,'$.cancelledAt') is not null or
 (actor_role()='inspector' and json_extract(new.data,'$.assignedTo') is not actor_id()) or
 not exists(select 1 from users u where u.id=json_extract(new.data,'$.assignedTo') and u.active=1 and
 (u.role='manager' or exists(select 1 from user_sites us where us.user_id=u.id and us.site_id=new.site_id)))
 then raise(abort,'forbidden') end;
end;
create trigger guard_visit_update before update on records when new.collection='visits' begin
 select case when json_extract(old.data,'$.status') is not 'planned' or
 json_extract(new.data,'$.status') not in ('planned','completed','cancelled') or
 json_extract(new.data,'$.status') is null or json_extract(new.data,'$.siteId') is not new.site_id or
 not exists(select 1 from users u where u.id=json_extract(new.data,'$.assignedTo') and u.active=1 and
 (u.role='manager' or exists(select 1 from user_sites us where us.user_id=u.id and us.site_id=new.site_id)))
 then raise(abort,'forbidden') end;
 select case when json_extract(new.data,'$.status')!='planned' and (
 json_extract(new.data,'$.date') is not json_extract(old.data,'$.date') or
 new.site_id is not old.site_id or
 json_extract(new.data,'$.kind') is not json_extract(old.data,'$.kind') or
 json_extract(new.data,'$.assignedTo') is not json_extract(old.data,'$.assignedTo') or
 coalesce(json_extract(new.data,'$.goal'),'')!=coalesce(json_extract(old.data,'$.goal'),''))
 then raise(abort,'forbidden') end;
 select case when json_extract(new.data,'$.status')='cancelled' and (
 length(trim(coalesce(json_extract(new.data,'$.cancellationReason'),''))) not between 1 and 1200 or
 json_extract(new.data,'$.cancelledBy') is not actor_id() or
 length(coalesce(json_extract(new.data,'$.cancelledAt'),''))=0)
 then raise(abort,'forbidden') end;
 select case when json_extract(new.data,'$.status')!='cancelled' and (
 coalesce(json_extract(new.data,'$.cancellationReason'),'')!='' or
 json_extract(new.data,'$.cancelledBy') is not null or json_extract(new.data,'$.cancelledAt') is not null)
 then raise(abort,'forbidden') end;
 select case when json_extract(new.data,'$.status')='completed' and (
 not exists(select 1 from records ins where ins.collection='inspections' and
 ins.id=json_extract(new.data,'$.inspectionId') and ins.site_id=new.site_id and
 ins.author_id=json_extract(new.data,'$.assignedTo')) or
 exists(select 1 from records v where v.collection='visits' and v.id!=new.id and
 json_extract(v.data,'$.inspectionId')=json_extract(new.data,'$.inspectionId')))
 then raise(abort,'forbidden') end;
 select case when json_extract(new.data,'$.status')!='completed' and json_extract(new.data,'$.inspectionId') is not null
 then raise(abort,'forbidden') end;
end;
create trigger if not exists no_record_delete before delete on records begin select raise(abort,'deletion disabled'); end;
create trigger if not exists log_insert after insert on records begin
 insert into audit(at,actor_id,collection,record_id,operation,after_json)
 values(new.updated_at,actor_id(),new.collection,new.id,'create',
 case when new.collection='visits' then json_set(new.data,'$.version',new.version) else new.data end);
end;
create trigger if not exists log_update after update on records begin
 insert into audit(at,actor_id,collection,record_id,operation,before_json,after_json)
 values(new.updated_at,actor_id(),new.collection,new.id,'update',
 case when new.collection='visits' then json_set(old.data,'$.version',old.version) else old.data end,
 case when new.collection='visits' then json_set(new.data,'$.version',new.version) else new.data end);
end;
create trigger if not exists audit_no_update before update on audit begin select raise(abort,'append only'); end;
create trigger if not exists audit_no_delete before delete on audit begin select raise(abort,'append only'); end;
commit;
"""


def is_postgres(value):
    value=str(value)
    return value=='postgresql' or value.startswith(('postgresql://','postgres://')) or bool(re.match(r'\w+=',value))


LEGACY_SCHEMA=SCHEMA
from .viewer_schema import USER_SQL
_old_user=SCHEMA[SCHEMA.index('create table if not exists users('):SCHEMA.index('create table if not exists user_sites(')]
SCHEMA=SCHEMA.replace(_old_user,USER_SQL.replace('create table users','create table if not exists users')+';\n')


class Database:
    backend='sqlite'
    decision_order='(select created_at from imports where hash=d.import_hash)'

    def __new__(cls,path):
        value=str(path)
        if is_postgres(value):
            from .postgres_backend import PostgresDatabase
            dsn=os.environ.get('KSU_DATABASE_URL') if value=='postgresql' else value
            if not dsn:raise ValueError('KSU_DATABASE_URL is required')
            return PostgresDatabase(dsn)
        return super().__new__(cls)

    def iterate(self,con,query,params=()):return con.execute(query,params)

    def revision(self,con):return con.execute('select coalesce(max(seq),0) from audit').fetchone()[0]

    def import_exists(self,con,signature):return bool(con.execute('select 1 from imports where hash=?',(signature,)).fetchone())
    def import_metadata(self,con,signature):return con.execute('select raw_json from imports where hash=?',(signature,)).fetchone()[0]
    def __init__(self, path):
        self.path = Path(path)
        if os.getenv('KSU_SCHEMA_MODE')=='verified' or os.getenv('KSU_OFFLINE_MODE') is not None:
            from .release_schema import inspect_sqlite
            if inspect_sqlite(self.path)['version']!=4:raise ValueError('Explicit offline schema migration required before release startup')
            return
        if self.path.is_file():
            from contextlib import closing
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)) as probe:
                users=probe.execute("select sql from sqlite_master where name='users'").fetchone()
                if users and "'viewer'" not in users[0]:raise ValueError('Explicit private-copy user-role migration required')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as con:
            con.executescript(SCHEMA)
            if not con.execute("select 1 from sqlite_master where type='table' and name='decision_documents'").fetchone():
                from .decision_version_schema import schema
                con.executescript(schema())
            if not con.execute("select 1 from sqlite_master where type='table' and name='operation_receipts'").fetchone():
                from .offline_schema import schema as offline_schema
                con.executescript(offline_schema())
            from .viewer_schema import sqlite_guards
            from .pg_admin import TABLES
            con.executescript(sqlite_guards(TABLES))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, actor=None):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('pragma foreign_keys=on')
        maximum=int(os.getenv('KSU_DB_MAX_BYTES','0'))
        if maximum:
            page_size=con.execute('pragma page_size').fetchone()[0]
            allowed=maximum//page_size
            if allowed<1:
                con.close()
                raise ValueError('SQLite size limit is too small')
            actual=con.execute(f'pragma max_page_count={allowed}').fetchone()[0]
            if actual>allowed:
                con.close()
                raise ValueError('Existing database exceeds the configured size limit')
        con.create_function('actor_id', 0, lambda: actor['id'] if actor else '')
        def active_role():
            if not actor:return ''
            row=con.execute('select role from users where id=? and active=1',(actor['id'],)).fetchone()
            return row[0] if row else ''
        con.create_function('actor_role',0,active_role)
        if actor and active_role()=='viewer':
            denied={getattr(sqlite3,key) for key in dir(sqlite3) if key.startswith(('SQLITE_CREATE_','SQLITE_DROP_'))}
            denied.update({sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_PRAGMA})
            con.set_authorizer(lambda op,a,b,c,d:sqlite3.SQLITE_DENY if op in denied else sqlite3.SQLITE_OK)
        con.create_function('sha256',1,lambda value:hashlib.sha256(value.encode()).hexdigest())
        con.create_function('accessible',1,lambda site:bool(actor and (actor['role']=='manager' or con.execute(
            'select 1 from user_sites where user_id=? and site_id=?',(actor['id'],site)).fetchone())))
        try:
            with con:
                yield con
        finally:
            con.close()

    def user(self, username):
        with self.connect() as con:
            row = con.execute('select * from users where username=? and active=1', (username,)).fetchone()
            return dict(row) if row else None

    def snapshot(self, con, actor):
        if actor['role']=='viewer':
            from .viewer import snapshot
            return snapshot(self,con,actor)
        state = {key: [] for key in ['sites','questions','inspections','findings','inventories','inventoryLines','visits']}
        rows = con.execute('select * from records order by created_at desc, id').fetchall()
        allowed = {r[0] for r in con.execute('select site_id from user_sites where user_id=?', (actor['id'],))}
        for r in rows:
            if actor['role'] != 'manager' and r['collection'] != 'questions' and (r['id'] if r['collection']=='sites' else r['site_id']) not in allowed:
                continue
            state[r['collection']].append({**json.loads(r['data']), 'id':r['id'], 'version':r['version'],
                                          'createdBy':r['author_id'], 'createdAt':r['created_at'], 'updatedAt':r['updated_at']})
        # Preserve the original prototype's numbered catalogue/checklist sequence.
        for collection,prefix in (('sites','s'),('questions','q')):
            state[collection].sort(key=lambda item: (0,int(item['id'][1:]))
                                   if item['id'].startswith(prefix) and item['id'][1:].isdigit()
                                   else (1,item['createdAt']))
        state['meta'] = {'mode':'work' if os.getenv('KSU_OFFLINE_MODE') is not None else 'demo', 'name':'ИНСТРАКТ | Складской учет'}
        state['assignees'] = [
            {'id':u['id'],'name':u['name'], 'siteIds':
             [s['id'] for s in state['sites']] if u['role']=='manager' else
             [r[0] for r in con.execute('select site_id from user_sites where user_id=?',(u['id'],))]}
            for u in con.execute("select id,name,role from users where active=1 and role in ('manager','inspector') order by name")
        ] if actor['role']=='manager' else []
        state['activities'] = []
        state['version'] = 2
        state['revision'] = self.revision(con)
        return state


def seed_demo(db, manager_password, inspector_password):
    """Explicit CLI only; never seed on server startup or overwrite an existing user."""
    actor = {'id':'manager', 'role':'manager'}
    seed = json.loads((ROOT/'server/seed.json').read_text())
    with db.connect(actor) as con:
        if con.execute('select count(*) from users').fetchone()[0]:
            raise ValueError('База уже содержит пользователей; инициализация отменена')
        for ident, name, role, password in [('manager','ДЕМО · Андрей','manager',manager_password),
                                             ('inspector','ДЕМО · Эльман','inspector',inspector_password)]:
            if len(password) < 12:
                raise ValueError('Пароли должны содержать не менее 12 символов')
            con.execute('insert into users(id,username,name,role,password) values(?,?,?,?,?)',
                        (ident,ident,name,role,password_hash(password)))
        for collection in ('sites','questions'):
            for item in seed[collection]:
                data = dict(item); ident = data.pop('id'); stamp = now()
                con.execute('insert into records values(?,?,?,?,?,?,?,?)',
                            (collection,ident,json.dumps(data,ensure_ascii=False),'manager',None,1,stamp,stamp))
                if collection == 'sites':
                    con.execute('insert into user_sites values(?,?)', ('inspector', ident))
