"""Synthetic clean production setup + focused checklist/offline tests, never owner data."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from server.app import create_app
from server.database import password_valid
from server.pg_admin import initialize as schema
from server.production_init import initialize,FORMAT
from server.managed_pg import verify_report
from server.pg_backup import backup_database,restore_database
from server.release_schema import checksum
from tests.managed_pg_environment import environment
from tests.managed_pg_acceptance import facts
from tests.test_production_init import approved
from tests.pg_environment import docker,unused_port


def change(client,collection,ident,data,version=0):
    return client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[{'collection':collection,'id':ident,'expectedVersion':version,'data':data}]})


def contracts(clients,credentials):
    for role,client in clients.items():
        account=credentials[role];reply=client.post('/api/login',json={'username':account['username'],'password':account['password']});assert reply.status_code==200
        client.headers['X-CSRF-Token']=reply.json()['csrf']
    owner=clients['manager'];field=clients['inspector'];state=owner.get('/api/state').json();restricted=field.get('/api/state').json()
    assert state['meta']['mode']=='work' and len(state['sites'])==2 and [s['id'] for s in restricted['sites']]==['qa-site']
    assert len(state['questions'])==22 and not state['inspections'] and not state['findings'] and not state['inventories']
    assert owner.get('/api/users').status_code==404
    old=field.get('/api/checklists/current').json();first=old['questions'][0];second=old['questions'][1]
    assert change(field,'questions',first['id'],{**first,'title':'СИНТЕТИКА · forbidden'},first['version']).status_code==403
    # Preserve a completed act/finding and a not-yet-submitted package across template changes.
    act={'siteId':'qa-site','date':'2026-11-10','inspector':'СИНТЕТИКА','checklistVersion':old['id'],
        'results':[{'questionId':q['id'],'status':'fail' if q['id']==first['id'] else 'ok','comment':'СИНТЕТИКА · Описание'} for q in old['questions']]}
    finding={'siteId':'qa-site','inspectionId':'qa-closed','questionId':first['id'],'section':first['section'],'title':first['title'],'description':'СИНТЕТИКА'}
    response=field.post('/api/changes',json={'mutationId':'qa-completed','changes':[
        {'collection':'inspections','id':'qa-closed','expectedVersion':0,'data':act},
        {'collection':'findings','id':'qa-finding','expectedVersion':0,'data':finding}]});assert response.status_code==200
    assert change(owner,'visits','qa-plan',{'siteId':'qa-site','date':'2026-11-10','assignedTo':credentials['inspector']['id']}).status_code==200
    visit=next(v for v in field.get('/api/state').json()['visits'] if v['id']=='qa-plan')
    package=field.post('/api/offline/packages',json={'siteIds':['qa-site']}).json();assert package['checklist']==old
    body={'mutationId':'qa-old-queued','packageId':package['id'],'changes':[
        {'collection':'inspections','id':'qa-queued-act','expectedVersion':0,'data':{**act,'results':[{**r,'status':'ok'} for r in act['results']]}},
        {'collection':'visits','id':visit['id'],'expectedVersion':visit['version'],'data':{**visit,'status':'completed','inspectionId':'qa-queued-act'}}]}
    assert change(owner,'questions',first['id'],{**first,'title':'СИНТЕТИКА · Изменённый вопрос','section':'СИНТЕТИКА · Новый раздел','archived':True},first['version']).status_code==200
    assert change(owner,'questions',second['id'],{**second,'position':0},second['version']).status_code==200
    assert change(owner,'questions','qa-added',{'section':'СИНТЕТИКА · Новый раздел','title':'СИНТЕТИКА · Новый пункт','position':99}).status_code==200
    current=field.get('/api/checklists/current').json();assert current['id']!=old['id'] and current['questions'][0]['id']==second['id']
    assert first['id'] not in {q['id'] for q in current['questions']} and len(current['questions'])==22
    assert field.get('/api/checklists/'+old['id']).json()==old
    before=next(a for a in field.get('/api/state').json()['inspections'] if a['id']=='qa-closed')
    assert before['results'][0]['title']==first['title'] and before['checklistVersion']==old['id']
    assert next(f for f in field.get('/api/state').json()['findings'] if f['id']=='qa-finding')['questionId']==first['id']
    receipt=field.post('/api/offline/changes',json=body);assert receipt.status_code==200
    assert field.post('/api/offline/changes',json=body).json()==receipt.json()
    assert field.get('/api/receipts/qa-old-queued').json()==receipt.json()
    # Last-active removal and stale edits must roll back, preserving the catalog.
    rows=[q for q in owner.get('/api/state').json()['questions'] if not q.get('archived')]
    request={'mutationId':'qa-remove-all','changes':[{'collection':'questions','id':q['id'],'expectedVersion':q['version'],'data':{**{k:v for k,v in q.items() if k not in ('id','version','createdAt','updatedAt','createdBy')},'archived':True}} for q in rows]}
    assert owner.post('/api/changes',json=request).status_code==422
    assert field.get('/api/checklists/current').json()==current
    assert change(owner,'questions',first['id'],{**first,'title':'stale'},first['version']).status_code==409
    for extension in ('csv','xlsx','pdf'):
        report=owner.get('/api/reports/'+extension);assert report.status_code==200 and 'demo' not in report.headers['content-disposition'].lower()
    return body,receipt.json(),old


def browser_check(origin,cert,credentials):
    from playwright.sync_api import sync_playwright,expect
    import base64,hashlib,shutil
    public=subprocess.run(['openssl','x509','-in',str(cert),'-pubkey','-noout'],check=True,capture_output=True).stdout
    der=subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=public,check=True,capture_output=True).stdout
    pin=base64.b64encode(hashlib.sha256(der).digest()).decode()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox','--ignore-certificate-errors-spki-list='+pin])
        try:
            for role in ('manager','inspector'):
                context=browser.new_context();page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(type(error).__name__))
                page.goto(origin);page.locator('#username').fill(credentials[role]['username']);page.locator('#password').fill(credentials[role]['password']);page.locator('#login-form button').click()
                expect(page.locator('.sidebar')).to_be_attached();page.locator('[data-nav="settings"]').click()
                if role=='inspector':
                    assert page.locator('#checklist-editor').count()==0
                    page.evaluate("navTo('schedule')")
                    page.locator('[data-action="start-audit"][data-visit="'+ui_plan+'"]').click()
                    expect(page.locator('[data-answer]').first).to_be_visible()
                    pinned=page.evaluate('ui.draft.checklistVersion');question_ids=page.evaluate('ui.draft.questions.map(q=>q.id)')
                    with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,headers={'Origin':origin},timeout=30) as owner:
                        a=credentials['manager'];login=owner.post('/api/login',json={'username':a['username'],'password':a['password']});assert login.status_code==200
                        owner.headers['X-CSRF-Token']=login.json()['csrf'];q=next(q for q in owner.get('/api/state').json()['questions'] if q['id']==question_ids[0])
                        assert change(owner,'questions',q['id'],{**q,'title':'СИНТЕТИКА · После начала черновика'},q['version']).status_code==200
                    for qid in question_ids:page.locator('[data-answer="'+qid+'"][data-value="ok"]').click()
                    page.route('**/api/changes',lambda route:route.abort())
                    page.locator('[data-action="save-audit"]').click()
                    expect(page.locator('[data-action="retry-save"]')).to_be_visible()
                    assert page.evaluate("pending.changes.find(c=>c.collection==='inspections').data.checklistVersion")==pinned
                    page.unroute('**/api/changes')
                    with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:page.locator('[data-action="retry-save"]').click()
                    assert saved.value.status==200
                    assert not errors;context.close();continue
                expect(page.locator('#checklist-editor')).to_be_visible()
                page.locator('[data-action="checklist-add"]').click();page.locator('#checklist-question-form [name="section"]').fill('СИНТЕТИКА · UI раздел');page.locator('#checklist-question-form [name="title"]').fill('СИНТЕТИКА · UI вопрос')
                with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:page.locator('[data-action="checklist-save"]').click()
                assert saved.value.status==200
                row=page.locator('[data-checklist-id]').filter(has_text='СИНТЕТИКА · UI вопрос');expect(row).to_be_visible();ident=row.get_attribute('data-checklist-id')
                row.locator('[data-action="checklist-edit"]').click();page.locator('#checklist-question-form [name="title"]').fill('СИНТЕТИКА · UI обновлён')
                with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:page.locator('[data-action="checklist-save"]').click()
                assert saved.value.status==200
                page.once('dialog',lambda dialog:dialog.accept())
                with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:page.locator('[data-checklist-id="'+ident+'"] [data-action="checklist-archive"]').click()
                assert saved.value.status==200
                page.locator('[data-action="checklist-archives"]').click();row=page.locator('[data-checklist-id="'+ident+'"]');expect(row).to_contain_text('UI обновлён')
                with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:row.locator('[data-action="checklist-restore"]').click()
                assert saved.value.status==200
                button=page.locator('[data-action="checklist-down"]:enabled').first
                if button.count():
                    with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:button.click()
                    assert saved.value.status==200
                # First real-account planning must use assigned user IDs, not 'inspector'.
                page.evaluate("navTo('schedule')");page.locator('[data-action="add-visit"]').click()
                page.locator('#visit-form [name="siteId"]').select_option('qa-site')
                page.locator('#visit-form [name="date"]').fill('2026-11-10')
                page.locator('#visit-form [name="assignedTo"]').select_option(credentials['inspector']['id'])
                page.locator('#visit-form [name="goal"]').fill('СИНТЕТИКА · UI план')
                with page.expect_response(lambda r:'/api/changes' in r.url and r.request.method=='POST') as saved:page.locator('[data-action="save-visit"]').click()
                assert saved.value.status==200
                ui_plan=next(v['id'] for v in saved.value.json()['visits'] if v['goal']=='СИНТЕТИКА · UI план')
                assert not errors;context.close()
        finally:browser.close()


def run(image=None,sha=None,one_cluster=False,browser=False,upstream=False):
    with environment(one_cluster) as env:
        root=env['root'];out=root/'private-output';out.mkdir();data=root/'archive';data.mkdir();archive=data/'ksu.sqlite3';archive.write_bytes(b'SYNTHETIC ARCHIVE ONLY - MUST NEVER BE OPENED OR MIGRATED');archive_hash=checksum(archive)
        config=approved();config['sites'].append({'id':'qa-other','name':'СИНТЕТИКА · Чужой объект'})
        config_file=root/'private-setup.json';config_file.write_text(json.dumps(config));name='ksu-clean-'+str(os.getpid());port=unused_port();origin=f'https://127.0.0.1:{port}'
        common=['--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g','--tmpfs','/tmp:rw,nosuid,nodev,size=768m',
            '--cap-drop','ALL','--security-opt','no-new-privileges','-v',str(env['pki'])+':/tls:ro','-v',str(out)+':/output:rw','-v',str(data)+':/data:ro','-v',str(config_file)+':/input/setup.json:ro']
        def command(kind,role,operation,*args,check=True):
            key='KSU_PG_BACKUP_DSN' if role=='backup' else 'KSU_PG_ADMIN_DSN';path=root/(kind+'-'+role+'.env');path.write_text(key+'='+env[kind]['container_'+role]+'\n');path.chmod(0o600)
            return docker('run','--rm','--pull','never','--network',env['network'],*common,'--env-file',str(path),'--entrypoint','python',image,'-m','server.managed_pg',operation,*args,check=check,capture_output=True,text=True)
        credentials=None
        def start(kind):
            path=root/'runtime.env';path.write_text('KSU_DATABASE_URL='+env[kind]['container_runtime']+'\n');path.chmod(0o600)
            app_common=[];parts=iter(common)
            for part in parts:
                if part=='-v':
                    mount=next(parts)
                    if mount.endswith(':/output:rw') or mount.endswith(':/input/setup.json:ro'):continue
                    app_common.extend([part,mount])
                else:app_common.append(part)
            docker('run','-d','--pull','never','--name',name,'--network',env['network'],*app_common,'--env-file',str(path),'-p',f'127.0.0.1:{port}:8000',
                '-e','KSU_ORIGIN='+origin,'-e','KSU_OFFLINE_MODE=pilot','-e','KSU_OFFLINE_DEPLOYMENT_ID='+'e'*32,'-e','KSU_OFFLINE_USER_IDS='+credentials['inspector']['id'],
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',image,stdout=subprocess.DEVNULL)
            with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False,timeout=5) as client:
                for _ in range(150):
                    try:
                        r=client.get('/api/health')
                        if r.status_code==200:
                            assert r.json()['build']==sha and r.json()['backend']=='postgresql';return
                    except httpx.TransportError:pass
                    time.sleep(.1)
            docker('logs','--tail','15',name,check=False);raise AssertionError('Clean runtime did not start')
        def stop():docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            if image:
                command('source','admin','init-schema');command('source','admin','init-production','--source','/input/setup.json','--output','/output/production')
            else:
                schema(env['source']['admin'],'ksu_runtime','ksu_backup')
                # Managed Codex marks all /tmp as Git; only this synthetic host harness
                # substitutes its fixture path. Real image/Windows guards run unchanged.
                with patch('server.production_init.output_directory',side_effect=lambda p:Path(p)):
                    initialize(env['source']['admin'],config,out/'production')
            credentials=json.loads((out/'production/credentials.json').read_text())['accounts'];report=out/'production/setup-report.json'
            assert len(credentials)==2 and (out/'production/credentials.json').stat().st_mode&0o777==0o600
            assert verify_report(env['source']['backup'],report)['verified']
            before=facts(env['source']['admin']);assert before['users']['count']==2 and before['media']['count']==0 and before['operation_receipts']['count']==0
            if image:
                retry=command('source','admin','init-production','--source','/input/setup.json','--output','/output/rejected',check=False);assert retry.returncode!=0
            else:
                try:
                    with patch('server.production_init.output_directory',side_effect=lambda p:Path(p)):
                        initialize(env['source']['admin'],config,out/'rejected')
                except ValueError:pass
                else:raise AssertionError('Repeated initialization permitted')
            assert not (out/'rejected/credentials.json').exists() and facts(env['source']['admin'])==before
            with psycopg.connect(env['source']['admin']) as con:
                for account in credentials.values():
                    stored=con.execute('select password from ksu.users where id=%s',(account['id'],)).fetchone()[0];assert stored!=account['password'] and password_valid(account['password'],stored)
                audit=''.join(r[0] or '' for r in con.execute('select after_json from ksu.audit'))
                assert all(a['password'] not in audit for a in credentials.values())
            if image:
                start('source')
                with ExitStack() as stack:
                    clients={role:stack.enter_context(httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False,headers={'Origin':origin},timeout=30)) for role in credentials}
                    body,receipt,old=contracts(clients,credentials)
                if browser:browser_check(origin,env['pki']/'cert.pem',credentials)
                command('source','backup','backup','--output','/output/clean.ksubackup');stop()
                command('restored','admin','restore','--source','/output/clean.ksubackup','--output','/output/restore.json')
            else:
                with patch.dict(os.environ,{'KSU_DATABASE_URL':env['source']['runtime'],'KSU_WORK_DIR':str(root/'work'),'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_DEPLOYMENT_ID':'e'*32,'KSU_OFFLINE_USER_IDS':credentials['inspector']['id']}):
                    app=create_app('postgresql','http://testserver',False)
                    try:
                        with ExitStack() as stack:body,receipt,old=contracts({role:stack.enter_context(TestClient(app,headers={'Origin':'http://testserver'})) for role in credentials},credentials)
                    finally:app.state.import_jobs.close()
                backup_database(env['source']['backup'],out/'clean.ksubackup');restore_database(out/'clean.ksubackup',env['restored']['admin'],'ksu_runtime','ksu_backup')
            assert facts(env['source']['admin'])==facts(env['restored']['admin'])
            if image:
                start('restored')
                with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False,headers={'Origin':origin},timeout=30) as client:
                    a=credentials['inspector'];login=client.post('/api/login',json={'username':a['username'],'password':a['password']});assert login.status_code==200;client.headers['X-CSRF-Token']=login.json()['csrf']
                    assert client.post('/api/offline/changes',json=body).json()==receipt and client.get('/api/checklists/'+old['id']).json()==old
                stop()
                # Both legacy seeding and migration must refuse in this production image.
                bad=command('source','admin','migrate','--source','/data/ksu.sqlite3','--output','/output/forbidden.json',check=False);assert bad.returncode!=0
                guard=docker('run','--rm','--network','none',*common,'--entrypoint','python',image,'-m','server','init-demo','--generate','--db','/tmp/should-not-exist.sqlite3',check=False,capture_output=True,text=True);assert guard.returncode!=0
                assert all(a['password'] not in guard.stdout+guard.stderr for a in credentials.values())
            assert checksum(archive)==archive_hash
            if upstream:
                settings={'KSU_TEST_PG_ADMIN_DSN':env['source']['root'],'KSU_TEST_PG_RUNTIME_DSN':make_conninfo(**{**conninfo_to_dict(env['source']['runtime']),'dbname':'postgres'}),
                    'KSU_TEST_PG_BACKUP_DSN':make_conninfo(**{**conninfo_to_dict(env['source']['backup']),'dbname':'postgres'}),'KSU_TEST_PG_RESTORE_ADMIN_DSN':env['restored']['root'],
                    'KSU_TEST_PG_RESTORE_RUNTIME_DSN':make_conninfo(**{**conninfo_to_dict(env['restored']['runtime']),'dbname':'postgres'})}
                modules=['tests.test_offline_stage1.PostgreSQLOffline','tests.test_offline_stage2_server.PostgreSQLStage2','tests.test_postgres','tests.test_postgres_versions','tests.test_postgres_analytics','tests.test_offline_release.PostgreSQLRelease','tests.test_viewer.PostgreSQLViewer']
                subprocess.run([sys.executable,'-m','unittest',*modules,'-v'],env={**os.environ,**settings},check=True)
            return {'synthetic_only':True,'source_sha':sha,'clean_initialization':True,'accounts':2,'private_generated_passwords':True,'repeat_refused_unchanged':True,
                'archive_sqlite_untouched':True,'manager_inspector_acl':True,'checklist_edit_order_archive':True,'completed_acts_and_old_packages_preserved':True,
                'backup_restore_all_16_tables':True,'independent_restore':not one_cluster,'retry_after_restore':bool(image),'focused_checklist_ui':browser,'full_ui_pwa_repeated':False}
        finally:stop()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--image');p.add_argument('--sha');p.add_argument('--one-cluster',action='store_true');p.add_argument('--browser',action='store_true');p.add_argument('--upstream',action='store_true');p.add_argument('--output')
    args=p.parse_args();result=run(args.image,args.sha,args.one_cluster,args.browser,args.upstream)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
