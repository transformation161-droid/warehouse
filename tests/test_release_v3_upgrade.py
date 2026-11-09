"""Actual PR33 v3 artifact -> v4 private copy; retain receipts/photos and existing users."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import create_app
from server.database import ROOT
from server.release_schema import prepare_sqlite,checksum
from server.pg_admin import fingerprints
from server.viewer_admin import add_to_copy

BASE='c787614f6c30eb1701a86b909b80a4c1bd0a143a'
FIXTURE=r'''
import json,os
from pathlib import Path
from tests.test_offline_release import Release
from tests import test_api
from server.maintenance import snapshot_database
case=Release();case.setUp()
try:
 assert case.change(case.manager,'visits',{'siteId':'s1','date':'2026-11-09','assignedTo':'inspector'},ident='demo-v3-plan').status_code==200
 photo=case.picture();receipt=case.inspector.post('/api/media',json=photo).json()
 package=case.inspector.post('/api/offline/packages',json={'siteIds':['s1']}).json()
 snapshot_database(case.app.state.db.path,Path(os.environ['SYNTHETIC_ROOT'])/'v3.sqlite3')
 (Path(os.environ['SYNTHETIC_ROOT'])/'private.json').write_text(json.dumps({'photo':photo,'receipt':receipt,'package':package,'manager':test_api.MANAGER_PASSWORD,'inspector':test_api.INSPECTOR_PASSWORD}))
finally:case.tearDown()
'''


class ExistingRelease(unittest.TestCase):
    def test_actual_v3_receipts_and_acl_survive_copy_upgrade_and_viewer_provisioning(self):
        with tempfile.TemporaryDirectory(prefix='ksu-pr33-synthetic-') as directory:
            root=Path(directory);old=root/'old';old.mkdir()
            archive=subprocess.run(['git','archive',BASE],cwd=ROOT,check=True,capture_output=True).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(old,filter='data')
            result=subprocess.run([sys.executable,'-c',FIXTURE],cwd=old,env={**os.environ,'SYNTHETIC_ROOT':str(root)},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr[-2000:])
            source=root/'v3.sqlite3';original=checksum(source)
            with closing(sqlite3.connect(source)) as con:before=fingerprints(con)
            report=prepare_sqlite(source,root/'new',closed_backup=True);target=root/'new/ksu.sqlite3'
            self.assertEqual((report['schema_before'],report['schema_after']),(3,4))
            with closing(sqlite3.connect(target)) as con:self.assertEqual(fingerprints(con),before)
            # Creation is explicit, closed-copy-only and does not broaden existing ACLs.
            viewer=add_to_copy(target,{'username':'demo-personal-viewer','name':'ДЕМО · Personal viewer',
                'password':'synthetic strong viewer password','siteIds':['s1']},closed=True)
            metadata=json.loads((root/'private.json').read_text())
            with patch.dict(os.environ,{'KSU_OFFLINE_MODE':'pilot','KSU_OFFLINE_DEPLOYMENT_ID':'f'*32,'KSU_OFFLINE_USER_IDS':'inspector'}):
                with self.assertRaises(ValueError):create_app(source,'http://testserver',False)
                app=create_app(target,'http://testserver',False)
                try:
                    for username,password in [('account-owner',metadata['manager']),('field-inspector',metadata['inspector']),('demo-personal-viewer','synthetic strong viewer password')]:
                        with TestClient(app,headers={'Origin':'http://testserver'}) as client:
                            login=client.post('/api/login',json={'username':username,'password':password});self.assertEqual(login.status_code,200)
                            client.headers['X-CSRF-Token']=login.json()['csrf']
                            if username=='field-inspector':
                                self.assertEqual(client.get('/api/receipts/'+metadata['photo']['mutationId']).json(),metadata['receipt'])
                                self.assertEqual(client.post('/api/media',json=metadata['photo']).json(),metadata['receipt'])
                                self.assertEqual(client.get(metadata['receipt']['url']).status_code,200)
                                state=client.get('/api/state').json();visit=next(v for v in state['visits'] if v['id']=='demo-v3-plan')
                                act='demo-v3-new-act';batch={'mutationId':'demo-v3-completion','packageId':metadata['package']['id'],'changes':[
                                    {'collection':'inspections','id':act,'expectedVersion':0,'data':{'siteId':'s1','date':visit['date'],'inspector':'ДЕМО · Inspector','checklistVersion':metadata['package']['checklist']['id'],
                                     'results':[{'questionId':q['id'],'status':'ok'} for q in metadata['package']['checklist']['questions']]}},
                                    {'collection':'visits','id':visit['id'],'expectedVersion':visit['version'],'data':{**visit,'status':'completed','inspectionId':act}}]}
                                accepted=client.post('/api/offline/changes',json=batch);self.assertEqual(accepted.status_code,200,accepted.text)
                                self.assertEqual(client.post('/api/offline/changes',json=batch).json(),accepted.json())
                            if username=='demo-personal-viewer':
                                self.assertEqual([s['id'] for s in client.get('/api/state').json()['sites']],['s1'])
                                self.assertEqual(client.post('/api/admin/backup').status_code,403)
                    with closing(sqlite3.connect(target)) as con:
                        self.assertEqual(con.execute('select count(*) from media').fetchone()[0],1)
                        self.assertEqual(con.execute('select count(*) from users').fetchone()[0],3)
                        self.assertEqual(con.execute('select count(*) from audit where collection=? and record_id=?',('users',viewer['id'])).fetchone()[0],1)
                finally:app.state.import_jobs.close()
            self.assertEqual(checksum(source),original)
