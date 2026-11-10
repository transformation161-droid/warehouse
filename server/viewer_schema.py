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
    return ddl+read_functions()+"UPDATE schema_version SET version=4;\n"


def read_functions():
    """Explicit admin refresh on a NEW PostgreSQL target, including existing v4 copies.

    No table/data version change: this adds read capabilities only; runtime verifies
    the capability marker and never installs/repairs these functions itself.
    """
    from .viewer import FIELDS,RESULT_FIELDS,media_links_sql
    def object_sql(fields,value):
        pairs=[]
        for key,kind in fields.items():
            expression=f"{value}->'{key}'"
            types={str:"'string'",float:"'number'",bool:"'boolean'"}
            if kind is list:
                result=object_sql(RESULT_FIELDS,'item')
                selected=f"(select coalesce(jsonb_agg({result}),'[]'::jsonb) from jsonb_array_elements(case when jsonb_typeof({expression})='array' then {expression} else '[]'::jsonb end) item where jsonb_typeof(item)='object')"
            else:
                attachment=" and exists(select 1 from viewer_media_links() v where '/api/media/'||v.id=("+expression+")#>>'{}')" if key in ('photo','evidence') else ''
                selected=f"case when jsonb_typeof({expression})={types[kind]}{attachment} then {expression} else null end"
            pairs.extend(("'"+key+"'",selected))
        return "jsonb_strip_nulls(jsonb_build_object("+','.join(pairs)+"))"
    cases=["WHEN '"+collection+"' THEN "+object_sql(fields,'r.data::jsonb') for collection,fields in FIELDS.items()]
    links="""CREATE OR REPLACE FUNCTION viewer_media_links() RETURNS TABLE(id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$
 """+media_links_sql(True)+""" AND actor_role()='viewer' $$;
REVOKE ALL ON FUNCTION viewer_media_links() FROM PUBLIC;
"""
    return links+"""CREATE OR REPLACE FUNCTION viewer_records() RETURNS TABLE(collection text,id text,data text,version integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$
 SELECT r.collection,r.id,((CASE r.collection """+' '.join(cases)+""" ELSE '{}'::jsonb END)||jsonb_build_object('createdAt',r.created_at))::text,r.version
 FROM records r WHERE actor_role()='viewer' AND
 ((r.collection='questions' AND EXISTS(SELECT 1 FROM user_sites u JOIN records s ON s.collection='sites' AND s.id=u.site_id WHERE u.user_id=actor_id()))
 OR (r.collection<>'questions' AND EXISTS(SELECT 1 FROM user_sites u WHERE u.user_id=actor_id() AND u.site_id=CASE WHEN r.collection='sites' THEN r.id ELSE r.site_id END)))
 ORDER BY r.collection,r.id $$;
REVOKE ALL ON FUNCTION viewer_records() FROM PUBLIC;
CREATE OR REPLACE FUNCTION viewer_media(ident text,thumbnail boolean) RETURNS TABLE(content bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$
 SELECT CASE WHEN $2 THEN m.thumbnail ELSE m.content END FROM media m
 WHERE m.id=$1 AND actor_role()='viewer' AND EXISTS(SELECT 1 FROM viewer_media_links() v WHERE v.id=m.id) $$;
REVOKE ALL ON FUNCTION viewer_media(text,boolean) FROM PUBLIC;
CREATE OR REPLACE FUNCTION viewer_read_version() RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 2 $$;
REVOKE ALL ON FUNCTION viewer_read_version() FROM PUBLIC;
"""
