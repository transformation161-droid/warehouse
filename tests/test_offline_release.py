"""Release activation, legacy permissions, no demo bootstrap and safe owner-local copies."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from server.app import create_app
from server.database import Database
from server.offline_release import Policy
from server.release_schema import inspect_sqlite,prepare_sqlite,checksum
from server.release_backup import snapshot_frozen
from server.pg_admin import fingerprints
from tests import test_api,test_offline_stage1 as stage1,test_postgres as pg

IDENTITY='f'*32


class Release(unittest.TestCase):
    client=test_api.API.client
    change=test_api.API.change
    state=test_api.API.state
    picture=stage1.Offline.picture

    def setUp(self):
        test_api.API.setUp(self)
        self.manager.close();self.inspector.close();self.app.state.import_jobs.close()
        path=self.app.state.db.path
        with self.app.state.db.connect() as con:
            con.execute("update users set username='account-owner',name='Synthetic owner' where id='manager'")
            con.execute("update users set username='field-inspector',name='Synthetic inspector' where id='inspector'")
        self.settings=patch.dict(os.environ,{'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_DEPLOYMENT_ID':IDENTITY,'KSU_OFFLINE_USER_IDS':'inspector'})
        self.settings.start();self.addCleanup(self.settings.stop);self.locator=path
        self.app=create_app(path,test_api.ORIGIN,False)
        self.manager=self.client('account-owner',test_api.MANAGER_PASSWORD);self.inspector=self.client('field-inspector',test_api.INSPECTOR_PASSWORD)

    def tearDown(self):
        try:self.app.state.import_jobs.close();test_api.API.tearDown(self)
        finally:self.settings.stop()

    def facts(self):
        with self.app.state.db.connect() as con:return fingerprints(con)

    def test_existing_accounts_cohort_and_no_public_demo_routes(self):
        context=self.inspector.get('/api/offline-visit/context').json()
        self.assertTrue(context['enabled']);self.assertNotIn('synthetic',context)
        self.assertFalse(self.manager.get('/api/offline-visit/context').json()['enabled'])
        self.assertNotIn('ДЕМО',self.inspector.get('/offline-visit/').text)
        self.assertEqual(self.manager.post('/api/offline/packages',json={'siteIds':['s1']}).status_code,403)
        self.assertEqual(self.inspector.get('/api/decisions').status_code,404)
        for path in ('/demo-package.json','/experiments/offline_visit/lab.py','/server/seed.json','/offline-visit/lab.py'):
            self.assertEqual(self.inspector.get(path).status_code,404)
        with self.app.state.db.connect() as con:self.assertEqual(con.execute('select count(*) from users').fetchone()[0],2)

    def test_legacy_permissions_and_online_assets_are_preserved_without_seed(self):
        self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2026-11-09'}).status_code,403)
        self.assertEqual(self.change(self.manager,'visits',{'siteId':'s1','date':'2026-11-09','assignedTo':'inspector'}).status_code,200)
        self.assertEqual(self.change(self.inspector,'findings',{'siteId':'s1','title':'Synthetic finding'}).status_code,200)
        source=self.manager.get('/app.js').text
        self.assertNotIn('const defaultSites=',source);self.assertNotIn('demo_a',source)
        self.assertNotIn('Андрей · Руководитель',source)
        self.assertNotIn('calendar.js',self.manager.get('/').text)
        self.assertEqual(self.state()['meta']['mode'],'work')
        self.assertEqual(self.manager.get('/api/reports/xlsx').status_code,200)

    def test_kill_switch_preserves_receipts_existing_sessions_and_all_rows(self):
        photo=self.picture();saved=self.inspector.post('/api/media',json=photo).json()
        self.assertEqual(saved['status'],200)
        before=None
        before=self.facts()
        with patch.dict(os.environ,{'KSU_OFFLINE_MODE':'off','KSU_OFFLINE_USER_IDS':''}):
            app=create_app(self.locator,test_api.ORIGIN,False)
            with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
                client.cookies.update(self.inspector.cookies)
                headers={'X-CSRF-Token':self.inspector.headers['X-CSRF-Token'],'X-KSU-Expected-User':'inspector','X-KSU-Offline-Deployment':IDENTITY}
                self.assertEqual(client.get('/api/me',headers=headers).status_code,200)
                self.assertFalse(client.get('/api/offline-visit/context',headers=headers).json()['enabled'])
                self.assertEqual(client.get('/api/receipts/'+photo['mutationId'],headers=headers).json(),saved)
                self.assertEqual(client.post('/api/media',headers=headers,json=photo).status_code,403)
                self.assertEqual(client.post('/api/offline/packages',headers=headers,json={'siteIds':['s1']}).status_code,403)
        self.assertEqual(self.facts(),before)

    def test_policy_refuses_lab_wildcards_and_startup_ddl(self):
        for values in ({'KSU_BOOTSTRAP_DEMO':'1'},{'KSU_OFFLINE_VISIT_LAB_ID':'a'*32},{'KSU_OFFLINE_USER_IDS':'*'}):
            with patch.dict(os.environ,values),self.assertRaises(ValueError):Policy.environment()
        # Unknown/partial schemas are refused without creating missing tables.
        path=Path(self.temp.name)/'unknown.sqlite3'
        with closing(sqlite3.connect(path)) as con:con.execute('create table sentinel(x)')
        before=checksum(path)
        with self.assertRaises(ValueError):Database(path)
        self.assertEqual(checksum(path),before)


class Migration(unittest.TestCase):
    def test_closed_copy_upgrade_and_source_unchanged_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.sqlite3'
            from server.database import seed_demo
            seed_demo(Database(source),'synthetic manager password','synthetic inspector password')
            from server.maintenance import snapshot_database
            closed=root/'closed.sqlite3';snapshot_database(source,closed)
            before=checksum(closed);report=prepare_sqlite(closed,root/'candidate',closed_backup=True)
            self.assertTrue(report['all_original_tables_preserved']);self.assertEqual(checksum(closed),before)
            self.assertEqual(inspect_sqlite(root/'candidate/ksu.sqlite3')['version'],3)
            with self.assertRaises(FileExistsError):prepare_sqlite(closed,root/'candidate',closed_backup=True)

    def test_full_frozen_wal_copy_includes_committed_changes_and_never_touches_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);live=root/'live.sqlite3'
            from server.database import seed_demo
            seed_demo(Database(live),'synthetic manager password','synthetic inspector password')
            frozen=root/'frozen';frozen.mkdir()
            with closing(sqlite3.connect(live)) as con:
                con.execute('pragma wal_autocheckpoint=0')
                con.execute("update users set name='Synthetic WAL marker' where id='inspector'");con.commit()
                for suffix in ('','-wal','-shm'):
                    path=Path(str(live)+suffix)
                    if path.exists():shutil.copyfile(path,frozen/('ksu.sqlite3'+suffix))
            before={p.name:checksum(p) for p in frozen.iterdir()}
            output=root/'backup'/'independent.sqlite3'
            report=snapshot_frozen(frozen,output,confirmed=True)
            self.assertTrue(report['copied_wal'])
            self.assertEqual(before,{p.name:checksum(p) for p in frozen.iterdir()})
            with closing(sqlite3.connect(output)) as con:
                self.assertEqual(con.execute("select name from users where id='inspector'").fetchone()[0],'Synthetic WAL marker')
                self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0],0)
            with self.assertRaises(ValueError):snapshot_frozen(frozen,output,confirmed=True)

    def test_active_sidecars_or_unconfirmed_input_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'x.sqlite3';path.write_bytes(b'synthetic')
            with self.assertRaises(ValueError):prepare_sqlite(path,Path(directory)/'new')
            Path(str(path)+'-wal').touch()
            with self.assertRaises(ValueError):prepare_sqlite(path,Path(directory)/'new',closed_backup=True)


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN'),'Real local PostgreSQL required')
class PostgreSQLRelease(Release):
    def setUp(self):
        pg.PostgreSQL.setUp(self)
        self.manager.close();self.inspector.close();self.app.state.import_jobs.close()
        import psycopg
        with psycopg.connect(self.admin) as con:
            con.execute("update ksu.users set username='account-owner',name='Synthetic owner' where id='manager'")
            con.execute("update ksu.users set username='field-inspector',name='Synthetic inspector' where id='inspector'")
        self.settings=patch.dict(os.environ,{'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_DEPLOYMENT_ID':IDENTITY,'KSU_OFFLINE_USER_IDS':'inspector'})
        self.settings.start();self.addCleanup(self.settings.stop);self.locator='postgresql'
        self.app=create_app('postgresql',test_api.ORIGIN,False)
        self.manager=self.client('account-owner',test_api.MANAGER_PASSWORD);self.inspector=self.client('field-inspector',test_api.INSPECTOR_PASSWORD)

    def tearDown(self):
        try:pg.PostgreSQL.tearDown(self)
        finally:self.settings.stop()

    def facts(self):
        import psycopg
        with psycopg.connect(self.admin) as con:return fingerprints(con,True)
