"""Disposable PostgreSQL 17 TLS clusters with production's three non-superuser roles."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.pg_environment import docker,unused_port,IMAGE
from server.managed_pg import bootstrap


@contextmanager
def environment(one_cluster=False,amvera=False):
    if amvera and one_cluster:raise ValueError('Amvera acceptance requires independent restore cluster')
    label='ksu-managed-'+uuid4().hex[:10];names=[]
    with tempfile.TemporaryDirectory(prefix='ksu-managed-synthetic-') as folder:
        root=Path(folder);pki=root/'tls';pki.mkdir()
        cert=pki/'cert.pem';key=pki/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU synthetic managed',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:ksu-managed-source,DNS:ksu-managed-restored',
            '-out',str(cert),'-keyout',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        passwords={role:secrets.token_urlsafe(32) for role in ('ksu_admin','ksu_runtime','ksu_backup')}
        super_password=secrets.token_urlsafe(32);env_file=root/'server.env';env_file.write_text('POSTGRES_PASSWORD='+super_password+'\n');env_file.chmod(0o600)
        docker('network','create',label,stdout=subprocess.DEVNULL)
        values={}
        try:
            for kind,dbname in (('source','ksu_app'),('restored','ksu_restore')):
                alias='ksu-managed-'+kind
                if one_cluster and kind=='restored':port=values['source']['port'];alias='ksu-managed-source'
                else:
                    name=label+'-'+kind;names.append(name);port=unused_port()
                    startup='cp /test-tls/* /tmp/ && chown postgres:postgres /tmp/key.pem /tmp/cert.pem && chmod 600 /tmp/key.pem && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/cert.pem -c ssl_key_file=/tmp/key.pem'
                    if amvera and kind=='source':startup=startup.replace('ssl=on','ssl=off')
                    aliases=['--network-alias','cnpg-instrakt-ksu-rw','--network-alias','synthetic-plain.invalid'] if amvera and kind=='source' else []
                    docker('run','-d','--name',name,'--network',label,'--network-alias',alias,*aliases,'--cpus','0.5','--memory','1g','--memory-swap','1g',
                        '--tmpfs','/var/lib/postgresql/data:rw,nosuid,nodev,size=512m','--env-file',str(env_file),'-v',str(pki)+':/test-tls:ro',
                        '-p',f'127.0.0.1:{port}:5432',IMAGE,'bash','-c',startup,stdout=subprocess.DEVNULL)
                common=dict(host='127.0.0.1',port=port,sslmode='disable' if amvera and kind=='source' else 'verify-full',sslrootcert=str(cert))
                super_dsn=make_conninfo(**common,dbname='postgres',user='postgres',password=super_password)
                for _ in range(150):
                    try:
                        with psycopg.connect(super_dsn,connect_timeout=1,autocommit=True) as con:con.execute(sql.SQL('create database {}').format(sql.Identifier(dbname)))
                        break
                    except psycopg.OperationalError:time.sleep(.1)
                else:raise AssertionError('Synthetic PostgreSQL did not start')
                target=make_conninfo(**common,dbname=dbname,user='postgres',password=super_password)
                if amvera and kind=='source':pass  # Exact release CLI bootstraps through the synthetic ingress.
                elif not one_cluster or kind=='source':bootstrap(target,passwords)
                else:
                    with psycopg.connect(target) as con:
                        con.execute(sql.SQL('revoke connect,create,temporary on database {} from public').format(sql.Identifier(dbname)))
                        con.execute('revoke create on schema public from public')
                        for role in passwords:con.execute(sql.SQL('grant connect on database {} to {}').format(sql.Identifier(dbname),sql.Identifier(role)))
                        con.execute(sql.SQL('grant create on database {} to ksu_admin').format(sql.Identifier(dbname)))
                entry={'port':port,'root':super_dsn,'database':dbname,
                    'container_owner':make_conninfo(host=alias,port=5432,dbname=dbname,user='postgres',password=super_password,sslmode='verify-full',sslrootcert='/tls/cert.pem')}
                for role in passwords:
                    field=role.removeprefix('ksu_')
                    entry[field]=make_conninfo(**common,dbname=dbname,user=role,password=passwords[role])
                    entry['container_'+field]=make_conninfo(host=alias,port=5432,dbname=dbname,user=role,password=passwords[role],sslmode='verify-full',sslrootcert='/tls/cert.pem')
                values[kind]=entry
            yield {'root':root,'pki':pki,'network':label,'passwords':passwords,**values}
        finally:
            for name in names:docker('rm','-fv',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            docker('network','rm',label,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
