import base64
import io
import json
import sqlite3
import secrets
import tempfile
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image
from server.app import create_app
from server.database import seed_demo, password_valid

MANAGER_PASSWORD=secrets.token_urlsafe(24)
INSPECTOR_PASSWORD=secrets.token_urlsafe(24)
ORIGIN='http://testserver'


class API(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.temp.name)/'test.sqlite3',ORIGIN,False)
        seed_demo(self.app.state.db,MANAGER_PASSWORD,INSPECTOR_PASSWORD)
        self.manager=self.client('manager',MANAGER_PASSWORD)
        self.inspector=self.client('inspector',INSPECTOR_PASSWORD)

    def tearDown(self):
        self.manager.close();self.inspector.close();self.temp.cleanup()

    def client(self,username,password):
        client=TestClient(self.app,headers={'Origin':ORIGIN})
        response=client.post('/api/login',json={'username':username,'password':password})
        self.assertEqual(response.status_code,200,response.text)
        client.headers['X-CSRF-Token']=response.json()['csrf']
        return client

    def state(self,client=None):return (client or self.manager).get('/api/state').json()

    def change(self,client,collection,data,ident=None,version=0,mutation=None):
        return client.post('/api/changes',json={'mutationId':mutation or uuid4().hex,'changes':[
            {'collection':collection,'id':ident or uuid4().hex,'expectedVersion':version,'data':data}]})

    def inv(self,client=None,ident='inventory1'):
        r=self.change(client or self.inspector,'inventories',{'siteId':'s1','date':date.today().isoformat(),'title':'ДЕМО · Пересчёт'},ident)
        self.assertEqual(r.status_code,200,r.text);return ident

    def photo(self,client=None,site='s1'):
        image=Image.new('RGB',(10,10),'red');out=io.BytesIO();image.save(out,format='PNG')
        r=(client or self.inspector).post('/api/media',json={'siteId':site,'content':base64.b64encode(out.getvalue()).decode()})
        self.assertEqual(r.status_code,200,r.text);return r.json()['url']

    def test_unauthenticated_and_static_secrets(self):
        with TestClient(self.app) as client:
            for path in ['/api/state','/api/history','/api/reports/xlsx','/api/media/nope']:
                self.assertEqual(client.get(path).status_code,401)
            for path in ['/var/ksu.sqlite3','/.env','/server/seed.json','/schema.sql','/../README.md']:
                self.assertEqual(client.get(path).status_code,404)
            self.assertNotIn('password_hash',client.get('/?filename=server/database.py').text)

    def test_original_catalogue_and_checklist_order(self):
        state=self.state()
        self.assertEqual([x['id'] for x in state['questions']],['q'+str(n) for n in range(1,23)])
        self.assertEqual([x['id'] for x in state['sites']],['s'+str(n) for n in range(1,13)])

    def test_cookie_csrf_origin_and_logout(self):
        self.assertIn('HttpOnly',self.manager.cookies.get('ksu_session') and self.manager.post('/api/login',json={'username':'manager','password':MANAGER_PASSWORD}).headers['set-cookie'])
        r=self.manager.get('/api/me');self.manager.headers['X-CSRF-Token']=r.json()['csrf']
        self.assertEqual(self.manager.post('/api/logout',headers={'X-CSRF-Token':'bad'}).status_code,403)
        self.assertEqual(self.manager.post('/api/logout',headers={'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.manager.post('/api/logout').status_code,200)
        self.assertEqual(self.manager.get('/api/state').status_code,401)

    def test_login_rate_limit_and_unknown_user(self):
        with TestClient(self.app,headers={'Origin':ORIGIN}) as client:
            for _ in range(10):self.assertEqual(client.post('/api/login',json={'username':'absent','password':'wrong'}).status_code,401)
            self.assertEqual(client.post('/api/login',json={'username':'absent','password':'wrong'}).status_code,429)

    def test_passwords_are_hashed_and_role_is_not_client_controlled(self):
        user=self.app.state.db.user('inspector')
        self.assertNotEqual(user['password'],INSPECTOR_PASSWORD);self.assertTrue(password_valid(INSPECTOR_PASSWORD,user['password']))
        self.assertEqual(self.inspector.post('/api/login',json={'username':'inspector','password':INSPECTOR_PASSWORD,'role':'manager'}).status_code,422)
        self.assertEqual(self.inspector.post('/api/users',json={'role':'manager'}).status_code,404)
        self.assertEqual(self.change(self.inspector,'sites',{'name':'ДЕМО · Обход'}).status_code,403)
        self.assertEqual(self.change(self.inspector,'questions',{'section':'Тест','title':'Тест'}).status_code,403)

    def test_site_scope_for_reads_writes_and_media(self):
        photo=self.photo(self.manager,'s2')
        with self.app.state.db.connect() as con:con.execute('delete from user_sites where user_id=? and site_id=?',('inspector','s2'))
        self.assertNotIn('s2',[s['id'] for s in self.state(self.inspector)['sites']])
        self.assertEqual(self.inspector.get(photo).status_code,404)
        self.assertEqual(self.change(self.inspector,'inventories',{'siteId':'s2','date':'2026-11-01','title':'ДЕМО'}).status_code,403)

    def test_foreign_records_cannot_be_edited(self):
        self.inv(self.manager)
        record=self.state()['inventories'][0]
        self.assertEqual(self.change(self.inspector,'inventories',{**record,'title':'Обход'},record['id'],1).status_code,403)

    def test_sql_enforces_permissions_and_audit_is_append_only(self):
        user=self.app.state.db.user('inspector')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.state.db.connect(user) as con:
                con.execute("insert into records values('sites','hack','{}','inspector',null,1,'now','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.state.db.connect() as con:con.execute('delete from audit')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.state.db.connect() as con:con.execute("update records set data='{}' where id='s1'")

    def test_atomic_rollback_and_versions(self):
        self.inv();record=self.state()['inventories'][0]
        request={'mutationId':uuid4().hex,'changes':[
            {'collection':'inventories','id':record['id'],'expectedVersion':1,'data':{**record,'title':'Изменено'}},
            {'collection':'sites','id':'hack','expectedVersion':0,'data':{'name':'Обход'}}]}
        self.assertEqual(self.inspector.post('/api/changes',json=request).status_code,403)
        self.assertEqual(self.state()['inventories'][0]['title'],'ДЕМО · Пересчёт')
        self.assertEqual(self.change(self.manager,'inventories',{**record,'title':'ДЕМО · Новое'},record['id'],1).status_code,200)
        self.assertEqual(self.change(self.inspector,'inventories',record,record['id'],1).status_code,409)

    def test_idempotency_and_concurrent_writes(self):
        self.inv();record=self.state()['inventories'][0];mutation=uuid4().hex
        for _ in range(2):self.assertEqual(self.change(self.manager,'inventories',{**record,'title':'ДЕМО · Обновлено'},record['id'],1,mutation).status_code,200)
        self.assertEqual(self.state()['inventories'][0]['version'],2)
        self.assertEqual(self.change(self.manager,'inventories',{**record,'title':'Другое'},record['id'],1,mutation).status_code,409)
        record=self.state()['inventories'][0]
        def update(title):return self.change(self.manager,'inventories',{**record,'title':title},record['id'],2).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(update,['ДЕМО · А','ДЕМО · Б']))
        self.assertEqual(sorted(statuses),[200,409])

    def test_negative_nan_and_bad_dates_rejected(self):
        self.inv()
        for number in [-1,'NaN','Infinity',100000001]:
            self.assertEqual(self.change(self.inspector,'inventoryLines',{'inventoryId':'inventory1','item':'ДЕМО','expected':number,'actual':0,'price':1}).status_code,422)
        self.assertEqual(self.change(self.inspector,'inventories',{'siteId':'s1','date':'2026-02-30','title':'ДЕМО'}).status_code,422)

    def test_stages_and_financials(self):
        self.inv();record=self.state()['inventories'][0]
        self.assertEqual(self.change(self.inspector,'inventories',{**record,'stage':'confirmed','decision':'ДЕМО'},record['id'],1).status_code,403)
        self.assertEqual(self.change(self.manager,'inventories',{**record,'stage':'approved','decision':'ДЕМО'},record['id'],1).status_code,422)
        for stage in ['confirmed','investigation','approved','recovered']:
            record=self.state()['inventories'][0]
            self.assertEqual(self.change(self.manager,'inventories',{**record,'stage':stage,'decision':'ДЕМО · решение'},record['id'],record['version']).status_code,200)
        record=self.state()['inventories'][0]
        self.assertEqual(self.change(self.manager,'inventoryLines',{'inventoryId':record['id'],'item':'ДЕМО','expected':1,'actual':0,'price':1}).status_code,409)

    def test_closure_evidence_and_audit(self):
        data={'siteId':'s1','title':'ДЕМО · Нарушение','status':'closed','resolution':'ДЕМО · Исправлено'}
        self.assertEqual(self.change(self.inspector,'findings',data).status_code,422)
        data['evidence']=self.photo();self.assertEqual(self.change(self.inspector,'findings',data,'finding1').status_code,200)
        history=self.manager.get('/api/history?collection=findings').json();self.assertEqual(len(history),1)
        self.assertEqual(history[0]['actor_id'],'inspector');self.assertEqual(history[0]['after']['status'],'closed')
        with TestClient(self.app) as client:self.assertEqual(client.get(data['evidence']).status_code,401)
        self.assertEqual(self.manager.get(data['evidence']).headers['content-type'],'image/jpeg')
        self.assertEqual(self.manager.get(data['evidence']+'?thumbnail=true').status_code,200)

    def test_media_validation(self):
        self.assertEqual(self.inspector.post('/api/media',json={'siteId':'s1','content':base64.b64encode(b'<svg/>').decode()}).status_code,422)
        self.assertEqual(self.change(self.inspector,'findings',{'siteId':'s1','title':'ДЕМО','photo':'javascript:alert(1)'}).status_code,422)
        self.assertEqual(self.inspector.post('/api/media',json={'siteId':'s1','content':17}).status_code,422)
        self.assertEqual(self.inspector.post('/api/import/preview',json={'name':17,'content':None}).status_code,422)

    def test_https_headers_and_cookie(self):
        with self.assertRaises(ValueError):create_app(Path(self.temp.name)/'bad.sqlite3','https://ksu.example.org',False)
        app=create_app(self.app.state.db.path,'https://ksu.example.org',True)
        with TestClient(app,base_url='https://ksu.example.org',headers={'Origin':'https://ksu.example.org'}) as client:
            r=client.post('/api/login',json={'username':'manager','password':MANAGER_PASSWORD})
            self.assertEqual(r.status_code,200)
            cookie=r.headers['set-cookie']
            for flag in ('HttpOnly','Secure','SameSite=strict'):self.assertIn(flag,cookie)
            self.assertIn('max-age=31536000',r.headers['strict-transport-security'])
            self.assertNotIn('unsafe-eval',r.headers['content-security-policy'])
            self.assertEqual(client.get('/api/state').status_code,200)
            self.assertEqual(client.get('/',headers={'Host':'evil.example.org'}).status_code,400)

    def test_repeated_finding_flag_does_not_change_retroactively(self):
        data={'siteId':'s1','questionId':'q1','title':'ДЕМО · Повтор'}
        self.assertEqual(self.change(self.inspector,'findings',data,'first').status_code,200)
        self.assertEqual(self.change(self.inspector,'findings',data,'second').status_code,200)
        records={f['id']:f for f in self.state()['findings']}
        self.assertFalse(records['first']['repeat']);self.assertTrue(records['second']['repeat'])
        self.assertEqual(self.change(self.inspector,'findings',{**records['first'],'resolution':'ДЕМО · В работе'},'first',1).status_code,200)
        self.assertFalse(next(f for f in self.state()['findings'] if f['id']=='first')['repeat'])

    def test_backup_restore_preserves_data_media_audit_and_revokes_sessions(self):
        self.inv();url=self.photo()
        target=Path(self.temp.name)/'backup.sqlite3'
        result=subprocess.run([sys.executable,'-m','server','backup',str(target),'--db',str(self.app.state.db.path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        with sqlite3.connect(target) as con:
            self.assertEqual(con.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(con.execute('pragma foreign_key_check').fetchall(),[])
            self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0],0)
        restored=create_app(target,ORIGIN,False)
        with TestClient(restored,headers={'Origin':ORIGIN}) as client:
            client.cookies.update(self.manager.cookies)
            self.assertEqual(client.get('/api/state').status_code,401)
            self.assertEqual(client.post('/api/login',json={'username':'manager','password':MANAGER_PASSWORD}).status_code,200)
            self.assertEqual(len(client.get('/api/state').json()['inventories']),1)
            self.assertEqual(client.get(url).status_code,200)
            self.assertTrue(client.get('/api/history?collection=inventories').json())
        repeated=subprocess.run([sys.executable,'-m','server','backup',str(target),'--db',str(self.app.state.db.path)],capture_output=True)
        self.assertNotEqual(repeated.returncode,0)

    def test_visit_and_inspection_atomic_flow(self):
        visit={'siteId':'s1','date':date.today().isoformat(),'assignee':'ДЕМО','assignedTo':'inspector'}
        self.assertEqual(self.change(self.manager,'visits',visit,'visit1').status_code,200)
        state=self.state(self.inspector)
        results=[{'questionId':q['id'],'status':'ok'} for q in state['questions']]
        self.assertEqual(self.change(self.inspector,'inspections',{'siteId':'s1','date':date.today().isoformat(),'inspector':'Spoof','results':results[:-1]}).status_code,422)
        payload={'mutationId':uuid4().hex,'changes':[
            {'collection':'inspections','id':'audit1','expectedVersion':0,'data':{'siteId':'s1','date':date.today().isoformat(),'inspector':'Spoof','results':results}},
            {'collection':'visits','id':'visit1','expectedVersion':1,'data':{**state['visits'][0],'status':'completed','inspectionId':'audit1'}}]}
        r=self.inspector.post('/api/changes',json=payload);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['visits'][0]['status'],'completed');self.assertEqual(r.json()['inspections'][0]['inspector'],'ДЕМО · Эльман')
        record=r.json()['inspections'][0]
        self.assertEqual(self.change(self.manager,'inspections',record,record['id'],1).status_code,403)

    def import_payload(self,content=None):
        text=content or 'object;date;item;expected;actual;price\nДЕМО · Нова;2026-11-01;ДЕМО · А;3;2;0.10\nДЕМО · Нова;2026-11-01;ДЕМО · Б;0;2;15.50'
        return {'name':'demo.csv','content':base64.b64encode(text.encode()).decode()}

    def test_import_preview_atomic_errors_and_deduplication(self):
        payload=self.import_payload();r=self.inspector.post('/api/import/preview',json=payload)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['count'],2)
        self.assertEqual(len(self.state()['inventories']),0)
        r=self.inspector.post('/api/import/commit',json=payload);self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(r.json()['duplicate']);self.assertTrue(self.inspector.post('/api/import/commit',json=payload).json()['duplicate'])
        self.assertEqual(len(self.state()['inventoryLines']),2)
        bad=self.import_payload('object;date;item;expected;actual;price\nДЕМО · Нова;2026-02-30;ДЕМО;1;0;1')
        self.assertTrue(self.inspector.post('/api/import/preview',json=bad).json()['errors'])
        self.assertEqual(self.inspector.post('/api/import/commit',json=bad).status_code,422)
        self.assertEqual(len(self.state()['inventoryLines']),2)

    def test_xlsx_mapping_and_formula_rejection(self):
        book=Workbook();sheet=book.active;sheet.append(['Объект','Дата','ТМЦ','Учёт','Факт','Цена']);sheet.append(['ДЕМО · Нова','2026-11-01','ДЕМО',2,1,10])
        out=io.BytesIO();book.save(out)
        payload={'name':'demo.xlsx','content':base64.b64encode(out.getvalue()).decode()}
        self.assertTrue(self.inspector.post('/api/import/preview',json=payload).json()['missing'])
        payload['mapping']=dict(zip(['object','date','item','expected','actual','price'],['Объект','Дата','ТМЦ','Учёт','Факт','Цена']))
        r=self.inspector.post('/api/import/commit',json=payload);self.assertEqual(r.status_code,200,r.text)
        sheet.cell(2,6,'=1+2');out=io.BytesIO();book.save(out);payload['content']=base64.b64encode(out.getvalue()).decode()
        self.assertEqual(self.inspector.post('/api/import/preview',json=payload).status_code,422)

    def test_reports_filters_formats_and_no_formula_injection(self):
        payload=self.import_payload();self.inspector.post('/api/import/commit',json=payload)
        r=self.manager.get('/api/reports/json?kind=inventory&group=site').json()
        self.assertEqual(r['rows'][0][2:4],[.1,31]);self.assertEqual(r['rows'][0][4:], [0,0])
        self.assertEqual(self.manager.get('/api/reports/json?kind=inventory&from=2027-01-01').json()['rows'],[])
        self.assertEqual(self.manager.get('/api/reports/json?kind=inventory&site=s2').json()['rows'],[])
        self.assertEqual(self.manager.get('/api/reports/json?from=wrong').status_code,422)
        for extension in ('xlsx','pdf','csv'):
            response=self.manager.get('/api/reports/'+extension+'?kind=inventory')
            self.assertEqual(response.status_code,200,response.text[:100] if extension=='csv' else '')
            if extension=='xlsx':self.assertEqual(load_workbook(io.BytesIO(response.content)).active.cell(2,3).value,.1)
            if extension=='pdf':self.assertTrue(response.content.startswith(b'%PDF'))
            if extension=='csv':self.assertTrue(response.content.startswith(b'\xef\xbb\xbf'))
        self.change(self.manager,'sites',{'name':'=DEMO(1)'},'s1',1)
        book=load_workbook(io.BytesIO(self.manager.get('/api/reports/xlsx').content));self.assertTrue(book.active.cell(2,1).value.startswith("'="))


if __name__=='__main__':unittest.main(verbosity=2)
