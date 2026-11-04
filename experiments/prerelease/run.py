"""Separate resource-limited API and PostgreSQL containers; never hosting-provider APIs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from tests.pg_environment import local_cluster,docker,test_environment,DOCKER,DOCKER_ENV


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',default='ksu-postgres:test');parser.add_argument('--output',required=True);parser.add_argument('--baseline-source',required=True);parser.add_argument('--block-restarts',action='store_true')
    args=parser.parse_args();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True);output.chmod(0o770)
    with local_cluster() as cluster:
        env=test_environment(cluster,container=True);env['KSU_PG_DATABASE_MAX_BYTES']='268435456';env['KSU_DATA_MAX_BYTES']='2147483648'
        private=cluster['private']/'load.env';private.write_text(''.join(k+'='+v+'\n' for k,v in env.items() if k.startswith(('KSU_TEST_PG_','KSU_PG_DATABASE_MAX_BYTES','KSU_DATA_MAX_BYTES'))));private.chmod(0o600)
        stop=threading.Event();db_resources={};app_name=cluster['network']+'-acceptance'
        def sample():
            while not stop.wait(1):
                for key in ('source','restored'):
                    name=cluster['network']+'-'+key
                    result=subprocess.run(DOCKER+['exec',name,'sh','-c','cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.stat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.events; du -sb /var/lib/postgresql/data'],env=DOCKER_ENV,capture_output=True,text=True)
                    if result.returncode:continue
                    lines=result.stdout.splitlines();stat={k:int(v) for k,v in (line.split() for line in lines[1:-1])}
                    item=db_resources.setdefault(key,{'total_peak_bytes':0,'anon_peak_bytes':0,'file_cache_peak_bytes':0,'disk_peak_bytes':0,'cpu_limit':0.5,'memory_limit_bytes':1073741824})
                    item['total_peak_bytes']=max(item['total_peak_bytes'],int(lines[0]));item['anon_peak_bytes']=max(item['anon_peak_bytes'],stat['anon']);item['file_cache_peak_bytes']=max(item['file_cache_peak_bytes'],stat['file'])
                    item['cpu_usage_seconds']=round(stat['usage_usec']/1e6,3);item['oom_kills']=stat['oom_kill']
                    item['cpu_throttled_seconds']=round(stat['throttled_usec']/1e6,3);item['disk_peak_bytes']=max(item['disk_peak_bytes'],int(lines[-1].split()[0]))
        watcher=threading.Thread(target=sample,daemon=True);watcher.start()
        try:
            with (output/'prerelease-log.txt').open('w') as log:
                result=docker('run','--name',app_name,'--network',cluster['network'],'--user','1000:1000','--group-add',str(os.getgid()),
                    '--cpus','0.5','--memory','1g','--memory-swap','1g','--pids-limit','128','--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=32m',
                    '--env-file',str(private),'-v',str(Path(args.baseline_source).resolve())+':/baseline:ro','-v',str(Path.cwd())+':/source:ro','-v',str(output)+':/out:rw',
                    '-v',str(cluster['pki']/'cert.pem')+':/certs/cert.pem:ro','-e','PYTHONPATH=/source','-e','KSU_TEST_DATA_DIR=/out',*(['-e','KSU_PRERELEASE_BLOCK_RESTARTS=1'] if args.block_restarts else []),'-w','/source',
                    args.image,'python','-m','experiments.prerelease.acceptance','/out/prerelease-metrics.json',stdout=log,stderr=log,check=False)
            if result.returncode:raise RuntimeError('PostgreSQL large acceptance failed: '+str(result.returncode))
        finally:
            # Whitelist fields: Docker inspect without a format would expose private env/DSNs.
            inspected=docker('inspect','--format','{{json .State}}',app_name,check=False,capture_output=True,text=True)
            if inspected.returncode==0:
                state=json.loads(inspected.stdout)
                (output/'prerelease-container.json').write_text(json.dumps({key:state[key] for key in ('Status','OOMKilled','ExitCode')},indent=2)+'\n')
            docker('rm','-f',app_name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            stop.set();watcher.join(timeout=10)
            (output/'prerelease-db-resources.json').write_text(json.dumps(db_resources,indent=2)+'\n')
        assert all(item['oom_kills']==0 for item in db_resources.values())
    print('PASS: 120 paired PostgreSQL samples, profiles, plans, exact metrics and RLS; caps unchanged')


if __name__=='__main__':main()
