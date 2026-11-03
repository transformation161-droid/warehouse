"""Combined PR #12/#13 acceptance on synthetic data, including snapshot restore."""
import json
from pathlib import Path
import unittest
from uuid import uuid4

from server.database import Database
from server.maintenance import snapshot_database
from tests import test_decision_import


class Integration(unittest.TestCase):
    setUp = test_decision_import.DecisionImport.setUp
    tearDown = test_decision_import.DecisionImport.tearDown
    state = test_decision_import.DecisionImport.state
    client = test_decision_import.DecisionImport.client
    book = test_decision_import.DecisionImport.book
    row = test_decision_import.DecisionImport.row
    upload = test_decision_import.DecisionImport.upload
    wait = test_decision_import.DecisionImport.wait
    preview = test_decision_import.DecisionImport.preview
    commit = test_decision_import.DecisionImport.commit

    def change(self, data, version=0):
        return self.inspector.post('/api/changes', json={
            'mutationId':uuid4().hex, 'changes':[{'collection':'visits','id':'DEMO-visit',
            'expectedVersion':version,'data':data}]})

    def history(self, client, collection, ident):
        response=client.get('/api/history',params={'collection':collection,'record_id':ident})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_visits_import_scope_audit_restart_and_backup_together(self):
        plan={'siteId':'s1','date':'2026-11-04','goal':'ДЕМО · Совместная проверка'}
        self.assertEqual(self.change(plan).status_code,200)
        source=self.book()
        job=self.upload(source);self.preview(job)
        self.assertEqual(self.commit(job)['status'],'done')
        record=self.manager.get('/api/decisions').json()['rows'][0]
        # Import writes must not break optimistic versions or terminal visit rules.
        self.assertEqual(self.change({**plan,'date':'2026-11-05'},1).status_code,200)
        self.assertEqual(self.change({**plan,'goal':'ДЕМО · Устаревшая правка'},1).status_code,409)
        self.assertEqual(self.change({**plan,'date':'2026-11-05','status':'cancelled',
                                     'cancellationReason':'ДЕМО · Перенос'},2).status_code,200)
        self.assertEqual(self.change(plan,3).status_code,403)
        for client in (self.manager,self.inspector):
            self.assertEqual([r['after']['version'] for r in self.history(client,'visits','DEMO-visit')],[3,2,1])
            audit=self.history(client,'decisions',record['id'])
            self.assertEqual(len(audit),1)
            self.assertEqual((audit[0]['actor_id'],audit[0]['operation']),('inspector','import'))
        # Reopening installs visit guards without changing imported rows/history.
        Database(self.app.state.db.path)
        again=self.upload(source);self.preview(again)
        self.assertTrue(self.commit(again)['duplicate'])
        with self.app.state.db.connect() as con:
            expected={table:con.execute('select * from '+table+' order by 1').fetchall()
                      for table in ('records','decision_records','imports','audit','users','media')}
            con.execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get('/api/decisions').json()['total'],0)
        self.assertEqual(self.inspector.get('/api/decisions/'+record['id']).status_code,404)
        self.assertEqual(self.history(self.inspector,'decisions',record['id']),[])
        self.assertEqual(self.history(self.inspector,'visits','DEMO-visit'),[])
        self.assertEqual(len(self.history(self.manager,'visits','DEMO-visit')),3)
        backup=Path(self.app.state.db.path).with_name('DEMO-restored.sqlite3')
        snapshot_database(self.app.state.db.path,backup)
        restored=Database(backup)
        with restored.connect() as con:
            for table,rows in expected.items():
                self.assertEqual(con.execute('select * from '+table+' order by 1').fetchall(),rows,table)
            self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0],0)
            self.assertEqual(con.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(con.execute('pragma foreign_key_check').fetchall(),[])
