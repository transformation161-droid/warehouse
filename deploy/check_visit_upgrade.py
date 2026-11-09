"""Rehearse PR #12 on a database made by the actual previous commit. Synthetic only."""
import argparse
from contextlib import closing
import hashlib
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
from uuid import uuid4

from fastapi.testclient import TestClient
from server.app import create_app
from server.database import Database, ROOT

OLD_FIXTURE = r'''
import base64, io, json, os
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from PIL import Image
from server.app import create_app
from server.database import seed_demo
config=json.loads(Path(os.environ['KSU_UPGRADE_CONFIG']).read_text())
app=create_app(config['database'],'http://testserver',False)
seed_demo(app.state.db,config['manager'],config['inspector'])
clients={}
for role in ('manager','inspector'):
 c=TestClient(app,headers={'Origin':'http://testserver'})
 r=c.post('/api/login',json={'username':role,'password':config[role]});assert r.status_code==200
 c.headers['X-CSRF-Token']=r.json()['csrf'];clients[role]=c

def change(role,collection,ident,data,version=0):
 r=clients[role].post('/api/changes',json={'mutationId':uuid4().hex,'changes':[{'collection':collection,'id':ident,'expectedVersion':version,'data':data}]})
 assert r.status_code==200,(r.status_code,r.text)
 return r.json()
for n in range(2):
 out=io.BytesIO();Image.new('RGB',(20,20),'blue').save(out,'PNG')
 r=clients['inspector'].post('/api/media',json={'siteId':'s1','content':base64.b64encode(out.getvalue()).decode()});assert r.status_code==200
 change('inspector','findings','finding'+str(n),{'siteId':'s1','title':'ДЕМО · Старая запись '+str(n),'photo':r.json()['url']})
change('inspector','inventories','inventory',{'siteId':'s1','date':'2026-11-01','title':'ДЕМО · Прежний пересчёт'})
for n in range(8):change('inspector','inventoryLines','line'+str(n),{'inventoryId':'inventory','item':'ДЕМО · ТМЦ '+str(n),'expected':3,'actual':2,'price':10.5,'sku':'DEMO-00'+str(n)})
for ident,status in [('planned','planned'),('cancelled','cancelled'),('foreign','planned')]:
 change('manager','visits',ident,{'siteId':'s1','date':'2026-11-04','assignedTo':'manager' if ident=='foreign' else 'inspector','assignee':'ДЕМО','status':status,'goal':'ДЕМО · Старый план'})
questions=clients['inspector'].get('/api/state').json()['questions']
change('inspector','inspections','old-act',{'siteId':'s1','date':'2026-11-01','inspector':'ДЕМО','results':[{'questionId':q['id'],'status':'ok'} for q in questions]})
change('manager','visits','completed',{'siteId':'s1','date':'2026-11-01','assignedTo':'inspector','assignee':'ДЕМО','status':'completed','inspectionId':'old-act'})
# Preserve an actual legacy import, source row and audit through the old API.
text='object;date;item;expected;actual;price\nДЕМО · Нова;2026-11-01;ДЕМО · Импорт;2;1;15'
r=clients['inspector'].post('/api/import/commit',json={'name':'synthetic.csv','content':base64.b64encode(text.encode()).decode()});assert r.status_code==200
for c in clients.values():c.close()
'''


def fingerprint(path):
    result={}
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as con:
        for (table,) in con.execute("select name from sqlite_master where type='table' order by name"):
            assert table.replace('_','').isalnum()
            rows=sorted(hashlib.sha256(repr(row).encode()).hexdigest() for row in con.execute('select * from '+table))
            result[table]={'count':len(rows),'sha256':hashlib.sha256(''.join(rows).encode()).hexdigest()}
    return result


