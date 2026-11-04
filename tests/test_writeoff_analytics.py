import csv
from decimal import Decimal
import io
import json
import unittest
import sqlite3
from uuid import uuid4
import psycopg
from unittest.mock import patch
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from tests import test_decision_versions as versions
from tests.analytics_fixture import insert,records,oracle


class Analytics(unittest.TestCase):
    setUp=versions.Versions.setUp
    tearDown=versions.Versions.tearDown
    client=versions.Versions.client
    state=versions.Versions.state
    book=versions.Versions.book
    row=versions.Versions.row
    upload=versions.Versions.upload
    wait=versions.Versions.wait
    preview=versions.Versions.preview
    commit=versions.Versions.commit
    initial=versions.Versions.initial
    correction=versions.Versions.correction
    confirm=versions.Versions.confirm

    def panel(self,client=None,**params):
        response=(client or self.manager).get('/api/writeoff-analytics',params=params)
        self.assertEqual(response.status_code,200,response.text);return response.json()

    def verify(self,data,rows):
        totals,coverage=oracle(rows)
        for field,expected in totals.items():
            actual=data['metrics'][field]['value']
            self.assertEqual(Decimal(actual) if actual is not None else None,Decimal(expected) if expected is not None else None,field)
            if field in coverage:
                self.assertEqual((data['metrics'][field]['knownRows'],data['metrics'][field]['missingRows']),coverage[field],field)
        self.assertEqual(data['totalRows'],len(rows));self.assertLessEqual(len(data['rows']),100)
        self.assertTrue(data['provisional']);self.assertIn('Не официальная',data['notice'])

    def test_all_metrics_and_every_chart_match_independent_integers(self):
        rows=insert(self.app.state.db,records(64));data=self.panel(limit=7)
        self.verify(data,rows);self.assertEqual(len(data['rows']),7)
        group_keys={'months':lambda r:(r.get('date') or '')[:7] or None,'sites':lambda r:r['_site'],
                    'reasons':lambda r:r.get('reason'),'tools':lambda r:r.get('toolType'),'categories':lambda r:r.get('caseCategory')}
        for group,key in group_keys.items():
            for point in data['charts'][group]['items']:
                selected=[r for r in rows if key(r)==point['key']]
                self.verify({'metrics':point['metrics'],'totalRows':len(selected),'rows':[],'provisional':True,'notice':data['notice']},selected)
        self.assertEqual(data['quality']['missingDateRows'],6)

    def test_all_filters_and_combinations_select_the_same_rows_metrics_and_exports(self):
        rows=insert(self.app.state.db,records(64))
        cases=[({'start':'2026-02-01','end':'2026-04-30'},lambda r:r['date'] and '2026-02-01'<=r['date']<='2026-04-30'),
               ({'site':'s2'},lambda r:r['_site']=='s2'),({'dateMode':'missing'},lambda r:not r['date']),
               ({'month':'2026-03'},lambda r:r['date'] and r['date'].startswith('2026-03')),
               ({'reasonMissing':'true'},lambda r:r['reason'] is None),
               ({'metricField':'cost','coverage':'missing'},lambda r:r['cost'] is None)]
        for field in ('toolType','reason','decisionType','responsible','investigation','caseCategory'):
            val=next(r[field] for r in rows if r[field]);cases.append(({field:val},lambda r,f=field,v=val:r[f]==v))
        cases.append(({'site':'s1','toolType':'ДЕМО · Инструмент 0','start':'2026-01-01','end':'2026-04-30'},
                      lambda r:r['_site']=='s1' and r['toolType']=='ДЕМО · Инструмент 0' and r['date'] and '2026-01-01'<=r['date']<='2026-04-30'))
        for params,select in cases:
            with self.subTest(params=params):
                chosen=[r for r in rows if select(r)];data=self.panel(**params);self.verify(data,chosen)
                exported=self.manager.get('/api/writeoff-analytics/export/rows.csv',params={**params,'expectedSnapshot':data['snapshot']})
                self.assertEqual(exported.status_code,200,exported.text)
                parsed=list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig')),delimiter=';'))
                self.assertEqual({r['ID записи'] for r in parsed},{r['_id'] for r in chosen})

    def test_correction_10_to_12_is_not_22_and_links_history_export_snapshot(self):
        old=self.initial()[0];before=self.panel();ident,meta=self.correction([self.row(cost='12',residual='5')],old['documentId'])
        self.assertEqual(self.confirm(ident,meta)['status'],'done');data=self.panel()
        self.assertEqual(data['metrics']['cost']['value'],'12');self.assertEqual(data['metrics']['decisions']['value'],'1')
        row=data['rows'][0];self.assertEqual((row['documentId'],row['currentVersion'],row['introducedVersion']),(old['documentId'],2,2))
        self.assertEqual(self.manager.get('/api/decisions/'+row['id']).json()['data']['cost'],'12')
        self.assertEqual(len(self.manager.get('/api/decision-documents/'+row['documentId']+'/versions').json()),2)
        for kind in ('rows','summary'):
            r=self.manager.get('/api/writeoff-analytics/export/'+kind+'.xlsx',params={'expectedSnapshot':before['snapshot']})
            self.assertEqual(r.status_code,409);self.assertEqual(list((self.app.state.db.path.parent/'analytics-exports').iterdir()),[])
        self.assertEqual(self.manager.get('/api/decisions?history=true').json()['total'],2)
        self.assertEqual(self.panel(documentId=old['documentId'])['totalRows'],1)

    def test_unknown_zero_exact_precision_and_no_cross_line_offset(self):
        source=list(records(3))
        for i,r in enumerate(source):r.update(cost=['0.0000',None,'999999999999.9999'][i],residual=None,reimbursable=['5','0',None][i],reimbursed=['3','4','1'][i])
        rows=insert(self.app.state.db,source);data=self.panel();self.verify(data,rows)
        self.assertEqual(data['metrics']['outstanding']['value'],'2');self.assertEqual(data['metrics']['outstanding']['missingRows'],1)
        self.assertIsNone(data['metrics']['residual']['value']);self.assertEqual(data['quality']['overpaymentValue'],'4')
        zero=self.panel(metricField='cost',coverage='known',documentId=rows[0]['_doc'])
        self.assertEqual(zero['metrics']['cost']['value'],'0.0000');self.assertEqual(zero['metrics']['cost']['missingRows'],0)

    def test_concurrent_correction_does_not_mix_snapshot_rows_kpis_or_charts(self):
        old=self.initial([self.row(sourceId='A',cost='10'),self.row(sourceId='B',cost='20')])
        ident,meta=self.correction([self.row(sourceId='A',cost='12'),self.row(sourceId='B',cost='20')],old[0]['documentId'])
        db=self.app.state.db;iterate=db.iterate;fired=[]
        def interleaved(con,sql,params=()):
            for row in iterate(con,sql,params):
                yield row
                if 'intro.number as introduced_version' in sql and not fired:
                    fired.append(True);self.assertEqual(self.confirm(ident,meta)['status'],'done')
        with patch.object(db,'iterate',interleaved):data=self.panel()
        self.assertTrue(fired);self.assertEqual(data['metrics']['cost']['value'],'30')
        self.assertEqual({r['currentVersion'] for r in data['rows']},{1})
        self.assertEqual(data['charts']['sites']['items'][0]['metrics']['cost']['value'],'30')
        current=self.panel();self.assertEqual(current['metrics']['cost']['value'],'32')
        self.assertNotEqual(data['snapshot'],current['snapshot'])

    def test_csv_xlsx_summary_rows_exact_safe_and_unknown_cells(self):
        rows=insert(self.app.state.db,records(8));data=self.panel(site='s1');selected=[r for r in rows if r['_site']=='s1']
        for kind in ('summary','rows'):
            for ext in ('csv','xlsx'):
                response=self.manager.get('/api/writeoff-analytics/export/'+kind+'.'+ext,params={'site':'s1','expectedSnapshot':data['snapshot']})
                self.assertEqual(response.status_code,200,response.text[:100] if ext=='csv' else response.status_code)
                self.assertEqual(response.headers['X-KSU-Report-Status'],'provisional')
                if ext=='csv':table=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'))
                else:
                    book=load_workbook(io.BytesIO(response.content),read_only=True,data_only=False);table=list(book.worksheets[0].values)
                    self.assertIn('Определения',book.sheetnames);self.assertFalse(any(c.data_type=='f' for sheet in book for row in sheet for c in row));book.close()
                if kind=='rows':
                    self.assertEqual(len(table)-1,len(selected));self.assertEqual({r[0] for r in table[1:]},{r['sourceId'] for r in selected})
                    self.assertTrue(all(str(r[16]).startswith("'=DEMO") for r in table[1:]))
                else:
                    expected,_=oracle(selected);self.assertEqual(Decimal(table[1][table[0].index('Стоимость списаний (₽)')]),Decimal(expected['cost']))

    def test_preserved_invalid_values_are_unknown_not_zero_and_empty_slice(self):
        source=list(records(2));source[0].update(date='2026-02-30',cost='NaN',residual='-1',quantity='1.0000001',reimbursable='0.00001')
        source[1].update(date=None,cost=None,residual=None,quantity=None,reimbursable=None)
        insert(self.app.state.db,source);data=self.panel()
        for field in ('cost','residual','quantity','reimbursable','outstanding'):
            self.assertIsNone(data['metrics'][field]['value']);self.assertEqual(data['metrics'][field]['missingRows'],2)
            self.assertEqual(data['metrics'][field]['invalidRows'],1)
        self.assertEqual(data['quality']['invalidDateRows'],1);self.assertEqual(data['quality']['missingDateRows'],2)
        self.assertTrue(all(r['cost'] is None for r in data['rows']))
        self.assertEqual(self.panel(dateMode='missing')['totalRows'],2)
        empty=self.panel(start='2026-01-01',end='2026-12-31');self.assertEqual(empty['totalRows'],0)
        self.assertEqual(empty['metrics']['documents']['value'],'0');self.assertIsNone(empty['metrics']['cost']['value'])

    def test_fractional_quantity_is_flagged_without_rounding_or_inferred_units(self):
        source=list(records(5))
        for row,value in zip(source,['0.000001','1.500000','2.000000',None,'DEMO invalid']):row['quantity']=value
        rows=insert(self.app.state.db,source);data=self.panel()
        self.assertEqual(data['metrics']['quantity']['value'],'3.500001')
        self.assertEqual(data['quality']['fractionalQuantityRows'],2)
        filtered=self.panel(metricField='quantity',coverage='fractional');self.verify(filtered,rows[:2])
        self.assertEqual({r['quantity'] for r in filtered['rows']},{'0.000001','1.500000'})
        exported=self.manager.get('/api/writeoff-analytics/export/rows.csv',params={'metricField':'quantity','coverage':'fractional','expectedSnapshot':filtered['snapshot']})
        self.assertEqual(exported.status_code,200)
        parsed=list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual({r['Кол-во'] for r in parsed},{'0.000001','1.500000'})
        self.assertEqual(self.manager.get('/api/writeoff-analytics?metricField=cost&coverage=fractional').status_code,422)
        old=self.initial([self.row(sourceId='DEMO-corrected-fraction',quantity='0.5')])[0]
        ident,meta=self.correction([self.row(sourceId='DEMO-corrected-fraction',quantity='1.000000',cost='12',residual='5')],old['documentId'])
        self.assertEqual(self.confirm(ident,meta)['status'],'done')
        self.assertEqual(self.panel(documentId=old['documentId'])['quality']['fractionalQuantityRows'],0)

    def test_database_rejects_an_entry_linked_to_another_document_version(self):
        source=list(records(2))
        for index,row in enumerate(source):row.update(_site='s1',document='ДЕМО · Документ '+str(index))
        rows=insert(self.app.state.db,source);db=self.app.state.db;actor=db.user('manager')
        # version_id determines the document: this guard is relied upon by the read join.
        with self.assertRaises((sqlite3.IntegrityError,psycopg.IntegrityError)):
            with db.connect(actor) as con:
                con.execute('begin immediate')
                original=con.execute('select * from decision_records where id=?',(rows[0]['_id'],)).fetchone()
                ident=uuid4().hex
                con.execute('insert into decision_records values(?,?,?,?,?,?,?,?)',(ident,original['import_hash'],original['file_sha'],original['sheet'],999,'s1',actor['id'],original['data']))
                con.execute('insert into decision_entries values(?,?,?,?,?)',(ident,rows[1]['_doc'],rows[0]['_doc']+'-1','s1',None))
        self.assertEqual(self.panel()['totalRows'],2)

    def test_auth_scope_facets_no_mutation_and_query_validation(self):
        rows=insert(self.app.state.db,records(12))
        if hasattr(self,'admin'):
            import psycopg
            with psycopg.connect(self.admin) as con:con.execute("delete from ksu.user_sites where user_id='inspector' and site_id<>'s1'")
        else:
            with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id<>'s1'")
        allowed={s['id'] for s in self.inspector.get('/api/state').json()['sites']};self.assertEqual(allowed,{'s1'})
        data=self.panel(self.inspector);self.verify(data,[r for r in rows if r['_site'] in allowed])
        for site in ('s1','s2','s3','DEMO-inaccessible'):
            self.verify(self.panel(self.inspector,site=site),[r for r in rows if r['_site']==site and site in allowed])
        before=self.manager.get('/api/history').json()
        for params in ({'limit':101},{'start':'2026-04-01','end':'2026-01-01'},{'history':'true'},{'cost':'22'},{'dateMode':'missing','start':'2026-01-01'},{'reason':'DEMO','reasonMissing':'true'}):
            self.assertEqual(self.manager.get('/api/writeoff-analytics',params=params).status_code,422)
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get('/api/writeoff-analytics').status_code,401)
            self.assertEqual(anonymous.get('/api/writeoff-analytics/export/rows.csv').status_code,401)
        self.assertEqual(self.manager.get('/api/history').json(),before)
        self.assertNotIn('sourceCells',json.dumps(data))
        for ext in ('csv','xlsx'):
            response=self.inspector.get('/api/writeoff-analytics/export/rows.'+ext,params={'site':'s2'})
            self.assertEqual(response.status_code,200)
            if ext=='csv':self.assertEqual(len(list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'))),1)
            else:
                book=load_workbook(io.BytesIO(response.content),read_only=True);self.assertEqual(len(list(book.worksheets[0].values)),1);book.close()


if __name__=='__main__':unittest.main()
