import os
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
from server.database import ROOT
from server.app import create_app
from server.pg_admin import LEGACY_TABLES,fingerprints,upgrade,grant_backup
from server.pg_backup import restore_database,run_tool,checksum
from tests import test_api
from tests import test_postgres as pg,test_decision_versions as versions


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN') and os.getenv('KSU_TEST_PG_RUNTIME_DSN'),'Real local PostgreSQL required')
class PostgreSQLVersions(versions.Versions):
    setUp=pg.PostgreSQL.setUp
    tearDown=pg.PostgreSQL.tearDown

    def test_v1_offline_upgrade_preserves_all_original_data(self):
        self.initial();name='ksu_legacy_'+uuid4().hex;dsn=pg.database_dsn(self.root_admin,name)
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn) as target,psycopg.connect(self.admin) as source:
                target.execute((ROOT/'server/postgres/001_initial.sql').read_text())
                for table in LEGACY_TABLES:target.execute(sql.SQL('ALTER TABLE ksu.{} DISABLE TRIGGER USER').format(sql.Identifier(table)))
                for table in LEGACY_TABLES:
                    with target.cursor().copy(sql.SQL('COPY ksu.{} FROM STDIN').format(sql.Identifier(table))) as copy:
                        for row in source.execute('select * from ksu.'+table):copy.write_row(row)
                for table in LEGACY_TABLES:target.execute(sql.SQL('ALTER TABLE ksu.{} ENABLE TRIGGER USER').format(sql.Identifier(table)))
                target.execute("select setval(pg_get_serial_sequence('ksu.audit','seq'),(select max(seq) from ksu.audit))")
                before=fingerprints(target,True,LEGACY_TABLES)
            # A real schema-v1 dump fixture proves backward compatibility of the independent format.
            dump=Path(self.temp.name)/'legacy.dump';package=Path(self.temp.name)/'legacy.ksubackup'
            run_tool('pg_dump',['--format=custom','--compress=0','--no-owner','--no-acl','--schema=ksu',
                '--exclude-table-data=ksu.sessions','--exclude-table-data=ksu.attempts','--file='+str(dump)],dsn)
            with zipfile.ZipFile(package,'w',compression=zipfile.ZIP_STORED) as archive:
                archive.write(dump,'database.dump');archive.writestr('manifest.json',json.dumps({
                    'format':'ksu-pg-backup-v1','schema_version':1,'dump_sha256':checksum(dump),'tables':before,'sessions_revoked':True}))
            copied=name+'_restored';copied_dsn=pg.database_dsn(self.root_admin,copied)
            with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(copied)))
            try:
                restored=restore_database(package,copied_dsn,self.role)
                self.assertTrue(restored['verified'])
                with psycopg.connect(copied_dsn) as con:
                    self.assertEqual(fingerprints(con,True,LEGACY_TABLES),before)
                    self.assertEqual(con.execute('select count(*) from ksu.decision_entries').fetchone()[0],1)
            finally:
                with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(copied)))
            result=upgrade(dsn,self.role)
            self.assertTrue(result['existing_data_preserved'])
            with psycopg.connect(dsn) as con:
                self.assertEqual(fingerprints(con,True,LEGACY_TABLES),before)
                self.assertEqual(con.execute('select count(*) from ksu.decision_entries').fetchone()[0],1)
                self.assertEqual(con.execute('select max(version) from ksu.schema_version').fetchone()[0],2)
        finally:
            with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))

    @unittest.skipUnless(os.getenv('KSU_TEST_PG_RESTORE_ADMIN_DSN'),'Independent PostgreSQL required')
    def test_http_backup_restores_version_links_active_12_and_continuation(self):
        old=self.initial()[0];ident,meta=self.correction([self.row(cost='12')],old['documentId']);self.assertEqual(self.confirm(ident,meta)['status'],'done')
        backup=pg.database_dsn(os.environ['KSU_TEST_PG_BACKUP_DSN'],self.name)
        with psycopg.connect(self.admin) as con:grant_backup(con,'ksu_backup');expected=fingerprints(con,True)
        with patch.dict(os.environ,{'KSU_PG_BACKUP_DSN':backup,'KSU_PG_BACKUP_DIR':self.temp.name}):
            response=self.manager.post('/api/admin/backup');self.assertEqual(response.status_code,200)
        package=Path(self.temp.name)/'synthetic-version.ksubackup';package.write_bytes(response.content)
        root=os.environ['KSU_TEST_PG_RESTORE_ADMIN_DSN'];name='ksu_versions_'+uuid4().hex;target=pg.database_dsn(root,name)
        with psycopg.connect(root,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            result=restore_database(package,target,self.role,'ksu_backup');self.assertEqual(result['tables'],expected)
            runtime=pg.database_dsn(os.environ['KSU_TEST_PG_RESTORE_RUNTIME_DSN'],name)
            with patch.dict(os.environ,{'KSU_DATABASE_URL':runtime,'KSU_WORK_DIR':str(Path(self.temp.name)/'restored-work')}):
                app=create_app('postgresql',test_api.ORIGIN,False)
                try:
                    with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
                        login=client.post('/api/login',json={'username':'manager','password':test_api.MANAGER_PASSWORD});self.assertEqual(login.status_code,200);client.headers['X-CSRF-Token']=login.json()['csrf']
                        self.assertEqual(client.get('/api/decisions/totals').json()['totals']['cost'],'12')
                        analytics=client.get('/api/writeoff-analytics').json()
                        self.assertEqual(analytics['metrics']['cost']['value'],'12')
                        self.assertEqual(analytics['metrics']['decisions']['value'],'1')
                        self.assertEqual(analytics['rows'][0]['currentVersion'],2)
                        self.assertEqual(client.get('/api/decisions?history=true').json()['total'],2)
                        self.assertEqual(client.get('/api/decision-documents/'+old['documentId']+'/versions').json()[0]['number'],2)
                        ident,meta=self.correction([self.row(cost='13')],old['documentId'],client=client)
                        self.assertEqual(self.confirm(ident,meta,client)['status'],'done')
                        self.assertEqual(client.get('/api/decisions/totals').json()['totals']['cost'],'13')
                finally:app.state.import_jobs.close()
        finally:
            with psycopg.connect(root,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
