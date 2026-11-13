"""Targeted release flow with synthetic XLSX, immutable v4 versions and role boundaries."""
import csv
from decimal import Decimal
import io
import os
from pathlib import Path
import secrets
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from openpyxl import load_workbook
from tests import test_api
from tests.writeoff_fixture import rows,workbook
from server.offline_release import Policy


class Pilot(unittest.TestCase):
    client=test_api.API.client
    state=test_api.API.state
    change=test_api.API.change

    def setUp(self):
        self.flag=patch.dict(os.environ,{'KSU_WRITEOFF_ANALYTICS_ENABLED':'on'});self.flag.start()
        test_api.API.setUp(self)
        with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id<>'s1'")
        self.alias={'ДЕМО · Север':'s1','ДЕМО · Юг':'s2'}
    def tearDown(self):
        self.app.state.import_jobs.close();test_api.API.tearDown(self);self.flag.stop()

    def wait(self,ident):
        for _ in range(500):
            r=self.manager.get('/api/decision-imports/'+ident);self.assertEqual(r.status_code,200)
            j=r.json()
            if j['status'] not in ('inspecting','checking','committing'):return j
            time.sleep(.025)
        self.fail('Synthetic worker timeout')
    def upload(self,source=None):
        r=self.manager.post('/api/decision-imports',content=source or workbook(),headers={'X-KSU-Filename':'DEMO.xlsx'})
        self.assertEqual(r.status_code,200,r.text);ident=r.json()['id'];j=self.wait(ident);self.assertEqual(j['status'],'uploaded');return ident
    def preview(self,ident,**settings):
        body={'reportPeriod':'2026-Q2','siteAliases':self.alias,'aliasesConfirmed':True,**settings}
        r=self.manager.post('/api/decision-imports/'+ident+'/preview',json=body);self.assertEqual(r.status_code,200,r.text);return self.wait(ident)
    def commit(self,ident):
        r=self.manager.post('/api/decision-imports/'+ident+'/commit');self.assertEqual(r.status_code,200,r.text);return self.wait(ident)
    def imported(self,source=None,**settings):
        ident=self.upload(source);j=self.preview(ident,**settings);self.assertEqual(j['status'],'ready',j.get('message'));j=self.commit(ident);self.assertEqual(j['status'],'done',j.get('message'));return ident
    def panel(self,client=None,**params):
        r=(client or self.manager).get('/api/writeoff-analytics',params=params);self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_source_totals_zeroes_dates_reimport_and_exports(self):
        before=self.state();source=workbook();ident=self.upload(source);j=self.preview(ident)
        self.assertEqual((j['accepted'],j['rejected'],j['summary']['zeroQuantityRows']),(39,0,5))
        for k,v in {'quantity':'56','cost':'356787.50','residual':'113827.87','reimbursable':'113827.87','reimbursed':'116992.36'}.items():self.assertEqual(Decimal(j['summary'][k]),Decimal(v))
        self.assertEqual(self.panel()['totalRows'],0)
        self.assertEqual(self.commit(ident)['committed'],39)
        data=self.panel(reportYear=2026,reportPeriod='2026-Q2');self.assertEqual(data['totalRows'],39)
        self.assertEqual(Decimal(data['metrics']['reimbursable']['value']),Decimal('113827.87'))
        self.assertEqual(Decimal(data['metrics']['reimbursed']['value']),Decimal('116992.36'))
        self.assertNotIn('reimbursementDifference',data['quality'])
        self.assertEqual(data['quality']['outsideReportPeriodRows'],3)
        self.assertEqual(self.panel(reportPeriod='2026-Q1')['totalRows'],0)
        self.assertEqual(self.panel(month='2026-07')['totalRows'],3)
        self.assertEqual(self.state()['inventories'],before['inventories'])
        self.assertEqual(self.state()['sites'],before['sites'])
        again=self.upload(source);self.preview(again);self.assertTrue(self.commit(again)['duplicate']);self.assertEqual(self.panel()['totalRows'],39)
        for kind in ('rows','summary'):
            for ext in ('csv','xlsx'):
                response=self.manager.get(f'/api/writeoff-analytics/export/{kind}.{ext}',params={'reportYear':2026,'reportPeriod':'2026-Q2','expectedSnapshot':data['snapshot']})
                self.assertEqual(response.status_code,200,response.text[:100] if ext=='csv' else response.status_code)
                if ext=='xlsx':
                    book=load_workbook(io.BytesIO(response.content),read_only=True,data_only=True);table=list(book.worksheets[0].values);book.close()
                else:table=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'))
                if kind=='rows':
                    self.assertEqual(len(table),40);self.assertIn('Отчётный квартал',table[0]);self.assertEqual(sum(Decimal(str(r[6])) for r in table[1:]),56)
                else:self.assertIn('356787.50',str(table))

    def test_mapping_diagnostics_alias_confirmation_and_reference_rejected(self):
        ident=self.upload(workbook(custom=True))
        j=self.preview(ident,siteAliases={},aliasesConfirmed=False,headerRow=3,firstRow=4)
        self.assertEqual(j['status'],'invalid');self.assertEqual(j['unknownSites'],sorted(self.alias))
        self.assertTrue(all(e.get('sheet') and e.get('row') and e.get('column') and e.get('fix') for e in j['errors']))
        csv=self.manager.get('/api/decision-imports/'+ident+'/errors.csv');self.assertIn('Лист',csv.text);self.assertIn('Ввод_списаний',csv.text)
        self.assertEqual(self.preview(ident,headerRow=3,firstRow=4,aliasesConfirmed=False)['status'],'error')
        self.assertEqual(self.preview(ident,headerRow=3,firstRow=4)['status'],'ready')
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/preview',json={'sourceKind':'historical'}).status_code,422)
        self.assertEqual(self.preview(ident,reportPeriod='')['status'],'error')
        self.assertEqual(self.manager.get('/api/writeoff-analytics?reportYear=2025&reportPeriod=2026-Q2').status_code,422)

    def test_excel_noise_is_exactly_bounded_with_original_preserved(self):
        from server.decision_import import normalize,FIELDS
        item=rows()[0];mapping={key:chr(65+i) for i,key in enumerate(FIELDS)}
        cells={mapping[k]:{'value':str(v),'type':'s'} for k,v in item.items()}
        cells['I']={'value':'14827.869999999999','type':'n'}
        record,errors=normalize(2,cells,mapping,{k.casefold():v for k,v in self.alias.items()},False)
        self.assertEqual(errors,[]);self.assertEqual(Decimal(record['residual']),Decimal('14827.87'))
        self.assertIn('residual',record['excelRoundingFields']);self.assertEqual(record['sourceCells']['residual']['original'],'14827.869999999999')
        for value,typ in [('1.234567','n'),('-0.000000001','n'),('14827.869999999999','s')]:
            cells['I']={'value':value,'type':typ};_,errors=normalize(2,cells,mapping,{k.casefold():v for k,v in self.alias.items()},False);self.assertTrue(errors,(value,typ))

    def test_role_acl_csrf_and_server_authorizer(self):
        self.imported();manager=self.panel();scoped=self.panel(self.inspector,site='all');self.assertGreater(scoped['totalRows'],0);self.assertLess(scoped['totalRows'],manager['totalRows'])
        foreign=self.panel(site='s2')['rows'][0]
        for path in ('/api/decisions/'+foreign['id'],'/api/decision-documents/'+foreign['documentId']+'/versions'):
            self.assertEqual(self.inspector.get(path).status_code,404)
        self.assertEqual(self.inspector.post('/api/decision-imports',content=workbook(),headers={'X-KSU-Filename':'DEMO.xlsx'}).status_code,403)
        self.assertEqual(self.manager.post('/api/decision-imports',content=workbook(),headers={'X-KSU-Filename':'DEMO.xlsx','X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.manager.get('/api/writeoff-analytics',headers={'X-KSU-Expected-User':'inspector'}).status_code,403)
        with self.app.state.db.connect() as con:
            from server.database import password_hash
            pw=secrets.token_urlsafe(24);con.execute('insert into users values(?,?,?,?,?,1)',('viewer','viewer','ДЕМО · Наблюдатель','viewer',password_hash(pw)));con.execute('insert into user_sites values(?,?)',('viewer','s1'))
        viewer=self.client('viewer',pw)
        try:
            for path in ('/api/writeoff-analytics','/api/writeoff-analytics/import-options','/api/writeoff-analytics/export/rows.xlsx','/api/decisions/'+foreign['id']):self.assertEqual(viewer.get(path).status_code,403,path)
        finally:viewer.close()

    def test_confirmed_event_date_idempotency_conflict_and_audit(self):
        self.imported();before=self.panel();doc=before['rows'][0]['documentId'];path='/api/writeoff-analytics/documents/'+doc+'/inventory-date'
        body={'expectedVersion':1,'mode':'exclude','date':None,'reason':'ДЕМО · Подтверждённое исключение','mutationId':uuid4().hex}
        self.assertEqual(self.inspector.post(path,json=body).status_code,403)
        self.assertEqual(self.manager.post(path,json=body).status_code,200)
        self.assertTrue(self.manager.post(path,json=body).json()['duplicate'])
        self.assertEqual(self.manager.post(path,json={**body,'reason':'другое'}).status_code,409)
        self.assertEqual(self.manager.post(path,json={**body,'mutationId':uuid4().hex}).status_code,409)
        after=self.panel();self.assertEqual(after['metrics']['quantity'],before['metrics']['quantity']);self.assertLess(int(after['metrics']['inventories']['value']),int(before['metrics']['inventories']['value']))
        self.assertEqual(self.manager.get('/api/writeoff-analytics/export/rows.csv',params={'expectedSnapshot':before['snapshot']}).status_code,409)
        body.update(expectedVersion=2,mode='date',date='2026-05-12',mutationId=uuid4().hex)
        self.assertEqual(self.manager.post(path,json=body).status_code,200)
        self.assertEqual(self.panel(documentId=doc)['rows'][0]['inventoryDate'],'2026-05-12')
        body.update(expectedVersion=3,mode='default',date=None,mutationId=uuid4().hex)
        self.assertEqual(self.manager.post(path,json=body).status_code,200)
        self.assertEqual(self.panel()['metrics']['inventories'],before['metrics']['inventories'])
        versions=self.manager.get('/api/decision-documents/'+doc+'/versions').json();self.assertEqual(len(versions),4)
        with self.app.state.db.connect() as con:self.assertGreater(con.execute("select count(*) from audit where collection='decisionVersions'").fetchone()[0],0)

    def test_independent_quarters_all_objects_and_no_invented_delta(self):
        self.imported();data=self.panel(reportYear=2026)
        self.assertEqual(data['matrixPeriods'],['2026-Q1','2026-Q2','2026-Q3','2026-Q4'])
        self.assertTrue(all(s['delta'] is None for s in data['matrixSites']))
        q3=rows()[:1];q3[0].update(sourceId='DEMO-Q3',document='ДЕМО · Q3',quantity='10');self.imported(workbook(q3),reportPeriod='2026-Q3')
        data=self.panel();self.assertEqual(data['totalRows'],40)
        self.assertEqual(len(data['reportPeriods']),2)
        site=next(s for s in data['matrixSites'] if s['id']=='s1');self.assertIsNotNone(site['delta'])
        q1=rows()[:1];q1[0].update(sourceId='DEMO-Q1',document='ДЕМО · Q1',quantity='3');self.imported(workbook(q1),reportPeriod='2026-Q1')
        self.assertEqual(self.panel(reportPeriod='2026-Q1')['totalRows'],1)
        self.assertEqual(self.panel(reportPeriod='2026-Q2')['totalRows'],39)
        for n in range(14):self.assertEqual(self.change(self.manager,'sites',{'name':f'ДЕМО · Пустой {n}','code':f'D{n}'}).status_code,200)
        self.assertGreater(len(self.panel()['matrixSites']),12)

    def test_addressed_release_routes_and_invalid_flag(self):
        policy=Policy('off','a'*32,frozenset());policy.routes('/api/writeoff-analytics','GET')
        for path,method in [('/api/decisions/unknown/write','POST'),('/api/decision-imports-more','GET'),('/api/decision-documents/'+uuid4().hex,'DELETE')]:
            with self.assertRaises(Exception):policy.routes(path,method)
        with patch.dict(os.environ,{'KSU_WRITEOFF_ANALYTICS_ENABLED':'off'}):
            with self.assertRaises(Exception):policy.routes('/api/writeoff-analytics','GET')
        with patch.dict(os.environ,{'KSU_WRITEOFF_ANALYTICS_ENABLED':'typo'}):
            with self.assertRaises(ValueError):Policy.environment()
