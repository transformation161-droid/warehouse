"""v4. Explicit administrator/private-copy migration; never production startup DDL."""
import sqlite3
from contextlib import closing

VERSION=4
USER_SQL="""create table users(id text primary key, username text not null unique, name text not null,
 role text not null check(role in ('manager','inspector','viewer')), password text not null, active integer not null default 1)"""


def upgrade_users_copy(path):
    # Called only on a disposable private byte copy by the migration helpers.
    with closing(sqlite3.connect(path)) as con:
        row=con.execute("select sql from sqlite_master where name='users'").fetchone()
        if not row or "'viewer'" in row[0]:return
        if [r[1] for r in con.execute('pragma table_info(users)')]!=['id','username','name','role','password','active']:
            raise ValueError('Unknown user columns; manual migration required')
        triggers=con.execute("select name,sql from sqlite_master where type='trigger'").fetchall()
        con.execute('pragma foreign_keys=off');con.execute('begin immediate')
        try:
            for name,_ in triggers:
                if not name.replace('_','').isalnum():raise ValueError('Unsupported trigger name')
                con.execute('drop trigger "'+name+'"')
            con.execute(USER_SQL.replace('create table users(','create table users_v4('))
            con.execute('insert into users_v4 select * from users');con.execute('drop table users')
            con.execute('alter table users_v4 rename to users')
            for _,ddl in triggers:con.execute(ddl)
            if con.execute('pragma foreign_key_check').fetchone():raise ValueError('User migration FK check failed')
            con.commit()
        except BaseException:con.rollback();raise


def sqlite_guards(tables):
    return '\n'.join(f"CREATE TRIGGER IF NOT EXISTS viewer_{table}_{op.lower()} BEFORE {op} ON {table} WHEN actor_role()='viewer' BEGIN SELECT raise(abort,'forbidden viewer operation'); END;"
                     for table in tables for op in ('INSERT','UPDATE','DELETE'))


def postgres():
    from .viewer import FIELDS
    # RESTRICTIVE policies close every permissive legacy policy, including source imports/media.
    tables=('records','media','audit','imports','mutations','decision_records','decision_documents','decision_versions',
            'decision_entries','decision_retirements','checklist_versions','operation_receipts','media_keys','offline_packages','users')
    ddl="""ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('manager','inspector','viewer'));
"""
    for table in tables:
        if table=='users':ddl+='ALTER TABLE users ENABLE ROW LEVEL SECURITY; DROP POLICY IF EXISTS users_auth ON users; CREATE POLICY users_auth ON users FOR SELECT USING(true);\n'
        ddl+=f"DROP POLICY IF EXISTS viewer_restriction ON {table}; CREATE POLICY viewer_restriction ON {table} AS RESTRICTIVE FOR ALL USING(coalesce(actor_role(),'')<>'viewer') WITH CHECK(coalesce(actor_role(),'')<>'viewer');\n"
    for table in ('sessions','attempts'):
        ddl+=f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; DROP POLICY IF EXISTS auth_only ON {table}; CREATE POLICY auth_only ON {table} FOR ALL USING(actor_id()='') WITH CHECK(actor_id()='');\n"
    cases=[]
    for collection,fields in FIELDS.items():
        pairs=','.join("'"+f+"',case when jsonb_typeof(r.data::jsonb->'"+f+"')='string' then r.data::jsonb->'"+f+"' else null end" for f in fields)
        cases.append("WHEN '"+collection+"' THEN jsonb_strip_nulls(jsonb_build_object("+pairs+"))")
    ddl+="""CREATE OR REPLACE FUNCTION viewer_records() RETURNS TABLE(collection text,id text,data text,version integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$
 SELECT r.collection,r.id,(CASE r.collection """+' '.join(cases)+""" ELSE '{}'::jsonb END)::text,r.version
 FROM records r WHERE actor_role()='viewer' AND r.collection IN ('sites','visits','inspections','findings')
 AND EXISTS(SELECT 1 FROM user_sites u WHERE u.user_id=actor_id() AND u.site_id=CASE WHEN r.collection='sites' THEN r.id ELSE r.site_id END)
 ORDER BY r.collection,r.id $$;
REVOKE ALL ON FUNCTION viewer_records() FROM PUBLIC;
UPDATE schema_version SET version=4;
"""
    return ddl
