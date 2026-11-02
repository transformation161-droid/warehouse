import io
import json
from pathlib import Path
import sqlite3
import time
import unittest
from urllib.parse import quote
from openpyxl import Workbook
from tests import test_api
from server.decision_import import FIELDS


class DecisionImport(unittest.TestCase):
    setUp=test_api.API.setUp
    state=test_api.API.state
    client=test_api.API.client

    def tearDown(self):
        self.app.state.import_jobs.close()
        test_api.API.tearDown(self)

    def book(self, rows=None, header_names=None):
        book=Workbook();sheet=book.active;sheet.title='DEMO decisions'
        sheet.append(header_names or list(FIELDS.values()))
        for row in rows or [self.row()]:sheet.append(row)
        output=io.BytesIO();book.save(output);return output.getvalue()

    def row(self, **values):
        data={'sourceId':'DEMO-1','date':'2026-11-01','site':'ДЕМО · Нова','document':'ДЕМО · Акт',
              'decisionType':'ДЕМО · Списание','toolType':'ДЕМО · Инструмент','quantity':'2','cost':'100.50',
              'residual':'80.25','reason':'ДЕМО · Причина','caseCategory':'ДЕМО','investigation':'ДЕМО · Проверяется',
              'decision':'ДЕМО · Решение','reimbursable':'25.50','reimbursed':'10','responsible':'ДЕМО · МОЛ','comment':'ДЕМО',**values}
        return [data[k] for k in FIELDS]

    def upload(self, data=None, client=None):
        client=client or self.inspector
        result=client.post('/api/decision-imports',content=data or self.book(),
                           headers={'X-KSU-Filename':quote('DEMO.xlsx'),'Content-Type':'application/octet-stream'})
        self.assertEqual(result.status_code,200,result.text)
        ident=result.json()['id'];meta=self.wait(ident,client)
        self.assertEqual(meta['status'],'uploaded',meta)
        return ident

    def wait(self, ident, client=None):
        client=client or self.inspector
        for _ in range(300):
            result=client.get('/api/decision-imports/'+ident);self.assertEqual(result.status_code,200,result.text)
            meta=result.json()
            if meta['status'] not in ('uploading','inspecting','checking','committing'):return meta
            time.sleep(.03)
        self.fail('Synthetic worker did not finish')

    def preview(self, ident, **settings):
        result=self.inspector.post('/api/decision-imports/'+ident+'/preview',json=settings)
        self.assertEqual(result.status_code,200,result.text);return self.wait(ident)

    def commit(self, ident):
        result=self.inspector.post('/api/decision-imports/'+ident+'/commit')
        self.assertEqual(result.status_code,200,result.text);return self.wait(ident)

    def test_stream_preview_exact_values_atomic_commit_history_and_duplicate(self):
        source=self.book();ident=self.upload(source)
        preview=self.preview(ident)
        self.assertEqual(preview['status'],'ready',preview)
        self.assertEqual((preview['processed'],preview['accepted'],preview['rejected']),(1,1,0))
        self.assertEqual(self.inspector.get('/api/decisions').json()['total'],0)
        done=self.commit(ident);self.assertEqual(done['status'],'done',done)
        self.assertEqual(done['committed'],1)
        listing=self.manager.get('/api/decisions').json();self.assertEqual(listing['total'],1)
        record=listing['rows'][0]
        for key,expected in [('quantity','2'),('cost','100.50'),('residual','80.25'),('reimbursable','25.50'),('reimbursed','10')]:
            self.assertEqual(record[key],expected)
        self.assertEqual(self.state()['inventories'],[])
        history=self.manager.get('/api/history?collection=decisions&record_id='+record['id']).json()
        self.assertEqual(len(history),1);self.assertEqual(history[0]['actor_id'],'inspector')
        self.assertEqual(history[0]['operation'],'import')
        self.assertEqual(self.inspector.post('/api/decision-imports/'+ident+'/commit').json()['status'],'done')
        second=self.upload(source);self.preview(second);again=self.commit(second)
        self.assertEqual(again['status'],'done',again);self.assertTrue(again['duplicate'])
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],1)
        with self.app.state.db.connect() as con:
            metadata=json.loads(con.execute('select raw_json from imports').fetchone()[0])
            self.assertNotIn('file',metadata);self.assertIn('sha256',metadata)
        text=self.manager.get('/api/decisions/export.csv').text
        self.assertIn('100.50',text);self.assertIn('ДЕМО · Акт',text)

    def test_precise_errors_no_partial_import_and_complete_error_csv(self):
        ident=self.upload(self.book([self.row(),self.row(quantity='wrong',site='ДЕМО · Неизвестный')]))
        preview=self.preview(ident)
        self.assertEqual(preview['status'],'invalid',preview)
        self.assertEqual((preview['accepted'],preview['rejected']),(1,1))
        errors=preview['errors']
        self.assertTrue(any(e['row']==3 and e['column']=='G' and e['fix'] for e in errors))
        csv=self.inspector.get('/api/decision-imports/'+ident+'/errors.csv')
        self.assertEqual(csv.status_code,200);self.assertIn('G',csv.text)
        self.assertEqual(self.inspector.post('/api/decision-imports/'+ident+'/commit').status_code,422)
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],0)
        with self.app.state.db.connect() as con:self.assertEqual(con.execute('select count(*) from imports').fetchone()[0],0)

    def test_ownership_csrf_and_scope(self):
        ident=self.upload()
        self.assertEqual(self.manager.get('/api/decision-imports/'+ident).status_code,404)
        self.assertEqual(self.inspector.post('/api/decision-imports',content=self.book(),headers={'X-CSRF-Token':'wrong','X-KSU-Filename':'DEMO.xlsx'}).status_code,403)
        self.preview(ident)
        with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        done=self.commit(ident);self.assertEqual(done['status'],'error',done)
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],0)

    def test_header_mapping_range_and_overlap_cannot_duplicate_rows(self):
        headers=['Custom '+str(i) for i in range(17)]
        source=self.book([self.row(sourceId='DEMO-1'),self.row(sourceId='DEMO-2')],headers)
        ident=self.upload(source)
        meta=self.preview(ident);self.assertEqual(meta['status'],'mapping',meta)
        mapping={field:chr(65+i) for i,field in enumerate(FIELDS)}
        meta=self.preview(ident,mapping=mapping,lastRow=2)
        self.assertEqual(meta['accepted'],1);self.assertEqual(self.commit(ident)['status'],'done')
        second=self.upload(source);self.preview(second,mapping=mapping,lastRow=3)
        done=self.commit(second);self.assertEqual(done['status'],'error',done)
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],1)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('select count(*) from imports').fetchone()[0],1)
            self.assertEqual(con.execute("select count(*) from audit where collection='decisions'").fetchone()[0],1)

    def test_cancel_admission_and_bad_workbook(self):
        ident=self.upload()
        busy=self.inspector.post('/api/decision-imports',content=self.book(),headers={'X-KSU-Filename':'DEMO.xlsx'})
        self.assertEqual(busy.status_code,422)
        self.assertEqual(self.inspector.delete('/api/decision-imports/'+ident).status_code,200)
        bad=self.inspector.post('/api/decision-imports',content=b'not a zip',headers={'X-KSU-Filename':'DEMO.xlsx'})
        self.assertEqual(bad.status_code,200)
        self.assertEqual(self.wait(bad.json()['id'])['status'],'error')

    def test_immutable_records_and_page_limits(self):
        ident=self.upload(self.book([self.row(sourceId='1'),self.row(sourceId='2')]))
        self.preview(ident);self.commit(ident)
        self.assertEqual(len(self.manager.get('/api/decisions?limit=1&page=2').json()['rows']),1)
        self.assertEqual(self.manager.get('/api/decisions?limit=1000').status_code,422)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.state.db.connect(self.app.state.db.user('manager')) as con:con.execute("update decision_records set data='{}'")
        with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get('/api/decisions').json()['total'],0)
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],2)

    def test_mid_transaction_failure_rolls_back_rows_import_header_and_audit(self):
        ident=self.upload(self.book([self.row(sourceId='1'),self.row(sourceId='2')]))
        self.preview(ident)
        with self.app.state.db.connect() as con:
            con.executescript("create trigger synthetic_failure before insert on decision_records when new.source_row=3 begin select raise(abort,'synthetic failure'); end;")
        done=self.commit(ident);self.assertEqual(done['status'],'error',done)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('select count(*) from decision_records').fetchone()[0],0)
            self.assertEqual(con.execute('select count(*) from imports').fetchone()[0],0)
            self.assertEqual(con.execute("select count(*) from audit where collection='decisions'").fetchone()[0],0)
        self.assertEqual(len(self.state()['sites']),12)

    def test_current_job_survives_dialog_reload_and_reconciles_completed_transaction(self):
        ident=self.upload();self.preview(ident)
        self.assertEqual(self.inspector.get('/api/decision-imports').json()['id'],ident)
        self.assertIsNone(self.manager.get('/api/decision-imports').json())
        done=self.commit(ident);self.assertEqual(done['status'],'done',done)
        folder,meta=self.app.state.import_jobs.get(ident,self.app.state.db.user('inspector'))
        meta['status']='committing'
        reconciled=self.app.state.import_jobs.reconcile(meta)
        self.assertEqual(reconciled['status'],'done');self.assertEqual(reconciled['committed'],1)
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],1)

    def test_upload_byte_cap_and_invalid_numbers_have_no_main_effect(self):
        response=self.inspector.post('/api/decision-imports',content=b'',headers={'X-KSU-Filename':'DEMO.xlsx','Content-Length':'30000001'})
        self.assertEqual(response.status_code,413)
        ident=self.upload(self.book([self.row(quantity='-1'),self.row(cost='NaN')]))
        preview=self.preview(ident)
        self.assertEqual(preview['status'],'invalid',preview)
        self.assertEqual(preview['rejected'],2)
        self.assertTrue(any(e['column']=='H' for e in preview['errors']))
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],0)

    def test_preflight_database_capacity_rejects_without_partial_writes(self):
        from unittest.mock import patch
        ident=self.upload();self.preview(ident)
        with self.app.state.db.connect() as con:
            size=con.execute('pragma page_count').fetchone()[0]*con.execute('pragma page_size').fetchone()[0]
        with patch.dict('os.environ',{'KSU_DB_MAX_BYTES':str(size+4096)}):
            done=self.commit(ident)
            self.assertEqual(done['status'],'error',done)
            self.assertIn('лимите SQLite',done['message'])
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('select count(*) from decision_records').fetchone()[0],0)
            self.assertEqual(con.execute('select count(*) from imports').fetchone()[0],0)

    def test_numeric_identifier_format_and_exact_original_money_are_preserved(self):
        book=Workbook();sheet=book.active;sheet.append(list(FIELDS.values()))
        sheet.append(self.row(sourceId=123,cost='1\u00a0000,50'))
        sheet['A2'].number_format='000000'
        out=io.BytesIO();book.save(out)
        ident=self.upload(out.getvalue());preview=self.preview(ident)
        self.assertEqual(preview['status'],'ready',preview)
        self.assertEqual(self.commit(ident)['status'],'done')
        record=self.manager.get('/api/decisions').json()['rows'][0]
        self.assertEqual(record['sourceId'],'000123')
        self.assertEqual(record['cost'],'1000.50')
        original=self.manager.get('/api/decisions/'+record['id']).json()
        self.assertEqual(original['data']['sourceCells']['cost']['original'],'1\u00a0000,50')
        self.assertIn('000000',original['importStyles'])
