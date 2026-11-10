"""Viewer against the REAL release image/SQLite, never the host Python's sqlite3.

The pinned a150419 image must reproduce the old authorizer/json_each failure;
the candidate must read all permitted data with the same authorizer still active.
All databases, credentials, ports, certificates and containers are disposable synthetic fixtures.
"""
import argparse
import base64
from contextlib import ExitStack,closing
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import tempfile
import time

import httpx
from PIL import Image
from server.pg_admin import fingerprints
from tests.pg_environment import docker,unused_port
from tests.release_acceptance_seed import initialize


PROBE=r'''
import json,os,sqlite3
from pathlib import Path
from server.database import Database
from server.viewer import media_links_sql
result={'sqlite_version':sqlite3.sqlite_version,'release_sha':Path('/app/RELEASE_SHA').read_text().strip()}
db=Database(os.environ['KSU_DB']);actor=db.user('demo-viewer')
with db.connect() as con:
    result['json_each_without_actor']=con.execute("select value from json_each('[1]')").fetchone()[0]==1
# Fresh actor connections: no cache warming or authorizer replacement.
for key,query,params in [
 ('json_each_with_viewer',"select value from json_each('[1]')",()),
 ('media_links_with_viewer',media_links_sql(),(actor['id'],))]:
    try:
        with db.connect(actor) as con:con.execute(query,params).fetchall()
        result[key]=True
    except sqlite3.DatabaseError as error:
        result[key]=False;result[key+'_error']=str(error)
for key,query in [
 ('ddl', 'create table viewer_must_not_create(x)'),
 ('dml', "update users set name=name where id='viewer'"),
 ('pragma', 'pragma user_version=1'),
 ('attach', "attach ':memory:' as viewer_must_not_attach")]:
    try:
        with db.connect(actor) as con:con.execute(query)
        result['authorizer_denies_'+key]=False
    except sqlite3.DatabaseError:result['authorizer_denies_'+key]=True
print(json.dumps(result))
'''


def seed_http(clients):
    owner=clients['manager'];inspector=clients['inspector']
    def change(client,collection,ident,data):
        reply=client.post('/api/changes',json={'mutationId':'demo-runtime-'+ident,'changes':[
            {'collection':collection,'id':ident,'expectedVersion':0,'data':data}]})
        assert reply.status_code==200,(collection,reply.text)
    def photo(site,color):
        buffer=io.BytesIO();Image.new('RGB',(60,40),color).save(buffer,'PNG')
        reply=owner.post('/api/media',json={'siteId':site,'content':base64.b64encode(buffer.getvalue()).decode()})
        assert reply.status_code==200,reply.text
        return reply.json()['url']
    urls={key:photo(site,color) for key,site,color in [
        ('act','s1','blue'),('finding','s1','green'),('evidence','s1','red'),
        ('foreign','s2','yellow'),('orphan','s1','purple')]}
    questions=inspector.get('/api/state').json()['questions']
    change(inspector,'inspections','demo-runtime-act',{'siteId':'s1','date':'2026-11-10','inspector':'ДЕМО',
        # The photo belongs ONLY to the LAST inspection result, not to a finding.
        'results':[{'questionId':q['id'],'status':'ok','comment':'ДЕМО · Runtime check detail',
                    'photo':urls['act'] if index==len(questions)-1 else ''} for index,q in enumerate(questions)]})
    change(inspector,'findings','demo-runtime-finding',{'siteId':'s1','title':'ДЕМО · Runtime finding',
        'description':'ДЕМО · Runtime finding detail','owner':'PRIVATE-SYNTHETIC-PERSON',
        'photo':urls['finding'],'evidence':urls['evidence'],'resolution':'ДЕМО · Runtime resolution'})
    change(owner,'findings','demo-runtime-foreign',{'siteId':'s2','title':'ДЕМО · Foreign site','photo':urls['foreign']})
    change(owner,'inventories','demo-runtime-inventory',{'siteId':'s1','date':'2026-11-10','title':'ДЕМО · Runtime inventory'})
    change(owner,'inventoryLines','demo-runtime-line',{'inventoryId':'demo-runtime-inventory',
        'item':'ДЕМО · Runtime item','expected':3,'actual':2,'price':12.5,'owner':'PRIVATE-SYNTHETIC-PERSON'})
    return urls


