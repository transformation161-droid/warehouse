"""Issue31: API read projection, all mutation paths, SQL guards and no business effects."""
from contextlib import closing
import os
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from server.database import password_hash
from tests import test_api,test_postgres as pg
from tests import test_offline_release as release


class Viewer(unittest.TestCase):
    client=test_api.API.client
    change=test_api.API.change
    photo=test_api.API.photo
    def setUp(self):
        release.Release.setUp(self);self.prepare()
    def tearDown(self):
        self.viewer.close();self.empty.close();release.Release.tearDown(self)
    def execute_admin(self,query,params=()):
        if self.app.state.db.backend=='postgresql':
            from server.postgres_backend import Connection
            with psycopg.connect(self.admin) as con:
                con.execute('set local search_path=ksu,pg_catalog');con.execute(Connection.parameters(query),params)
        else:
            with self.app.state.db.connect() as con:con.execute(query,params)
    def facts(self):return release.Release.facts(self)
    def prepare(self):
        for ident in ('viewer','unassigned'):
            self.execute_admin('insert into users values(?,?,?,?,?,1)',(ident,'synthetic-'+ident,'ДЕМО · '+ident,'viewer',password_hash('synthetic viewer password')))
        self.execute_admin('insert into user_sites values(?,?)',('viewer','s1'))
        self.viewer=self.client('synthetic-viewer','synthetic viewer password');self.empty=self.client('synthetic-unassigned','synthetic viewer password')
        self.assertEqual(self.change(self.inspector,'findings',{'siteId':'s1','title':'ДЕМО · Published title','description':'ДЕМО · Operational detail','owner':'PRIVATE-OWNER'}).status_code,200)
    def test_scoped_projection_and_no_account_data_cache(self):
        response=self.viewer.get('/api/state');state=response.json();self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.viewer.get('/api/state?role=manager').json(),state)
        self.assertEqual([r['id'] for r in state['sites']],['s1']);self.assertEqual(state['assignees'],[])
        self.assertNotIn('PRIVATE-',response.text);self.assertNotIn('createdBy',response.text)
        self.assertEqual(state['inventoryLines'],[]);self.assertEqual(len(state['questions']),22)
        self.assertIn('Published title',response.text);self.assertEqual(self.empty.get('/api/state').json()['sites'],[])
        self.assertFalse(self.viewer.get('/api/offline-visit/context').json()['enabled'])
        self.assertEqual(self.viewer.get('/api/me').json()['role'],'viewer')
    def test_every_direct_mutation_download_and_admin_path_is_denied(self):
        before=self.facts()
        writes=['/api/changes','/api/media','/api/offline/packages','/api/offline/changes','/api/admin/backup',
                '/api/import/preview','/api/import/commit','/api/decision-imports','/api/decision-imports/unknown/preview',
                '/api/decision-imports/unknown/commit','/api/decision-imports/unknown/version-confirm','/api/users','/api/unknown']
        for path in writes:
            for method in ('POST','PUT','PATCH','DELETE'):
                with self.subTest(path=path,method=method):self.assertEqual(self.viewer.request(method,path,json={}).status_code,403)
        for path in ('/api/history','/api/checklists/current','/api/receipts/unknown',
                     '/api/decisions','/api/decisions/export.csv','/api/writeoff-analytics'):
            self.assertEqual(self.viewer.get(path).status_code,403,path)
        self.assertEqual(self.viewer.get('/api/media/unknown').status_code,404)
        for extension in ('json','csv','xlsx','pdf'):
            self.assertEqual(self.viewer.get('/api/reports/'+extension).status_code,200)
        self.assertEqual(before,self.facts())
    def test_database_guards_and_role_assignment_are_not_ui_only(self):
        before=self.facts();actor=self.app.state.db.user('synthetic-viewer')
        for query,params in [
          ('insert into media values(?,?,?,?,?,?)',('synthetic-forbidden','s1','viewer',b'x',b'x','2026-11-09')),
          ('insert into imports values(?,?,?,?)',('synthetic-hash','viewer','{}','2026-11-09')),
          ('insert into mutations values(?,?,?)',('viewer','synthetic-mutation','x')),
          ("update records set data=data,version=version+1 where collection='sites' and id='s1'",()),
          ("update users set role='manager' where id='viewer'",())]:
            with self.subTest(query=query):
                try:
                    with self.app.state.db.connect(actor) as con:con.execute(query,params)
                except (sqlite3.DatabaseError,psycopg.Error):pass
        try:
            with self.app.state.db.connect({**actor,'role':'manager'}) as con:
                con.execute('insert into media values(?,?,?,?,?,?)',('forged-role','s1','viewer',b'x',b'x','2026-11-09'))
        except (sqlite3.DatabaseError,psycopg.Error):pass
        self.assertEqual(before,self.facts())
        self.assertEqual(self.change(self.manager,'visits',{'siteId':'s1','date':'2026-11-09','assignedTo':'viewer'}).status_code,422)
    def detailed_fixture(self):
        photo=self.photo(self.inspector,'s1');foreign=self.photo(self.manager,'s2');orphan=self.photo(self.manager,'s1')
        questions=self.inspector.get('/api/state').json()['questions']
        self.assertEqual(self.change(self.inspector,'inspections',{'siteId':'s1','date':'2026-11-10','inspector':'PRIVATE-INSPECTOR',
            'results':[{'questionId':q['id'],'status':'ok','comment':'ДЕМО · Check detail','photo':photo if index==0 else ''} for index,q in enumerate(questions)]},'demo-viewer-act').status_code,200)
        self.assertEqual(self.change(self.inspector,'findings',{'siteId':'s1','title':'ДЕМО · With photo','description':'ДЕМО · Detail text',
            'owner':'PRIVATE-OWNER','photo':photo,'evidence':photo,'resolution':'ДЕМО · Result'},'demo-viewer-photo').status_code,200)
        self.assertEqual(self.change(self.manager,'findings',{'siteId':'s2','title':'PRIVATE-FOREIGN-SITE','photo':foreign},'demo-foreign').status_code,200)
        self.assertEqual(self.change(self.manager,'inventories',{'siteId':'s1','date':'2026-11-10','title':'ДЕМО · Inventory'},'demo-viewer-inv').status_code,200)
        self.assertEqual(self.change(self.manager,'inventoryLines',{'inventoryId':'demo-viewer-inv','item':'ДЕМО · Item','expected':3,'actual':2,'price':12.5,'owner':'PRIVATE-LINE-OWNER'},'demo-viewer-line').status_code,200)
        return photo,foreign,orphan

    def test_shared_details_photos_and_all_report_formats_are_scoped_and_read_only(self):
        photo,foreign,orphan=self.detailed_fixture();before=self.facts()
        state=self.viewer.get('/api/state').json()
        self.assertEqual(state['inspections'][0]['results'][0]['photo'],photo+'?expectedUser=viewer')
        # A photo URL issued for viewer cannot borrow a manager cookie.
        pinned=state['inspections'][0]['results'][0]['photo']
        self.assertEqual(self.viewer.get(pinned).status_code,200)
        self.assertEqual(self.manager.get(pinned).status_code,403)
        self.assertEqual(len(state['inspections'][0]['results']),22)
        self.assertEqual(state['inspections'][0]['inspector'],'')
        self.assertEqual(state['inventoryLines'][0]['price'],12.5)
        self.assertNotIn('PRIVATE-',json.dumps(state))
        self.assertEqual(self.empty.get('/api/state').json()['questions'],[])
        for suffix in ('','?thumbnail=true'):
            self.assertEqual(self.viewer.get(photo+suffix).status_code,200)
            for client,url in ((self.viewer,foreign),(self.viewer,orphan),(self.empty,photo)):
                self.assertEqual(client.get(url+suffix).status_code,404)
        for kind in ('inventory','findings','audits','weekly','inventory-detail'):
            for extension in ('json','csv','xlsx','pdf'):
                params={'kind':kind,'site':'s1','inventoryId':'demo-viewer-inv','group':'owner'}
                reply=self.viewer.get('/api/reports/'+extension,params=params)
                self.assertEqual(reply.status_code,200,(kind,extension,reply.text[:100] if extension=='json' else ''))
                if extension in ('json','csv'):self.assertNotIn('PRIVATE-',reply.text)
        self.assertEqual(self.viewer.get('/api/reports/json?site=s2').status_code,404)
        self.assertEqual(self.empty.get('/api/reports/json?kind=inventory-detail&inventoryId=demo-viewer-inv').status_code,404)
        self.assertEqual(self.viewer.get('/api/reports/json?kind=inventory-detail&inventoryId=foreign').status_code,404)
        self.assertEqual(before,self.facts())
        # Removing access invalidates previously known image and export URLs.
        self.execute_admin('delete from user_sites where user_id=?',('viewer',))
        self.assertEqual(self.viewer.get(photo).status_code,404)
        self.assertEqual(self.viewer.get('/api/reports/xlsx?site=s1').status_code,404)
        self.assertEqual(self.viewer.get('/api/state').json()['sites'],[])

    def test_projection_rejects_nested_personal_fields_and_wrong_scalar_types(self):
        from server.viewer import project
        result=project('inspections',{'siteId':'s1','inspector':'PRIVATE-NAME','results':[
            {'questionId':'q1','status':'ok','comment':{'owner':'PRIVATE-NAME'},'photo':{'token':'PRIVATE'},'author':'PRIVATE','title':'ДЕМО'},'PRIVATE']})
        self.assertEqual(result,{'siteId':'s1','results':[{'questionId':'q1','status':'ok','title':'ДЕМО'}]})
        self.assertEqual(project('findings',{'title':{'name':'PRIVATE'},'owner':'PRIVATE','repeat':{'secret':'PRIVATE'}}),{})

    def test_sqlite_attachment_array_edges_keep_parent_and_media_acl(self):
        if self.app.state.db.backend!='sqlite':self.skipTest('SQLite JSON compatibility contract')
        from server.viewer import media_links_sql
        url='/api/media/demo-allowed'
        cases=[([],set()),([None,1,'text',[],{'photo':url}],{'demo-allowed'}),
               ([None]*199+[{'photo':url}],{'demo-allowed'}),
               ({'photo':url},set()),([{'photo':{'secret':url}}],set()),
               ([{'photo':url+'-suffix'}],set()),([{'photo':'/api/media/demo-foreign'}],set())]
        actor=self.app.state.db.user('synthetic-viewer')
        # Only read CTEs: the real viewer connection/authorizer remains active.
        query="""with records(collection,site_id,data) as (values('inspections','s1',?)),
          user_sites(user_id,site_id) as (values('viewer','s1')),
          media(id,site_id) as (values('demo-allowed','s1'),('demo-orphan','s1'),('demo-foreign','s2')) """+media_links_sql()
        with self.app.state.db.connect(actor) as con:
            for results,expected in cases:
                with self.subTest(results_type=type(results).__name__,length=len(results)):
                    payload=json.dumps({'siteId':'s1','results':results})
                    self.assertEqual({row['id'] for row in con.execute(query,(payload,actor['id']))},expected)
            payload=json.dumps({'siteId':'s2','results':[{'photo':url}]})
            self.assertEqual(con.execute(query,(payload,actor['id'])).fetchall(),[])
            self.assertEqual(con.execute(query,(json.dumps({'siteId':'s1','results':[{'photo':url}]}),'unassigned')).fetchall(),[])

    def test_logout_and_cross_account_sessions_preserve_existing_roles(self):
        original=dict(self.viewer.cookies);self.assertEqual(self.viewer.post('/api/logout').status_code,200)
        self.assertEqual(self.viewer.get('/api/me').status_code,401)
        with TestClient(self.app,headers={'Origin':test_api.ORIGIN}) as client:
            client.cookies.update(original);self.assertEqual(client.get('/api/state').status_code,401)
        self.assertEqual(self.manager.get('/api/me').json()['role'],'manager')
        self.assertEqual(self.inspector.get('/api/me').json()['role'],'inspector')

    def test_viewer_kill_switch_preserves_users_acl_and_other_roles(self):
        before=self.facts()
        with patch.dict(os.environ,{'KSU_VIEWER_ACCESS':'off'}):
            self.assertEqual(self.viewer.get('/api/state').status_code,403)
            self.assertEqual(self.manager.get('/api/state').status_code,200)
            self.assertEqual(self.inspector.get('/api/state').status_code,200)
        self.assertEqual(before,self.facts());self.assertEqual(self.viewer.get('/api/state').status_code,200)


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN'),'Real local PostgreSQL required')
class PostgreSQLViewer(Viewer):
    def setUp(self):release.PostgreSQLRelease.setUp(self);self.prepare()
    def tearDown(self):
        self.viewer.close();self.empty.close();release.PostgreSQLRelease.tearDown(self)
    def facts(self):return release.PostgreSQLRelease.facts(self)
    def test_raw_sql_cannot_read_private_business_rows(self):
        with self.app.state.db.connect(self.app.state.db.user('synthetic-viewer')) as con:
            for table in ('records','media','imports','audit','users','sessions','attempts','operation_receipts'):
                self.assertEqual(con.execute('select count(*) from '+table).fetchone()[0],0)
            rows=con.execute('select * from viewer_records()').fetchall();self.assertTrue(rows)
            self.assertNotIn('PRIVATE-',str(rows))

    def test_existing_v4_requires_explicit_read_capability_refresh_without_business_changes(self):
        from server.pg_admin import upgrade
        from server.postgres_backend import PostgresDatabase
        before=self.facts()
        self.execute_admin('drop function viewer_read_version()')
        legacy=PostgresDatabase(self.app.state.db.dsn)
        self.assertFalse(legacy.viewer_read_capable)
        from server.app import create_app
        older=create_app(self.locator,test_api.ORIGIN,False)
        try:
            with TestClient(older,headers={'Origin':test_api.ORIGIN}) as client:
                client.cookies.update(self.viewer.cookies)
                self.assertEqual(client.get('/api/state').status_code,403)
                client.cookies.clear();client.cookies.update(self.manager.cookies)
                self.assertEqual(client.get('/api/state').status_code,200)
                client.cookies.clear();client.cookies.update(self.inspector.cookies)
                self.assertEqual(client.get('/api/state').status_code,200)
        finally:older.state.import_jobs.close()
        self.assertEqual(before,self.facts())
        result=upgrade(self.admin,self.role)
        self.assertTrue(result['existing_data_preserved'])
        self.assertEqual(before,self.facts())
        PostgresDatabase(self.app.state.db.dsn)

    def test_sql_projection_and_attachment_function_do_not_bypass_site_acl(self):
        photo,foreign,orphan=self.detailed_fixture()
        with self.app.state.db.connect(self.app.state.db.user('synthetic-viewer')) as con:
            self.assertNotIn('PRIVATE-',str(con.execute('select * from viewer_records()').fetchall()))
            self.assertEqual(len(con.execute('select * from viewer_media(?,?)',(photo.rsplit('/',1)[1],False)).fetchall()),1)
            for url in (foreign,orphan):
                self.assertEqual(con.execute('select * from viewer_media(?,?)',(url.rsplit('/',1)[1],False)).fetchall(),[])

    def test_real_v3_native_archive_restores_to_v4_with_all_tables_preserved(self):
        import hashlib,json,zipfile
        from server.database import ROOT
        from server.pg_admin import TABLES,fingerprints
        from server.decision_version_schema import schema as decisions
        from server.offline_schema import schema as offline
        from server.pg_backup import run_tool,restore_database
        from psycopg import sql
        names=[self.name+'_legacy3',self.name+'_restored4'];legacy,restored=[pg.database_dsn(self.root_admin,n) for n in names]
        with psycopg.connect(self.root_admin,autocommit=True) as con:
            for name in names:con.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
        try:
            # Exact predecessor DDL: untouched v1 SQL + v2 + v3 generators, before viewer migration.
            with psycopg.connect(legacy) as dst,psycopg.connect(self.admin) as src:
                dst.execute((ROOT/'server/postgres/001_initial.sql').read_text());dst.execute(decisions(True));dst.execute(offline(True))
                for table in TABLES:dst.execute(sql.SQL('alter table ksu.{} disable trigger user').format(sql.Identifier(table)))
                for table in TABLES:
                    query='select * from ksu.'+table
                    if table=='users':query+=" where role in ('manager','inspector')"
                    if table=='user_sites':query+=" where user_id in ('manager','inspector')"
                    with dst.cursor().copy(sql.SQL('copy ksu.{} from stdin').format(sql.Identifier(table))) as copy:
                        for row in src.execute(query):copy.write_row(row)
                for table in TABLES:dst.execute(sql.SQL('alter table ksu.{} enable trigger user').format(sql.Identifier(table)))
                dst.execute("select setval(pg_get_serial_sequence('ksu.audit','seq'),coalesce(max(seq),1),max(seq) is not null) from ksu.audit")
                expected=fingerprints(dst,True)
            root=Path(self.temp.name);dump=root/'v3.dump'
            run_tool('pg_dump',['--format=custom','--compress=0','--no-owner','--no-privileges','--file='+str(dump)],legacy)
            package=root/'v3.ksubackup'
            with dump.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
            manifest={'format':'ksu-pg-backup-v1','schema_version':3,'dump_sha256':digest,'tables':expected,'sessions_revoked':True}
            with zipfile.ZipFile(package,'x',compression=zipfile.ZIP_STORED) as archive:
                archive.write(dump,'database.dump');archive.writestr('manifest.json',json.dumps(manifest))
            result=restore_database(package,restored,self.role);self.assertTrue(result['verified']);self.assertEqual(result['tables'],expected)
            with psycopg.connect(restored) as con:
                self.assertEqual(con.execute('select max(version) from ksu.schema_version').fetchone()[0],4)
                self.assertEqual(fingerprints(con,True),expected)
        finally:
            with psycopg.connect(self.root_admin,autocommit=True) as con:
                for name in names:con.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))
