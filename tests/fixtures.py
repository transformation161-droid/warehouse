"""Synthetic API fixtures shared by calendar and PostgreSQL acceptance."""
from uuid import uuid4
from fastapi.testclient import TestClient
from server.app import create_app
from tests.test_api import MANAGER_PASSWORD, INSPECTOR_PASSWORD


def calendar_source_fixture(database):
    app=create_app(database,'http://testserver',False)
    def client(role,password):
        c=TestClient(app,headers={'Origin':'http://testserver'})
        response=c.post('/api/login',json={'username':role,'password':password})
        c.headers['X-CSRF-Token']=response.json()['csrf'];return c
    def change(client, ident, data, version=0, extra=None):
        response=client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':(extra or [])+[
            {'collection':'visits','id':ident,'expectedVersion':version,'data':data}]})
        assert response.status_code==200,response.text
    with client('manager',MANAGER_PASSWORD) as m,client('inspector',INSPECTOR_PASSWORD) as i:
        for ident,date,assignee in [('DEMO-other','2026-11-01','manager'),('DEMO-second','2026-11-01','inspector'),
                                   ('DEMO-past','2026-10-30','inspector'),('DEMO-cancel','2026-11-01','inspector'),
                                   ('DEMO-done','2026-11-01','inspector')]:
            change(m,ident,{'siteId':'s1','date':date,'assignedTo':assignee,'goal':'ДЕМО · '+ident})
        visits={v['id']:v for v in m.get('/api/state').json()['visits']}
        change(i,'DEMO-cancel',{**visits['DEMO-cancel'],'status':'cancelled','cancellationReason':'ДЕМО · Исходная отмена'},1)
        questions=i.get('/api/state').json()['questions']
        act={'collection':'inspections','id':'DEMO-act','expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-01',
             'inspector':'ДЕМО','results':[{'questionId':q['id'],'status':'ok'} for q in questions]}}
        change(i,'DEMO-done',{**visits['DEMO-done'],'status':'completed','inspectionId':'DEMO-act'},1,[act])
    app.state.import_jobs.close()

