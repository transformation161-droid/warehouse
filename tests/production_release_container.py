"""Operational checks on the delivery image's actual SQLite; no browser/UI replay."""
import argparse
from contextlib import closing
from datetime import datetime,timezone,timedelta
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
from server.pg_admin import fingerprints
from server.database import Database
from server.release_schema import checksum
from tests.pg_environment import docker,unused_port
from tests.release_acceptance_seed import initialize
from tests.viewer_release_container import PROBE


def run(image,expected_sha):
    name='ksu-operations-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='ksu-operations-synthetic-') as directory:
        root=Path(directory);data=root/'data';data.mkdir();accounts=initialize(data)
        source=data/'ksu.sqlite3'
        # Exercise new local retention/restore at the already accepted backup size,
        # within the existing 0.5 CPU / 1 GiB envelope (no UI workload).
        db=Database(source)
        with db.connect({'id':'manager','role':'manager'}) as con:
            for index in range(150):
                con.execute('insert into media values(?,?,?,?,?,?)',('synthetic-ops-'+str(index),'s1','manager',bytes([index])*1048576,b'thumbnail','2026-11-10'))
        with closing(sqlite3.connect(source)) as con:before=fingerprints(con)
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU synthetic operations',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost','-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        port=unused_port();assert port not in (8003,8018,8019,8020)
        origin=f'https://127.0.0.1:{port}'
        common=['--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--cap-drop','ALL','--security-opt','no-new-privileges','-v',str(data)+':/data:rw']
        def start(cap='2147483648'):
            docker('run','-d','--pull','never','--name',name,*common,'-p',f'127.0.0.1:{port}:8000',
                '-v',str(cert)+':/tls/cert.pem:ro','-v',str(key)+':/tls/key.pem:ro',
                '-e','KSU_OFFLINE_MODE=off','-e','KSU_OFFLINE_DEPLOYMENT_ID='+'c'*32,
                '-e','KSU_DB=/data/ksu.sqlite3','-e','KSU_ORIGIN='+origin,
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',
                '-e','KSU_LOCAL_BACKUP_MODE=daily','-e','KSU_DATA_MAX_BYTES='+cap,image,stdout=subprocess.DEVNULL)
        def stop():
            docker('stop',name,stdout=subprocess.DEVNULL);docker('rm',name,stdout=subprocess.DEVNULL)
        def health(client,healthy):
            for _ in range(600):
                try:
                    response=client.get('/api/health')
                    if response.status_code==200:
                        value=response.json();state=value.get('localBackup',{})
                        if state.get('healthy')==healthy and state.get('lastAttempt')==('success' if healthy else 'failed'):return value
                except httpx.TransportError:pass
                time.sleep(.1)
            raise AssertionError('Operational health did not reach expected backup state')
        try:
            start()
            with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,timeout=20,headers={'Origin':origin}) as client:
                value=health(client,True);assert value['build']==expected_sha and value['offline']=='off'
                probe=docker('exec','-i',name,'python','-',input=PROBE,capture_output=True,text=True)
                actual=json.loads(probe.stdout)
                assert actual['release_sha']==expected_sha and actual['media_links_with_viewer'],actual
                assert all(actual['authorizer_denies_'+kind] for kind in ('ddl','dml','pragma','attach')),actual
                login=client.post('/api/login',json={'username':'demo-viewer','password':accounts['viewer']});assert login.status_code==200
                client.headers['X-CSRF-Token']=login.json()['csrf']
                state=client.get('/api/state');assert state.status_code==200 and [s['id'] for s in state.json()['sites']]==['s1']
                assert client.post('/api/admin/backup').status_code==403
                stop()
                # Two further verified runs in the actual image, with no network or app.
                script="from server.local_backup import LocalBackups; from pathlib import Path; j=LocalBackups('/data/ksu.sqlite3','/data','/data/backup-v4'); j.run_once(); j.run_once(); assert len(j.owned())==2; print('Two verified copies retained')"
                docker('run','--rm','--pull','never','--network','none',*common,'--entrypoint','python',image,'-c',script,stdout=subprocess.DEVNULL)
                status=json.loads((data/'backup-v4/status.json').read_text());last=status['lastSuccess'];target=data/'backup-v4'/last['file']
                assert checksum(target)==last['sha256'] and last['restored'] and last['business_tables_identical']
                files=set(p.name for p in (data/'backup-v4').glob('local-v4-*.sqlite3'));assert len(files)==2
                start();health(client,True);stop()
                assert files==set(p.name for p in (data/'backup-v4').glob('local-v4-*.sqlite3'))
                # Missed runtime: only job metadata is aged; the DB/copies stay unchanged.
                status['lastSuccess']['verifiedAt']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
                (data/'backup-v4/status.json').write_text(json.dumps(status))
                start();health(client,True);stop()
                caught=json.loads((data/'backup-v4/status.json').read_text())['lastSuccess'];assert caught['file']!=last['file']
                protected=data/'backup-v4'/caught['file'];digest=checksum(protected)
                start('1');value=health(client,False);assert value['status']=='ok';stop()
                failed=json.loads((data/'backup-v4/status.json').read_text())
                assert failed['lastSuccess']==caught and checksum(protected)==digest
            with closing(sqlite3.connect(source)) as con:assert fingerprints(con)==before
            return {'synthetic_only':True,'source_sha':expected_sha,'sqlite_version':actual['sqlite_version'],
                'source_bytes':source.stat().st_size,
                'viewer_media_query_and_authorizer':True,'daily_restore_retention_restart_catchup':True,
                'disk_failure_preserves_last_success':True,'business_tables_unchanged':True,
                'release_build_from_delivery_without_build_args':True,'browser_tests_repeated':False,'cpu':'0.5','ram':'1 GiB'}
        finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);parser.add_argument('--sha',required=True);parser.add_argument('--output')
    args=parser.parse_args();result=run(args.image,args.sha)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
