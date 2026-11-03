"""Disposable local-only PostgreSQL/TLS harness. Nothing calls a hosting provider."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from server.pg_admin import initialize,migrate_sqlite

IMAGE='postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
DOCKER=['docker','--host=unix:///var/run/docker.sock']
DOCKER_ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args,check=True,**kwargs):
    result=subprocess.run(DOCKER+list(args),env=DOCKER_ENV,check=False,**kwargs)
    if check and result.returncode:raise RuntimeError('Local Docker command failed: exit '+str(result.returncode))
    return result


def unused_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def prepare_browser_database(source):
    dsn=os.getenv('KSU_BROWSER_PG_ADMIN_DSN')
    if dsn:migrate_sqlite(source,dsn)


@contextmanager
def local_cluster():
    label='ksu-pg-'+uuid4().hex[:12];names=[]
    with tempfile.TemporaryDirectory(prefix='ksu-pg-private-') as folder:
        private=Path(folder);pki=private/'pki';pki.mkdir()
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(pki/'key.pem'),
            '-out',str(pki/'cert.pem'),'-days','2','-subj','/CN=KSU synthetic local test',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:ksu-pg-source,DNS:ksu-pg-restored'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        password=secrets.token_urlsafe(24);runtime_password=secrets.token_urlsafe(24);backup_password=secrets.token_urlsafe(24)
        envfile=private/'server.env';envfile.write_text('POSTGRES_PASSWORD='+password+'\n');envfile.chmod(0o600)
        docker('network','create',label,stdout=subprocess.DEVNULL)
        try:
            values={}
            for key,alias in [('source','ksu-pg-source'),('restored','ksu-pg-restored')]:
                name=label+'-'+key;names.append(name);port=unused_port()
                startup='cp /test-certs/* /tmp/ && chown postgres:postgres /tmp/key.pem /tmp/cert.pem && chmod 600 /tmp/key.pem && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/cert.pem -c ssl_key_file=/tmp/key.pem'
                docker('run','-d','--name',name,'--network',label,'--network-alias',alias,'--cpus','0.5','--memory','1g','--memory-swap','1g',
                       '--env-file',str(envfile),'-v',str(pki)+':/test-certs:ro','-p',f'127.0.0.1:{port}:5432',IMAGE,'bash','-c',startup,stdout=subprocess.DEVNULL)
                dsn=make_conninfo(host='127.0.0.1',port=port,user='postgres',password=password,dbname='postgres',sslmode='verify-full',sslrootcert=str(pki/'cert.pem'))
                for _ in range(150):
                    try:
                        with psycopg.connect(dsn,connect_timeout=1) as connection:
                            connection.execute(sql.SQL('CREATE ROLE ksu_runtime LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS PASSWORD {}').format(sql.Literal(runtime_password)))
                            connection.execute(sql.SQL('CREATE ROLE ksu_backup LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD {}').format(sql.Literal(backup_password)))
                        break
                    except psycopg.OperationalError:time.sleep(.1)
                else:raise RuntimeError('Local PostgreSQL startup failed')
                runtime=make_conninfo(**{**conninfo_to_dict(dsn),'user':'ksu_runtime','password':runtime_password})
                values[key]={'admin':dsn,'runtime':runtime,'backup':make_conninfo(**{**conninfo_to_dict(dsn),'user':'ksu_backup','password':backup_password})}
                for field in ('admin','runtime','backup'):
                    values[key]['container_'+field]=make_conninfo(**{**conninfo_to_dict(values[key][field]),'host':alias,'port':5432,'sslrootcert':'/certs/cert.pem'})
            yield {'network':label,'pki':pki,'private':private,**values}
        finally:
            for name in names:docker('rm','-fv',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            docker('network','rm',label,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def test_environment(cluster,container=False):
    prefix='container_' if container else ''
    return {**os.environ,'KSU_TEST_PG_ADMIN_DSN':cluster['source'][prefix+'admin'],
            'KSU_TEST_PG_RUNTIME_DSN':cluster['source'][prefix+'runtime'],'KSU_TEST_PG_BACKUP_DSN':cluster['source'][prefix+'backup'],
            'KSU_TEST_PG_RESTORE_ADMIN_DSN':cluster['restored'][prefix+'admin'],
            'KSU_TEST_PG_RESTORE_RUNTIME_DSN':cluster['restored'][prefix+'runtime']}


def packaged_cli(cluster,image,output):
    """Exercise the actual image modules used by the Windows offline scripts."""
    from server.database import Database,seed_demo
    name='ksu_cli_'+uuid4().hex;role='ksu_cli_role_'+uuid4().hex
    password=secrets.token_urlsafe(24);login_password=secrets.token_urlsafe(24)
    live=cluster['private']/'cli-live.sqlite3';seed_demo(Database(live),login_password,secrets.token_urlsafe(24))
    source=cluster['private']/'cli-source.sqlite3'
    from server.maintenance import snapshot_database
    import sqlite3
    snapshot_database(live,source)
    # A prepared standalone snapshot, not a hidden-WAL main-file mount.
    with sqlite3.connect(source) as con:con.execute('pragma journal_mode=delete')
    work=cluster['private']/'cli-work';work.mkdir()
    host_admin=make_conninfo(**{**conninfo_to_dict(cluster['source']['admin']),'dbname':name})
    admin=make_conninfo(**{**conninfo_to_dict(cluster['source']['container_admin']),'dbname':name})
    runtime=make_conninfo(**{**conninfo_to_dict(admin),'user':role,'password':password})
    values={'KSU_PG_ADMIN_DSN':admin,'KSU_PG_RUNTIME_PASSWORD':password,'KSU_DATABASE_URL':runtime,
        'KSU_WORK_DIR':'/work/staging','KSU_PG_MIGRATION_DIR':'/work','KSU_ORIGIN':'http://testserver',
        'KSU_SECURE_COOKIES':'0','KSU_REQUIRE_INITIALIZED':'1','KSU_TEST_LOGIN_PASSWORD':login_password}
    envfile=cluster['private']/'cli.env';envfile.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));envfile.chmod(0o600)
    args=['run','--rm','--network',cluster['network'],'--user',str(os.getuid())+':'+str(os.getgid()),
        '--env-file',str(envfile),'-v',str(work)+':/work:rw','-v',str(source)+':/input/source.sqlite3:ro',
        '-v',str(cluster['pki']/'cert.pem')+':/certs/cert.pem:ro','--entrypoint','python',image]
    with psycopg.connect(cluster['source']['admin'],autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with (output/'packaged-cli.txt').open('w') as log:
            for command in (['provision-roles','--runtime-role',role],['init','--runtime-role',role],['migrate-sqlite','/input/source.sqlite3']):
                docker(*args,'-m','server.pg_admin',*command,stdout=log,stderr=log)
            probe="import os; from fastapi.testclient import TestClient; from server.app import create_app; app=create_app(); c=TestClient(app,headers={'Origin':'http://testserver'}); r=c.post('/api/login',json={'username':'manager','password':os.environ['KSU_TEST_LOGIN_PASSWORD']}); assert r.status_code==200; assert app.state.db.backend=='postgresql'; print('PASS: packaged CLI provisioning/migration and restricted PostgreSQL login')"
            docker(*args,'-c',probe,stdout=log,stderr=log)
    finally:
        with psycopg.connect(cluster['source']['admin'],autocommit=True) as con:
            con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            con.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


def main():
    import argparse
    import sys
    parser=argparse.ArgumentParser();parser.add_argument('--image',default='ksu-postgres:test');parser.add_argument('--output',required=True);parser.add_argument('--backup-only',action='store_true')
    args=parser.parse_args();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True);output.chmod(0o770)
    if not args.backup_only:
        with local_cluster() as cluster:
            env=test_environment(cluster)
            with (output/'postgres-tests.txt').open('w') as log:
                subprocess.run([sys.executable,'-m','unittest','tests.test_postgres','-v'],env=env,stdout=log,stderr=log,check=True)
            packaged_cli(cluster,args.image,output)
            for script in ('smoke_test.py','v11_flow_test.py','visits_flow_test.py','decisions_flow_test.py','calendar_flow_test.py'):
                name='ksu_browser_'+uuid4().hex
                admin=make_conninfo(**{**conninfo_to_dict(cluster['source']['admin']),'dbname':name})
                runtime=make_conninfo(**{**conninfo_to_dict(cluster['source']['runtime']),'dbname':name})
                with psycopg.connect(cluster['source']['admin'],autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
                try:
                    initialize(admin,'ksu_runtime')
                    with (output/(script+'.txt')).open('w') as log:
                        subprocess.run([sys.executable,script],env={**env,'KSU_BROWSER_PG_ADMIN_DSN':admin,'KSU_DATABASE_URL':runtime,
                            'KSU_WORK_DIR':str(cluster['private']/name)},stdout=log,stderr=log,check=True)
                finally:
                    with psycopg.connect(cluster['source']['admin'],autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    # Acceptance runs in a real cgroup: app/client/migration/dump all share 0.5 CPU / 1 GiB.
    with (output/'independent-backup.txt').open('w') as log:
        docker('run','--rm','--network','none','--user','1000:1000','--group-add',str(os.getgid()),
            '--cpus','0.5','--memory','1g','--memory-swap','1g','--pids-limit','128',
            '--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=32m',
            '-v',str(Path.cwd())+':/source:ro','-v',str(output)+':/out:rw',
            '-e','PYTHONPATH=/source','-e','KSU_TEST_DATA_DIR=/out','-w','/source',args.image,
            'python','-m','experiments.postgres.local_stack','/out/postgres-backup-metrics.json',stdout=log,stderr=log)
    print('PASS: real PostgreSQL contracts, five browser scenarios and independent >=150 MiB backup/restore')


if __name__=='__main__':main()
