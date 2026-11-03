"""Schema v2: immutable source facts, append-only document versions and exclusions."""
TABLES=('decision_documents','decision_versions','decision_entries','decision_retirements')


def schema(postgres=False):
    json_ok="jsonb_typeof(summary_json::jsonb)='object'" if postgres else 'json_valid(summary_json)'
    tables=f'''
CREATE TABLE decision_documents(id text PRIMARY KEY,site_id text NOT NULL,label text NOT NULL,
 author_id text NOT NULL REFERENCES users(id),created_at text NOT NULL,import_hash text NOT NULL REFERENCES imports(hash));
CREATE INDEX document_site ON decision_documents(site_id,created_at,id);
CREATE TABLE decision_versions(id text PRIMARY KEY,document_id text NOT NULL REFERENCES decision_documents(id),
 number integer NOT NULL CHECK(number>0),parent_id text REFERENCES decision_versions(id) DEFERRABLE INITIALLY DEFERRED,
 site_id text NOT NULL,author_id text NOT NULL REFERENCES users(id),created_at text NOT NULL,
 mode text NOT NULL CHECK(mode IN ('initial','full','partial')),reason text NOT NULL,
 import_hash text NOT NULL REFERENCES imports(hash),summary_json text NOT NULL CHECK({json_ok}),
 operation_key text NOT NULL UNIQUE,UNIQUE(document_id,number));
CREATE TABLE decision_entries(record_id text PRIMARY KEY REFERENCES decision_records(id),
 document_id text NOT NULL REFERENCES decision_documents(id),version_id text NOT NULL REFERENCES decision_versions(id),
 site_id text NOT NULL,replaces_id text UNIQUE REFERENCES decision_records(id));
CREATE INDEX entry_document ON decision_entries(document_id,record_id);
CREATE TABLE decision_retirements(record_id text PRIMARY KEY REFERENCES decision_records(id),
 version_id text NOT NULL REFERENCES decision_versions(id),site_id text NOT NULL);
'''
    # Length-prefixed labels: identical deterministic grouping in both engines, no SHA/row identity inference.
    expression="d.import_hash||'/'||length(d.site_id)||':'||d.site_id||length(json_extract(d.data,'$.document'))||':'||json_extract(d.data,'$.document')"
    hashed=f"encode(sha256(convert_to({expression},'UTF8')),'hex')" if postgres else f'sha256({expression})'
    tables+=f'''
INSERT INTO decision_documents SELECT DISTINCT {hashed},d.site_id,json_extract(d.data,'$.document'),d.author_id,i.created_at,d.import_hash
 FROM decision_records d JOIN imports i ON i.hash=d.import_hash;
INSERT INTO decision_versions SELECT id||'-1',id,1,NULL,site_id,author_id,created_at,'initial',
 'Ранее импортированные данные',import_hash,'{{}}',id||'-1' FROM decision_documents;
INSERT INTO decision_entries SELECT d.id,{hashed},{hashed}||'-1',d.site_id,NULL FROM decision_records d;
'''
    rules={
      'decision_documents':"coalesce(actor_role(),'') NOT IN ('manager','inspector') OR NEW.author_id<>actor_id() OR NOT EXISTS(SELECT 1 FROM imports i WHERE i.hash=NEW.import_hash AND i.actor_id=actor_id()) OR NOT accessible(NEW.site_id)",
      'decision_versions':"""coalesce(actor_role(),'') NOT IN ('manager','inspector') OR NEW.author_id<>actor_id() OR NOT accessible(NEW.site_id)
 OR NOT EXISTS(SELECT 1 FROM decision_documents d WHERE d.id=NEW.document_id AND d.site_id=NEW.site_id)
 OR NEW.number<>(SELECT coalesce(max(number),0)+1 FROM decision_versions WHERE document_id=NEW.document_id)
 OR (NEW.number=1 AND (NEW.mode<>'initial' OR NEW.parent_id IS NOT NULL OR NOT EXISTS(
 SELECT 1 FROM decision_documents d WHERE d.id=NEW.document_id AND d.author_id=actor_id() AND d.import_hash=NEW.import_hash)))
 OR (NEW.number>1 AND (actor_role()<>'manager' OR NEW.mode='initial' OR length(trim(NEW.reason))=0 OR NOT EXISTS(
 SELECT 1 FROM decision_versions v WHERE v.id=NEW.parent_id AND v.document_id=NEW.document_id AND v.number=NEW.number-1)))""",
      'decision_entries':"""coalesce(actor_role(),'') NOT IN ('manager','inspector') OR NOT accessible(NEW.site_id)
 OR NOT EXISTS(SELECT 1 FROM decision_records r JOIN decision_versions v ON v.id=NEW.version_id
 WHERE r.id=NEW.record_id AND r.site_id=NEW.site_id AND r.author_id=actor_id() AND r.import_hash=v.import_hash
 AND v.document_id=NEW.document_id AND v.site_id=NEW.site_id AND v.author_id=actor_id()
 AND v.number=(SELECT max(number) FROM decision_versions WHERE document_id=NEW.document_id)
 AND (v.mode='initial' OR actor_role()='manager'))
 OR (NEW.replaces_id IS NOT NULL AND (actor_role()<>'manager' OR NOT EXISTS(
 SELECT 1 FROM decision_entries e JOIN decision_retirements x ON x.record_id=e.record_id
 WHERE e.record_id=NEW.replaces_id AND e.document_id=NEW.document_id AND x.version_id=NEW.version_id)))""",
      'decision_retirements':"""coalesce(actor_role(),'')<>'manager' OR NOT accessible(NEW.site_id) OR NOT EXISTS(
 SELECT 1 FROM decision_entries e JOIN decision_versions v ON v.id=NEW.version_id
 WHERE e.record_id=NEW.record_id AND e.site_id=NEW.site_id AND e.document_id=v.document_id AND v.site_id=NEW.site_id
 AND v.author_id=actor_id() AND v.mode IN ('full','partial') AND v.number>1
 AND v.number=(SELECT max(number) FROM decision_versions WHERE document_id=v.document_id))"""
    }
    if postgres:
        tables+='SET CONSTRAINTS ALL IMMEDIATE;\n'
        for table,condition in rules.items():
            tables+=f'''CREATE FUNCTION {table}_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=ksu,pg_catalog AS $$ BEGIN
 IF {condition} THEN RAISE EXCEPTION 'forbidden version operation' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER version_insert BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {table}_guard();
CREATE TRIGGER version_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_change();
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY version_read ON {table} FOR SELECT USING(accessible(site_id));
CREATE POLICY version_insert ON {table} FOR INSERT WITH CHECK(accessible(site_id));
REVOKE ALL ON FUNCTION {table}_guard() FROM PUBLIC;
'''
        tables+='''CREATE FUNCTION version_audit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$ BEGIN
 IF NEW.number>1 THEN INSERT INTO audit(at,actor_id,collection,record_id,operation,before_json,after_json)
 VALUES(NEW.created_at,actor_id(),'decisionVersions',NEW.document_id,'version',
 json_build_object('version',NEW.number-1,'id',NEW.parent_id)::text,NEW.summary_json); END IF; RETURN NEW; END $$;
CREATE TRIGGER version_audit AFTER INSERT ON decision_versions FOR EACH ROW EXECUTE FUNCTION version_audit();
REVOKE ALL ON FUNCTION version_audit() FROM PUBLIC;
ALTER POLICY audit_read ON audit USING(actor_role()='manager' OR EXISTS(SELECT 1 FROM records r WHERE r.collection=audit.collection AND r.id=audit.record_id AND accessible(r.site_id) AND (r.author_id=actor_id() OR r.collection='visits' AND r.data::jsonb->>'assignedTo'=actor_id())) OR EXISTS(SELECT 1 FROM decision_records d WHERE audit.collection='decisions' AND d.id=audit.record_id AND d.author_id=actor_id() AND accessible(d.site_id)) OR EXISTS(SELECT 1 FROM decision_documents d WHERE audit.collection='decisionVersions' AND d.id=audit.record_id AND accessible(d.site_id)));
CREATE OR REPLACE FUNCTION state_revision() RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=ksu,pg_catalog AS $$
 SELECT coalesce(max(a.seq),0) FROM audit a WHERE actor_role()='manager' OR
 (actor_role()='inspector' AND (EXISTS(SELECT 1 FROM records r WHERE r.collection=a.collection AND r.id=a.record_id AND
 (r.collection='questions' OR accessible(CASE WHEN r.collection='sites' THEN r.id ELSE r.site_id END))) OR
 EXISTS(SELECT 1 FROM decision_records d WHERE a.collection='decisions' AND d.id=a.record_id AND accessible(d.site_id)) OR
 EXISTS(SELECT 1 FROM decision_documents d WHERE a.collection='decisionVersions' AND d.id=a.record_id AND accessible(d.site_id)))) $$;
UPDATE schema_version SET version=2;
'''
        return tables
    # SQLite's API-side site checks are backed by insert guards and append-only triggers.
    # accessible() is registered on its actor-bound connection as in PostgreSQL.
    for table,condition in rules.items():
        tables+=f'''CREATE TRIGGER {table}_guard BEFORE INSERT ON {table} BEGIN
 SELECT CASE WHEN {condition} THEN raise(abort,'forbidden version operation') END; END;
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT raise(abort,'append only'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT raise(abort,'append only'); END;
'''
    tables+='''CREATE TRIGGER version_audit AFTER INSERT ON decision_versions WHEN NEW.number>1 BEGIN
 INSERT INTO audit(at,actor_id,collection,record_id,operation,before_json,after_json)
 VALUES(NEW.created_at,actor_id(),'decisionVersions',NEW.document_id,'version',json_object('version',NEW.number-1,'id',NEW.parent_id),NEW.summary_json); END;
'''
    return 'BEGIN IMMEDIATE;\n'+tables+'COMMIT;'