def browser_read(origin,cert,accounts):
    from playwright.sync_api import sync_playwright,expect
    # Trust exactly this ephemeral synthetic certificate, not all TLS errors.
    public=subprocess.run(['openssl','x509','-in',str(cert),'-pubkey','-noout'],check=True,capture_output=True).stdout
    der=subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=public,check=True,capture_output=True).stdout
    pin=base64.b64encode(hashlib.sha256(der).digest()).decode()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),
            args=['--no-sandbox','--ignore-certificate-errors-spki-list='+pin])
        try:
            for width in (1280,390):
                context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                page=context.new_page();errors=[];failures=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('response',lambda reply:failures.append(reply.status) if '/api/' in reply.url and reply.status>=500 else None)
                page.goto(origin+'/');page.locator('#username').fill('demo-viewer')
                page.locator('#password').fill(accounts['viewer']);page.locator('#login-form button').click()
                expect(page.locator('.sidebar')).to_be_attached()
                assert page.locator('.nav [data-nav]').count()==8
                page.evaluate("navTo('sites','s1')");expect(page.locator('.page-heading h2')).to_contain_text('ДЕМО · s1')
                page.evaluate("navTo('audits')");page.locator('[data-action="audit-detail"][data-id="demo-runtime-act"]').click()
                expect(page.locator('.modal')).to_contain_text('ДЕМО · Runtime check detail')
                expect(page.locator('.modal .photo-preview')).to_have_js_property('naturalWidth',60,timeout=10000)
                page.locator('[data-action="close-modal"]').first.click()
                page.evaluate("navTo('findings')");page.locator('[data-action="finding-detail"][data-id="demo-runtime-finding"]').first.click()
                expect(page.locator('#finding-form textarea[name="description"]')).to_have_value('ДЕМО · Runtime finding detail')
                assert page.locator('.modal input:enabled,.modal select:enabled,.modal textarea:enabled').count()==0
                assert page.locator('.modal input[type="file"],[data-action="save-finding"]').count()==0
                assert page.locator('.modal .photo-preview').count()==2
                for img in page.locator('.modal .photo-preview').all():expect(img).to_have_js_property('naturalWidth',60,timeout=10000)
                page.locator('[data-action="close-modal"]').first.click()
                page.evaluate("navTo('reports')");page.locator('[data-report-field="kind"]').select_option('audits')
                for action in ('export-report','report-xlsx','report-pdf'):
                    with page.expect_download() as download:page.locator('[data-action="'+action+'"]').click()
                    assert download.value.failure() is None
                assert not errors,errors
                assert not failures,failures
                context.close()
        finally:browser.close()


