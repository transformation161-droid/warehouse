"""Approved contract: two independent source amounts, including missing/zero values."""
import csv
from decimal import Decimal
import io
import json
import unittest
from openpyxl import load_workbook
from tests import test_writeoff_pilot as api
from tools.writeoff_demo.fixture import rows,workbook

PAIRS=[('10.00','20.00'),('10.00','10.00'),('10.00','3.00'),(None,'0'),('0',None)]
FORBIDDEN=('outstanding','overpayment','reimbursementDifference','переплат','невозмещ','долг','несогласованност','возмещено больше','разница итогов')


def source_rows():
    result=[]
    for i,(assigned,paid) in enumerate(PAIRS):
        row=rows()[0].copy();row.update(sourceId='DEMO-REC-'+str(i),document='ДЕМО · Возмещения '+str(i),reimbursable=assigned,reimbursed=paid);result.append(row)
    return result


def assert_no_derived(case,value):
    text=json.dumps(value,ensure_ascii=False).casefold()
    for term in FORBIDDEN:case.assertNotIn(term.casefold(),text)


class Reimbursements(unittest.TestCase):
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

    def test_greater_equal_less_missing_and_zero_import_independently(self):
        ident=self.upload(workbook(source_rows()));preview=self.preview(ident)
        self.assertEqual(preview['status'],'ready');self.assertEqual(preview['rejected'],0);self.assertEqual(preview['warning'],'');self.assertFalse(preview['errors'])
        self.assertEqual(Decimal(preview['summary']['reimbursable']),30);self.assertEqual(Decimal(preview['summary']['reimbursed']),33)
        self.assertEqual(self.commit(ident)['status'],'done')
        data=self.panel();assert_no_derived(self,data)
        for metric,total in [('reimbursable',30),('reimbursed',33)]:
            self.assertEqual(Decimal(data['metrics'][metric]['value']),total)
            self.assertEqual((data['metrics'][metric]['knownRows'],data['metrics'][metric]['missingRows']),(4,1))
            assert_no_derived(self,self.panel(chartMetric=metric))
        by_id={r['sourceId']:r for r in data['rows']}
        for i,(assigned,paid) in enumerate(PAIRS):
            row=by_id['DEMO-REC-'+str(i)]
            for field,expected in [('reimbursable',assigned),('reimbursed',paid)]:
                self.assertEqual(Decimal(row[field]) if row[field] is not None else None,Decimal(expected) if expected is not None else None)
            detail=self.manager.get('/api/decisions/'+row['id']).json()
            self.assertEqual(detail['data']['reimbursable'],assigned);self.assertEqual(detail['data']['reimbursed'],paid);assert_no_derived(self,detail)
            selected=self.panel(documentId=row['documentId']);assert_no_derived(self,selected)
        self.assertEqual(self.inspector.get('/api/writeoff-analytics').json()['metrics']['reimbursed']['value'],'33.00')
        for key in ('chartMetric','metricField'):self.assertEqual(self.manager.get('/api/writeoff-analytics',params={key:'outstanding'}).status_code,422)

    def test_row_and_summary_csv_xlsx_have_only_source_reimbursement_fields(self):
        self.imported(workbook(source_rows()));data=self.panel()
        for kind in ('rows','summary'):
            for ext in ('csv','xlsx'):
                with self.subTest(kind=kind,ext=ext):
                    response=self.manager.get(f'/api/writeoff-analytics/export/{kind}.{ext}',params={'expectedSnapshot':data['snapshot']});self.assertEqual(response.status_code,200)
                    if ext=='csv':table=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'));assert_no_derived(self,table)
                    else:
                        book=load_workbook(io.BytesIO(response.content),read_only=True,data_only=True);table=list(book.worksheets[0].values)
                        for sheet in book:assert_no_derived(self,list(sheet.values))
                        book.close()
                    if kind=='rows':
                        self.assertEqual(len(table),6);by_id={r[0]:r for r in table[1:]}
                        # Original 17 source columns retain their positions and independent values.
                        for i,(assigned,paid) in enumerate(PAIRS):
                            for col,expected in [(13,assigned),(14,paid)]:
                                actual=by_id['DEMO-REC-'+str(i)][col]
                                self.assertEqual(Decimal(str(actual)) if actual not in ('',None) else None,Decimal(expected) if expected is not None else None)
                    else:
                        self.assertEqual(Decimal(table[1][table[0].index('К возмещению (₽)')]),30)
                        self.assertEqual(Decimal(table[1][table[0].index('Фактически возмещено (₽)')]),33)
