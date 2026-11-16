"""Full source rows stay scoped, paginated and identical to their immutable imports."""
import csv
import io
import unittest
from server.decision_import import FIELDS
from tests import test_writeoff_pilot as api
from tools.writeoff_demo.fixture import rows,workbook


class Statement(unittest.TestCase):
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

    def test_all_columns_comments_and_task_text_follow_site_acl_and_export(self):
        source=rows();source[0]['comment']='ДЕМО · полный комментарий\nПродолжение';self.imported(workbook(source))
        data=self.panel(site='s1');self.assertEqual(data['totalRows'],20)
        by_id={r['sourceId']:r for r in data['rows']}
        for original in (r for i,r in enumerate(source) if i%2==0):
            row=by_id[original['sourceId']]
            for key in FIELDS:self.assertEqual(row[key],original[key],key)
        self.assertEqual(by_id['DEMO-001']['comment'],source[0]['comment']);self.assertIn('https://bitrix.demo.invalid/',by_id['DEMO-001']['decision'])
        scoped=self.panel(self.inspector,site='s1');self.assertEqual(scoped['rows'],data['rows'])
        foreign=self.panel(self.inspector,site='s2');self.assertEqual(foreign['totalRows'],0);self.assertFalse(foreign['rows'])
        exported=self.inspector.get('/api/writeoff-analytics/export/rows.csv',params={'site':'s1','expectedSnapshot':scoped['snapshot']})
        self.assertEqual(exported.status_code,200);table=list(csv.reader(io.StringIO(exported.content.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(len(table),21);first=next(r for r in table[1:] if r[0]=='DEMO-001');self.assertEqual(first[12],source[0]['decision']);self.assertEqual(first[16],source[0]['comment'])
        self.assertEqual(self.panel(site='s1',q='полный комментарий')['totalRows'],1)
        self.assertEqual(self.panel(q='полный комментарий')['totalRows'],1)

    def test_all_positions_across_pages_keep_duplicate_looking_and_zero_rows(self):
        source=[]
        for i in range(121):
            row=rows()[0].copy();row.update(sourceId='DEMO-ALL-'+str(i),quantity='0' if i%11==0 else '1',comment='ДЕМО · строка '+str(i));source.append(row)
        self.imported(workbook(source));found=[]
        for page in (1,2,3):
            d=self.panel(site='s1',page=page,limit=50);self.assertEqual(d['totalRows'],121);found+=d['rows']
        self.assertEqual([r['sourceRow'] for r in found],list(range(2,123)))
        self.assertEqual(len(found),121);self.assertEqual(len({r['id'] for r in found}),121)
        self.assertEqual({r['sourceId'] for r in found},{r['sourceId'] for r in source});self.assertEqual(sum(r['quantity']=='0' for r in found),11)
        self.assertTrue(all('comment' in r and 'decision' in r for r in found))
