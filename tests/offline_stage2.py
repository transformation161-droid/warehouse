"""Real browser + real API, persistent encrypted IDB and disposable synthetic databases."""
import argparse
from contextlib import contextmanager,closing
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener,ProxyHandler

from PIL import Image
from playwright.sync_api import sync_playwright,expect
import psycopg
from server.database import ROOT,Database
from server.pg_admin import fingerprints,initialize,migrate_sqlite
from experiments.offline_visit.lab import initialize as initialize_lab

PHRASE='stage two synthetic local phrase'
STORE='/offline-visit/store.js'
RELEASE=False


def release_fixture(f):
    if RELEASE:
        f['release']=True
        f['logins']={'manager':'account-owner','inspector':'field-inspector'}
        for role,login in f['logins'].items():
            sql(f,'update users set username=? where id=?',(login,role))
    return f


@contextmanager
def fixture(backend):
    with tempfile.TemporaryDirectory(prefix='ksu-stage2-private-') as directory:
        root=Path(directory);source,lab_id=initialize_lab(root/'lab')
        accounts={line.split(': ',1)[0]:line.split(': ',1)[1] for line in (root/'lab/demo-accounts.txt').read_text().splitlines() if ': ' in line}
        if backend=='postgres':
            from tests.pg_environment import local_cluster
            with local_cluster() as cluster:
                initialize(cluster['source']['admin'],'ksu_runtime','ksu_backup');migrate_sqlite(source,cluster['source']['admin'])
                yield release_fixture({'root':root,'database':'postgresql','admin':cluster['source']['admin'],'runtime':cluster['source']['runtime'],'accounts':accounts,'lab_id':lab_id})
        else:yield release_fixture({'root':root,'database':str(source),'accounts':accounts,'lab_id':lab_id})


def facts(f):
    if f['database']=='postgresql':
        with psycopg.connect(f['admin']) as con:return fingerprints(con,True)
    with closing(sqlite3.connect(f['database'])) as con:return fingerprints(con)


def sql(f,query,params=()):
    if f['database']=='postgresql':
        from server.postgres_backend import Connection
        with psycopg.connect(f['admin']) as con:
            con.execute('set local search_path=ksu,pg_catalog');return con.execute(Connection.parameters(query),params).fetchall() if query.lstrip().lower().startswith('select') else con.execute(Connection.parameters(query),params)
    with closing(sqlite3.connect(f['database'])) as con:
        with con:
            cursor=con.execute(query,params);return cursor.fetchall() if cursor.description else None


