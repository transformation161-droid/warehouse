"""PR41 API, concurrent versions, audit and photo ACLs on isolated PostgreSQL17."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
import threading
from pathlib import Path

import psycopg
from fastapi import HTTPException
from fastapi.testclient import TestClient
from inventory_pg_smoke import acceptance,CI_DB,MANAGER,ELMAN,OUTSIDER,ADMIN_DSN,import_plan
from pr41_fixtures import seed,session,VIEWER
from server.inventory_workflow import count_line
from server.models import InventoryCountUpdate
import server.app as app_module


def concurrent_count():
    db=CI_DB();plan=import_plan(MANAGER,day='2026-11-20',stamp='PR41-RACE')
    line=next(x for x in plan['state']['inventoryLines'] if x['inventoryId']==plan['inventoryId'])
    barrier=threading.Barrier(2)
    def write(actor,actual):
        barrier.wait(timeout=10)
        try:count_line(db,actor,line['id'],InventoryCountUpdate(expectedVersion=line['version'],actual=actual,reason='ДЕМО · одновременный пересчёт'));return 200
        except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls=[pool.submit(write,MANAGER,0),pool.submit(write,ELMAN,2)]
        assert sorted(c.result(timeout=30) for c in calls)==[200,409]
    with psycopg.connect(ADMIN_DSN) as con:
        row=con.execute("select version,data from ksu.records where collection='inventoryLines' and id=%s",(line['id'],)).fetchone()
        assert row[0]==line['version']+1
        logs=con.execute("select actor_id,before_json,after_json from ksu.audit where collection='inventoryLines' and record_id=%s and operation='update'",(line['id'],)).fetchall()
        assert len(logs)==1 and json.loads(logs[0][1])['actual'] is None
        saved=json.loads(row[1]);assert json.loads(logs[0][2])['actual']==saved['actual']
        assert logs[0][0]==saved['countedBy'] and saved['countedAt']
    print('PASS: real simultaneous manager/inspector update: 200/409, one version and one audited change')


def http_contracts():
    os.environ['KSU_VIEWER_ACCESS']='enabled'
    seed();db=CI_DB();db.path=Path('/tmp/ksu-pr41-api')/'staging';db.path.parent.mkdir(parents=True,exist_ok=True)
    original=app_module.Database;app_module.Database=lambda _:db
    app=app_module.create_app('postgresql',origin='https://pr41.example.test',secure=True)
    try:
        with ExitStack() as stack:
            clients={}
            for actor in (MANAGER,ELMAN,VIEWER,OUTSIDER):
                c=stack.enter_context(TestClient(app,base_url='https://pr41.example.test'));token,csrf=session(actor)
                c.cookies.set('ksu_session',token,domain='pr41.example.test');c.headers.update({'Origin':'https://pr41.example.test','X-CSRF-Token':csrf,'X-KSU-Expected-User':actor['id']});clients[actor['id']]=c
            manager=clients[MANAGER['id']];field=clients[ELMAN['id']];viewer=clients[VIEWER['id']];other=clients[OUTSIDER['id']]
            plan=import_plan(MANAGER,day='2026-11-21',stamp='PR41-API');ident=plan['inventoryId']
            line=next(x for x in plan['state']['inventoryLines'] if x['inventoryId']==ident)
            payload={'expectedVersion':line['version'],'actual':1,'reason':'ДЕМО · Подтверждено','photo':'/api/media/synthetic-alpha'}
            route='/api/inventory-lines/'+line['id']+'/count'
            assert viewer.patch(route,json=payload).status_code==403
            assert other.patch(route,json=payload).status_code==404
            assert field.patch(route,json={**payload,'price':1}).status_code==422
            assert field.patch(route,json=payload,headers={'X-CSRF-Token':'invalid'}).status_code==403
            assert field.patch(route,json=payload).status_code==200
            assert field.patch(route,json=payload).status_code==409
            current=next(x for x in manager.get('/api/state').json()['inventoryLines'] if x['id']==line['id'])
            assert all(current[key]==line[key] for key in ('sku','sourceId','inventoryNumber','expected','price','brand','sourceRow'))
            assert current['photo']==payload['photo']
            for ext,signature in [('pdf',b'%PDF'),('xlsx',b'PK')]:
                url='/api/inventories/'+ident+'/statement.'+ext
                response=manager.get(url);assert response.status_code==200 and response.content.startswith(signature)
                assert field.get(url).status_code==200 and other.get(url).status_code==404
                # Retain viewer's established export boundary; do not widen it.
                assert viewer.get(url).status_code==403
            read=viewer.get('/api/state');assert read.status_code==200,read.text
            state=read.json();assert len(state['findings'])==3 and not any(f['siteId']=='site_beta' for f in state['findings'])
            assert all('owner' not in f and 'createdBy' not in f for f in state['findings'])
            for c in (manager,field,viewer):assert c.get('/api/media/synthetic-alpha').status_code==200
            assert viewer.get('/api/media/synthetic-orphan').status_code==404
            assert viewer.get('/api/media/synthetic-beta').status_code==404
            assert other.get('/api/media/synthetic-alpha').status_code==404
            assert len(viewer.get('/api/state').json()['questions'])==22
    finally:app.state.import_jobs.close();app_module.Database=original
    print('PASS: fact-only API/CSRF/roles/site ACL, immutable source, duplicate rejection, photo ACL and PDF/XLSX')


if __name__=='__main__':
    acceptance();concurrent_count();http_contracts()