def run(image,expect_broken=False,browser=False):
    name='ksu-viewer-runtime-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='ksu-viewer-runtime-synthetic-') as directory:
        root=Path(directory);data=root/'data';data.mkdir();accounts=initialize(data)
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=KSU synthetic viewer runtime','-addext','subjectAltName=IP:127.0.0.1,DNS:localhost',
            '-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        port=unused_port();assert port not in (8003,8018,8019)
        origin=f'https://127.0.0.1:{port}';trust=ssl.create_default_context(cafile=str(cert))
        try:
            docker('run','-d','--pull','never','--name',name,'--user',f'{os.getuid()}:{os.getgid()}',
                '--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--cap-drop','ALL','--security-opt','no-new-privileges',
                '-p',f'127.0.0.1:{port}:8000','-v',str(data)+':/data:rw',
                '-v',str(cert)+':/tls/cert.pem:ro','-v',str(key)+':/tls/key.pem:ro',
                '-e','KSU_OFFLINE_MODE=pilot','-e','KSU_OFFLINE_DEPLOYMENT_ID='+'d'*32,
                '-e','KSU_OFFLINE_USER_IDS=inspector','-e','KSU_DB=/data/ksu.sqlite3','-e','KSU_ORIGIN='+origin,
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',image,stdout=subprocess.DEVNULL)
            with ExitStack() as stack:
                clients={role:stack.enter_context(httpx.Client(base_url=origin,verify=trust,trust_env=False,timeout=30,headers={'Origin':origin})) for role in accounts}
                for _ in range(300):
                    try:
                        if clients['manager'].get('/api/health').status_code==200:break
                    except httpx.TransportError:pass
                    time.sleep(.1)
                else:raise AssertionError('Synthetic release viewer container did not start')
                for role,client in clients.items():
                    login=client.post('/api/login',json={'username':'demo-'+role,'password':accounts[role]})
                    assert login.status_code==200,(role,login.text)
                    client.headers['X-CSRF-Token']=login.json()['csrf']
                urls=seed_http(clients)
                with closing(sqlite3.connect(data/'ksu.sqlite3')) as con:before=fingerprints(con)
                probe=docker('exec','-i',name,'python','-',input=PROBE,capture_output=True,text=True)
                result=json.loads(probe.stdout);result.update(synthetic_only=True,image=image)
                assert result['json_each_without_actor']
                assert all(result['authorizer_denies_'+kind] for kind in ('ddl','dml','pragma','attach'))
                viewer=clients['viewer'];response=viewer.get('/api/state')
                if expect_broken:
                    assert result['release_sha']=='a150419d0bfd6afa7546b0578094f57eee62672c',result
                    assert result['sqlite_version']=='3.40.1',result
                    assert response.status_code==503,response.status_code
                    assert not result['media_links_with_viewer'],result
                    assert 'vtable constructor failed: json_each' in result.get('media_links_with_viewer_error',''),result
                    result.update(reproduced_a150419_http_503=True,viewer_state_status=503)
                    return result
                assert result['media_links_with_viewer'],result
                assert response.status_code==200,response.text
                state=response.json();assert [row['id'] for row in state['sites']]==['s1']
                act=next(row for row in state['inspections'] if row['id']=='demo-runtime-act')
                assert len(act['results'])==22 and act['results'][-1]['photo'].startswith(urls['act']+'?expectedUser=')
                assert act['inspector']=='' and 'PRIVATE-SYNTHETIC-PERSON' not in response.text
                for role in ('manager','inspector'):assert clients[role].get('/api/state').status_code==200
                assert not clients['unassigned'].get('/api/state').json()['sites']
                for kind in ('act','finding','evidence'):
                    for suffix in ('','?thumbnail=true'):assert viewer.get(urls[kind]+suffix).status_code==200
                for kind in ('foreign','orphan'):
                    for suffix in ('','?thumbnail=true'):assert viewer.get(urls[kind]+suffix).status_code==404
                pinned=act['results'][-1]['photo']
                assert viewer.get(pinned).status_code==200 and clients['manager'].get(pinned).status_code==403
                for extension in ('json','csv','xlsx','pdf'):
                    for kind in ('audits','findings','inventory','weekly'):
                        report=viewer.get('/api/reports/'+extension,params={'kind':kind,'site':'s1'})
                        assert report.status_code==200,(kind,extension,report.status_code)
                    assert viewer.get('/api/reports/'+extension+'?site=s2').status_code==404
                for path in ('/api/changes','/api/media','/api/import/preview','/api/import/commit','/api/offline/packages','/api/offline/changes','/api/admin/backup'):
                    for method in ('POST','PUT','PATCH','DELETE'):assert viewer.request(method,path,json={}).status_code==403,(method,path)
                assert not viewer.get('/api/offline-visit/context').json()['enabled']
                if browser:browser_read(origin,cert,accounts)
                with closing(sqlite3.connect(data/'ksu.sqlite3')) as con:assert fingerprints(con)==before
                revoke="from server.database import Database; db=Database('/data/ksu.sqlite3');\nwith db.connect() as con:con.execute(\"delete from user_sites where user_id='viewer'\")"
                docker('exec',name,'python','-c',revoke,stdout=subprocess.DEVNULL)
                assert viewer.get(pinned).status_code==404 and viewer.get('/api/reports/xlsx?site=s1').status_code==404
                assert viewer.get('/api/state').status_code==200 and viewer.get('/api/state').json()['sites']==[]
                for role in ('manager','inspector'):assert clients[role].get(urls['act']).status_code==200
                result.update(viewer_state_status=200,object_act_findings_photos_reports=True,
                    direct_writes_forbidden=True,foreign_orphan_and_revoked_photos_forbidden=True,
                    business_tables_unchanged_by_reads=True,manager_inspector_preserved=True,
                    desktop_mobile_browser=browser,tls_verified=True)
                return result
        finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    parser.add_argument('--expect-broken',action='store_true');parser.add_argument('--browser',action='store_true');parser.add_argument('--output')
    args=parser.parse_args();report=run(args.image,args.expect_broken,args.browser)
    if args.output:Path(args.output).write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
