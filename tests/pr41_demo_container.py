"""Real unchanged PR41 image + isolated PG17 + trusted HTTPS, no owner data."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
from tests.pg_environment import docker,unused_port
from tools.pr41_demo.demo import APP_SHA
from tools.pr41_demo.package import check


def run(kit,image):
    kit=Path(kit).resolve();check(kit);ident=uuid4().hex;name='instrakt-pr41-ci-'+ident[:10]
    net=name+'-net';private=name+'-private';pki=name+'-tls';data=name+'-data';pg=name+'-db';app=name+'-app';prep=name+'-prepare';seed=name+'-seed'
    label='instrakt.pr41.test='+ident;port=unused_port();origin=f'https://127.0.0.1:{port}'
    containers=[];volumes=[]
    with tempfile.TemporaryDirectory(prefix='pr41-only-synthetic-') as folder:
        root=Path(folder);copied=root/'private';copied.mkdir()
        def invoke(*args,check=True):
            result=docker(*args,check=False,capture_output=True,text=True)
            if check and result.returncode:
                # Helper diagnostics are intentionally generic; never emit private env/inspect.
                message=result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'Docker command failed'
                if 'PR41 synthetic lab refused:' not in message:message='Local synthetic Docker step failed'
                raise AssertionError(message)
            return result
        def vol(name,where,ro=False):return ['--mount',f'type=volume,src={name},dst={where}'+(',readonly' if ro else '')]
        helper=['--mount',f'type=bind,src={kit}/demo.py,dst=/acceptance/demo.py,readonly']
        limits=['--cpus','0.5','--memory','1g','--memory-swap','1g']
        try:
            invoke('network','create','--label',label,net)
            for v in (private,pki,data):invoke('volume','create','--label',label,v);volumes.append(v)
            containers.append(prep)
            invoke('run','--name',prep,'--network','none','--label',label,'--user','0:0',*vol(private,'/private'),*vol(pki,'/tls'),*helper,
                   '--entrypoint','python',image,'/acceptance/demo.py','prepare','/private','/tls','127.0.0.1',str(port),ident)
            invoke('cp',prep+':/private/.',str(copied))
            credentials=json.loads((copied/'secrets.json').read_text())['accounts'];secret_hash=hashlib.sha256((copied/'secrets.json').read_bytes()).hexdigest()
            containers.append(pg)
            invoke('run','-d','--name',pg,'--label',label,'--network',net,'--network-alias','pr41-db','--user','0:0',*limits,
                   '--env-file',str(copied/'server.env'),*vol(private,'/private',True),*vol(pki,'/tls',True),*vol(data,'/var/lib/postgresql/data'),
                   '--mount',f'type=bind,src={kit}/pg-start.sh,dst=/acceptance/pg-start.sh,readonly','--entrypoint','bash',image,'/acceptance/pg-start.sh')
            def pg_ready():
                for _ in range(120):
                    if invoke('exec',pg,'pg_isready','-U','postgres','-d','ksu_pr41_demo',check=False).returncode==0:return
                    time.sleep(.25)
                raise AssertionError('Synthetic PG not ready')
            pg_ready();containers.append(seed)
            seed_args=['run','--name',seed,'--label',label,'--network',net,*limits,'--cap-drop','ALL','--security-opt','no-new-privileges',
                       '--env-file',str(copied/'bootstrap.env'),*vol(private,'/private'),*vol(pki,'/tls',True),*helper,'--entrypoint','python',image,'/acceptance/demo.py','seed','/private']
            invoke(*seed_args);invoke('cp',seed+':/private/seed-complete.json',str(copied/'seed-complete.json'))
            assert json.loads((copied/'seed-complete.json').read_text())['questions']==22
            containers.append(app)
            invoke('run','-d','--name',app,'--label',label,'--network',net,*limits,'--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=768m',
                   '--cap-drop','ALL','--security-opt','no-new-privileges','--env-file',str(copied/'runtime.env'),*vol(pki,'/tls',True),'-p',f'127.0.0.1:{port}:8000',image)
            ca=ssl.create_default_context(cafile=str(copied/'PR41-Test-CA.crt'))
            def app_ready():
                with httpx.Client(base_url=origin,verify=ca,trust_env=False,timeout=3) as c:
                    for _ in range(180):
                        try:
                            reply=c.get('/api/health')
                            if reply.status_code==200:
                                assert reply.json()['build']==APP_SHA and reply.json()['backend']=='postgresql';return
                        except httpx.TransportError:pass
                        time.sleep(.2)
                raise AssertionError('Synthetic app HTTPS not ready')
            app_ready()
            pg_inspect=json.loads(invoke('container','inspect',pg).stdout)[0]
            assert not pg_inspect['HostConfig']['PortBindings']
            assert json.loads(invoke('container','inspect',app).stdout)[0]['HostConfig']['PortBindings']['8000/tcp'][0]['HostIp']=='127.0.0.1'
            with ExitStack() as stack:
                clients={}
                for role,a in credentials.items():
                    c=stack.enter_context(httpx.Client(base_url=origin,verify=ca,trust_env=False,headers={'Origin':origin},timeout=30))
                    r=c.post('/api/login',json={'username':a['username'],'password':a['password']});assert r.status_code==200
                    c.headers['X-CSRF-Token']=r.json()['csrf'];clients[role]=c
                manager=clients['manager'];field=clients['inspector'];viewer=clients['viewer']
                state=manager.get('/api/state').json();assert len(state['sites'])==2 and len(state['questions'])==22 and len(state['inventories'])==4
                assert len(viewer.get('/api/state').json()['sites'])==1 and len(field.get('/api/state').json()['sites'])==1
                assert len(state['findings'])==4 and sum(f['status']!='closed' for f in state['findings'])==2
                assert viewer.get('/api/media/demo-photo-north').status_code==200 and viewer.get('/api/media/demo-photo-south').status_code==404
                plan=next(i for i in state['inventories'] if '1. Быстрая' in i['title'])
                lines=[x for x in state['inventoryLines'] if x['inventoryId']==plan['id']];assert len(lines)==5 and all(x['actual'] is None for x in lines)
                sample=next(i for i in state['inventories'] if '2. Пример' in i['title'])
                for ext,magic in [('pdf',b'%PDF'),('xlsx',b'PK')]:
                    response=manager.get('/api/inventories/'+sample['id']+'/statement.'+ext);assert response.status_code==200 and response.content.startswith(magic)
                line=sorted(lines,key=lambda x:x['sourceRow'])[0];payload={'expectedVersion':line['version'],'actual':line['expected'],'reason':'','photo':''}
                url='/api/inventory-lines/'+line['id']+'/count'
                assert viewer.patch(url,json=payload).status_code==403 and field.patch(url,json=payload).status_code==200 and field.patch(url,json=payload).status_code==409
                # Both containers restart; no new seed, passwords or data reset.
                invoke('stop',app,pg);invoke('start',pg);pg_ready();invoke('start',app);app_ready()
                assert manager.get('/api/state').status_code==200
                after=manager.get('/api/state').json();saved=next(x for x in after['inventoryLines'] if x['id']==line['id'])
                assert saved['actual']==line['expected'] and saved['version']==line['version']+1
                # Nonempty seed is rejected and cannot erase the accepted count.
                retry=seed_args.copy();retry[retry.index(seed)]=name+'-forbidden-reseed';containers.append(name+'-forbidden-reseed')
                assert invoke(*retry,check=False).returncode!=0
                assert manager.get('/api/state').json()['inventoryLines']==after['inventoryLines']
            assert hashlib.sha256((copied/'secrets.json').read_bytes()).hexdigest()==secret_hash
            try:
                with httpx.Client(base_url=origin,trust_env=False,timeout=3) as c:c.get('/api/health')
            except httpx.TransportError:pass
            else:raise AssertionError('Test CA must be explicitly trusted, no TLS bypass')
            result={'synthetic_only':True,'app_sha':APP_SHA,'real_image_https':True,'private_generated_passwords':True,'actual_logins':3,
                    'questions':22,'inventories':4,'photo_acl':True,'pdf_xlsx':True,'no_pg_port':True,'restart_preserved_data':True,'reseed_refused':True,'production_contacted':False}
            print(json.dumps(result,indent=2));return result
        finally:
            for c in reversed(containers):invoke('rm','-f',c,check=False)
            for v in volumes:invoke('volume','rm',v,check=False)
            invoke('network','rm',net,check=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--kit',required=True);p.add_argument('--image',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    report=run(a.kit,a.image);Path(a.output).write_text(json.dumps(report,indent=2))
