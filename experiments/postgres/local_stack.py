"""One cgroup for both independent local PostgreSQL instances and the complete acceptance."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import threading
import json

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def run(output):
    with tempfile.TemporaryDirectory(prefix='local-pg-',dir=os.environ['KSU_TEST_DATA_DIR']) as folder:
        root=Path(folder);password=secrets.token_urlsafe(24);runtime_password=secrets.token_urlsafe(24);backup_password=secrets.token_urlsafe(24)
        password_file=root/'password';password_file.write_text(password);password_file.chmod(0o600)
        started=[];stop=threading.Event();peaks={'anon':0,'file':0}
        def monitor():
            while not stop.wait(.1):
                values=dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
                for key in peaks:peaks[key]=max(peaks[key],int(values[key]))
        watcher=threading.Thread(target=monitor,daemon=True);watcher.start()
        try:
            for name,prefix in [('source',''),('restored','RESTORE_')]:
                directory=root/name
                with (root/(name+'.log')).open('w') as log:
                    subprocess.run(['initdb','-D',str(directory),'--no-locale','--encoding=UTF8','--auth-local=trust',
                        '--auth-host=scram-sha-256','--username=postgres','--pwfile='+str(password_file)],stdout=log,stderr=log,check=True)
                    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                    with (directory/'postgresql.conf').open('a') as config:
                        config.write("\nlisten_addresses='127.0.0.1'\nport="+str(port)+"\nshared_buffers='16MB'\nwork_mem='2MB'\nmax_connections=20\nunix_socket_directories=''\n")
                    subprocess.run(['pg_ctl','-D',str(directory),'-l',str(root/(name+'-server.log')),'-w','start'],stdout=log,stderr=log,check=True)
                    started.append(directory)
                admin=make_conninfo(host='127.0.0.1',port=port,user='postgres',password=password,dbname='postgres')
                with psycopg.connect(admin) as con:
                    con.execute(sql.SQL('CREATE ROLE ksu_runtime LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS PASSWORD {}').format(sql.Literal(runtime_password)))
                    con.execute(sql.SQL('CREATE ROLE ksu_backup LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD {}').format(sql.Literal(backup_password)))
                os.environ['KSU_TEST_PG_'+prefix+'ADMIN_DSN']=admin
                os.environ['KSU_TEST_PG_'+prefix+'RUNTIME_DSN']=make_conninfo(host='127.0.0.1',port=port,user='ksu_runtime',password=runtime_password,dbname='postgres')
                if not prefix:os.environ['KSU_TEST_PG_BACKUP_DSN']=make_conninfo(host='127.0.0.1',port=port,user='ksu_backup',password=backup_password,dbname='postgres')
            from .acceptance import run as accept
            accept(output)
            metrics=json.loads(Path(output).read_text())
            events=dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
            assert int(events['oom_kill'])==0 and metrics['memory_limit']=='1073741824' and metrics['cpu_limit']=='50000 100000'
            metrics.update(whole_stack_one_cgroup=True,anonymous_memory_peak_bytes=peaks['anon'],file_cache_peak_bytes=peaks['file'],oom_kills=int(events['oom_kill']))
            Path(output).write_text(json.dumps(metrics,indent=2)+'\n')
        except Exception:
            for log in root.glob('*-server.log'):
                text=log.read_text()[-2000:]
                for secret in (password,runtime_password,backup_password):text=text.replace(secret,'[redacted]')
                print(text,file=sys.stderr)
            raise
        finally:
            stop.set();watcher.join(timeout=2)
            for directory in started:subprocess.run(['pg_ctl','-D',str(directory),'-m','fast','-w','stop'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':run(sys.argv[1])
