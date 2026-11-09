"""Two real scheduled-job invocations + failure, HTTPS, 150 MiB, isolated capped containers."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time

from server.pg_admin import fingerprints
from server.release_schema import checksum
from tests.release_acceptance_seed import initialize
from tests.pg_environment import docker


def run(image):
    name='ksu-backup-synthetic-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='ksu-scheduled-synthetic-') as directory:
        root=Path(directory);data=root/'data';data.mkdir();out=root/'out';out.mkdir();accounts=initialize(data)
        source=data/'ksu.sqlite3'
        with closing(sqlite3.connect(source)) as con:
            con.create_function('actor_role',0,lambda:'')
            for index in range(75):
                con.execute('insert into media values(?,?,?,?,?,?)',('synthetic-blob-'+str(index),'s1','inspector',bytes([index])*2097152,b'synthetic thumbnail','2026-11-09'));con.commit()
            before=fingerprints(con)
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU synthetic backup',
            '-addext','subjectAltName=IP:127.0.0.1','-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        limits=['--user',f'{os.getuid()}:{os.getgid()}','--cpus','0.5','--memory','1g','--memory-swap','1g','--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=32m']
        try:
            docker('run','-d','--pull','never','--name',name,*limits,'-v',str(data)+':/data',
                '-v',str(cert)+':/tls/cert.pem:ro','-v',str(key)+':/tls/key.pem:ro',
                '-e','KSU_OFFLINE_MODE=pilot','-e','KSU_OFFLINE_DEPLOYMENT_ID='+'b'*32,'-e','KSU_OFFLINE_USER_IDS=inspector',
                '-e','KSU_DB=/data/ksu.sqlite3','-e','KSU_ORIGIN=https://127.0.0.1:8000',
                '-e','KSU_RELEASE_TLS_CERT=/tls/cert.pem','-e','KSU_RELEASE_TLS_KEY=/tls/key.pem',
                '-e','KSU_BACKUP_EXPORT_DIR=/data/export-work',image,stdout=subprocess.DEVNULL)
            probe="import ssl,urllib.request; c=ssl.create_default_context(cafile='/tls/cert.pem'); print(urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=c)).open('https://127.0.0.1:8000/api/health',timeout=2).status)"
            for _ in range(100):
                ready=docker('exec',name,'python','-c',probe,check=False,capture_output=True,text=True)
                if ready.returncode==0:break
                time.sleep(.2)
            else:raise AssertionError('Synthetic server not ready')
            args=['run','--rm','--pull','never','-i','--network','container:'+name,*limits,
                '-v',str(out)+':/backups','-v',str(cert)+':/trust/cert.pem:ro','--entrypoint','python',image,
                '-m','server.backup_job','--origin','https://127.0.0.1:8000','--directory','/backups','--ca','/trust/cert.pem']
            results=[]
            for _ in range(2):
                result=docker(*args,input=json.dumps({'username':'demo-manager','password':accounts['manager']}),capture_output=True,text=True)
                results.append(json.loads(result.stdout))
            assert results[0]['file']!=results[1]['file']
            assert checksum(out/results[0]['file'])==results[0]['sha256']
            assert all(r['bytes']>=150*1024**2 and r['restored'] for r in results)
            failed=docker(*args,input=json.dumps({'username':'demo-viewer','password':accounts['viewer']}),capture_output=True,text=True,check=False)
            assert failed.returncode!=0
            status=json.loads((out/'status.json').read_text());assert status['lastAttempt']=='failed' and status['lastSuccess']==results[-1]
            docker('stop',name,stdout=subprocess.DEVNULL)
            with closing(sqlite3.connect(source)) as con:assert fingerprints(con)==before
            return {'syntheticOnly':True,'source_bytes':source.stat().st_size,'verified_runs':2,'failed_role_change_preserves_last_success':True,
                'old_backup_not_overwritten':True,'source_business_unchanged':True,'independent_restore_all_tables':True,
                'tls_verified':True,'streamed':True,'cpu_per_container':'0.5','ram_per_container':'1 GiB'}
        finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);print(json.dumps(run(parser.parse_args().image),indent=2))
