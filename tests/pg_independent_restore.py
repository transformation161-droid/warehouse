"""Real launcher -> manager HTTP backup -> source stopped -> independent PG restore."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from unittest.mock import patch
import zipfile

import httpx
import psycopg
from fastapi.testclient import TestClient
from server.database import Database, seed_demo
from server.maintenance import snapshot_database
from server.pg_admin import fingerprints
from server.pg_backup import BackupError, restore_database
from tests.pg_environment import local_cluster, docker, unused_port, DOCKER_ENV
from tests.pg_tools import prepare


def main():
    pwsh=shutil.which('pwsh')
    if not pwsh:raise RuntimeError('PowerShell 7 (pwsh) required to test the actual launcher')
    with tempfile.TemporaryDirectory(prefix='ksu-launcher-http-') as temporary, local_cluster() as cluster:
        folder=Path(temporary);project=folder/'project';project.mkdir()
        # Only tracked source files: no local data, passwords, .git or virtual environments.
        for name in subprocess.check_output(['git','ls-files'],text=True).splitlines():
            source=Path(name)
            if source.is_file():
                destination=project/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,destination)
        passwords={name:secrets.token_urlsafe(24) for name in ('manager','inspector')}
        live=folder/'live.sqlite3';source=folder/'check1.sqlite3'
        seed_demo(Database(live),passwords['manager'],passwords['inspector'])
        snapshot_database(live,source)
        # Standalone fixture, same contract as packaged_cli: no writable WAL sidecars.
        with sqlite3.connect(source) as connection:connection.execute('pragma journal_mode=delete')
        # GitHub's host uid can differ from image uid 1000. Only this synthetic
        # read-only fixture is readable inside Docker; its host parent remains 0700.
        source.chmod(0o644)
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        port=unused_port();origin=f'http://localhost:{port}'
        state_file=project/'var/windows-postgres.json'
        def launch(*args):
            subprocess.run([pwsh,'-NoProfile','-File',str(project/'tools/Start-Postgres.ps1'),*args],
                           env=DOCKER_ENV,check=True,timeout=240)
        try:
            launch('-Source',str(source),'-AppImage','ksu-postgres:test','-Port',str(port))
            state=json.loads(state_file.read_text(encoding='utf-8-sig'))
            migration=json.loads((project/'var/windows-postgres-migration.json').read_text(encoding='utf-8-sig'))
            def check_directory():
                probe="import os,stat; p=os.environ['KSU_PG_BACKUP_DIR']; s=os.stat(p); assert p=='/work/backups'; assert s.st_uid==1000 and s.st_gid==1000; assert stat.S_IMODE(s.st_mode)==0o700; assert os.access(p,os.W_OK); print('PASS: persistent backup directory uid/gid=1000:1000 mode=0700')"
                docker('exec',state['app'],'python','-c',probe)
            check_directory()
            # Real stop/start must retain the directory and application backup configuration.
            launch('-Action','Stop');launch()
            check_directory()
            bundle=folder/'independent.ksubackup'
            for username in ('inspector','manager'):
                with httpx.Client(base_url=origin,headers={'Origin':origin},trust_env=False,timeout=120) as client:
                    response=client.post('/api/login',json={'username':username,'password':passwords[username]})
                    assert response.status_code==200,(username,response.status_code)
                    csrf=response.json()['csrf']
                    with client.stream('POST','/api/admin/backup',headers={'X-CSRF-Token':csrf}) as response:
                        if username=='inspector':
                            assert response.status_code==403,response.status_code
                            print('PASS: inspector HTTP backup denied (403)')
                            continue
                        assert response.status_code==200,response.status_code
                        expected=response.headers['X-Backup-SHA256']
                        with bundle.open('xb') as output:
                            for chunk in response.iter_bytes():output.write(chunk)
                        assert hashlib.sha256(bundle.read_bytes()).hexdigest()==expected
            with zipfile.ZipFile(bundle) as archive:manifest=json.loads(archive.read('manifest.json'))
            assert manifest['tables']==migration['tables']
            assert hashlib.sha256(source.read_bytes()).hexdigest()==source_sha
            launch('-Action','Stop')
            assert docker('inspect','--format','{{.State.Running}}',state['db'],capture_output=True,text=True).stdout.strip()=='false'
            with patch.dict(os.environ,prepare('ksu-postgres:test',folder/'tools')):
                # Only the downloaded package and new destination are used after source shutdown.
                restored=restore_database(bundle,cluster['restored']['admin'],'ksu_runtime','ksu_backup')
                assert restored['verified'] and restored['tables']==manifest['tables']
                try:restore_database(bundle,cluster['restored']['admin'],'ksu_runtime')
                except BackupError:pass
                else:raise AssertionError('Existing destination overwritten')
                with psycopg.connect(cluster['restored']['admin']) as con:
                    assert fingerprints(con,True)==manifest['tables']
                    assert con.execute('select count(*) from ksu.sessions').fetchone()[0]==0
                with patch.dict(os.environ,{'KSU_DATABASE_URL':cluster['restored']['runtime'],
                        'KSU_WORK_DIR':str(folder/'restored-app'),'KSU_ORIGIN':'http://testserver','KSU_SECURE_COOKIES':'0'}):
                    from server.app import create_app
                    with TestClient(create_app()) as client:
                        for username,password in passwords.items():
                            response=client.post('/api/login',json={'username':username,'password':password},headers={'Origin':'http://testserver'})
                            assert response.status_code==200,(username,response.status_code)
            print('PASS: actual PowerShell launcher; manager HTTP download/SHA-256; source stopped; independent restore counts/SHA-256; both logins; overwrite refused')
        finally:
            # This drill removes only its own disposable resources, never a user installation.
            if state_file.exists():
                state=json.loads(state_file.read_text(encoding='utf-8-sig'))
                for name in (state['app'],state['db']):docker('rm','-fv',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                for suffix in ('-data','-work'):docker('volume','rm',state['db']+suffix,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                docker('network','rm',state['network'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
