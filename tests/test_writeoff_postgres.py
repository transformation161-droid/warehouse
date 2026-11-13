"""Actual PG17/RLS flow, using the production roles and unchanged schema."""
import io
import json
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import unittest
import psycopg
from fastapi.testclient import TestClient
from tests.writeoff_environment import pilot
from tests import test_writeoff_pilot as pilot_api
from tests.writeoff_fixture import workbook


class PostgreSQL(unittest.TestCase):
    def test_actual_pg17_release_and_role_boundaries(self):
        with pilot() as (app,accounts,pg):
            qa=pilot_api.Pilot('test_source_totals_zeroes_dates_reimport_and_exports');qa.app=app;qa.alias={'ДЕМО · Север':'s1','ДЕМО · Юг':'s2'}
            qa.manager=qa.client('manager',accounts['manager']['password']);qa.inspector=qa.client('inspector',accounts['inspector']['password'])
            try:
                qa.test_source_totals_zeroes_dates_reimport_and_exports()
                scoped=qa.panel(qa.inspector,site='all');self.assertEqual(scoped['totalRows'],20)
                foreign=qa.panel(site='s2')['rows'][0]
                self.assertEqual(qa.inspector.get('/api/decisions/'+foreign['id']).status_code,404)
                self.assertEqual(qa.inspector.get('/api/decision-documents/'+foreign['documentId']+'/versions').status_code,404)
                viewer=qa.client('viewer',accounts['viewer']['password'])
                try:
                    for path in ('/api/writeoff-analytics','/api/writeoff-analytics/export/rows.xlsx','/api/decision-imports','/api/decisions/'+foreign['id']):self.assertEqual(viewer.get(path).status_code,403)
                finally:viewer.close()
                self.assertEqual(qa.inspector.post('/api/decision-imports',content=workbook(),headers={'X-KSU-Filename':'DEMO.xlsx'}).status_code,403)
                doc=qa.panel()['rows'][0]['documentId'];url='/api/writeoff-analytics/documents/'+doc+'/inventory-date'
                body={'expectedVersion':1,'mode':'date','date':'2026-05-01','reason':'ДЕМО · Подтверждено','mutationId':uuid4().hex}
                self.assertEqual(qa.manager.post(url,json=body).status_code,200)
                self.assertTrue(qa.manager.post(url,json=body).json()['duplicate'])
                self.assertEqual(qa.manager.post(url,json={**body,'mutationId':uuid4().hex}).status_code,409)
                self.assertEqual(qa.panel(documentId=doc)['rows'][0]['inventoryDate'],'2026-05-01')
                racers=[{**body,'expectedVersion':2,'mode':'exclude','date':None,'mutationId':uuid4().hex} for _ in range(2)]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda item:qa.manager.post(url,json=item).status_code,racers))
                self.assertEqual(sorted(results),[200,409])
                with app.state.db.connect(accounts['inspector']) as con:
                    sites=[r[0] for r in con.execute('select distinct site_id from decision_records')];self.assertEqual(sites,['s1'])
                with app.state.db.connect(accounts['viewer']) as con:self.assertEqual(con.execute('select count(*) from decision_records').fetchone()[0],0)
                with app.state.db.connect(accounts['manager']) as con:
                    self.assertEqual(con.execute('select max(version) from schema_version').fetchone()[0],4)
                    role=con.execute('select rolsuper,rolbypassrls from pg_roles where rolname=current_user').fetchone();self.assertFalse(any(role.values()))
                    with self.assertRaises(psycopg.Error):con.execute('create table forbidden(value int)')
                self.assertEqual(qa.manager.get('/api/health').json()['backend'],'postgresql')
            finally:qa.manager.close();qa.inspector.close()
