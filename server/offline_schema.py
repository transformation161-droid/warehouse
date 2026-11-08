"""Additive stage-1 schema. PostgreSQL migration is explicit, never run by runtime."""
TABLES=('checklist_versions','operation_receipts','media_keys','offline_packages')
KEYS={'checklist_versions':'id','operation_receipts':'user_id,id','media_keys':'user_id,site_id,content_hash','offline_packages':'id'}


def schema(postgres=False):
    ddl='''CREATE TABLE checklist_versions(id text primary key, data text not null, created_at text not null);
CREATE TABLE operation_receipts(user_id text not null references users(id), id text not null,
 kind text not null check(kind in ('changes','media')), body_hash text not null,
 status integer not null check(status in (200,409)), result text not null, resources text not null,
 created_at text not null, primary key(user_id,id));
CREATE TABLE media_keys(user_id text not null references users(id), site_id text not null,
 content_hash text not null, media_id text not null references media(id), primary key(user_id,site_id,content_hash));
CREATE TABLE offline_packages(id text primary key, user_id text not null references users(id),
 expires_at double precision not null, data text not null, created_at text not null);
CREATE INDEX offline_packages_owner ON offline_packages(user_id,expires_at);
'''
    checksum="id=encode(sha256(convert_to(data,'UTF8')),'hex')" if postgres else "length(id)=64 and json_valid(data) and json_type(data)='array'"
    ddl=ddl.replace('id text primary key, data text not null, created_at text not null',
                    f'id text primary key check({checksum}), data text not null, created_at text not null')
    rules={
      'checklist_versions':"actor_role() in ('manager','inspector')",
      'operation_receipts':"actor_role() in ('manager','inspector') and user_id=actor_id()",
      'media_keys':"actor_role() in ('manager','inspector') and user_id=actor_id() and accessible(site_id) and exists(select 1 from media m where m.id=media_id and m.author_id=actor_id() and m.site_id=media_keys.site_id)",
      'offline_packages':"actor_role() in ('manager','inspector') and user_id=actor_id()"}
    if postgres:
        for table,rule in rules.items():
            read="actor_role() in ('manager','inspector')" if table=='checklist_versions' else 'user_id=actor_id()'
            if table=='media_keys':read+=' and accessible(site_id)'
            if table=='offline_packages':read+=" and not exists(select 1 from jsonb_array_elements(data::jsonb->'sites') s where not accessible(s->>'id'))"
            if table=='operation_receipts':read+=" and not exists(select 1 from jsonb_array_elements(resources::jsonb) r where coalesce(r->>'siteId','')!='' and not accessible(r->>'siteId'))"
            ddl+=f'''ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY {table}_read ON {table} FOR SELECT USING({read});
CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK({rule});
CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_change();
'''
        return ddl+'UPDATE schema_version SET version=3;'
    for table,rule in rules.items():
        # SQLite trigger row references need NEW qualification.
        rule=rule.replace('user_id=actor_id()', 'NEW.user_id=actor_id()').replace('accessible(site_id)', 'accessible(NEW.site_id)')
        rule=rule.replace('m.id=media_id','m.id=NEW.media_id').replace('m.site_id=media_keys.site_id','m.site_id=NEW.site_id')
        ddl+=f'''CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} BEGIN
 SELECT CASE WHEN NOT ({rule}) THEN raise(abort,'forbidden offline operation') END; END;
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT raise(abort,'append only'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT raise(abort,'append only'); END;
'''
    return 'BEGIN IMMEDIATE;\n'+ddl+'COMMIT;'
