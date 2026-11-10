"""Real PostgreSQL migration/restore and role/offline API contracts; no UI/PWA replay."""
from contextlib import ExitStack,closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
from unittest.mock import patch

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from fastapi.testclient import TestClient
from server.app import create_app
from server.managed_pg import migrate,preflight,verify_report
from server.maintenance import snapshot_database
from server.pg_admin import fingerprints
from server.pg_backup import backup_database,restore_database
from server.release_schema import checksum,inspect_sqlite
from server.release_backup import snapshot_frozen
from tests import test_api
from tests.test_viewer import Viewer
from tests.test_offline_release import Release
from tests.pg_environment import docker,unused_port
from tests.managed_pg_environment import environment


def frozen_fixture(target):
    case=Viewer();case.setUp()
    try:
        photo,foreign,orphan=case.detailed_fixture()
        assert case.change(case.manager,'visits',{'siteId':'s1','date':'2026-11-10','assignedTo':'inspector'},'synthetic-managed-plan').status_code==200
        photo_request=Release.picture(case);receipt=case.inspector.post('/api/media',json=photo_request).json()
        package=case.inspector.post('/api/offline/packages',json={'siteIds':['s1']}).json()
        visit=next(v for v in case.inspector.get('/api/state').json()['visits'] if v['id']=='synthetic-managed-plan')
        snapshot_database(case.app.state.db.path,target)
        return {'accounts':{'account-owner':test_api.MANAGER_PASSWORD,'field-inspector':test_api.INSPECTOR_PASSWORD,
            'synthetic-viewer':'synthetic viewer password'},'photo':photo,'foreign':foreign,'orphan':orphan,
            'photo_request':photo_request,'receipt':receipt,'package':package,'visit':visit}
    finally:case.tearDown()


def facts(dsn):
    with psycopg.connect(dsn) as con:
        con.execute('set transaction isolation level repeatable read read only')
        return fingerprints(con,True)


