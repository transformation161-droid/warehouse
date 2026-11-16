"""Graphic payloads reuse the existing immutable data/ACL/export contracts."""
from decimal import Decimal
import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from tests import test_writeoff_pilot as api
from tools.writeoff_demo.fixture import quarter_rows,workbook


class Charts(unittest.TestCase):
    setUp=api.Pilot.setUp
    tearDown=api.Pilot.tearDown
    client=api.Pilot.client
    state=api.Pilot.state
    wait=api.Pilot.wait
    upload=api.Pilot.upload
    preview=api.Pilot.preview
    commit=api.Pilot.commit
    imported=api.Pilot.imported
    panel=api.Pilot.panel

    def test_separate_quarter_reports_no_invented_points_and_scoped_exports(self):
        before=self.state();self.imported()
        for period in ('2026-Q1','2026-Q3'):self.imported(workbook(quarter_rows(period)),reportPeriod=period)
        d=self.panel(reportYear=2026);self.assertEqual(d['totalRows'],48)
        points={p['key']:p['metrics']['cost']['value'] for p in d['charts']['periods']['items']}
        self.assertEqual({k:Decimal(v) for k,v in points.items()},{'2026-Q1':Decimal('120000'),'2026-Q2':Decimal('356787.50'),'2026-Q3':Decimal('140000')})
        cells={(p['siteId'],p['period']):p['metrics']['cost']['value'] for p in d['matrix']}
        self.assertNotIn(('s2','2026-Q1'),cells);self.assertEqual(Decimal(cells['s2','2026-Q3']),0)
        self.assertNotIn('2026-Q4',points);self.assertEqual(d['matrixPeriods'][-1],'2026-Q4')
        scoped=self.panel(self.inspector);self.assertEqual(scoped['totalRows'],26);self.assertEqual({s['id'] for s in scoped['matrixSites']},{'s1'})
        self.assertEqual(self.panel(reportPeriod='2026-Q2',month='2026-07')['totalRows'],3)
        self.assertEqual(self.panel(reportPeriod='2026-Q3',site='s2')['metrics']['cost']['value'],'0')
        self.assertEqual(self.panel(reportPeriod='2026-Q4')['metrics']['cost']['value'],None)
        for metric in ('quantity','cost','residual','reimbursable','reimbursed'):
            data=self.panel(chartMetric=metric,reportPeriod='2026-Q2')
            reply=self.manager.get('/api/writeoff-analytics/export/rows.csv',params={'chartMetric':metric,'reportPeriod':'2026-Q2','expectedSnapshot':data['snapshot']})
            self.assertEqual(reply.status_code,200);self.assertEqual(data['totalRows'],39)
        self.assertEqual(self.state()['sites'],before['sites']);self.assertEqual(self.state()['inventories'],before['inventories'])

    def test_chart_assets_follow_production_feature_gate(self):
        values={'KSU_OFFLINE_MODE':'off','KSU_OFFLINE_DEPLOYMENT_ID':'b'*32,'KSU_WRITEOFF_ANALYTICS_ENABLED':'off'}
        with patch.dict(os.environ,values):
            gated=create_app(self.app.state.db.path,'http://testserver',False)
            try:
                with TestClient(gated) as client:
                    index=client.get('/').text
                    for asset in ('writeoff_analytics.js','writeoff_charts.js','writeoff_charts.css','writeoff_statement.js'):
                        self.assertNotIn(asset,index);self.assertEqual(client.get('/'+asset).status_code,404)
            finally:gated.state.import_jobs.close()
