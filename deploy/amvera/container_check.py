"""Exercise the exact image under 0.5 CPU/1 GiB with a mounted persistent volume.
All accounts, photos and inventory lines are synthetic and are destroyed after the test.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
from PIL import Image
from deploy.accept_live import clients,create_receipt,verify_receipt
from server.maintenance import validate_database

DOCKER=['docker','--host=unix:///var/run/docker.sock']
DOCKER_ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args,check=True):
    result=subprocess.run(DOCKER+list(args),env=DOCKER_ENV,capture_output=True,text=True)
    if check and result.returncode:raise RuntimeError('Docker operation failed: '+result.stderr[-1200:])
    return result


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def run(image,caddy,output):
    label='ksu-amvera-check-'+uuid4().hex[:12];volume=label+'-data';api_port=port();tls_port=port()
    origin=f'https://localhost:{tls_port}';running=[];proxy=None;results={}
    with tempfile.TemporaryDirectory(prefix='ksu-amvera-check-') as tmp:
        folder=Path(tmp)
        docker('volume','create',volume)
        def start(suffix,bootstrap=False,database='ksu.sqlite3',selected_image=None):
            name=label+suffix
            docker('run','-d','--name',name,'--memory','1g','--memory-swap','1g','--cpus','0.5','--pids-limit','128',
                   '--read-only','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=96m','--security-opt','no-new-privileges:true',
                   '-v',volume+':/data','-p',f'127.0.0.1:{api_port}:8000',
                   '-e','KSU_ORIGIN='+origin,'-e','KSU_BOOTSTRAP_DEMO='+('1' if bootstrap else '0'),
                   '-e','KSU_DB=/data/'+database,selected_image or image)
            running.append(name);return name
        def stop(name):docker('stop','--time','20',name);docker('rm',name);running.remove(name)
        def ready(client):
            for _ in range(200):
                try:
                    if client.get('/api/health',timeout=1).status_code==200:return
                except httpx.TransportError:pass
                time.sleep(.1)
            raise RuntimeError('Container HTTPS readiness failed')
        try:
            container=start('-one',True)
            config=folder/'Caddyfile'
            config.write_text('''{
              admin off
              auto_https disable_redirects
              skip_install_trust
              storage file_system {
                root %s
              }
            }
            https://localhost:%d {
              bind 127.0.0.1
              tls internal
              reverse_proxy 127.0.0.1:%d
            }
            '''%(folder/'caddy-state',tls_port,api_port))
            with (folder/'caddy.log').open('w') as log:
                proxy=subprocess.Popen([caddy,'run','--config',str(config),'--adapter','caddyfile'],stdout=log,stderr=log,
                                       env={**os.environ,'XDG_DATA_HOME':str(folder/'xdg'),'XDG_CONFIG_HOME':str(folder/'config')})
            ca=folder/'caddy-state/pki/authorities/local/root.crt'
            for _ in range(100):
                if ca.exists():break
                if proxy.poll() is not None:raise RuntimeError('Caddy failed to start')
                time.sleep(.1)
            context=ssl.create_default_context(cafile=str(ca))
            with httpx.Client(base_url=origin,verify=context,trust_env=False,timeout=60) as anonymous:
                ready(anonymous)
                # Capture generated test credentials into memory only; never print/store them in reports.
                account_text=docker('exec','--user','10001:10001',container,'cat','/data/demo-accounts.txt').stdout
                credentials=dict(re.findall(r'Логин: (\w+)\nПароль: ([^\n]+)',account_text))
                accounts=clients(origin,credentials,str(ca))
                try:
                    for client in accounts.values():client.timeout=httpx.Timeout(60)
                    receipt=create_receipt(origin,accounts);verify_receipt(receipt,accounts)
                    print('PASS: image bootstrap, real HTTPS, two accounts, file permissions',flush=True)
                    inv=uuid4().hex
                    def change(data):
                        response=accounts['inspector'].post('/api/changes',json={'mutationId':uuid4().hex,'changes':data})
                        if response.status_code!=200:raise RuntimeError('Synthetic fixture failed: HTTP '+str(response.status_code))
                    change([{'collection':'inventories','id':inv,'expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-01','title':'ДЕМО · Нагрузочный пересчёт','plannedPositions':1000}}])
                    change([{'collection':'inventoryLines','id':uuid4().hex,'expectedVersion':0,'data':{
                        'inventoryId':inv,'item':f'ДЕМО · ТМЦ {n}','category':'ДЕМО','expected':2,'actual':1,'price':10.5}} for n in range(1000)])
                    photo=io.BytesIO();Image.new('RGB',(4000,3000),'blue').save(photo,format='JPEG')
                    import base64
                    encoded=base64.b64encode(photo.getvalue()).decode()
                    measurements={'state':[],'xlsx':[],'pdf':[],'photo':[]}
                    def workload(role):
                        client=accounts[role]
                        for operation in ('state','xlsx','pdf','photo','state','xlsx','pdf'):
                            started=time.perf_counter()
                            if operation=='photo':response=client.post('/api/media',json={'siteId':'s1','content':encoded})
                            else:response=client.get('/api/state' if operation=='state' else '/api/reports/'+operation+'?kind=inventory&group=item')
                            if response.status_code!=200:raise RuntimeError(operation+' HTTP '+str(response.status_code))
                            if operation=='pdf' and not response.content.startswith(b'%PDF'):raise AssertionError('Invalid PDF')
                            if operation=='xlsx' and not response.content.startswith(b'PK'):raise AssertionError('Invalid XLSX')
                            measurements[operation].append(time.perf_counter()-started)
                    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(workload,('manager','inspector')))
                    peak=int(docker('exec',container,'cat','/sys/fs/cgroup/memory.peak').stdout.strip())
                    cgroup_memory=docker('exec',container,'cat','/sys/fs/cgroup/memory.max').stdout.strip()
                    cgroup_cpu=docker('exec',container,'cat','/sys/fs/cgroup/cpu.max').stdout.strip()
                    if cgroup_memory!='1073741824' or cgroup_cpu!='50000 100000':
                        raise AssertionError('The declared resource limits were not actually applied')
                    process_status=docker('exec',container,'cat','/proc/1/status').stdout
                    if not re.search(r'^Uid:\s+10001\s+10001\s+10001\s+10001$',process_status,re.M):
                        raise AssertionError('Runtime did not drop root privileges')
                    oom=docker('inspect','--format','{{.State.OOMKilled}}',container).stdout.strip()
                    if peak>=800*1024*1024 or oom!='false':raise AssertionError('Insufficient RAM headroom')
                    results={'scenario':'synthetic; two simultaneous sessions, 1000 inventory rows, 12 MP JPEG, grouped XLSX/PDF',
                             'configured_cpu':0.5,'configured_memory_bytes':1073741824,'memory_peak_bytes':peak,
                             'cgroup_memory_max':cgroup_memory,'cgroup_cpu_max':cgroup_cpu,'oom_killed':False,'runtime_uid':10001,
                             'latency_seconds':{key:{'count':len(values),'max':round(max(values),3),'mean':round(sum(values)/len(values),3)} for key,values in measurements.items()}}
                    print('PASS: concurrent reports/photo; peak MiB',round(peak/1024**2,1),'latencies',results['latency_seconds'],flush=True)
                    # The running supervisor must prevent two replicas on the same persistent data volume.
                    duplicate=docker('run','--rm','--read-only','--tmpfs','/tmp','-v',volume+':/data','-e','KSU_ORIGIN='+origin,image,check=False)
                    if duplicate.returncode==0 or 'Another replica' not in duplicate.stderr:raise AssertionError('Second replica was not rejected')
                    docker('restart','--time','20',container);ready(anonymous);verify_receipt(receipt,accounts)
                    # Actually rebuild a new image configuration, then replace the container.
                    rebuilt_image=label+'-rebuilt'
                    docker('build','--label','ksu.synthetic-rebuild='+label,'-t',rebuilt_image,'.')
                    old_id=docker('image','inspect','--format','{{.Id}}',image).stdout.strip()
                    rebuilt_id=docker('image','inspect','--format','{{.Id}}',rebuilt_image).stdout.strip()
                    if old_id==rebuilt_id:raise AssertionError('Rebuild did not produce a new image')
                    stop(container);container=start('-two',False,selected_image=rebuilt_image);ready(anonymous);verify_receipt(receipt,accounts)
                    docker('exec','--user','10001:10001',container,'python','-m','server','backup','/data/backups/acceptance.sqlite3','--db','/data/ksu.sqlite3')
                    docker('cp',container+':/data/backups/acceptance.sqlite3',str(folder/'independent.sqlite3'))
                    validate_database(folder/'independent.sqlite3')
                    docker('exec','--user','10001:10001',container,'python','-m','server','restore','/data/backups/acceptance.sqlite3','/data/restored.sqlite3')
                    for client in accounts.values():client.close()
                    stop(container);container=start('-restored',False,'restored.sqlite3');ready(anonymous)
                    accounts=clients(origin,credentials,str(ca));verify_receipt(receipt,accounts)
                    results.update({'restart_verified':True,'container_recreation_verified':True,'new_image_rebuild_verified':True,'photo_and_audit_persisted':True,
                                    'independent_copy_verified':True,'restored_copy_verified':True,'second_replica_rejected':True,
                                    'tls_verification':'trusted test CA; hostname verification on; no insecure flags',
                                    'image_id':docker('image','inspect','--format','{{.Id}}',image).stdout.strip()})
                    print('PASS: restart, container replacement, persistent photo/audit, independent backup/restore, single replica',flush=True)
                finally:
                    for client in accounts.values():client.close()
            Path(output).write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
        except Exception as error:
            if os.getenv('GITHUB_ACTIONS')=='true':
                # Surface the synthetic test failure through the Checks API as well as logs.
                message=(type(error).__name__+': '+str(error)).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
                print('::error title=Container acceptance::'+message,flush=True)
            for name in running:
                diagnostic=docker('logs','--tail','35',name,check=False).stderr[-3500:]
                print('Container diagnostic:',diagnostic)
                if os.getenv('GITHUB_ACTIONS')=='true':
                    message=diagnostic.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
                    print('::error title=Synthetic container traceback::'+message,flush=True)
            if (folder/'caddy.log').exists():print('Proxy diagnostic:',(folder/'caddy.log').read_text()[-1500:])
            raise
        finally:
            if proxy and proxy.poll() is None:proxy.terminate();proxy.wait(timeout=10)
            for name in running:docker('rm','-f',name,check=False)
            docker('volume','rm',volume,check=False)
            docker('image','rm',label+'-rebuilt',check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',default='ksu-amvera:issue4');parser.add_argument('--caddy',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();run(args.image,args.caddy,args.output)