def run(previous_ref='3e3771ec0dd93eba5cc16d83cb477002273315a4'):
    with tempfile.TemporaryDirectory(prefix='ksu-upgrade-synthetic-') as folder:
        parent=Path(folder);old=parent/'previous';old.mkdir()
        archive=subprocess.run(['git','archive',previous_ref],cwd=ROOT,capture_output=True,check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            files.extractall(old,filter='data')
        source=parent/'old.sqlite3';target=parent/'upgraded.sqlite3'
        passwords={role:secrets.token_urlsafe(24) for role in ('manager','inspector')}
        config=parent/'private.json';config.write_text(json.dumps({'database':str(source),**passwords}));config.chmod(0o600)
        process=subprocess.run([sys.executable,'-c',OLD_FIXTURE],cwd=old,
                env={**os.environ,'KSU_UPGRADE_CONFIG':str(config)},capture_output=True,text=True)
        if process.returncode:raise RuntimeError('Previous-version synthetic fixture failed: '+process.stderr[-2000:])
        baseline=fingerprint(source)
        with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dest:src.backup(dest)
        from server.viewer_schema import upgrade_users_copy
        upgrade_users_copy(target)
        app=create_app(target,'http://testserver',False)
        upgraded=fingerprint(target)
        assert {k:upgraded[k] for k in baseline}==baseline
        assert upgraded['decision_records']['count']==0
        Database(target)
        upgraded=fingerprint(target)
        assert {k:upgraded[k] for k in baseline}==baseline
        assert upgraded['decision_records']['count']==0
        for role in ('manager','inspector'):
            with TestClient(app,headers={'Origin':'http://testserver'}) as client:
                response=client.post('/api/login',json={'username':role,'password':passwords[role]});assert response.status_code==200
                client.headers['X-CSRF-Token']=response.json()['csrf']
                state=client.get('/api/state').json()
                for url in [x['photo'] for x in state['findings'] if x.get('photo')]:assert client.get(url).status_code==200
                for extension in ('xlsx','pdf'):assert client.get('/api/reports/'+extension).status_code==200
                def change(ident,data,version=0):
                    return client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[{'collection':'visits','id':ident,'expectedVersion':version,'data':data}]})
                for ident in ('completed','cancelled'):
                    record=next(v for v in state['visits'] if v['id']==ident)
                    assert change(ident,{**record,'goal':'Обход'},record['version']).status_code==403
                if role=='inspector':
                    assert change('new',{'siteId':'s1','date':'2026-11-06','goal':'ДЕМО · Новый план'}).status_code==200
                    record=next(v for v in client.get('/api/state').json()['visits'] if v['id']=='planned')
                    response=change('planned',{**record,'date':'2026-11-08'},record['version']);assert response.status_code==200
                    record=next(v for v in response.json()['visits'] if v['id']=='planned')
                    assert change('planned',{**record,'status':'cancelled','cancellationReason':'ДЕМО · Перенос работ'},record['version']).status_code==200
                    foreign=next(v for v in state['visits'] if v['id']=='foreign')
                    assert change('foreign',{**foreign,'goal':'Обход'},foreign['version']).status_code==403
        with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dest:
            assert src.execute('select * from users order by id').fetchall()==dest.execute('select * from users order by id').fetchall()
            old_audit=src.execute('select * from audit order by seq').fetchall()
            assert old_audit==dest.execute('select * from audit order by seq limit ?',(len(old_audit),)).fetchall()
            assert dest.execute('pragma integrity_check').fetchone()[0]=='ok'
            assert not dest.execute('pragma foreign_key_check').fetchall()
        assert fingerprint(source)==baseline
        return {'previous_ref':previous_ref,'synthetic_only':True,'tables_before':{k:v['count'] for k,v in baseline.items()},
                'all_rows_preserved_before_new_operations':True,'users_passwords_preserved':True,'old_audit_unchanged':True,
                'original_database_unchanged':True,'repeated_upgrade_idempotent':True,'both_logins_media_reports':True,
                'new_create_move_cancel':True,'foreign_and_terminal_protected':True,'integrity':'ok','foreign_keys':'ok'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--previous-ref',default='3e3771ec0dd93eba5cc16d83cb477002273315a4');args=parser.parse_args()
    print(json.dumps(run(args.previous_ref),ensure_ascii=False,indent=2))
