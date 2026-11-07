"""Focused synthetic recovery drill: source PG is stopped before restoring elsewhere."""
import os
from pathlib import Path
import secrets
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from server.database import Database, seed_demo
from server.maintenance import snapshot_database
from server.pg_admin import initialize, migrate_sqlite, fingerprints
from server.pg_backup import BackupError, backup_database, restore_database
from tests.pg_environment import local_cluster, docker
from tests.pg_tools import prepare


def main():
    with local_cluster() as cluster:
        folder=cluster['private']
        passwords={name:secrets.token_urlsafe(24) for name in ('manager','inspector')}
        live=folder/'live.sqlite3';source=folder/'check1.sqlite3'
        seed_demo(Database(live),passwords['manager'],passwords['inspector'])
        snapshot_database(live,source)
        initialize(cluster['source']['admin'],'ksu_runtime','ksu_backup')
        migrated=migrate_sqlite(source,cluster['source']['admin'])
        with patch.dict(os.environ,prepare('ksu-postgres:test',folder/'tools')):
            bundle=folder/'independent.ksubackup'
            saved=backup_database(cluster['source']['backup'],bundle)
            assert saved['tables']==migrated['tables']
            docker('stop',cluster['network']+'-source',capture_output=True)
            # Restore receives only the independent package and destination credentials.
            restored=restore_database(bundle,cluster['restored']['admin'],'ksu_runtime','ksu_backup')
            assert restored['verified'] and restored['tables']==saved['tables']
            try:
                restore_database(bundle,cluster['restored']['admin'],'ksu_runtime')
            except BackupError:
                pass
            else:
                raise AssertionError('Existing destination overwritten')
            with psycopg.connect(cluster['restored']['admin']) as con:
                assert fingerprints(con,True)==saved['tables']
                assert con.execute('select count(*) from ksu.sessions').fetchone()[0]==0
            with patch.dict(os.environ,{'KSU_DATABASE_URL':cluster['restored']['runtime'],
                    'KSU_WORK_DIR':str(folder/'app'),'KSU_ORIGIN':'http://testserver','KSU_SECURE_COOKIES':'0'}):
                from server.app import create_app
                with TestClient(create_app()) as client:
                    for username,password in passwords.items():
                        response=client.post('/api/login',json={'username':username,'password':password},headers={'Origin':'http://testserver'})
                        assert response.status_code==200,(username,response.status_code)
        print('PASS: PG17 source stopped; independent restore into new cluster; counts/SHA-256 identical; both logins preserved; overwrite refused')


if __name__=='__main__':main()
