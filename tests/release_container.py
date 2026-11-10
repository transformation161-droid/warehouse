"""Real candidate image; isolated /data, HTTPS, fail-closed startup and persistent restart."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import subprocess
import tempfile
import time

import httpx
from server.database import Database,seed_demo
from server.maintenance import snapshot_database
from server.release_schema import prepare_sqlite
from tests.pg_environment import docker,unused_port


def run(image):
    name='ksu-release-probe-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='ksu-release-container-synthetic-') as directory:
        root=Path(directory);data=root/'data';data.mkdir();cert=root/'cert.pem';key=root/'key.pem'
        source=root/'live.sqlite3';seed_demo(Database(source),'synthetic owner password','synthetic inspector password')
        with sqlite3.connect(source) as con:
            con.create_function('actor_role',0,lambda:'')
            con.execute("update users set username='field-inspector' where id='inspector'")
        closed=root/'backup.sqlite3';snapshot_database(source,closed);prepare_sqlite(closed,data/'candidate',closed_backup=True)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU synthetic release',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost','-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        port=unused_port();origin=f'https://127.0.0.1:{port}'
        mounts=['-v',str(data)+':/data:rw','-v',str(cert)+':/tls/cert.pem:ro','-v',str(key)+':/tls/key.pem:ro']
        common=['--user',str(os.getuid())+':'+str(os.getgid()),'--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--cap-drop','ALL','--security-opt','no-new-privileges',
            '-p',f'127.0.0.1:{port}:8000',*mounts,'-e','KSU_OFFLINE_DEPLOYMENT_ID='+'a'*32,
            '-e','KSU_DB=/data/candidate/ksu.sqlite3','-e','KSU_ORIGIN='+origin,
            '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem']
        def start(mode='staging'):
            docker('run','-d','--pull','never','--name',name,*common,'-e','KSU_OFFLINE_MODE='+mode,
                '-e','KSU_OFFLINE_USER_IDS=inspector',image,stdout=subprocess.DEVNULL)
        def stop():docker('stop',name,stdout=subprocess.DEVNULL);docker('rm',name,stdout=subprocess.DEVNULL)
        def ready(client):
            for _ in range(300):
                try:
                    if client.get('/api/health').status_code==200:return
                except httpx.TransportError:pass
                time.sleep(.1)
            raise AssertionError('Release container did not start')
        try:
            start()
            with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,timeout=20,headers={'Origin':origin}) as client:
                ready(client);health=client.get('/api/health').json();assert len(health['build'])==40 and health['data']=='application'
                login=client.post('/api/login',json={'username':'field-inspector','password':'synthetic inspector password'});assert login.status_code==200
                cookie=login.headers['set-cookie'];assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
                context=client.get('/api/offline-visit/context').json();assert context['enabled'] and 'synthetic' not in context
                assert client.get('/offline-visit/').status_code==200 and client.get('/').status_code==200
                assert 'const defaultSites=' not in client.get('/app.js').text
                assert client.get('/server/seed.json').status_code==404
                assert client.get('/api/writeoff-analytics').status_code==404
                before=client.get('/api/state').json();stop();start();ready(client)
                assert client.get('/api/state').json()==before
                stop();start('off');ready(client)
                assert client.get('/api/state').json()==before and not client.get('/api/offline-visit/context').json()['enabled']
                assert client.post('/api/offline/packages',headers={'X-CSRF-Token':login.json()['csrf']},json={'siteIds':['s1']}).status_code==403
            probe="from pathlib import Path; assert not Path('/app/server/seed.json').exists(); assert not Path('/app/experiments').exists(); print('No seed/prototype shipped')"
            docker('exec',name,'python','-c',probe,stdout=subprocess.DEVNULL)
            stop()
            # Existing schema must be complete. Removing a guard on this disposable copy
            # must refuse startup, not repair it or seed new users.
            with closing(sqlite3.connect(data/'candidate/ksu.sqlite3')) as con:
                con.execute('drop trigger guard_insert');con.commit()
            start();status=docker('wait',name,capture_output=True,text=True).stdout.strip();assert status!='0'
            with closing(sqlite3.connect(data/'candidate/ksu.sqlite3')) as con:
                assert con.execute('select count(*) from users').fetchone()[0]==2
                assert not con.execute("select 1 from sqlite_master where name='guard_insert'").fetchone()
            return {'synthetic_only':True,'candidate_image':image,'https_verified':True,'secure_cookie':True,
                'restart_sessions_records_preserved':True,'kill_switch_online_preserved':True,
                'missing_guard_refused_without_ddl':True,'no_seed_or_lab_in_artifact':True,'cpu':'0.5','ram':'1 GiB'}
        finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);args=parser.parse_args();print(json.dumps(run(args.image),indent=2))
