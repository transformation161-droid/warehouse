"""Synthetic PG17 release off/on/off, independent restore and PR41 code rollback.

No owner data, provider APIs, external DSNs or browser acceptance replay.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from deploy.release_candidate.bundle import export,verify
from server.app import create_app
from server.managed_pg import preflight
from server.pg_admin import fingerprints,TABLES
from server.pg_backup import backup_database,restore_database
from tests.pg_environment import IMAGE
from tests.pg_tools import prepare
from tests import test_offline_stage1 as offline_tests,test_writeoff_pilot as pilot_tests
from tests.writeoff_environment import pilot

BASE='cc0b18238fdca4cd02876b5daf0cbef4481c3eb2'
ACCEPTED='366c7c43548cc6ca88fbcd31c531c96516294a7b'


class Rollout(unittest.TestCase):
    def test_pg17_flag_rollout_backup_restore_and_same_database_code_rollback(self):
        with tempfile.TemporaryDirectory(prefix='pr46-rollout-synthetic-') as folder:
            root=Path(folder);old=root/'accepted-pr41';export(BASE,old,'postgresql')
            self.assertEqual(verify(old),BASE)
            # The preparation task must not change the accepted application.
            sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
            current=root/'candidate';export(sha,current,'postgresql')
            accepted=root/'accepted-mvp';export(ACCEPTED,accepted,'postgresql')
            def application_files(directory):
                return {p.relative_to(directory).as_posix():p.read_bytes() for p in directory.rglob('*')
                        if p.is_file() and p.name not in ('release-manifest.json','Dockerfile.managed-postgres')}
            self.assertEqual(application_files(current),application_files(accepted))
            self.assertEqual((current/'Dockerfile.managed-postgres').read_bytes().replace(sha.encode(),b'PINNED_BUILD'),
                             (accepted/'Dockerfile.managed-postgres').read_bytes().replace(ACCEPTED.encode(),b'PINNED_BUILD'))
            clients=prepare(IMAGE,root/'pg17-clients')
            with patch.dict(os.environ,clients),pilot() as (app,accounts,pg):
                def facts(kind='source'):
                    with psycopg.connect(pg[kind]['admin']) as con:
                        con.execute('set transaction isolation level repeatable read read only')
                        return fingerprints(con,True)
                qa=pilot_tests.Pilot('test_source_totals_zeroes_dates_reimport_and_exports');qa.app=app
                qa.alias={'ДЕМО · Север':'s1','ДЕМО · Юг':'s2'}
                qa.manager=qa.client('manager',accounts['manager']['password'])
                qa.inspector=qa.client('inspector',accounts['inspector']['password'])
                try:
                    # Simulate already-issued offline packages and durable media receipts.
                    with patch.dict(os.environ,{'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_USER_IDS':'inspector','KSU_WORK_DIR':str(root/'offline-work')}):
                        offline=create_app('postgresql','http://testserver',False)
                        try:
                            with TestClient(offline,headers={'Origin':'http://testserver'}) as field:
                                login=field.post('/api/login',json={'username':'inspector','password':accounts['inspector']['password']})
                                self.assertEqual(login.status_code,200);field.headers['X-CSRF-Token']=login.json()['csrf']
                                self.assertEqual(field.post('/api/offline/packages',json={'siteIds':['s1']}).status_code,200)
                                photo=offline_tests.Offline.picture(None);saved=field.post('/api/media',json=photo)
                                self.assertEqual(saved.status_code,200);receipt=saved.json()
                        finally:offline.state.import_jobs.close()
                    before=facts()
                    with psycopg.connect(pg['source']['admin']) as con:old_audit=con.execute('select * from ksu.audit order by seq').fetchall()
                    for role in ('runtime','admin','backup'):self.assertEqual(preflight(pg['source'][role],role)['schema'],4)
                    with patch.dict(os.environ,{'KSU_WRITEOFF_ANALYTICS_ENABLED':'off'}):
                        self.assertEqual(qa.manager.get('/api/writeoff-analytics').status_code,404)
                        self.assertEqual(qa.manager.get('/writeoff_statement.js').status_code,404)
                        self.assertEqual(qa.inspector.get('/api/receipts/'+photo['mutationId']).json(),receipt)
                    self.assertEqual(facts(),before)
                    qa.imported();after=facts()
                    for table in TABLES:
                        if table not in ('imports','decision_records','decision_documents','decision_versions','decision_entries','audit'):
                            self.assertEqual(after[table],before[table],table)
                    with psycopg.connect(pg['source']['admin']) as con:new_audit=con.execute('select * from ksu.audit order by seq').fetchall()
                    self.assertEqual(new_audit[:len(old_audit)],old_audit)
                    self.assertEqual(len(new_audit),len(old_audit)+39)
                    self.assertEqual(qa.panel()['totalRows'],39)
                    with patch.dict(os.environ,{'KSU_WRITEOFF_ANALYTICS_ENABLED':'off'}):
                        self.assertEqual(qa.manager.get('/api/writeoff-analytics').status_code,404)
                    self.assertEqual(facts(),after)
                    package=root/'backup.ksubackup';dump=backup_database(pg['source']['backup'],package)
                    restored=restore_database(package,pg['restored']['admin'],'ksu_runtime','ksu_backup')
                    self.assertTrue(restored['verified']);self.assertEqual(restored['package_sha256'],dump['sha256'])
                    self.assertEqual(facts('restored'),after);self.assertEqual(facts(),after)
                    for role in ('runtime','admin','backup'):self.assertEqual(preflight(pg['restored'][role],role)['schema'],4)
                    # A separate process imports ONLY the accepted PR41 modules against
                    # the SAME current DB. It never restores an older data snapshot.
                    code="""
import json,sys
from fastapi.testclient import TestClient
from server.app import create_app
v=json.load(sys.stdin);app=create_app('postgresql','http://testserver',False)
try:
 with TestClient(app,headers={'Origin':'http://testserver'}) as c:
  assert c.get('/api/health').json()['backend']=='postgresql'
  for role in ('manager','inspector'):
   r=c.post('/api/login',json={'username':role,'password':v['accounts'][role]['password']});assert r.status_code==200
   assert c.get('/api/state').status_code==200
  assert c.get('/api/receipts/'+v['mutation']).json()==v['receipt']
  assert c.get('/api/writeoff-analytics').status_code==404
 print('PASS: accepted PR41 code reads current data and receipts')
finally:app.state.import_jobs.close()
"""
                    environment={**os.environ,'PYTHONPATH':str(old),'KSU_WRITEOFF_ANALYTICS_ENABLED':'off','KSU_WORK_DIR':str(root/'rollback-work')}
                    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=environment,text=True,capture_output=True,
                        input=json.dumps({'accounts':accounts,'mutation':photo['mutationId'],'receipt':receipt}),timeout=60)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(facts(),after)
                    self.assertEqual(qa.panel()['totalRows'],39)
                finally:qa.manager.close();qa.inspector.close()


if __name__=='__main__':unittest.main()
