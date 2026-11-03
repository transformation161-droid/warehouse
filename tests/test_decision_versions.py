"""Version contracts run unchanged on SQLite and real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import create_app
from server.database import Database
from tests import test_decision_import as decision_tests
from tests import test_api


class Versions(unittest.TestCase):
    setUp=test_api.API.setUp
    tearDown=decision_tests.DecisionImport.tearDown
    client=test_api.API.client
    state=test_api.API.state
    book=decision_tests.DecisionImport.book
    row=decision_tests.DecisionImport.row
    upload=decision_tests.DecisionImport.upload
    wait=decision_tests.DecisionImport.wait
    preview=decision_tests.DecisionImport.preview
    commit=decision_tests.DecisionImport.commit

    def initial(self,rows=None):
        ident=self.upload(self.book(rows or [self.row(cost='10',residual='5')]))
        self.assertEqual(self.preview(ident)['status'],'ready');self.assertEqual(self.commit(ident)['status'],'done')
        return self.manager.get('/api/decisions').json()['rows']

    def correction(self,rows,doc,mode='full',target_ids=None,matches=None,client=None):
        client=client or self.manager;self.last_correction=self.book(rows);ident=self.upload(self.last_correction,client)
        client.post('/api/decision-imports/'+ident+'/preview',json={});self.assertEqual(self.wait(ident,client)['status'],'ready')
        expected=client.get('/api/decision-documents/'+doc+'/versions').json()[0]['number']
        settings={'documentId':doc,'expectedVersion':expected,'mode':mode,'reason':'ДЕМО · Исправление первоисточника','targetIds':target_ids or [],'matches':matches or {}}
        self.assertEqual(client.post('/api/decision-imports/'+ident+'/version-preview',json=settings).status_code,200)
        meta=self.wait(ident,client);self.assertEqual(meta['status'],'ready',meta)
        return ident,meta

    def confirm(self,ident,meta,client=None):
        client=client or self.manager
        response=client.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']})
        self.assertEqual(response.status_code,200,response.text);return self.wait(ident,client)

    def test_full_10_to_12_active_totals_export_and_preserved_history(self):
        old=self.initial()[0];ident,meta=self.correction([self.row(cost='12',residual='5')],old['documentId'])
        plan=meta['versionPlan'];self.assertEqual(plan['summary']['counts'],{'added':0,'changed':1,'removed':0,'unchanged':0})
        self.assertEqual(plan['summary']['before']['cost'],'10');self.assertEqual(plan['summary']['after']['cost'],'12')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'10')
        compare=self.manager.get('/api/decision-imports/'+ident+'/comparison').json()['rows'][0]
        self.assertEqual((compare['before']['cost'],compare['after']['cost']),('10','12'))
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/commit').status_code,422)
        self.assertEqual(self.confirm(ident,meta)['status'],'done')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'12')
        self.assertEqual(self.inspector.get('/api/decisions').json()['total'],1)
        self.assertEqual(self.manager.get('/api/decisions?history=true').json()['total'],2)
        old_detail=self.manager.get('/api/decisions/'+old['id']).json();self.assertFalse(old_detail['active']);self.assertEqual(old_detail['data']['cost'],'10')
        self.assertEqual(self.manager.get('/api/decisions',params={'documentId':old['documentId'],'version':1}).json()['rows'][0]['cost'],'10')
        self.assertEqual(len(self.manager.get('/api/decisions/export.csv').text.splitlines()),2)
        self.assertEqual(self.confirm(ident,meta)['status'],'done')
        versions=self.inspector.get('/api/decision-documents/'+old['documentId']+'/versions').json()
        self.assertEqual([v['number'] for v in versions],[2,1]);self.assertEqual(versions[0]['author_id'],'manager')
        audit=self.manager.get('/api/history?collection=decisionVersions').json();self.assertEqual(len(audit),1)
        self.assertEqual(audit[0]['after']['after']['cost'],'12')
        source=self.manager.get('/api/history?collection=decisions&record_id='+old['id']).json();self.assertEqual(source[0]['after']['cost'],'10')
        repeat=self.upload(self.last_correction,self.manager)
        self.manager.post('/api/decision-imports/'+repeat+'/preview',json={});self.wait(repeat,self.manager)
        self.manager.post('/api/decision-imports/'+repeat+'/commit');self.assertTrue(self.wait(repeat,self.manager)['duplicate'])

    def test_full_added_removed_and_partial_explicit_selection(self):
        old=self.initial([self.row(sourceId='A',cost='10'),self.row(sourceId='B',cost='20'),self.row(sourceId='C',cost='30')])
        doc=old[0]['documentId'];by_id={r['sourceId']:r for r in old}
        ident,meta=self.correction([self.row(sourceId='A',cost='12'),self.row(sourceId='D',cost='4.0001')],doc,'partial',[by_id['A']['id'],by_id['B']['id']])
        self.assertEqual(meta['versionPlan']['summary']['counts'],{'added':1,'changed':1,'removed':1,'unchanged':0})
        self.assertEqual(meta['versionPlan']['summary']['after']['cost'],'46.0001')
        self.assertEqual(self.confirm(ident,meta)['status'],'done')
        rows=self.manager.get('/api/decisions').json()['rows'];self.assertEqual({r['sourceId'] for r in rows},{'A','C','D'})
        self.assertEqual(next(r for r in rows if r['sourceId']=='C')['id'],by_id['C']['id'])

    def test_zero_and_four_decimals_preserved_missing_value_is_a_change(self):
        old=self.initial([self.row(cost='10',residual='',reimbursable='0.0000',reimbursed='0')])[0]
        ident,meta=self.correction([self.row(cost='0.0000',residual='0',reimbursable='0',reimbursed='0')],old['documentId'])
        compared=self.manager.get('/api/decision-imports/'+ident+'/comparison').json()['rows'][0]
        self.assertIn('residual',compared['fields']);self.assertNotIn('reimbursable',compared['fields'])
        self.assertEqual(self.confirm(ident,meta)['status'],'done')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'0.0000')
        self.assertIsNone(self.manager.get('/api/decisions/'+old['id']).json()['data']['residual'])

    def test_missing_and_ambiguous_ids_require_manual_pairs(self):
        old=self.initial([self.row(sourceId='',cost='10')])[0]
        ident,meta=self.correction([self.row(sourceId='',cost='12')],old['documentId'])
        self.assertFalse(meta['versionPlan']['ready']);self.assertTrue(meta['versionPlan']['conflicts'])
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']}).status_code,409)
        settings=meta['versionPlan']['settings'];settings['matches']={'2':old['id']}
        self.manager.post('/api/decision-imports/'+ident+'/version-preview',json=settings);meta=self.wait(ident,self.manager)
        self.assertTrue(meta['versionPlan']['ready']);self.assertEqual(self.confirm(ident,meta)['status'],'done')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'12')

    def test_duplicate_ids_and_out_of_scope_manual_mapping_are_not_guessed(self):
        old=self.initial([self.row(sourceId='DUP',cost='10'),self.row(sourceId='DUP',cost='20')])
        ident,meta=self.correction([self.row(sourceId='DUP',cost='12')],old[0]['documentId'])
        self.assertFalse(meta['versionPlan']['ready'])
        settings=meta['versionPlan']['settings'];settings['matches']={'2':'DEMO-outside-target'}
        self.manager.post('/api/decision-imports/'+ident+'/version-preview',json=settings)
        self.assertEqual(self.wait(ident,self.manager)['status'],'error')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'30')
        settings['matches']={'2':old[0]['id']}
        self.manager.post('/api/decision-imports/'+ident+'/version-preview',json=settings);meta=self.wait(ident,self.manager)
        self.assertTrue(meta['versionPlan']['ready']);self.assertEqual(self.confirm(ident,meta)['status'],'done')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'12')

    def test_same_id_in_other_object_is_independent(self):
        old=self.initial()[0];ident=self.upload(self.book([self.row(cost='20',site='ДЕМО · Дом Горизонтов')]))
        self.assertEqual(self.preview(ident)['status'],'ready');self.assertEqual(self.commit(ident)['status'],'done')
        self.assertEqual(self.manager.get('/api/decisions').json()['total'],2)
        ident=self.upload(self.book([self.row(cost='12',site='ДЕМО · Дом Горизонтов')]),self.manager)
        self.manager.post('/api/decision-imports/'+ident+'/preview',json={});self.wait(ident,self.manager)
        settings={'documentId':old['documentId'],'expectedVersion':1,'mode':'full','reason':'ДЕМО · Недопустимый объект'}
        self.manager.post('/api/decision-imports/'+ident+'/version-preview',json=settings)
        self.assertEqual(self.wait(ident,self.manager)['status'],'error')
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'30')

    def test_other_document_cannot_be_included_accidentally(self):
        old=self.initial([self.row(document='ДЕМО · Акт A',sourceId='SAME',cost='10'),self.row(document='ДЕМО · Акт B',sourceId='SAME',cost='20')])
        doc=next(row['documentId'] for row in old if row['document']=='ДЕМО · Акт A')
        for rows in ([self.row(document='ДЕМО · Акт B',sourceId='SAME',cost='22')],
                     [self.row(document='ДЕМО · Акт A',sourceId='SAME',cost='12'),self.row(document='ДЕМО · Акт B',sourceId='SAME',cost='22')]):
            ident=self.upload(self.book(rows),self.manager)
            self.manager.post('/api/decision-imports/'+ident+'/preview',json={});self.wait(ident,self.manager)
            self.manager.post('/api/decision-imports/'+ident+'/version-preview',json={'documentId':doc,'expectedVersion':1,'mode':'full','reason':'ДЕМО · Неверно выбран документ'})
            self.assertEqual(self.wait(ident,self.manager)['status'],'error')
            self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'30')

    def test_roles_scope_and_unconfirmed_or_changed_tokens(self):
        old=self.initial()[0];ident=self.upload(self.book([self.row(cost='12')]))
        self.preview(ident)
        settings={'documentId':old['documentId'],'expectedVersion':1,'mode':'full','reason':'ДЕМО'}
        self.assertEqual(self.inspector.post('/api/decision-imports/'+ident+'/version-preview',json=settings).status_code,403)
        self.assertEqual(self.inspector.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':'0'*64}).status_code,403)
        self.assertEqual(self.commit(ident)['status'],'error')
        ident,meta=self.correction([self.row(cost='12')],old['documentId'])
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':'0'*64}).status_code,409)
        self.assertEqual(self.manager.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']},headers={'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'10')
        if hasattr(self,'admin'):
            import psycopg
            with psycopg.connect(self.admin) as con:con.execute("delete from ksu.user_sites where user_id='inspector' and site_id='s1'")
        else:
            with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.inspector.get('/api/decision-documents/'+old['documentId']+'/versions').status_code,404)
        self.assertEqual(self.inspector.get('/api/decisions/totals').json()['rows'],0)

    def second_manager(self):
        app=create_app(self.app.state.db.locator if self.app.state.db.backend=='postgresql' else self.app.state.db.path,test_api.ORIGIN,False)
        app.state.import_jobs.root=Path(self.temp.name)/'second-import-worker';app.state.import_jobs.root.mkdir()
        client=TestClient(app,headers={'Origin':test_api.ORIGIN})
        response=client.post('/api/login',json={'username':'manager','password':test_api.MANAGER_PASSWORD})
        self.assertEqual(response.status_code,200);client.headers['X-CSRF-Token']=response.json()['csrf']
        return app,client

    def test_two_servers_concurrent_corrections_have_one_winner(self):
        old=self.initial()[0];first,one=self.correction([self.row(cost='12')],old['documentId'])
        app,other=self.second_manager()
        try:
            second,two=self.correction([self.row(cost='13')],old['documentId'],client=other)
            def commit(args):
                client,ident,meta=args
                return client.post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']}).status_code
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(commit,[(self.manager,first,one),(other,second,two)])),[200,200])
            results=[self.wait(first,self.manager),self.wait(second,other)]
            self.assertEqual(sorted(r['status'] for r in results),['done','error'])
            self.assertIn('изменилась',next(r['message'] for r in results if r['status']=='error'))
            totals=self.manager.get('/api/decisions/totals').json();self.assertEqual(totals['rows'],1);self.assertIn(totals['totals']['cost'],('12','13'))
            versions=self.manager.get('/api/decision-documents/'+old['documentId']+'/versions').json();self.assertEqual(len(versions),2)
        finally:app.state.import_jobs.close();other.close()

    def test_version_mid_transaction_failure_and_immutable_sql_guards(self):
        old=self.initial()[0];ident,meta=self.correction([self.row(cost='12')],old['documentId'])
        before=self.manager.get('/api/history').json()
        if self.app.state.db.backend=='postgresql':
            import psycopg
            with psycopg.connect(self.admin) as con:
                con.execute("create function ksu.version_failure() returns trigger language plpgsql as $$ begin raise exception 'synthetic version failure' using errcode='23514'; end $$")
                con.execute('create trigger version_failure before insert on ksu.decision_entries for each row execute function ksu.version_failure()')
        else:
            with self.app.state.db.connect() as con:con.executescript("create trigger version_failure before insert on decision_entries begin select raise(abort,'synthetic version failure'); end;")
        self.assertEqual(self.confirm(ident,meta)['status'],'error')
        self.assertEqual(self.manager.get('/api/history').json(),before)
        self.assertEqual(self.manager.get('/api/decisions/totals').json()['totals']['cost'],'10')
        self.assertEqual(len(self.manager.get('/api/decision-documents/'+old['documentId']+'/versions').json()),1)
        for table in ('decision_documents','decision_versions','decision_entries'):
            with self.assertRaises(Exception):
                with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:con.execute('delete from '+table)
        with self.assertRaises(Exception):
            with self.app.state.db.connect(self.app.state.db.user('inspector')) as con:
                con.execute('insert into decision_retirements values(?,?,?)',(old['id'],old['documentId']+'-1','s1'))


if __name__=='__main__':unittest.main()