def legacy_fixture(root,target):
    from tests.release_upgrade import BASE,OLD_FIXTURE
    old=root/'old-source';old.mkdir();frozen=root/'old-frozen';frozen.mkdir()
    archive=subprocess.run(['git','archive',BASE],check=True,capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(old,filter='data')
    config=root/'private-legacy.json'
    config.write_text(json.dumps({'database':str(frozen/'ksu.sqlite3'),'manager':test_api.MANAGER_PASSWORD,'inspector':test_api.INSPECTOR_PASSWORD}))
    subprocess.run([sys.executable,'-c',OLD_FIXTURE],cwd=old,env={**os.environ,'KSU_UPGRADE_CONFIG':str(config)},check=True,capture_output=True)
    snapshot_frozen(frozen,target,confirmed=True)
    assert inspect_sqlite(target)['version']==0
    # Read-only inspection may create empty WAL-index sidecars; this is our owned
    # synthetic copy. Publish one fresh closed snapshot for the standalone input.
    closed=target.with_name('legacy-closed.sqlite3');snapshot_database(target,closed)
    return closed


def api_contracts(clients,fixture,repeat=False):
    owner,inspector,viewer=(clients[name] for name in ('account-owner','field-inspector','synthetic-viewer'))
    for name,client in clients.items():
        reply=client.post('/api/login',json={'username':name,'password':fixture['accounts'][name]});assert reply.status_code==200,(name,reply.status_code)
        client.headers['X-CSRF-Token']=reply.json()['csrf']
    state=viewer.get('/api/state');assert state.status_code==200
    assert [s['id'] for s in state.json()['sites']]==['s1'] and 'PRIVATE-' not in state.text
    act=next(row for row in state.json()['inspections'] if row['id']=='demo-viewer-act');assert len(act['results'])==22
    assert viewer.get(fixture['photo']).status_code==200
    for url in (fixture['foreign'],fixture['orphan']):assert viewer.get(url).status_code==404
    for path in ('/api/changes','/api/media','/api/import/preview','/api/offline/packages','/api/offline/changes','/api/admin/backup'):
        for method in ('POST','PUT','PATCH','DELETE'):assert viewer.request(method,path,json={}).status_code==403,(method,path)
    for extension in ('pdf','xlsx','json','csv'):
        assert viewer.get('/api/reports/'+extension+'?kind=audits&site=s1').status_code==200
        assert viewer.get('/api/reports/'+extension+'?site=s2').status_code==404
    for client in (owner,inspector):assert client.get('/api/state').status_code==200 and client.get(fixture['photo']).status_code==200
    saved=inspector.get('/api/receipts/'+fixture['photo_request']['mutationId']);assert saved.status_code==200 and saved.json()==fixture['receipt']
    assert inspector.post('/api/media',json=fixture['photo_request']).json()==fixture['receipt']
    visit=fixture['visit'];package=fixture['package'];ident='synthetic-managed-act'
    body={'mutationId':'synthetic-managed-complete','packageId':package['id'],'changes':[
        {'collection':'inspections','id':ident,'expectedVersion':0,'data':{'siteId':'s1','date':visit['date'],'inspector':'ДЕМО · Managed inspector',
            'checklistVersion':package['checklist']['id'],'results':[{'questionId':q['id'],'status':'ok'} for q in package['checklist']['questions']]}},
        {'collection':'visits','id':visit['id'],'expectedVersion':visit['version'],'data':{**visit,'status':'completed','inspectionId':ident}}]}
    first=inspector.post('/api/offline/changes',json=body);assert first.status_code==200,first.text
    assert inspector.post('/api/offline/changes',json=body).json()==first.json()
    assert inspector.get('/api/receipts/'+body['mutationId']).json()==first.json()
    if not repeat:
        for client,ident in ((owner,'synthetic-owner-write'),(inspector,'synthetic-inspector-write')):
            response=client.post('/api/changes',json={'mutationId':ident,'changes':[{'collection':'findings','id':ident,'expectedVersion':0,
                'data':{'siteId':'s1','title':'ДЕМО · Managed role write'}}]});assert response.status_code==200,response.text
    return first.json()


def run(image=None,sha=None,one_cluster=False,contracts=False):
    with environment(one_cluster) as env:
        root=env['root'];data=root/'untouched-data';data.mkdir();source=data/'ksu.sqlite3'
        fixture=frozen_fixture(source);original=checksum(source)
        with closing(__import__('sqlite3').connect(source)) as con:expected=fingerprints(con)
        out=root/'private-output';out.mkdir();name='ksu-managed-app-'+str(os.getpid());port=unused_port();origin=f'https://127.0.0.1:{port}'
        assert port not in (8003,8018,8019,8020)
        def env_file(kind,role,key):
            path=root/(kind+'-'+role+'.env');path.write_text(key+'='+env[kind]['container_'+role]+'\n');path.chmod(0o600);return path
        common=['--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=768m','--cap-drop','ALL','--security-opt','no-new-privileges',
            '-v',str(env['pki'])+':/tls:ro','-v',str(data)+':/data:ro','-v',str(out)+':/output:rw']
        def command(kind,role,operation,*args,check=True):
            key='KSU_PG_BACKUP_DSN' if role=='backup' else 'KSU_PG_ADMIN_DSN'
            return docker('run','--rm','--pull','never','--network',env['network'],*common,'--env-file',str(env_file(kind,role,key)),
                '--entrypoint','python',image,'-m','server.managed_pg',operation,*args,check=check,capture_output=True,text=True)
        def start(kind='source',mode='pilot',viewer='enabled',extra=()):
            docker('run','-d','--pull','never','--name',name,'--network',env['network'],*common,
                '--env-file',str(env_file(kind,'runtime','KSU_DATABASE_URL')),'-p',f'127.0.0.1:{port}:8000',
                '-e','KSU_ORIGIN='+origin,'-e','KSU_OFFLINE_MODE='+mode,'-e','KSU_VIEWER_ACCESS='+viewer,
                '-e','KSU_OFFLINE_DEPLOYMENT_ID='+'f'*32,'-e','KSU_OFFLINE_USER_IDS=inspector',
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',*extra,image,stdout=subprocess.DEVNULL)
        def stop():docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def ready():
            trust=ssl.create_default_context(cafile=str(env['pki']/'cert.pem'))
            with httpx.Client(base_url=origin,verify=trust,trust_env=False,timeout=5) as client:
                for _ in range(150):
                    try:
                        response=client.get('/api/health')
                        if response.status_code==200:
                            assert response.json()['backend']=='postgresql' and response.json()['build']==sha;return
                    except httpx.TransportError:pass
                    if docker('inspect','--format','{{.State.Running}}',name,capture_output=True,text=True).stdout.strip()!='true':break
                    time.sleep(.2)
            docker('logs','--tail','30',name,check=False);raise AssertionError('Managed runtime failed readiness')
        try:
            # Real former deployed source v0, in addition to the current v4 fixture.
            legacy=legacy_fixture(root,data/'legacy.sqlite3');legacy_hash=checksum(legacy)
            from psycopg import sql
            with psycopg.connect(env['source']['root'],autocommit=True) as con:con.execute('create database ksu_legacy')
            legacy_admin=make_conninfo(**{**conninfo_to_dict(env['source']['admin']),'dbname':'ksu_legacy'})
            legacy_root=make_conninfo(**{**conninfo_to_dict(env['source']['root']),'dbname':'ksu_legacy'})
            with psycopg.connect(legacy_root) as con:
                con.execute('grant create,connect on database ksu_legacy to ksu_admin');con.execute('revoke create on schema public from public')
            env['legacy']={'container_admin':make_conninfo(**{**conninfo_to_dict(env['source']['container_admin']),'dbname':'ksu_legacy'})}
            if image:command('legacy','admin','migrate','--source','/data/legacy-closed.sqlite3','--output','/output/legacy-migration.json')
            else:migrate(legacy,legacy_admin,out/'legacy-migration.json')
            assert checksum(legacy)==legacy_hash and json.loads((out/'legacy-migration.json').read_text())['schema_before']==0
            if image:
                result=command('source','admin','migrate','--source','/data/ksu.sqlite3','--output','/output/migration.json')
                assert json.loads(result.stdout)['source_unchanged']
            else:migrate(source,env['source']['admin'],out/'migration.json')
            assert checksum(source)==original and facts(env['source']['admin'])==expected
            for role in ('runtime','admin','backup'):preflight(env['source'][role],role)
            assert verify_report(env['source']['backup'],out/'migration.json')['verified']
            # Direct SQL: service credentials cannot escalate or write using backup role.
            for dsn,query in [(env['source']['backup'],"update ksu.users set name=name"),
                              (env['source']['runtime'],'set role ksu_admin'),
                              (env['source']['runtime'],'create table ksu.forbidden(x integer)')]:
                try:
                    with psycopg.connect(dsn) as con:con.execute(query)
                except psycopg.errors.InsufficientPrivilege:pass
                else:raise AssertionError('Service privilege escalation permitted')
            bad=conninfo_to_dict(env['source']['runtime']);bad.update(host='wrong.synthetic.invalid',hostaddr='127.0.0.1')
            try:psycopg.connect(make_conninfo(**bad),connect_timeout=3)
            except psycopg.OperationalError:pass
            else:raise AssertionError('Wrong TLS hostname accepted')
            if image:
                start();ready()
                with ExitStack() as stack:
                    clients={name:stack.enter_context(httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),
                        trust_env=False,timeout=30,headers={'Origin':origin})) for name in fixture['accounts']}
                    receipt=api_contracts(clients,fixture)
                    stop();start();ready()
                    assert clients['field-inspector'].get('/api/receipts/synthetic-managed-complete').json()==receipt
                    stop();start(mode='off',viewer='off');ready()
                    assert clients['synthetic-viewer'].get('/api/state').status_code==403
                    assert clients['field-inspector'].get('/api/receipts/synthetic-managed-complete').json()==receipt
                    assert clients['field-inspector'].post('/api/offline/packages',json={'siteIds':['s1']}).status_code==403
                    assert clients['account-owner'].get('/api/state').status_code==200
                    stop();start();ready()
                expected_final=facts(env['source']['admin'])
                command('source','backup','backup','--output','/output/backup.ksubackup')  # App remains online.
                stop()
                command('restored','admin','restore','--source','/output/backup.ksubackup','--output','/output/restore.json')
            else:
                with patch.dict(os.environ,{'KSU_DATABASE_URL':env['source']['runtime'],'KSU_WORK_DIR':str(root/'work'),
                    'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_DEPLOYMENT_ID':'f'*32,'KSU_OFFLINE_USER_IDS':'inspector'}):
                    app=create_app('postgresql',test_api.ORIGIN,False)
                    try:
                        with ExitStack() as stack:
                            clients={name:stack.enter_context(TestClient(app,headers={'Origin':test_api.ORIGIN})) for name in fixture['accounts']}
                            api_contracts(clients,fixture)
                    finally:app.state.import_jobs.close()
                expected_final=facts(env['source']['admin'])
                backup_database(env['source']['backup'],out/'backup.ksubackup')
                restore_database(out/'backup.ksubackup',env['restored']['admin'],'ksu_runtime','ksu_backup')
            assert facts(env['restored']['admin'])==expected_final
            for role in ('runtime','backup','admin'):preflight(env['restored'][role],role)
            with psycopg.connect(env['restored']['runtime']) as con:
                assert not con.execute("select has_function_privilege(current_user,'ksu.record_audit()','EXECUTE')").fetchone()[0]
            if image:
                start('restored');ready()
                with ExitStack() as stack:
                    clients={name:stack.enter_context(httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),
                        trust_env=False,timeout=30,headers={'Origin':origin})) for name in fixture['accounts']}
                    api_contracts(clients,fixture,repeat=True)
                assert facts(env['restored']['admin'])==expected_final  # Lost reply/retry after restore creates no duplicate.
                stop()
                start(extra=('-e','KSU_PG_ADMIN_DSN=forbidden-synthetic-binding'))
                assert docker('wait',name,capture_output=True,text=True).stdout.strip()!='0';stop()
            assert checksum(source)==original
            if contracts:
                settings={'KSU_TEST_PG_ADMIN_DSN':env['source']['root'],'KSU_TEST_PG_RUNTIME_DSN':make_conninfo(**{**conninfo_to_dict(env['source']['runtime']),'dbname':'postgres'}),
                    'KSU_TEST_PG_BACKUP_DSN':make_conninfo(**{**conninfo_to_dict(env['source']['backup']),'dbname':'postgres'}),
                    'KSU_TEST_PG_RESTORE_ADMIN_DSN':env['restored']['root'],'KSU_TEST_PG_RESTORE_RUNTIME_DSN':make_conninfo(**{**conninfo_to_dict(env['restored']['runtime']),'dbname':'postgres'})}
                modules=['tests.test_offline_stage1.PostgreSQLOffline','tests.test_offline_stage2_server.PostgreSQLStage2',
                    'tests.test_postgres','tests.test_postgres_versions','tests.test_postgres_analytics','tests.test_offline_release.PostgreSQLRelease','tests.test_viewer.PostgreSQLViewer']
                subprocess.run([sys.executable,'-m','unittest',*modules,'-v'],env={**os.environ,**settings},check=True)
            return {'synthetic_only':True,'source_sha':sha,'backend':'postgresql17','business_tables':len(expected),
                'deployed_v0_and_current_v4_migration':True,
                'source_sqlite_unchanged':True,'users_passwords_photos_history_receipts_identical':True,
                'backup_nobypassrls_select_only':True,'runtime_admin_backup_separated':True,'tls_hostname_rejection':True,
                'manager_inspector_viewer_offline_api':True,'independent_restore':not one_cluster,
                'retry_after_restore_idempotent':bool(image),'public_function_rights_restored':True,'browser_tests_repeated':False}
        finally:stop()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--image');parser.add_argument('--sha');parser.add_argument('--output');parser.add_argument('--one-cluster',action='store_true');parser.add_argument('--contracts',action='store_true')
    args=parser.parse_args();result=run(args.image,args.sha,args.one_cluster,args.contracts)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
