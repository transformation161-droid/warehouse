"""Optional identity/deployment guards preserve legacy APIs; lab assets stay scoped."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from server.app import create_app
from server.pg_admin import fingerprints
from experiments.offline_visit.lab import initialize
from tests import test_api,test_postgres as pg,test_offline_stage1 as stage1

LAB='2'*32


class Stage2(unittest.TestCase):
    client=test_api.API.client
    state=test_api.API.state
    change=test_api.API.change
    picture=stage1.Offline.picture

    def setUp(self):
        self.lab_env=patch.dict(os.environ,{'KSU_OFFLINE_VISIT_LAB_ID':LAB});self.lab_env.start()
        test_api.API.setUp(self)

    def tearDown(self):
        try:
            self.app.state.import_jobs.close();test_api.API.tearDown(self)
        finally:self.lab_env.stop()

    def test_identity_and_deployment_are_checked_before_writes(self):
        body={'mutationId':uuid4().hex,'changes':[{'collection':'visits','id':'DEMO-bound','expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-09'}}]}
        for headers,status in [({'X-KSU-Expected-User':'manager'},403),({'X-KSU-Expected-User':'inspector','X-KSU-Offline-Deployment':'0'*32},409)]:
            self.assertEqual(self.inspector.post('/api/changes',json=body,headers=headers).status_code,status)
            self.assertEqual(self.inspector.post('/api/media',json=self.picture(),headers=headers).status_code,status)
        self.assertEqual(self.state()['visits'],[])
        headers={'X-KSU-Expected-User':'inspector','X-KSU-Offline-Deployment':LAB}
        self.assertEqual(self.inspector.post('/api/changes',json=body,headers=headers).status_code,200)
        self.assertEqual(self.inspector.get('/api/receipts/'+body['mutationId'],headers=headers).status_code,200)
        self.assertEqual(self.inspector.get('/api/me',headers={'X-KSU-Expected-User':'manager'}).status_code,403)
        # Headers are additional restrictions, never a replacement for auth/CSRF.
        self.assertEqual(self.inspector.post('/api/media',json=self.picture(),headers={**headers,'X-CSRF-Token':'bad'}).status_code,403)

    def test_shell_whitelist_service_worker_scope_and_context_auth(self):
        with TestClient(self.app,headers={'Origin':test_api.ORIGIN}) as anonymous:
            self.assertEqual(anonymous.get('/api/offline-visit/context').status_code,401)
            self.assertEqual(anonymous.get('/offline-visit/').status_code,200)
            for path in ('lab.py','stage2-lab.json','Start-VisitLab.ps1','../../server/database.py'):
                self.assertEqual(anonymous.get('/offline-visit/'+path).status_code,404)
            worker=anonymous.get('/offline-visit/sw.js')
            self.assertEqual(worker.headers['service-worker-allowed'],'/offline-visit/')
            self.assertNotIn('__BUILD__',worker.text)
        context=self.inspector.get('/api/offline-visit/context').json()
        self.assertEqual(context,{'synthetic':True,'deploymentId':LAB,'stage':2,'userId':'inspector','role':'inspector'})

    def test_default_instance_does_not_expose_lab(self):
        with patch.dict(os.environ,{'KSU_OFFLINE_VISIT_LAB_ID':''}):
            app=create_app('postgresql' if self.app.state.db.backend=='postgresql' else self.app.state.db.path,test_api.ORIGIN,False)
        with TestClient(app,headers={'Origin':test_api.ORIGIN}) as client:
            self.assertEqual(client.get('/offline-visit/').status_code,404)
            self.assertEqual(client.get('/api/offline-visit/context').status_code,404)


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN'),'Real local PostgreSQL required')
class PostgreSQLStage2(Stage2):
    def setUp(self):
        self.lab_env=patch.dict(os.environ,{'KSU_OFFLINE_VISIT_LAB_ID':LAB});self.lab_env.start()
        pg.PostgreSQL.setUp(self)
    def tearDown(self):
        try:pg.PostgreSQL.tearDown(self)
        finally:self.lab_env.stop()


class LabIsolation(unittest.TestCase):
    def test_nonempty_unmarked_directory_and_missing_db_never_reseed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'owner-file.txt').write_text('DEMO sentinel')
            with self.assertRaises(ValueError):initialize(root)
            self.assertEqual((root/'owner-file.txt').read_text(),'DEMO sentinel')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);database,lab=initialize(root)
            credentials=(root/'demo-accounts.txt').read_bytes()
            with closing(sqlite3.connect(database)) as con:before=fingerprints(con)
            self.assertEqual(initialize(root),(database,lab))
            with closing(sqlite3.connect(database)) as con:self.assertEqual(fingerprints(con),before)
            self.assertEqual((root/'demo-accounts.txt').read_bytes(),credentials)
            database.unlink()
            with self.assertRaises(ValueError):initialize(root)
