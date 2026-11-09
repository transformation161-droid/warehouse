"""Probe the Windows launcher's exact mount boundary in a disposable Linux Docker lab."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import time

import httpx
from server.database import ROOT
from tests.pg_environment import docker


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);args=parser.parse_args()
    name='ksu-stage2-probe-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='ksu-stage2-probe-') as directory:
        root=Path(directory);data=root/'data';data.mkdir()
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU synthetic stage2',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost','-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'https://127.0.0.1:{port}'
        mounts=['-v',str(ROOT/'server')+':/app/server:ro','-v',str(ROOT/'experiments/offline_visit')+':/app/experiments/offline_visit:ro',
                '-v',str(data)+':/lab:rw','-v',str(cert)+':/cert.pem:ro','-v',str(key)+':/key.pem:ro']
        for filename in ('styles.css','index.html','app.js','sync.js','decisions.js','calendar.js','writeoff_analytics.js'):
            mounts+=['-v',str(ROOT/filename)+':/app/'+filename+':ro']
        def start():
            docker('run','-d','--pull','never','--name',name,'--user',str(os.getuid())+':'+str(os.getgid()),
                '--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g','--tmpfs','/tmp:rw,nosuid,nodev,size=32m',
                '--cap-drop','ALL','--security-opt','no-new-privileges','-p',f'127.0.0.1:{port}:{port}',*mounts,
                '-w','/app','--entrypoint','python',args.image,'-m','experiments.offline_visit.lab','--directory','/lab',
                '--host','0.0.0.0','--port',str(port),'--origin',origin,'--cert','/cert.pem','--key','/key.pem',stdout=subprocess.DEVNULL)
        def ready(client):
            for _ in range(200):
                try:
                    if client.get('/api/health').status_code==200:return
                except httpx.TransportError:pass
                time.sleep(.1)
            raise AssertionError('Isolated container did not become ready')
        try:
            start()
            with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,timeout=5,headers={'Origin':origin}) as client:
                ready(client)
                accounts={line.split(': ',1)[0]:line.split(': ',1)[1] for line in (data/'demo-accounts.txt').read_text().splitlines() if ': ' in line}
                digest=hashlib.sha256((data/'demo-accounts.txt').read_bytes()).hexdigest()
                login=client.post('/api/login',json={'username':'inspector','password':accounts['inspector']});assert login.status_code==200
                assert 'Secure' in login.headers['set-cookie']
                context=client.get('/api/offline-visit/context').json();assert context['synthetic'] is True
                assert client.get('/offline-visit/').status_code==200
                assert client.get('/offline-visit/sw.js').headers['service-worker-allowed']=='/offline-visit/'
                docker('stop',name,stdout=subprocess.DEVNULL);docker('rm',name,stdout=subprocess.DEVNULL)
                start();ready(client)
                assert hashlib.sha256((data/'demo-accounts.txt').read_bytes()).hexdigest()==digest
                login=client.post('/api/login',json={'username':'inspector','password':accounts['inspector']});assert login.status_code==200
                assert client.get('/api/offline-visit/context').json()==context
                assert len(client.get('/api/state').json()['visits'])==1
            print('PASS: Docker mount whitelist, dedicated SQLite directory, verified HTTPS/Secure cookie, restart without reseed; no existing DB/port 8003/DSN used')
        finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
