"""Exact release image, synthetic ingress/plain PG/private service/independent TLS restore.

Never connects to Amvera. No UI/PWA run. Secret fixture files are deleted, never artifacts.
"""
import argparse
import base64
import io
from PIL import Image
from contextlib import ExitStack
import json
import os
import re
from pathlib import Path
import ssl
import subprocess
import time
import zipfile
from uuid import uuid4

import httpx
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from tests.managed_pg_environment import environment
from tests.managed_pg_acceptance import facts
from tests.pg_environment import docker,unused_port
from tests.test_production_init import approved

HOST='synthetic-amvera.invalid'


def run(image,sha):
    with environment(amvera=True) as env:
        root=env['root'];out=root/'output';out.mkdir();config=root/'private-config.json';config.write_text(json.dumps(approved()))
        proxy='ksu-ingress-'+uuid4().hex[:10];app='ksu-amvera-app-'+uuid4().hex[:10];port=unused_port();origin=f'https://127.0.0.1:{port}'
        mounts=['--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=768m','--cap-drop','ALL','--security-opt','no-new-privileges','-v',str(env['pki'])+':/tls:ro']
        def settings(kind,selected,role):
            values=conninfo_to_dict(env[kind]['container_'+role])
            if selected=='amvera-external':values.update(host=HOST,sslmode='require');values.pop('sslrootcert',None)
            elif selected=='amvera-internal':values.update(host='cnpg-instrakt-ksu-rw',sslmode='disable');values.pop('sslrootcert',None)
            return make_conninfo(**values)
        def envfile(kind,selected,role,key):
            path=root/(uuid4().hex+'.env');path.write_text(key+'='+settings(kind,selected,role)+'\nKSU_PG_TRANSPORT_PROFILE='+selected+'\nKSU_AMVERA_PG_EXTERNAL_HOST='+HOST+'\nKSU_AMVERA_INTERNAL_NETWORK=confirmed-private\n');path.chmod(0o600);return path
        def command(kind,selected,role,operation,*args,check=True,input=None):
            key={'owner':'KSU_PG_BOOTSTRAP_DSN','admin':'KSU_PG_ADMIN_DSN','runtime':'KSU_DATABASE_URL','backup':'KSU_PG_BACKUP_DSN'}[role]
            call=docker('run','--rm','-i','--pull','never','--network',env['network'],*mounts,'--env-file',str(envfile(kind,selected,role,key)),
                '-v',str(out)+':/output','-v',str(config)+':/input/config.json:ro','--entrypoint','python',image,'-m','server.managed_pg',operation,*args,
                check=False,input=input,capture_output=True,text=True)
            if check and call.returncode:
                reason=re.search(r'operation refused: ([A-Za-z]+)\.',call.stderr)
                raise AssertionError(operation+' refused: '+(reason[1] if reason else 'unknown private diagnostic'))
            return json.loads(call.stdout) if check else call
        def start(selected):
            docker('run','-d','--pull','never','--name',app,'--network',env['network'],*mounts,
                '--env-file',str(envfile('source',selected,'runtime','KSU_DATABASE_URL')),'-p',f'127.0.0.1:{port}:8000',
                '-e','KSU_ORIGIN='+origin,'-e','KSU_OFFLINE_MODE=off','-e','KSU_OFFLINE_DEPLOYMENT_ID='+'f'*32,
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',image,stdout=subprocess.DEVNULL)
            with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False,timeout=5) as client:
                for _ in range(150):
                    try:
                        r=client.get('/api/health')
                        if r.status_code==200:assert r.json()['build']==sha and r.json()['backend']=='postgresql';return
                    except httpx.TransportError:pass
                    time.sleep(.1)
            log=docker('logs','--tail','10',app,check=False,capture_output=True,text=True)
            reason=re.search(r'preflight refused: ([A-Za-z]+)\.',log.stdout+log.stderr)
            raise AssertionError('Synthetic runtime failed: '+(reason[1] if reason else 'unknown private diagnostic'))
        def stop():docker('rm','-f',app,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            docker('run','-d','--pull','never','--name',proxy,'--network',env['network'],'--network-alias',HOST,*mounts,
                '-v',str(Path('tests/amvera_tls_proxy.py').resolve())+':/test/proxy.py:ro','--entrypoint','python',image,'/test/proxy.py',stdout=subprocess.DEVNULL)
            # Wrong-host certificate is trusted as a CA, proving identity failure is real.
            bad=command('source','verify-full','owner','probe','--role','bootstrap',check=False)
            # Direct plaintext PG refuses the default verify-full profile.
            assert bad.returncode!=0
            assert command('source','amvera-external','owner','probe','--role','bootstrap')['postgres_tls'] is False
            # Same ingress certificate with strict CA/hostname validation must fail.
            strict=conninfo_to_dict(settings('source','verify-full','owner'));strict['host']=HOST
            path=root/'strict-proxy.env';path.write_text('KSU_PG_BOOTSTRAP_DSN='+make_conninfo(**strict)+'\n');path.chmod(0o600)
            rejected=docker('run','--rm','--pull','never','--network',env['network'],*mounts,'--env-file',str(path),'--entrypoint','python',image,'-m','server.managed_pg','probe','--role','bootstrap',check=False,capture_output=True,text=True)
            assert rejected.returncode!=0
            command('source','amvera-external','owner','bootstrap',input=json.dumps(env['passwords']))
            command('source','amvera-external','admin','init-schema')
            command('source','amvera-external','admin','init-production','--source','/input/config.json','--output','/output/production')
            accounts=json.loads((out/'production/credentials.json').read_text())['accounts'];before=facts(env['source']['admin'])
            for selected in ('amvera-external','amvera-internal'):
                for role in ('admin','runtime','backup'):
                    state=command('source',selected,role,'preflight','--role',role)
                    assert state['client_tls']==(selected=='amvera-external') and state['postgres_tls'] is False
                    assert state['tls_verified'] is False and state['server_identity_verified'] is False and state['end_to_end_tls_claimed'] is False
                assert command('source',selected,'backup','verify','--report','/output/production/setup-report.json')['verified']
                assert command('source',selected,'admin','init-production','--source','/input/config.json','--output','/output/refused',check=False).returncode!=0
                assert facts(env['source']['admin'])==before
            plain=root/'plain-external.env'
            plain.write_text('KSU_DATABASE_URL='+settings('source','amvera-external','runtime').replace(HOST,'synthetic-plain.invalid')+'\nKSU_PG_TRANSPORT_PROFILE=amvera-external\nKSU_AMVERA_PG_EXTERNAL_HOST=synthetic-plain.invalid\n');plain.chmod(0o600)
            rejected=docker('run','--rm','--pull','never','--network',env['network'],*mounts,'--env-file',str(plain),'--entrypoint','python',image,'-m','server.managed_pg','probe',check=False,capture_output=True,text=True)
            assert rejected.returncode!=0
            assert command('source','amvera-external','admin','preflight','--role','runtime',check=False).returncode!=0
            packages=[];body=None;receipt=None;questions=None
            for selected in ('amvera-external','amvera-internal'):
                start(selected)
                with ExitStack() as stack:
                    clients={role:stack.enter_context(httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False,headers={'Origin':origin},timeout=30)) for role in accounts}
                    for role,c in clients.items():
                        a=accounts[role];login=c.post('/api/login',json={'username':a['username'],'password':a['password']});assert login.status_code==200
                        c.headers['X-CSRF-Token']=login.json()['csrf'];state=c.get('/api/state').json();assert len(state['questions'])==22 and len(state['sites'])==1
                    state=clients['inspector'].get('/api/state').json()
                    if questions is None:questions=state['questions']
                    else:assert state['questions']==questions
                    denied={'mutationId':'synthetic-question-denied','changes':[{'collection':'questions','id':'q1','expectedVersion':1,'data':{'section':'СИНТЕТИКА','title':'СИНТЕТИКА · forbidden'}}]}
                    assert clients['inspector'].post('/api/changes',json=denied).status_code==403
                    if body is None:
                        picture=io.BytesIO();Image.new('RGB',(12,12),'blue').save(picture,format='PNG')
                        media=clients['inspector'].post('/api/media',json={'siteId':'qa-site','content':base64.b64encode(picture.getvalue()).decode()});assert media.status_code==200
                        current=clients['inspector'].get('/api/checklists/current').json()
                        body={'mutationId':'synthetic-transport-act','changes':[{'collection':'inspections','id':'synthetic-transport-act','expectedVersion':0,'data':{
                            'siteId':'qa-site','date':'2026-11-11','inspector':'СИНТЕТИКА','checklistVersion':current['id'],
                            'results':[{'questionId':q['id'],'status':'ok','photo':media.json()['url'] if i==0 else ''} for i,q in enumerate(current['questions'])]}}]}
                    assert clients['inspector'].post('/api/changes',json=body).status_code==200
                    received=clients['inspector'].get('/api/receipts/synthetic-transport-act');assert received.status_code==200
                    if receipt is None:receipt=received.json()
                    else:assert received.json()==receipt
                    assert clients['manager'].get('/api/state').json()['questions']==questions
                command('source',selected,'backup','backup','--output','/output/'+selected+'.ksubackup')
                with zipfile.ZipFile(out/(selected+'.ksubackup')) as archive:packages.append(json.loads(archive.read('manifest.json'))['tables'])
                with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(env['pki']/'cert.pem')),trust_env=False) as c:assert c.get('/api/health').status_code==200
                stop()
            assert packages[0]==packages[1]
            command('restored','verify-full','admin','restore','--source','/output/amvera-external.ksubackup','--output','/output/restore.json')
            assert facts(env['source']['admin'])==facts(env['restored']['admin'])
            for role in ('admin','runtime','backup'):
                result=command('restored','verify-full',role,'preflight','--role',role);assert result['server_identity_verified'] and result['postgres_tls'] and result['client_tls']
            return {'synthetic_only':True,'source_sha':sha,'actual_amvera_contacted':False,'tls_terminating_ingress':True,'external_require_no_plain_fallback':True,
                'private_internal_disable':True,'all_three_service_roles':True,'clean_initialization':True,'two_personal_logins':True,'catalog_22_unchanged':True,
                'repeat_initialization_refused':True,'native_backup_both_profiles':True,'independent_tls_restore_all_16_tables':True,'full_ui_pwa_repeated':False}
        finally:
            stop();docker('rm','-f',proxy,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--sha',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    result=run(a.image,a.sha);Path(a.output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