@contextmanager
def server(f):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    env={**os.environ,'KSU_DB':f['database'],'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0','KSU_OFFLINE_VISIT_LAB_ID':f['lab_id'],'KSU_WORK_DIR':str(f['root']/'work')}
    env.pop('KSU_DATABASE_URL',None)
    if f.get('release'):
        env.pop('KSU_OFFLINE_VISIT_LAB_ID',None)
        env.update(KSU_OFFLINE_MODE='staging',KSU_OFFLINE_DEPLOYMENT_ID=f['lab_id'],KSU_OFFLINE_USER_IDS='inspector')
    if f['database']=='postgresql':env['KSU_DATABASE_URL']=f['runtime']
    with (f['root']/'api-private.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-m','server','run','--host','127.0.0.1','--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
        try:
            opener=build_opener(ProxyHandler({}))
            for _ in range(150):
                try:
                    if opener.open(origin+'/api/health',timeout=.3).status==200:break
                except OSError:time.sleep(.1)
            else:raise AssertionError('Synthetic API did not start')
            if f.get('release'):
                def switch_off():
                    nonlocal process
                    process.terminate();process.wait(timeout=15);env['KSU_OFFLINE_MODE']='off'
                    process=subprocess.Popen([sys.executable,'-m','server','run','--host','127.0.0.1','--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
                    for _ in range(150):
                        try:
                            if opener.open(origin+'/api/health',timeout=.3).status==200:return
                        except OSError:time.sleep(.1)
                    raise AssertionError('Release kill-switch restart failed')
                f['switch_off']=switch_off
            yield origin
        finally:process.terminate();process.wait(timeout=15)


class Browser:
    def __init__(self,p,f,origin):self.p=p;self.f=f;self.origin=origin;self.context=None
    def open(self,offline=False):
        self.context=self.p.chromium.launch_persistent_context(str(self.f['root']/'profile'),headless=True,
            executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),viewport={'width':390,'height':844},locale='ru-RU',args=['--no-sandbox'])
        self.context.set_offline(offline);self.page=self.context.pages[0]
        self.page.on('dialog',lambda dialog:dialog.accept())
        self.page.goto(self.origin+'/offline-visit/');expect(self.page.locator('#worker-status')).to_contain_text('активна',timeout=15000)
        self.page.locator('#phrase').fill(PHRASE);self.page.locator('#open').click();expect(self.page.locator('#editor')).to_be_visible()
        return self.page
    def close(self):
        if self.context:self.context.close();self.context=None
    def login(self,role='inspector'):
        self.page.locator('#username').fill(self.f.get('logins',{}).get(role,role));self.page.locator('#password').fill(self.f['accounts'][role]);self.page.locator('#login').click()
        if role=='inspector':expect(self.page.locator('#session-status')).to_contain_text('Сессия инспектора')
    def prepare(self):
        self.login();self.page.locator('#load-visits').click();expect(self.page.locator('#visit option')).to_have_count(1)
        self.page.locator('#prepare').click();expect(self.page.locator('.question-card')).to_have_count(22)
        expect(self.page.locator('#local-status')).to_contain_text('Сохранено на устройстве')
    def fill(self,failures=1):
        page=self.page
        for select in page.locator('[data-answer]').all():select.select_option('ok')
        ids=page.locator('[data-answer]').evaluate_all('(els)=>els.map(e=>e.dataset.answer)')
        for index,ident in enumerate(ids[:failures]):
            page.locator(f'[data-answer="{ident}"]').select_option('fail')
            page.locator(f'[data-comment="{ident}"]').fill('ДЕМО · Stage2 confidential marker '+str(index))
        expect(page.locator('#local-status')).to_contain_text('Сохранено на устройстве')
        return ids[:failures]
    def photo(self,ident,color='blue',camera=False):
        page=self.page;page.locator('#photo-question').select_option(ident)
        image=io.BytesIO();Image.new('RGB',(30,20),color).save(image,'PNG')
        count=page.locator('#photos img').count()
        with page.expect_file_chooser() as choice:page.locator('#take-photo' if camera else '#choose-photo').click()
        choice.value.set_files({'name':'DEMO.png','mimeType':'image/png','buffer':image.getvalue()})
        expect(page.locator('#photos img')).to_have_count(count+1)
    def seal(self):
        self.page.locator('#complete').click();expect(self.page.locator('#sync-status')).to_have_text('Ожидает отправки')
    def local(self):
        return self.page.evaluate('''async ({url,phrase})=>{const {Vault}=await import(url);const vault=new Vault();return await vault.unlock(phrase)}''',{'url':STORE,'phrase':PHRASE})


def full_visit(p,backend):
    with fixture(backend) as f,server(f) as origin:
        b=Browser(p,f,origin);page=b.open();b.prepare()
        assert page.evaluate('document.documentElement.scrollWidth')==390
        b.context.set_offline(True);ids=b.fill(2);b.photo(ids[0],camera=True);b.photo(ids[1],'red')
        before=b.local();b.close();page=b.open(offline=True)
        expect(page.locator('#photos img')).to_have_count(2)
        assert b.local()['answers']==before['answers'] and b.local()['photos']==before['photos']
        b.seal();sealed=b.local();b.close();page=b.open(offline=True)
        assert b.local()['operationId']==sealed['operationId']
        print('PASS: '+backend+' encrypted offline answers/photos, cold restart and stable sealed IDs',flush=True)
        # Expire the real server session; the local profile and queue must survive.
        sql(f,'delete from sessions')
        b.context.set_offline(False);page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Ошибка')
        assert sql(f,'select count(*) from media')[0][0]==0
        b.login('manager');expect(page.locator('#message')).to_contain_text('Нужна сессия')
        page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Ошибка')
        assert sql(f,'select count(*) from media')[0][0]==0
        b.login()
        photo_posts=[];batch_posts=[];held_routes=[]
        def photo_lost(route):
            response=route.fetch();photo_posts.append(response.status)
            if len(photo_posts)==1:route.abort('connectionreset')
            else:route.fulfill(response=response)
        def batch_lost(route):
            response=route.fetch();batch_posts.append(response.status)
            held_routes.append(route)
            # Deliberately leave the response held. Closing Chromium below loses it
            # while the durable local queue still says "sending", after real commit.
        page.route('**/api/media',photo_lost);page.route('**/api/offline/changes',batch_lost)
        page.locator('#sync').click()
        for _ in range(200):
            if batch_posts:break
            page.wait_for_timeout(50)
        assert batch_posts,'No committed batch observed'
        assert photo_posts==[200,200] and batch_posts==[200],(photo_posts,batch_posts)
        assert sql(f,'select count(*) from media')[0][0]==2
        assert sql(f,"select count(*) from records where collection='inspections'")[0][0]==1
        assert sql(f,"select count(*) from records where collection='findings'")[0][0]==2
        stored=json.loads(sql(f,"select data from records where collection='visits' and id='DEMO-stage2-visit'")[0][0]);assert stored['status']=='completed'
        committed=facts(f);pending=b.local();assert pending['queue']['batch']['mutationId']==sealed['operationId']
        assert pending['queue']['status']=='Отправляется'
        # Stop renderer JS before dropping the held response, then close Chromium.
        # This exercises durable "sending" recovery without leaking an unresolved route task.
        cdp=b.context.new_cdp_session(page);cdp.send('Emulation.setScriptExecutionDisabled',{'value':True})
        for route in held_routes:route.abort('connectionreset')
        b.close()
        if f.get('release'):f['switch_off']()
        page=b.open(offline=True);assert b.local()['queue']['batch']==pending['queue']['batch']
        expect(page.locator('#sync-status')).to_have_text('Ожидает отправки')
        b.context.set_offline(False);page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Принято сервером',timeout=15000)
        accepted=b.local();assert accepted['queue']['receipt']['committed'] is True
        assert facts(f)==committed
        page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Принято сервером');assert facts(f)==committed
        # No package, account or API response ever enters Cache API.
        cached=page.evaluate('''async()=>{const keys=await caches.keys();const urls=[];for(const key of keys)for(const r of await (await caches.open(key)).keys())urls.push(new URL(r.url).pathname);return urls}''')
        assert cached and all(url.startswith('/offline-visit/') for url in cached)
        encrypted=page.evaluate('''async url=>{const {Vault,exportJSON}=await import(url);return exportJSON(await new Vault().export())}''',STORE)
        assert 'Stage2 confidential marker' not in encrypted and f['accounts']['inspector'] not in encrypted
        b.close();page=b.open(offline=True);expect(page.locator('#sync-status')).to_have_text('Принято сервером');assert b.local()['photos']==sealed['photos']
        b.close();print('PASS: '+backend+' expired session, wrong account, lost photo/batch responses AFTER commit, receipt recovery, zero duplicates, offline accepted restart'+(' including server kill-switch before receipt recovery' if f.get('release') else ''),flush=True)


def refusal(p,backend,kind):
    with fixture(backend) as f,server(f) as origin:
        b=Browser(p,f,origin);page=b.open();b.prepare();b.context.set_offline(True);ids=b.fill();b.photo(ids[0]);b.seal();local=b.local()
        if kind=='conflict':
            old=json.loads(sql(f,"select data from records where collection='visits' and id='DEMO-stage2-visit'")[0][0]);old['goal']='ДЕМО · Изменено руководителем'
            # Use the actual manager API, never bypass record guards.
            b.context.set_offline(False)
            client=b.context.request;login=client.post(origin+'/api/login',headers={'Origin':origin},data={'username':f.get('logins',{}).get('manager','manager'),'password':f['accounts']['manager']}).json()
            response=client.post(origin+'/api/changes',headers={'Origin':origin,'X-CSRF-Token':login['csrf']},data={'mutationId':secrets.token_hex(16),'changes':[{'collection':'visits','id':'DEMO-stage2-visit','expectedVersion':1,'data':old}]});assert response.status==200
            b.login()
        else:
            sql(f,"delete from user_sites where user_id='inspector' and site_id='s1'");b.context.set_offline(False)
        before=facts(f);page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Конфликт' if kind=='conflict' else 'Ошибка')
        assert facts(f)==before and sql(f,'select count(*) from media')[0][0]==0
        assert b.local()['answers']==local['answers'] and b.local()['operationId']==local['operationId']
        b.close();page=b.open(offline=True);assert b.local()['photos']==local['photos'];b.close()
        print('PASS: '+backend+' '+kind+' refuses uploads and preserves encrypted queue after restart',flush=True)


def account_race(p,backend):
    with fixture(backend) as f,server(f) as origin:
        b=Browser(p,f,origin);page=b.open();b.prepare();ids=b.fill();b.photo(ids[0]);b.seal();photo_id=b.local()['photos'][0]['id']
        def switch_cookie(route):
            response=route.fetch();assert response.status==404
            login=b.context.request.post(origin+'/api/login',headers={'Origin':origin},data={'username':f.get('logins',{}).get('manager','manager'),'password':f['accounts']['manager']});assert login.status==200
            route.fulfill(response=response)
        page.route('**/api/receipts/'+photo_id,switch_cookie)
        before=facts(f);page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Ошибка')
        assert sql(f,'select count(*) from media')[0][0]==0 and facts(f)==before
        b.close();print('PASS: '+backend+' cookie changed AFTER preflight; expected-user server guard blocks photo POST',flush=True)


def storage_failure(p,backend):
    with fixture(backend) as f,server(f) as origin:
        b=Browser(p,f,origin);page=b.open();b.prepare();ids=b.fill()
        page.evaluate('''async url=>{const {Vault}=await import(url);window.restoreStage2Save=Vault.prototype.save;Vault.prototype.save=async()=>{throw new DOMException('DEMO quota refusal','QuotaExceededError')}}''',STORE)
        page.locator(f'[data-comment="{ids[0]}"]').fill('ДЕМО · Не потерять неподтверждённый текст')
        expect(page.locator('#message')).to_contain_text('Не сохранено на устройстве')
        page.locator('#complete').click();expect(page.locator('#message')).to_contain_text('DEMO quota refusal')
        assert b.local()['completed'] is False
        page.evaluate('''async url=>{const {Vault}=await import(url);Vault.prototype.save=window.restoreStage2Save}''',STORE)
        page.locator('#save').click();expect(page.locator('#local-status')).to_contain_text('Сохранено на устройстве')
        page.evaluate("Object.defineProperty(HTMLInputElement.prototype,'showPicker',{configurable:true,value:undefined})")
        b.photo(ids[0],camera=True)
        b.close();page=b.open(offline=True);expect(page.locator(f'[data-comment="{ids[0]}"]')).to_have_value('ДЕМО · Не потерять неподтверждённый текст');b.close()
        print('PASS: '+backend+' storage refusal never seals an unsaved queue; explicit save recovers dirty text',flush=True)


def late_conflict(p,backend):
    with fixture(backend) as f,server(f) as origin:
        b=Browser(p,f,origin);page=b.open();b.prepare();ids=b.fill();b.photo(ids[0]);b.seal();sealed=b.local()
        manager=p.request.new_context(base_url=origin,extra_http_headers={'Origin':origin})
        login=manager.post('/api/login',data={'username':f.get('logins',{}).get('manager','manager'),'password':f['accounts']['manager']}).json()
        def concurrent_change(route):
            old=json.loads(sql(f,"select data from records where collection='visits' and id='DEMO-stage2-visit'")[0][0]);old['goal']='ДЕМО · Изменено после загрузки фото'
            changed=manager.post('/api/changes',headers={'X-CSRF-Token':login['csrf']},data={'mutationId':secrets.token_hex(16),'changes':[{'collection':'visits','id':'DEMO-stage2-visit','expectedVersion':1,'data':old}]})
            assert changed.status==200
            route.fulfill(response=route.fetch())
        page.route('**/api/offline/changes',concurrent_change)
        page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Конфликт')
        local=b.local();assert local['queue']['receipt']['status']==409 and local['operationId']==sealed['operationId']
        assert sql(f,'select count(*) from media')[0][0]==1
        assert sql(f,"select count(*) from records where collection='inspections'")[0][0]==0
        assert sql(f,"select count(*) from records where collection='findings'")[0][0]==0
        before=facts(f);page.locator('#sync').click();expect(page.locator('#sync-status')).to_have_text('Конфликт');assert facts(f)==before
        b.close();manager.dispose();page=b.open(offline=True);assert b.local()['queue']['receipt']==local['queue']['receipt'];b.close()
        print('PASS: '+backend+' server expectedVersion conflict AFTER photo commit rolls back whole act; durable 409 and local photo retained',flush=True)


def release_online(p,backend):
    with fixture(backend) as f,server(f) as origin:
        browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
        try:
            for role in ('manager','inspector'):
                context=browser.new_context();page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(origin+'/');page.locator('#username').fill(f['logins'][role]);page.locator('#password').fill(f['accounts'][role]);page.locator('#login-form button').click()
                expect(page.locator('.sidebar')).to_be_visible()
                for section in ('sites','audits','schedule','findings','inventory','reports','settings'):
                    page.locator('nav [data-nav="'+section+'"]').click()
                    expect(page.locator('.workspace')).to_be_visible()
                assert not errors,errors
                assert 'calendar.js' not in page.content() and 'writeoff_analytics.js' not in page.content()
                assert page.locator('.demo-badge').count()==0
                if role=='inspector':expect(page.locator('a[href="/offline-visit/"]')).to_have_count(1)
                context.close()
        finally:browser.close()
        print('PASS: '+backend+' deployed online navigation, both existing renamed accounts, no demo/extra feature UI',flush=True)


def main():
    global RELEASE
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=('sqlite','postgres'),required=True);parser.add_argument('--release',action='store_true');args=parser.parse_args();RELEASE=args.release
    with sync_playwright() as p:
        if RELEASE:release_online(p,args.backend)
        full_visit(p,args.backend)
        for kind in ('conflict','revoked'):refusal(p,args.backend,kind)
        account_race(p,args.backend);storage_failure(p,args.backend)
        late_conflict(p,args.backend)


if __name__=='__main__':main()
