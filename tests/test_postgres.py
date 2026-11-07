"""Real PostgreSQL tests, opt-in with administrator and restricted runtime DSNs."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from fastapi.testclient import TestClient
from PIL import Image
from server.app import create_app
from server.database import Database,seed_demo
from server.pg_admin import initialize,migrate_sqlite
from tests import test_api,test_decision_import,test_calendar


def database_dsn(dsn,name):return make_conninfo(**{**conninfo_to_dict(dsn),'dbname':name})


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN') and os.getenv('KSU_TEST_PG_RUNTIME_DSN'),'Real local PostgreSQL required')
class PostgreSQL(unittest.TestCase):
    client=test_api.API.client
    state=test_api.API.state
    change=test_api.API.change
    inv=test_api.API.inv
    photo=test_api.API.photo
    import_payload=test_api.API.import_payload
    book=test_decision_import.DecisionImport.book
    row=test_decision_import.DecisionImport.row
    upload=test_decision_import.DecisionImport.upload
    wait=test_decision_import.DecisionImport.wait
    preview=test_decision_import.DecisionImport.preview
    commit=test_decision_import.DecisionImport.commit

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ksu-pg-tests-')
        self.name='ksu_test_'+uuid4().hex
        self.root_admin=os.environ['KSU_TEST_PG_ADMIN_DSN'];self.runtime_template=os.environ['KSU_TEST_PG_RUNTIME_DSN']
        self.admin=database_dsn(self.root_admin,self.name);self.runtime=database_dsn(self.runtime_template,self.name)
        self.role=conninfo_to_dict(self.runtime)['user']
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.name)))
        initialize(self.admin,self.role)
        source=Path(self.temp.name)/'source.sqlite3';seed_demo(Database(source),test_api.MANAGER_PASSWORD,test_api.INSPECTOR_PASSWORD)
        self.migration=migrate_sqlite(source,self.admin)
        self.old_env={key:os.environ.get(key) for key in ('KSU_DATABASE_URL','KSU_WORK_DIR')}
        os.environ['KSU_DATABASE_URL']=self.runtime;os.environ['KSU_WORK_DIR']=str(Path(self.temp.name)/'work')
        self.app=create_app('postgresql',test_api.ORIGIN,False)
        self.manager=self.client('manager',test_api.MANAGER_PASSWORD);self.inspector=self.client('inspector',test_api.INSPECTOR_PASSWORD)

    def tearDown(self):
        self.app.state.import_jobs.close();self.manager.close();self.inspector.close()
        for key,value in self.old_env.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.name)))
        self.temp.cleanup()

    def test_legacy_sqlite_is_upgraded_only_on_private_copy(self):
        import hashlib,sqlite3
        source=Path(self.temp.name)/'source.sqlite3'
        with sqlite3.connect(source) as con:con.execute('drop table decision_records')
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        extra=self.name+'_legacy';dsn=database_dsn(self.root_admin,extra)
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(extra)))
        try:
            initialize(dsn,self.role);result=migrate_sqlite(source,dsn)
            self.assertEqual(result['tables']['decision_records']['count'],0)
            self.assertEqual(result['tables']['records']['count'],34)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
            with sqlite3.connect(source) as con:self.assertFalse(con.execute("select 1 from sqlite_master where name='decision_records'").fetchone())
        finally:
            with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(extra)))

    def test_unknown_source_table_is_not_silently_omitted(self):
        import sqlite3
        source=Path(self.temp.name)/'source.sqlite3'
        with sqlite3.connect(source) as con:con.execute('create table synthetic_future(value text)')
        with self.assertRaisesRegex(ValueError,'Unsupported source tables'):migrate_sqlite(source,self.admin)

    def test_migration_refuses_nonempty_destination_without_changing_source(self):
        import hashlib
        from server.pg_admin import fingerprints
        source=Path(self.temp.name)/'source.sqlite3'
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        with psycopg.connect(self.admin) as con:expected=fingerprints(con,True)
        for standalone in (False,True):
            with self.subTest(standalone=standalone),self.assertRaises(ValueError):migrate_sqlite(source,self.admin,standalone=standalone)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        with psycopg.connect(self.admin) as con:self.assertEqual(fingerprints(con,True),expected)

    def test_capacity_failure_is_atomic_and_owner_role_is_rejected(self):
        from unittest.mock import patch
        before=self.state()['revision']
        with patch.dict(os.environ,{'KSU_PG_DATABASE_MAX_BYTES':'1'}):
            response=self.change(self.inspector,'visits',{'siteId':'s1','date':'2026-11-04'},'DEMO-cap')
            self.assertEqual(response.status_code,507)
        self.assertEqual(self.state()['revision'],before)
        self.assertEqual(self.state()['visits'],[])
        batch={'mutationId':'DEMO-idempotent-at-cap','changes':[{'collection':'visits','id':'DEMO-applied','expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-04'}}]}
        self.assertEqual(self.inspector.post('/api/changes',json=batch).status_code,200)
        with patch.dict(os.environ,{'KSU_PG_DATABASE_MAX_BYTES':'1'}):
            self.assertEqual(self.inspector.post('/api/changes',json=batch).status_code,200)
        self.assertEqual(len(self.state()['visits']),1)
        with self.assertRaises(ValueError):Database(self.admin)
        with self.app.state.db.connect() as con:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):con.execute('set role postgres')

    def test_cross_role_duplicate_and_private_source_metadata(self):
        source=self.book();ident=self.upload(source,self.manager)
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/preview',json={}).status_code,200)
        self.assertEqual(self.wait(ident,self.manager)['status'],'ready')
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/commit').status_code,200)
        self.assertEqual(self.wait(ident,self.manager)['status'],'done')
        ident=self.upload(source);self.preview(ident);self.assertTrue(self.commit(ident)['duplicate'])
        row=self.inspector.get('/api/decisions').json()['rows'][0]
        self.assertEqual(self.inspector.get('/api/decisions/'+row['id']).status_code,200)
        with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:
            self.assertEqual(con.execute('select count(*) from imports').fetchone()[0],0)
        self.assertEqual(self.inspector.get('/api/history?collection=decisions&record_id='+row['id']).json(),[])
        # Shared rows sort chronologically without granting access to another actor's raw file.
        second=self.book([self.row(sourceId='DEMO-2')]);ident=self.upload(second,self.manager)
        self.manager.post('/api/decision-imports/'+ident+'/preview',json={});self.wait(ident,self.manager)
        self.manager.post('/api/decision-imports/'+ident+'/commit');self.assertEqual(self.wait(ident,self.manager)['status'],'done')
        third=self.book([self.row(sourceId='DEMO-3')]);ident=self.upload(third);self.preview(ident);self.assertEqual(self.commit(ident)['status'],'done')
        self.assertEqual([r['sourceId'] for r in self.inspector.get('/api/decisions').json()['rows']],['DEMO-3','DEMO-2','DEMO-1'])

    def test_poll_revision_and_private_history_are_independent(self):
        before=self.state(self.inspector)['revision']
        self.assertEqual(self.change(self.manager,'visits',{'siteId':'s1','date':'2026-11-04','assignedTo':'manager'},'DEMO-shared').status_code,200)
        state=self.state(self.inspector)
        self.assertGreater(state['revision'],before)
        self.assertTrue(any(v['id']=='DEMO-shared' for v in state['visits']))
        self.assertEqual(self.inspector.get('/api/history?collection=visits&record_id=DEMO-shared').json(),[])

    def test_https_headers_and_cookie(self):
        with TestClient(create_app('postgresql','https://testserver',True),base_url='https://testserver',headers={'Origin':'https://testserver'}) as client:
            response=client.post('/api/login',json={'username':'manager','password':test_api.MANAGER_PASSWORD})
            self.assertEqual(response.status_code,200)
            self.assertIn('Secure',response.headers['set-cookie'])
            self.assertIn('HttpOnly',response.headers['set-cookie'])
            self.assertIn('max-age=',response.headers['strict-transport-security'])

    def test_sql_rls_roles_media_audit_and_immutable_source(self):
        self.assertEqual(self.change(self.manager,'visits',{'siteId':'s1','date':'2026-12-31','assignedTo':'manager'},'DEMO-foreign').status_code,200)
        foreign=self.state()['visits'][0]
        self.assertEqual(self.change(self.inspector,'visits',{**foreign,'date':'2027-01-01'},'DEMO-foreign',1).status_code,403)
        with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                con.execute("update users set role='manager' where id='inspector'")
        self.inv();url=self.photo()
        with psycopg.connect(self.admin) as con:con.execute("delete from ksu.user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get(url).status_code,404)
        with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:
            self.assertFalse(con.execute("select 1 from records where site_id='s1'").fetchall())
            self.assertFalse(con.execute("select 1 from media where site_id='s1'").fetchall())
            self.assertFalse(con.execute("select 1 from audit where collection='visits'").fetchall())
        with self.app.state.db.connect(self.app.state.db.user('manager')) as con:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):con.execute("update audit set operation='fake'")
        with self.app.state.db.connect() as con:self.assertEqual(con.execute('select count(*) from records').fetchone()[0],0)
        self.assertTrue(self.migration['sessions_revoked'])

    def test_streamed_decisions_exact_amounts_dedup_and_scoped_history(self):
        source=self.book();ident=self.upload(source);self.assertEqual(self.preview(ident)['status'],'ready')
        self.assertEqual(self.commit(ident)['status'],'done')
        rows=self.manager.get('/api/decisions').json()['rows'];self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['cost'],rows[0]['reimbursable']),('100.50','25.50'))
        history=self.manager.get('/api/history?collection=decisions&record_id='+rows[0]['id'])
        self.assertEqual(history.status_code,200);self.assertEqual(history.json()[0]['actor_id'],'inspector')
        self.assertEqual(self.inspector.get('/api/decisions/'+rows[0]['id']).status_code,200)
        repeat=self.upload(source);self.preview(repeat);self.assertTrue(self.commit(repeat)['duplicate'])
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],1)
        self.assertIn('100.50',self.manager.get('/api/decisions/export.csv').text)
        with self.app.state.db.connect(self.app.state.db.user('manager')) as con:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):con.execute("delete from decision_records")
        with psycopg.connect(self.admin) as con:con.execute("delete from ksu.user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get('/api/decisions').json()['total'],0)
        self.assertEqual(self.inspector.get('/api/decisions/'+rows[0]['id']).status_code,404)

    def test_import_failure_is_atomic_and_cross_role_duplicate(self):
        ident=self.upload(self.book([self.row(sourceId='1'),self.row(sourceId='2')]))
        self.preview(ident)
        with psycopg.connect(self.admin) as con:
            con.execute("create function ksu.synthetic_failure() returns trigger language plpgsql as $$ begin if NEW.source_row=3 then raise exception 'synthetic failure' using errcode='23514'; end if; return NEW; end $$")
            con.execute('create trigger synthetic_failure before insert on ksu.decision_records for each row execute function ksu.synthetic_failure()')
        self.assertEqual(self.commit(ident)['status'],'error')
        with psycopg.connect(self.admin) as con:
            self.assertEqual(con.execute('select count(*) from ksu.decision_records').fetchone()[0],0)
            self.assertEqual(con.execute('select count(*) from ksu.imports').fetchone()[0],0)
            self.assertEqual(con.execute("select count(*) from ksu.audit where collection='decisions'").fetchone()[0],0)

# Run the existing HTTP contracts on the real PG adapter, not an in-memory imitation.
for name in (
 'test_atomic_rollback_and_versions','test_closure_evidence_and_audit','test_cookie_csrf_origin_and_logout',
 'test_foreign_records_cannot_be_edited','test_idempotency_and_concurrent_writes',
 'test_import_preview_atomic_errors_and_deduplication','test_login_rate_limit_and_unknown_user','test_media_validation',
 'test_negative_nan_and_bad_dates_rejected','test_original_catalogue_and_checklist_order',
 'test_passwords_are_hashed_and_role_is_not_client_controlled','test_repeated_finding_flag_does_not_change_retroactively',
 'test_reports_filters_formats_and_no_formula_injection','test_stages_and_financials',
 'test_unauthenticated_and_static_secrets','test_visit_and_inspection_atomic_flow','test_xlsx_mapping_and_formula_rejection'):
    setattr(PostgreSQL,name,getattr(test_api.API,name))
for name in ('test_date_only_leap_boundary_multiple_visits_and_audited_move','test_invalid_dates_rollback_and_overdue_is_not_a_server_status'):
    setattr(PostgreSQL,name,getattr(test_calendar.Calendar,name))
