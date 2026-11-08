"""Same server contracts exercised on SQLite and a real restricted PostgreSQL role."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import psycopg
from server.app import create_app
from server.database import Database
from server.maintenance import snapshot_database
from tests import test_api, test_postgres as pg


class Offline(unittest.TestCase):
    setUp=test_api.API.setUp
    client=test_api.API.client
    state=test_api.API.state
    change=test_api.API.change

    def tearDown(self):
        self.app.state.import_jobs.close()
        test_api.API.tearDown(self)

    def batch(self, ident='DEMO-visit', version=0, mutation=None, **values):
        return {'mutationId':mutation or uuid4().hex,'changes':[{'collection':'visits','id':ident,'expectedVersion':version,
                'data':{'siteId':'s1','date':'2026-11-09',**values}}]}

    def picture(self, mutation=None, color='blue', site='s1'):
        out=io.BytesIO();Image.new('RGB',(20,20),color).save(out,'PNG');raw=out.getvalue()
        return {'siteId':site,'content':base64.b64encode(raw).decode(),
                'mutationId':mutation or uuid4().hex,'sha256':hashlib.sha256(raw).hexdigest()}

    def package(self, client=None, sites=None):
        response=(client or self.inspector).post('/api/offline/packages',json={'siteIds':sites or ['s1']})
        self.assertEqual(response.status_code,200,response.text);return response.json()

    def operation(self,package,body):
        return self.inspector.post('/api/offline/changes',json={**body,'packageId':package['id']})

    def admin_execute(self,query,params=()):
        if self.app.state.db.backend=='postgresql':
            from server.postgres_backend import Connection
            with psycopg.connect(self.admin) as con:
                con.execute('set local search_path=ksu,pg_catalog')
                con.execute(Connection.parameters(query),params)
        else:
            with self.app.state.db.connect() as con:con.execute(query,params)

    def count(self,table):
        assert table in ('media','audit','mutations','operation_receipts','offline_packages')
        with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:
            return con.execute('select count(*) from '+table).fetchone()[0]

    def drop_response_once(self,path):
        fired=[]
        @self.app.middleware('http')
        async def synthetic_disconnect(request,call_next):
            response=await call_next(request)
            if request.url.path==path and request.method=='POST' and not fired:
                fired.append(True)
                raise ConnectionResetError('DEMO response lost AFTER committed handler')
            return response

    def test_response_lost_after_commit_and_restart_receipt_exactly_once(self):
        # Install failure injection before the middleware stack is first constructed.
        self.manager.close();self.inspector.close()
        self.app.state.import_jobs.close()
        locator='postgresql' if self.app.state.db.backend=='postgresql' else self.app.state.db.path
        self.app=create_app(locator,test_api.ORIGIN,False)
        self.drop_response_once('/api/changes')
        self.inspector=self.client('inspector',test_api.INSPECTOR_PASSWORD)
        self.manager=self.client('manager',test_api.MANAGER_PASSWORD)
        batch=self.batch();before=self.count('audit')
        with self.assertRaises(ConnectionResetError):self.inspector.post('/api/changes',json=batch)
        receipt=self.inspector.get('/api/receipts/'+batch['mutationId']).json()
        self.assertTrue(receipt['committed']);self.assertEqual(receipt['records'][0]['version'],1)
        self.assertEqual(self.inspector.post('/api/changes',json=batch).status_code,200)
        self.assertEqual(self.count('audit'),before+1)
        self.assertEqual(self.count('mutations'),1)
        # A fresh app/session sees the durable receipt, not an in-process cache.
        restarted=create_app(locator,test_api.ORIGIN,False)
        with TestClient(restarted,headers={'Origin':test_api.ORIGIN}) as client:
            login=client.post('/api/login',json={'username':'inspector','password':test_api.INSPECTOR_PASSWORD})
            self.assertEqual(login.status_code,200)
            self.assertEqual(client.get('/api/receipts/'+batch['mutationId']).json(),receipt)

    def test_photo_response_loss_dedup_new_mutation_and_isolated_actor_site(self):
        self.app.state.import_jobs.close();self.manager.close();self.inspector.close()
        self.app=create_app('postgresql' if self.app.state.db.backend=='postgresql' else self.app.state.db.path,test_api.ORIGIN,False)
        self.drop_response_once('/api/media')
        self.manager=self.client('manager',test_api.MANAGER_PASSWORD);self.inspector=self.client('inspector',test_api.INSPECTOR_PASSWORD)
        photo=self.picture()
        with self.assertRaises(ConnectionResetError):self.inspector.post('/api/media',json=photo)
        saved=self.inspector.get('/api/receipts/'+photo['mutationId']).json()
        self.assertEqual(self.inspector.post('/api/media',json=photo).json(),saved)
        second=self.inspector.post('/api/media',json={**photo,'mutationId':uuid4().hex})
        self.assertEqual(second.json()['url'],saved['url']);self.assertEqual(self.count('media'),1)
        for client in (self.inspector,self.manager):
            self.assertEqual(client.get(saved['url']).status_code,200)
            self.assertEqual(client.get(saved['url']+'?thumbnail=true').status_code,200)
        self.assertNotEqual(self.manager.post('/api/media',json={**photo,'mutationId':uuid4().hex}).json()['url'],saved['url'])
        self.assertNotEqual(self.inspector.post('/api/media',json={**photo,'siteId':'s2','mutationId':uuid4().hex}).json()['url'],saved['url'])

    def test_reuse_different_body_kind_checksum_and_legacy_photo(self):
        photo=self.picture();saved=self.inspector.post('/api/media',json=photo).json()
        self.assertEqual(self.inspector.post('/api/media',json=self.picture(photo['mutationId'],color='red')).status_code,409)
        self.assertEqual(self.inspector.post('/api/media',json={**photo,'sha256':'0'*64}).status_code,409)
        self.assertEqual(self.inspector.post('/api/changes',json=self.batch(mutation=photo['mutationId'])).status_code,409)
        self.assertEqual(self.inspector.post('/api/media',json={k:v for k,v in photo.items() if k not in ('mutationId','sha256')}).status_code,200)
        self.assertEqual(self.inspector.post('/api/media',json={k:v for k,v in photo.items() if k!='sha256'}).status_code,422)
        self.assertEqual(self.manager.get('/api/receipts/'+photo['mutationId']).status_code,404)
        self.admin_execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get('/api/receipts/'+photo['mutationId']).status_code,404)
        self.assertEqual(self.inspector.post('/api/media',json=photo).status_code,403)
        self.assertEqual(self.inspector.get(saved['url']).status_code,404)

    def test_checklist_history_pinned_inspection_and_immutability(self):
        old=self.inspector.get('/api/checklists/current').json();question=old['questions'][0]
        response=self.change(self.manager,'questions',{**question,'title':'ДЕМО · Изменённый пункт'},question['id'],question['version'])
        self.assertEqual(response.status_code,200,response.text)
        new=self.inspector.get('/api/checklists/current').json();self.assertNotEqual(new['id'],old['id'])
        self.assertEqual(self.inspector.get('/api/checklists/'+old['id']).json(),old)
        batch={'mutationId':uuid4().hex,'changes':[{'collection':'inspections','id':'DEMO-historical','expectedVersion':0,
            'data':{'siteId':'s1','date':'2026-11-09','inspector':'DEMO','checklistVersion':old['id'],
                    'results':[{'questionId':q['id'],'status':'ok','title':'ДЕМО · Подмена'} for q in old['questions']]}}]}
        self.assertEqual(self.inspector.post('/api/changes',json=batch).status_code,200)
        inspection=self.state()['inspections'][0]
        self.assertEqual(inspection['checklistVersion'],old['id'])
        self.assertEqual(inspection['results'][0]['title'],question['title'])
        batch['mutationId']=uuid4().hex;batch['changes'][0]['id']='DEMO-invalid';batch['changes'][0]['data']['checklistVersion']='0'*64
        self.assertEqual(self.inspector.post('/api/changes',json=batch).status_code,404)
        with self.assertRaises((sqlite3.IntegrityError,psycopg.Error)):
            with self.app.state.db.connect(self.app.state.db.user('manager')) as con:con.execute("update checklist_versions set data='[]'")

    def test_durable_conflict_rolls_back_whole_batch(self):
        first=self.batch();self.assertEqual(self.inspector.post('/api/changes',json=first).status_code,200)
        package=self.package();batch=self.batch(ident='DEMO-new')
        batch['changes']+=self.batch()['changes'] # stale expectedVersion=0 after another new row
        before=self.count('audit');response=self.operation(package,batch)
        self.assertEqual(response.status_code,409,response.text);saved=response.json()
        self.assertFalse(saved['committed']);self.assertEqual(self.count('audit'),before)
        self.assertEqual(len(self.state()['visits']),1)
        self.assertEqual(self.operation(package,batch).json(),saved)
        self.assertEqual(self.inspector.get('/api/receipts/'+batch['mutationId']).json(),saved)
        self.assertEqual(self.count('mutations'),1)
        self.assertEqual(self.operation(package,{**batch,'changes':batch['changes'][:1]}).status_code,409)

    def test_concurrent_retry_and_concurrent_expected_version(self):
        batch=self.batch()
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(lambda _:self.inspector.post('/api/changes',json=batch),range(4)))
        self.assertEqual([r.status_code for r in responses],[200]*4)
        self.assertEqual(len(self.state()['visits']),1);self.assertEqual(self.count('mutations'),1)
        batches=[self.batch(version=1,goal='ДЕМО · '+str(n)) for n in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda value:self.inspector.post('/api/changes',json=value).status_code,batches))
        self.assertEqual(sorted(results),[200,409])
        self.assertEqual(self.state()['visits'][0]['version'],2)

    def test_package_limits_privacy_rights_expiry_and_pinned_version(self):
        self.inspector.post('/api/changes',json=self.batch())
        value=self.package();self.assertEqual([v['id'] for v in value['visits']],['DEMO-visit'])
        self.assertEqual([s['id'] for s in value['sites']],['s1'])
        self.assertFalse(value['mediaIncluded']);self.assertNotIn('users',value);self.assertNotIn('decision_records',value)
        for body in ({'siteIds':['s1']*6},{'siteIds':['s1','s1']},{'siteIds':['s1'],'ttlHours':25}):
            self.assertEqual(self.inspector.post('/api/offline/packages',json=body).status_code,422)
        wrong=self.package(self.manager)
        self.assertEqual(self.operation(wrong,self.batch(ident='DEMO-denied')).status_code,404)
        self.assertEqual(self.operation(value,self.batch(ident='DEMO-outside',siteId='s2')).status_code,403)
        with patch('server.offline.epoch',return_value=time.time()+25*3600):
            self.assertEqual(self.operation(value,self.batch(ident='DEMO-expired')).status_code,409)
        for _ in range(19):self.package()
        self.assertEqual(self.inspector.post('/api/offline/packages',json={'siteIds':['s1']}).status_code,429)
        self.admin_execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertIn(self.operation(value,self.batch(ident='DEMO-revoked')).status_code,(403,404))

    def test_auth_csrf_and_origin_for_new_contracts(self):
        with TestClient(self.app,headers={'Origin':test_api.ORIGIN}) as anonymous:
            for path in ('/api/checklists/current','/api/checklists/'+'0'*64,'/api/receipts/DEMO'):
                self.assertEqual(anonymous.get(path).status_code,401)
        for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://evil.test'}):
            self.assertEqual(self.inspector.post('/api/offline/packages',json={'siteIds':['s1']},headers=headers).status_code,403)

    def test_pinned_package_submission_after_checklist_edit_and_expired_retry(self):
        package=self.package();question=package['checklist']['questions'][0]
        self.assertEqual(self.change(self.manager,'questions',{**question,'title':'ДЕМО · Новый текст'},question['id'],question['version']).status_code,200)
        inspection={'collection':'inspections','id':'DEMO-offline-act','expectedVersion':0,'data':{
            'siteId':'s1','date':'2026-11-09','inspector':'DEMO','checklistVersion':package['checklist']['id'],
            'results':[{'questionId':q['id'],'status':'ok'} for q in package['checklist']['questions']]}}
        batch={'mutationId':uuid4().hex,'changes':[inspection]}
        response=self.operation(package,batch);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.state()['inspections'][0]['results'][0]['title'],question['title'])
        with patch('server.offline.epoch',return_value=time.time()+25*3600):
            self.assertEqual(self.operation(package,batch).json(),response.json())
        bad=json.loads(json.dumps(batch));bad['mutationId']=uuid4().hex;bad['changes'][0]['id']='DEMO-wrong-version'
        bad['changes'][0]['data']['checklistVersion']=self.inspector.get('/api/checklists/current').json()['id']
        self.assertEqual(self.operation(package,bad).status_code,409)

    def test_oversized_package_is_rejected_and_offline_tables_are_append_only(self):
        changes=[self.batch(ident='DEMO-many-'+str(n))['changes'][0] for n in range(51)]
        response=self.inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':changes})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.inspector.post('/api/offline/packages',json={'siteIds':['s1']}).status_code,413)
        self.assertEqual(self.count('offline_packages'),0)
        self.inspector.post('/api/media',json=self.picture())
        self.assertEqual(self.inspector.get('/api/checklists/current').status_code,200)
        for table in ('operation_receipts','media_keys','checklist_versions'):
            with self.assertRaises((sqlite3.IntegrityError,psycopg.Error)):
                with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:con.execute('delete from '+table)

    def test_sqlite_independent_restore_retains_all_new_tables(self):
        if self.app.state.db.backend!='sqlite':self.skipTest('PostgreSQL has its own native restore test')
        package=self.package();photo=self.picture();saved=self.inspector.post('/api/media',json=photo).json()
        batch=self.batch();self.inspector.post('/api/changes',json=batch)
        receipt=self.inspector.get('/api/receipts/'+batch['mutationId']).json()
        source=self.app.state.db.path;copy=Path(self.temp.name)/'independent'/'backup.sqlite3';target=Path(self.temp.name)/'restored'/'ksu.sqlite3'
        snapshot_database(source,copy);original_hash=hashlib.sha256(copy.read_bytes()).hexdigest()
        snapshot_database(copy,target);app=create_app(target,test_api.ORIGIN,False)
        with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
            login=client.post('/api/login',json={'username':'inspector','password':test_api.INSPECTOR_PASSWORD})
            self.assertEqual(login.status_code,200)
            self.assertEqual(client.get('/api/receipts/'+batch['mutationId']).json(),receipt)
            self.assertEqual(client.get('/api/checklists/'+package['checklist']['id']).json(),package['checklist'])
            self.assertEqual(client.get(saved['url']).status_code,200)
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(),original_hash)


@unittest.skipUnless(__import__('os').getenv('KSU_TEST_PG_ADMIN_DSN'),'Real local PostgreSQL required')
class PostgreSQLOffline(Offline):
    setUp=pg.PostgreSQL.setUp
    tearDown=pg.PostgreSQL.tearDown

    def test_explicit_v2_upgrade_preserves_old_rows_and_role_boundaries(self):
        from psycopg import sql
        from server.database import ROOT
        from server.decision_version_schema import schema
        from server.pg_admin import V2_TABLES,fingerprints,upgrade
        name='ksu_v2_'+uuid4().hex;dsn=pg.database_dsn(self.root_admin,name)
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn) as dest,psycopg.connect(self.admin) as source:
                dest.execute((ROOT/'server/postgres/001_initial.sql').read_text());dest.execute(schema(True))
                for table in V2_TABLES:
                    dest.execute(sql.SQL('alter table ksu.{} disable trigger user').format(sql.Identifier(table)))
                    with dest.cursor().copy(sql.SQL('copy ksu.{} from stdin').format(sql.Identifier(table))) as copy:
                        for row in source.execute('select * from ksu.'+table):copy.write_row(row)
                    dest.execute(sql.SQL('alter table ksu.{} enable trigger user').format(sql.Identifier(table)))
                dest.execute("select setval(pg_get_serial_sequence('ksu.audit','seq'),(select max(seq) from ksu.audit))")
                before=fingerprints(dest,True,V2_TABLES)
            upgrade(dsn,self.role);upgrade(dsn,self.role)
            with psycopg.connect(dsn) as con:
                self.assertEqual(fingerprints(con,True,V2_TABLES),before)
                self.assertEqual(con.execute('select max(version) from ksu.schema_version').fetchone()[0],3)
                self.assertEqual(con.execute('select count(*) from ksu.operation_receipts').fetchone()[0],0)
            app=create_app(pg.database_dsn(self.runtime,name),test_api.ORIGIN,False)
            with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
                self.assertEqual(client.post('/api/login',json={'username':'inspector','password':test_api.INSPECTOR_PASSWORD}).status_code,200)
                self.assertEqual(client.get('/api/checklists/current').status_code,200)
        finally:
            with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))

    def test_backup_restore_preserves_checklists_media_receipts_and_packages(self):
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from server.pg_admin import grant_backup, fingerprints
        from server.pg_backup import backup_database,restore_database
        import os
        if not os.getenv('KSU_PG_DUMP_BIN'):self.skipTest('Real native PostgreSQL tools required')
        package=self.package();photo=self.picture();saved=self.inspector.post('/api/media',json=photo).json()
        batch=self.batch();response=self.operation(package,batch);self.assertEqual(response.status_code,200)
        receipt=response.json()
        backup_dsn=pg.database_dsn(os.environ['KSU_TEST_PG_BACKUP_DSN'],self.name)
        with psycopg.connect(self.admin) as con:
            grant_backup(con,conninfo_to_dict(backup_dsn)['user']);before=fingerprints(con,True)
        archive=Path(self.temp.name)/'DEMO-stage1.ksubackup';backup_database(backup_dsn,archive)
        root=os.environ['KSU_TEST_PG_RESTORE_ADMIN_DSN'];name='ksu_offline_'+uuid4().hex
        target=pg.database_dsn(root,name)
        runtime=pg.database_dsn(os.environ['KSU_TEST_PG_RESTORE_RUNTIME_DSN'],name)
        with psycopg.connect(root,autocommit=True) as con:con.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
        try:
            restore_database(archive,target,conninfo_to_dict(runtime)['user'])
            with psycopg.connect(target) as con:self.assertEqual(fingerprints(con,True),before)
            with patch.dict(os.environ,{'KSU_WORK_DIR':str(Path(self.temp.name)/'restored-work')}):
                app=create_app(runtime,test_api.ORIGIN,False)
            with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
                login=client.post('/api/login',json={'username':'inspector','password':test_api.INSPECTOR_PASSWORD})
                self.assertEqual(login.status_code,200);client.headers['X-CSRF-Token']=login.json()['csrf']
                self.assertEqual(client.get('/api/receipts/'+batch['mutationId']).json(),receipt)
                self.assertEqual(client.get('/api/checklists/'+package['checklist']['id']).json(),package['checklist'])
                self.assertEqual(client.get(saved['url']).status_code,200)
                self.assertEqual(client.post('/api/media',json=photo).json(),saved)
                self.assertEqual(client.post('/api/offline/changes',json={**batch,'packageId':package['id']}).json(),receipt)
        finally:
            with psycopg.connect(root,autocommit=True) as con:con.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))
