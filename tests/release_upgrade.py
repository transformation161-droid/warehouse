"""Actual deployed Git revision -> independent backup -> release copy. Synthetic only."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from deploy.check_visit_upgrade import OLD_FIXTURE,fingerprint
from server.database import ROOT
from server.app import create_app
from server.release_backup import snapshot_frozen
from server.release_schema import checksum,prepare_sqlite,inspect_sqlite

BASE='3e3771ec0dd93eba5cc16d83cb477002273315a4'


def run():
    with tempfile.TemporaryDirectory(prefix='ksu-release-upgrade-synthetic-') as directory:
        root=Path(directory);old=root/'previous';old.mkdir();frozen=root/'frozen';frozen.mkdir()
        archive=subprocess.run(['git','archive',BASE],cwd=ROOT,capture_output=True,check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(old,filter='data')
        source=frozen/'ksu.sqlite3';accounts={r:secrets.token_urlsafe(24) for r in ('manager','inspector')}
        config=root/'private.json';config.write_text(json.dumps({'database':str(source),**accounts}));config.chmod(0o600)
        result=subprocess.run([sys.executable,'-c',OLD_FIXTURE],cwd=old,
                env={**os.environ,'KSU_UPGRADE_CONFIG':str(config)},capture_output=True,text=True)
        if result.returncode:raise AssertionError('Old-code fixture failed: '+result.stderr[-2000:])
        before=fingerprint(source);original=checksum(source)
        assert inspect_sqlite(source)['version']==0
        backup=root/'independent'/'backup.sqlite3';snapshot_frozen(frozen,backup,confirmed=True)
        report=prepare_sqlite(backup,root/'candidate',closed_backup=True)
        target=root/'candidate/ksu.sqlite3';migrated=fingerprint(target)
        for table in before:
            if table not in ('sessions','attempts','sqlite_sequence'):assert migrated[table]==before[table],table
        assert report['schema_before']==0 and report['schema_after']==4
        malformed=root/'unsupported.sqlite3'
        with closing(sqlite3.connect(backup)) as src,closing(sqlite3.connect(malformed)) as dst:
            src.backup(dst);dst.execute('alter table users add column unsupported_extension text');dst.commit()
        malformed_hash=checksum(malformed)
        try:prepare_sqlite(malformed,root/'rejected-candidate',closed_backup=True)
        except ValueError:pass
        else:raise AssertionError('Unknown legacy constraints/columns must not be certified as migrated')
        assert checksum(malformed)==malformed_hash
        with patch.dict(os.environ,{'KSU_OFFLINE_MODE':'staging','KSU_OFFLINE_DEPLOYMENT_ID':'c'*32,'KSU_OFFLINE_USER_IDS':'inspector'}):
            app=create_app(target,'http://testserver',False)
            clients={}
            try:
                for role in accounts:
                    client=TestClient(app,headers={'Origin':'http://testserver'});clients[role]=client
                    login=client.post('/api/login',json={'username':role,'password':accounts[role]});assert login.status_code==200
                    client.headers['X-CSRF-Token']=login.json()['csrf']
                    state=client.get('/api/state').json();assert state['meta']['mode']=='work'
                    for row in state['findings']:
                        if row.get('photo'):
                            for suffix in ('','?thumbnail=true'):assert client.get(row['photo']+suffix).status_code==200
                    for extension in ('xlsx','pdf'):assert client.get('/api/reports/'+extension).status_code==200
                    assert client.get('/api/history?collection=visits&record_id=planned').status_code==200
                inspector=clients['inspector'];state=inspector.get('/api/state').json()
                visit=next(v for v in state['visits'] if v['id']=='planned')
                package=inspector.post('/api/offline/packages',json={'siteIds':['s1']});assert package.status_code==200,package.text
                package=package.json();act='synthetic-new-act'
                changes=[{'collection':'inspections','id':act,'expectedVersion':0,'data':{'siteId':'s1','date':visit['date'],
                    'inspector':'Synthetic inspector','checklistVersion':package['checklist']['id'],
                    'results':[{'questionId':q['id'],'status':'ok'} for q in state['questions']]}},
                    {'collection':'visits','id':'planned','expectedVersion':visit['version'],'data':{**visit,'status':'completed','inspectionId':act}}]
                mutation=uuid4().hex;body={'mutationId':mutation,'packageId':package['id'],'changes':changes}
                response=inspector.post('/api/offline/changes',json=body);assert response.status_code==200,response.text
                committed=fingerprint(target)
                receipt=inspector.get('/api/receipts/'+mutation).json();assert receipt['committed']
                assert inspector.post('/api/offline/changes',json=body).json()==response.json()
                assert fingerprint(target)==committed
                moved=inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[{'collection':'visits','id':'no-create','expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-09'}}]})
                assert moved.status_code==403
                with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dst:
                    assert src.execute('select * from users order by id').fetchall()==dst.execute('select * from users order by id').fetchall()
                    audit=src.execute('select * from audit order by seq').fetchall()
                    assert audit==dst.execute('select * from audit order by seq limit ?',(len(audit),)).fetchall()
                    assert dst.execute('pragma integrity_check').fetchone()[0]=='ok'
                    assert not dst.execute('pragma foreign_key_check').fetchall()
            finally:
                for client in clients.values():client.close()
                app.state.import_jobs.close()
        assert checksum(source)==original
        return {'synthetic_only':True,'source_git':BASE,'schema_before':0,'schema_after':4,
                'original_unchanged':True,'old_business_tables_identical':True,'users_passwords_audit_preserved':True,
                'both_logins_photos_thumbnails_xlsx_pdf':True,'offline_completion_receipt_no_duplicates':True,
                'legacy_inspector_plan_permissions':True,'records_before':before['records']['count'],'media_before':before['media']['count']}


if __name__=='__main__':print(json.dumps(run(),indent=2))
