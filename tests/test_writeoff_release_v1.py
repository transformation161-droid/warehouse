"""Targeted regressions for the write-off analytics release (no production data)."""
from datetime import date
from decimal import Decimal
import unittest

from server.decision_import import normalize
from server.models import DecisionPreview
from server.writeoff_analytics import Bucket, Query, matches


class WriteoffReleaseV1(unittest.TestCase):
    def test_zero_quantity_remains_a_valid_decision(self):
        mapping={'date':'A','site':'B','document':'C','decisionType':'D','toolType':'E',
                 'quantity':'F','cost':'G'}
        values={'A':('2026-04-01','s'),'B':('ДЕМО · Нова','s'),
                'C':('Акт №1','s'),'D':('Списание','s'),'E':('Уровень','s'),
                'F':('0','n'),'G':('0','n')}
        cells={col:{'value':v,'type':typ,'numberFormat':'General'} for col,(v,typ) in values.items()}
        data,errors=normalize(2,cells,mapping,{'демо · нова':'s1'},False)
        self.assertEqual(errors,[])
        self.assertEqual(data['quantity'],'0')
        self.assertEqual(data['cost'],'0')
        self.assertEqual(data['siteId'],'s1')

    def test_distinct_inventory_dates_are_per_object(self):
        bucket=Bucket()
        def row(site,day,quantity):
            return {'site_id':site,'day':date.fromisoformat(day),'document_id':site+day,
                    'numbers':{'quantity':Decimal(quantity),'cost':Decimal('10')},'invalid':set()}
        bucket.add(row('s1','2026-04-10','1'))
        bucket.add(row('s1','2026-04-10','0'))
        bucket.add(row('s2','2026-04-10','2'))
        bucket.add(row('s1','2026-04-11','1'))
        self.assertEqual(bucket.report()['inventories']['value'],'3')
        self.assertEqual(bucket.report()['decisions']['value'],'4')
        self.assertEqual(bucket.report()['quantity']['value'],'4')

    def test_quarter_is_independent_of_act_date(self):
        q=Query(reportPeriod='2026-Q2')
        row={'day':date(2026,7,8),'reportPeriod':'2026-Q2','document_id':'d1',
             'dimensions':{},'numbers':{'quantity':Decimal('1')}}
        self.assertTrue(matches(row,q))
        self.assertFalse(matches(row,Query(reportPeriod='2026-Q3')))
        self.assertTrue(matches(row,Query(month='2026-07')))

    def test_strict_preview_contract(self):
        good=DecisionPreview(reportPeriod='2026-Q2',siteAliases={'Селигер сити':'s2'})
        self.assertEqual(good.reportPeriod,'2026-Q2')
        self.assertEqual(good.siteAliases['Селигер сити'],'s2')
        with self.assertRaises(ValueError):
            DecisionPreview(reportPeriod='2026-Q5')


if __name__=='__main__':
    unittest.main()
