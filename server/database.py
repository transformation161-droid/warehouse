"""SQLite is private to the server. Connection-bound identity also gates writes in SQL."""
import hashlib
import json
import os
import secrets
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
create trigger if not exists guard_insert before insert on records begin
 select case when actor_role() not in ('manager','inspector') then raise(abort,'forbidden') end;
 select case when new.author_id != actor_id() then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and (new.collection in ('sites','questions','visits') or
 not exists(select 1 from user_sites where user_id=actor_id() and site_id=new.site_id))
 then raise(abort,'forbidden') end;
 select case when new.collection='inventories' and json_extract(new.data,'$.stage')!='primary' then raise(abort,'forbidden') end;
end;
create trigger if not exists guard_update before update on records begin
 select case when actor_role() not in ('manager','inspector') or new.author_id!=old.author_id or
 new.collection!=old.collection or new.id!=old.id or new.created_at!=old.created_at or new.version!=old.version+1
 then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and new.collection='inventories' and
 (json_extract(new.data,'$.stage')!=json_extract(old.data,'$.stage') or json_extract(new.data,'$.decision')!=json_extract(old.data,'$.decision'))
 then raise(abort,'forbidden') end;
 select case when actor_role()='inspector' and (
 new.collection in ('sites','questions','inspections') or new.site_id is not old.site_id or
 not exists(select 1 from user_sites where user_id=actor_id() and site_id=new.site_id) or
 (new.collection!='visits' and old.author_id!=actor_id()) or
 (new.collection='visits' and (json_extract(old.data,'$.assignedTo')!=actor_id() or
 json_extract(old.data,'$.status')!='planned' or json_extract(new.data,'$.status')!='completed' or
 json_remove(new.data,'$.status','$.inspectionId')!=json_remove(old.data,'$.status','$.inspectionId'))))
 then raise(abort,'forbidden') end;
end;
create trigger if not exists no_record_delete before delete on records begin select raise(abort,'deletion disabled'); end;
create trigger if not exists log_insert after insert on records begin
 insert into audit(at,actor_id,collection,record_id,operation,after_json)
 values(new.updated_at,actor_id(),new.collection,new.id,'create',new.data);
end;
create trigger if not exists log_update after update on records begin
 insert into audit(at,actor_id,collection,record_id,operation,before_json,after_json)
 values(new.updated_at,actor_id(),new.collection,new.id,'update',old.data,new.data);
end;
create trigger if not exists audit_no_update before update on audit begin select raise(abort,'append only'); end;
create trigger if not exists audit_no_delete before delete on audit begin select raise(abort,'append only'); end;
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as con:
            con.executescript(SCHEMA)
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
        con.create_function('actor_role', 0, lambda: actor['role'] if actor else '')
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
        state['meta'] = {'mode':'demo', 'name':'ИНСТРАКТ · КСУ Контроль'}
        state['activities'] = []
        state['version'] = 2
        state['revision'] = con.execute('select coalesce(max(seq),0) from audit').fetchone()[0]
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
