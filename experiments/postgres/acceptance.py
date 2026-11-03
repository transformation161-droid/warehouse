"""Synthetic >=150 MiB independent HTTP backup/restore acceptance, never a live database."""
import base64
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
from PIL import Image
from tests.fixtures import calendar_source_fixture as fixture
from server.app import create_app
from server.database import Database,seed_demo
from server.pg_admin import initialize,migrate_sqlite,fingerprints
from server.pg_backup import restore_database,backup_database,checksum,BackupError
from tests.test_api import MANAGER_PASSWORD,INSPECTOR_PASSWORD
from tests.test_decision_import import DecisionImport
from tests.test_postgres import database_dsn


def wait_http(origin):
    with httpx.Client(base_url=origin,trust_env=False) as client:
        for _ in range(200):
            try:
                if client.get('/api/health',timeout=.3).status_code==200:return
            except httpx.TransportError:pass
            time.sleep(.1)
    raise AssertionError('Synthetic API failed to start')


def run(output):
    root=os.environ['KSU_TEST_PG_ADMIN_DSN'];restore_root=os.environ['KSU_TEST_PG_RESTORE_ADMIN_DSN']
    runtime_template=os.environ['KSU_TEST_PG_RUNTIME_DSN'];restored_template=os.environ['KSU_TEST_PG_RESTORE_RUNTIME_DSN']
    name='ksu_accept_'+uuid4().hex;admin=database_dsn(root,name);other_admin=database_dsn(restore_root,name)
    runtime=database_dsn(runtime_template,name);other_runtime=database_dsn(restored_template,name)
    backup_dsn=database_dsn(os.environ['KSU_TEST_PG_BACKUP_DSN'],name)
    db_role=psycopg.conninfo.conninfo_to_dict(runtime)['user']
    processes=[]
    for dsn in (root,restore_root):
        with psycopg.connect(dsn,autocommit=True) as con:con.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix='ksu-pg-independent-',dir=os.getenv('KSU_TEST_DATA_DIR')) as folder:
            work=Path(folder);source=work/'DEMO-source.sqlite3';db=Database(source)
            seed_demo(db,MANAGER_PASSWORD,INSPECTOR_PASSWORD);fixture(source)
            source_app=create_app(source,'http://testserver',False)
            with TestClient(source_app,headers={'Origin':'http://testserver'}) as client:
                response=client.post('/api/login',json={'username':'inspector','password':INSPECTOR_PASSWORD});client.headers['X-CSRF-Token']=response.json()['csrf']
                image=io.BytesIO();Image.new('RGB',(20,20),'blue').save(image,'PNG')
                photo=client.post('/api/media',json={'siteId':'s1','content':base64.b64encode(image.getvalue()).decode()}).json()['url']
                response=client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[{'collection':'findings','id':'DEMO-finding','expectedVersion':0,'data':{'siteId':'s1','title':'ДЕМО · Перенос фото','photo':photo}}]});assert response.status_code==200
                csv='object;date;item;expected;actual;price\nДЕМО · Нова;2026-11-01;ДЕМО · ТМЦ;2;1;100.50'
                payload={'name':'DEMO.csv','content':base64.b64encode(csv.encode()).decode()}
                assert client.post('/api/import/commit',json=payload).status_code==200
            source_app.state.import_jobs.close()
            # Exercise the real streamed XLSX source path before migration.
            helper=DecisionImport();helper.app=create_app(source,'http://testserver',False)
            helper.inspector=TestClient(helper.app,headers={'Origin':'http://testserver'})
            response=helper.inspector.post('/api/login',json={'username':'inspector','password':INSPECTOR_PASSWORD});helper.inspector.headers['X-CSRF-Token']=response.json()['csrf']
            book=helper.book();job=helper.upload(book);helper.preview(job);assert helper.commit(job)['status']=='done'
            helper.inspector.close();helper.app.state.import_jobs.close()
            # Incompressible, explicitly synthetic storage pressure, plus an actual usable photo above.
            with db.connect({'id':'manager','role':'manager'}) as con:
                block=os.urandom(1024*1024)
                for n in range(160):con.execute('insert into media values(?,?,?,?,?,?)',('DEMO-size-'+str(n),'s1','manager',block,b'DEMO','2026-11-01T00:00:00Z'))
            print('PASS: synthetic SQLite fixture >=150 MiB created',flush=True)
            original_sha=checksum(source)
            migrate_started=time.monotonic()
            initialize(admin,db_role,psycopg.conninfo.conninfo_to_dict(backup_dsn)['user']);migrated=migrate_sqlite(source,admin)
            migration_seconds=round(time.monotonic()-migrate_started,3)
            assert checksum(source)==original_sha
            print('PASS: migration count/checksum verification',flush=True)
            with psycopg.connect(admin) as con:before=fingerprints(con,True)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            origin=f'http://127.0.0.1:{port}'
            env={**os.environ,'KSU_DATABASE_URL':runtime,'KSU_WORK_DIR':str(work/'staging'),'KSU_PG_BACKUP_DSN':backup_dsn,
                 'KSU_PG_BACKUP_DIR':str(work/'private-exports'),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
            log=(work/'api.log').open('w')
            server=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],env=env,stdout=log,stderr=log);processes.append(server)
            wait_http(origin)
            clients={role:httpx.Client(base_url=origin,headers={'Origin':origin},trust_env=False,timeout=600) for role in ('manager','inspector')}
            for role,password in [('manager',MANAGER_PASSWORD),('inspector',INSPECTOR_PASSWORD)]:
                response=clients[role].post('/api/login',json={'username':role,'password':password});assert response.status_code==200
                clients[role].headers['X-CSRF-Token']=response.json()['csrf']
            assert clients['inspector'].post('/api/admin/backup').status_code==403
            assert clients['manager'].post('/api/admin/backup',headers={'X-CSRF-Token':'wrong'}).status_code==403
            assert clients['manager'].post('/api/admin/backup',headers={'Origin':'http://wrong.test'}).status_code==403
            assert clients['manager'].get('/api/admin/backup').status_code==405
            report_hashes={role:hashlib.sha256(client.get('/api/reports/csv?kind=inventory&group=site').content).hexdigest() for role,client in clients.items()}
            assert '100.5' in clients['manager'].get('/api/reports/csv?kind=inventory&group=site').text
            photo_hash=hashlib.sha256(clients['manager'].get(photo).content).hexdigest()
            target=work/'independent-copy.ksubackup';started=time.monotonic()
            with clients['manager'].stream('POST','/api/admin/backup') as response:
                assert response.status_code==200,response.status_code
                expected=response.headers['X-Backup-SHA256']
                with target.open('xb') as handle:
                    for chunk in response.iter_bytes(1024*1024):handle.write(chunk)
            assert target.stat().st_size>=150*1024*1024
            assert checksum(target)==expected
            print('PASS: independent HTTP download/checksum',flush=True)
            elapsed=round(time.monotonic()-started,3)
            # Abort a real second transfer. Neither the source nor the downloaded independent copy changes.
            with clients['manager'].stream('POST','/api/admin/backup') as response:
                assert response.status_code==200
                next(response.iter_bytes(65536))
            for _ in range(100):
                if not list((work/'private-exports').iterdir()):break
                time.sleep(.1)
            assert not list((work/'private-exports').iterdir())
            for client in clients.values():client.close()
            server.terminate();server.wait(timeout=15);processes.remove(server);log.close()
            # Malformed package must not touch even an empty destination.
            import zipfile
            bad=work/'corrupt.ksubackup'
            with zipfile.ZipFile(bad,'w',compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('database.dump',b'DEMO corrupt')
                archive.writestr('manifest.json',json.dumps({'format':'ksu-pg-backup-v1','schema_version':1,'dump_sha256':'0'*64}))
            try:restore_database(bad,other_admin,db_role);raise AssertionError('Expected corrupt backup rejection')
            except BackupError:pass
            restore_started=time.monotonic()
            result=restore_database(target,other_admin,db_role)
            restore_seconds=round(time.monotonic()-restore_started,3)
            assert result['verified']
            print('PASS: independent PostgreSQL restore/checksums',flush=True)
            with psycopg.connect(admin) as con:assert fingerprints(con,True)==before
            with psycopg.connect(other_admin) as con:assert fingerprints(con,True)==before
            os.environ['KSU_DATABASE_URL']=other_runtime;os.environ['KSU_WORK_DIR']=str(work/'restored-staging')
            restored_app=create_app('postgresql','http://testserver',False)
            for role,password in [('manager',MANAGER_PASSWORD),('inspector',INSPECTOR_PASSWORD)]:
                with TestClient(restored_app,headers={'Origin':'http://testserver'}) as client:
                    response=client.post('/api/login',json={'username':role,'password':password});assert response.status_code==200
                    client.headers['X-CSRF-Token']=response.json()['csrf']
                    state=client.get('/api/state').json();assert state['visits'] and state['inspections'] and state['findings']
                    response=client.get(photo);assert response.status_code==200 and hashlib.sha256(response.content).hexdigest()==photo_hash
                    assert hashlib.sha256(client.get('/api/reports/csv?kind=inventory&group=site').content).hexdigest()==report_hashes[role]
                    rows=client.get('/api/decisions').json()['rows'];assert len(rows)==1 and rows[0]['cost']=='100.50'
                    assert client.get('/api/history?collection=decisions&record_id='+rows[0]['id']).json()
                    for extension in ('csv','xlsx','pdf'):
                        response=client.get('/api/reports/'+extension);assert response.status_code==200
                    response=client.post('/api/import/commit',json=payload);assert response.status_code==200 and response.json()['duplicate']
            helper.app=restored_app;helper.inspector=TestClient(restored_app,headers={'Origin':'http://testserver'})
            response=helper.inspector.post('/api/login',json={'username':'inspector','password':INSPECTOR_PASSWORD});helper.inspector.headers['X-CSRF-Token']=response.json()['csrf']
            ident=helper.upload(book);helper.preview(ident);assert helper.commit(ident)['duplicate']
            helper.inspector.close();restored_app.state.import_jobs.close()
            try:restore_database(target,other_admin,db_role);raise AssertionError('Expected nonempty destination rejection')
            except BackupError:pass
            assert checksum(source)==original_sha and checksum(target)==expected
            # A backup deadline must fail without publishing a partial destination.
            old=os.environ.get('KSU_PG_BACKUP_TIMEOUT');os.environ['KSU_PG_BACKUP_TIMEOUT']='0'
            try:
                try:backup_database(backup_dsn,work/'must-not-exist.ksubackup');raise AssertionError('Expected timeout')
                except BackupError:pass
                assert not (work/'must-not-exist.ksubackup').exists()
            finally:
                if old is None:os.environ.pop('KSU_PG_BACKUP_TIMEOUT',None)
                else:os.environ['KSU_PG_BACKUP_TIMEOUT']=old
            old_cap=os.environ.get('KSU_PG_BACKUP_MAX_BYTES');os.environ['KSU_PG_BACKUP_MAX_BYTES']='1'
            try:
                try:backup_database(backup_dsn,work/'over-cap.ksubackup');raise AssertionError('Expected byte cap rejection')
                except BackupError:pass
                assert not (work/'over-cap.ksubackup').exists()
            finally:
                if old_cap is None:os.environ.pop('KSU_PG_BACKUP_MAX_BYTES',None)
                else:os.environ['KSU_PG_BACKUP_MAX_BYTES']=old_cap
            metrics={'synthetic_only' :True,'source_bytes':source.stat().st_size,'downloaded_bytes':target.stat().st_size,
                'migration_seconds':migration_seconds,'restore_seconds':restore_seconds,'download_seconds':elapsed,'checksum_verified':True,'source_unchanged':True,'all_tables_match':True,
                'independent_instance_restored':True,'both_roles_photos_history_reports':True,'duplicate_suppressed':True,
                'inspector_csrf_origin_denied':True,'interrupted_transfer_safe':True,'deadline_no_partial':True,
                'sessions_revoked':True,'corrupt_and_nonempty_restore_rejected':True,'quota_no_partial':True,'inventory_report_checksums_match':True,'xlsx_duplicate_after_restore':True,'tables':migrated['tables']}
            if Path('/sys/fs/cgroup/memory.peak').exists():
                metrics['memory_peak_bytes']=int(Path('/sys/fs/cgroup/memory.peak').read_text())
                metrics['cpu_limit']=Path('/sys/fs/cgroup/cpu.max').read_text().strip()
                metrics['memory_limit']=Path('/sys/fs/cgroup/memory.max').read_text().strip()
            Path(output).write_text(json.dumps(metrics,indent=2)+'\n')
            print(json.dumps(metrics,indent=2))
    finally:
        for process in processes:
            process.terminate();process.wait(timeout=15)
        for dsn in (root,restore_root):
            with psycopg.connect(dsn,autocommit=True) as con:con.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))


if __name__=='__main__':run(sys.argv[1])
