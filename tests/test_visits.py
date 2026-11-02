"""Issue #11: synthetic visits, real API authorization and SQLite guards."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import unittest
from uuid import uuid4

from server.database import Database
from tests import test_api


class Visits(unittest.TestCase):
    setUp = test_api.API.setUp
    tearDown = test_api.API.tearDown
    client = test_api.API.client
    state = test_api.API.state
    change = test_api.API.change

    def visit(self, client=None, ident='visit', **values):
        data = {'siteId':'s1','date':'2026-11-04','kind':'inspection','goal':'ДЕМО · Маркировка',**values}
        response=self.change(client or self.inspector,'visits',data,ident)
        self.assertEqual(response.status_code,200,response.text)
        return next(v for v in response.json()['visits'] if v['id']==ident)

    def edit(self, record, client=None, **values):
        return self.change(client or self.inspector,'visits',{**record,**values},record['id'],record['version'])

    def history(self, ident='visit', client=None):
        return (client or self.manager).get('/api/history?collection=visits&record_id='+ident).json()

    def inspection(self, ident='inspection'):
        results=[{'questionId':q['id'],'status':'ok'} for q in self.state()['questions']]
        return {'collection':'inspections','id':ident,'expectedVersion':0,
                'data':{'siteId':'s1','date':'2026-11-04','inspector':'ДЕМО','results':results}}

    def complete(self, record, ident='inspection', **values):
        return self.inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
            self.inspection(ident),{'collection':'visits','id':record['id'],'expectedVersion':record['version'],
            'data':{**record,'status':'completed','inspectionId':ident,**values}}]})

    def test_create_self_is_server_assigned_and_visible_to_manager(self):
        record=self.visit(assignee='Подложное имя',createdBy='manager')
        self.assertEqual(record['assignedTo'],'inspector')
        self.assertEqual(record['assignee'],'ДЕМО · Эльман')
        self.assertEqual(record['createdBy'],'inspector')
        self.assertEqual(self.state()['visits'],[record])
        self.assertEqual(self.history()[0]['after']['version'],1)
        self.assertEqual(self.history()[0]['actor_id'],'inspector')

    def test_move_edit_and_cancel_are_versioned_and_audited(self):
        record=self.visit()
        response=self.edit(record,date='2026-11-06',siteId='s2',kind='followup',goal='ДЕМО · Новая цель')
        self.assertEqual(response.status_code,200,response.text)
        record=response.json()['visits'][0]
        self.assertEqual(record['version'],2)
        response=self.edit(record,status='cancelled',cancellationReason='ДЕМО · Объект закрыт',
                           cancelledBy='manager',cancelledAt='1900-01-01')
        self.assertEqual(response.status_code,200,response.text)
        cancelled=response.json()['visits'][0]
        self.assertEqual(cancelled['cancelledBy'],'inspector')
        self.assertNotEqual(cancelled['cancelledAt'],'1900-01-01')
        self.assertEqual(cancelled['version'],3)
        history=self.history(client=self.inspector)
        self.assertEqual([e['after']['version'] for e in history],[3,2,1])
        self.assertEqual(history[1]['before']['date'],'2026-11-04')
        self.assertEqual(history[1]['after']['date'],'2026-11-06')
        self.assertEqual(history[0]['after']['cancellationReason'],'ДЕМО · Объект закрыт')
        self.assertEqual(history[0]['actor_id'],'inspector')
        for client in (self.manager,self.inspector):
            self.assertEqual(self.edit(cancelled,client,goal='Обход').status_code,403)
        self.assertEqual(len(self.history()),3)
        self.assertEqual(self.state()['visits'][0]['status'],'cancelled')

    def test_foreign_site_and_assignee_are_forbidden(self):
        for target in ('manager','unknown',None):
            self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2026-11-04','assignedTo':target}).status_code,403)
        record=self.visit()
        self.assertEqual(self.edit(record,assignedTo='manager').status_code,403)
        with self.app.state.db.connect() as con:
            con.execute("delete from user_sites where user_id='inspector' and site_id='s2'")
        self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s2','date':'2026-11-04'}).status_code,403)
        self.assertEqual(self.edit(record,siteId='s2').status_code,403)
        self.assertEqual(len(self.history()),1)

    def test_current_inspector_id_is_used_instead_of_demo_login(self):
        with self.app.state.db.connect() as con:
            con.execute("insert into users values(?,?,?,?,?,1)",
                        ('inspector-two','inspector-two','ДЕМО · Второй инспектор','inspector',
                         self.app.state.db.user('inspector')['password']))
            con.execute("insert into user_sites values('inspector-two','s1')")
        client=self.client('inspector-two',test_api.INSPECTOR_PASSWORD)
        try:
            record=self.visit(client)
            self.assertEqual(record['assignedTo'],'inspector-two')
            self.assertEqual(record['createdBy'],'inspector-two')
            self.assertEqual(self.edit(record,goal='Чужая запись').status_code,403)
            self.assertEqual(self.edit(record,client,siteId='s2').status_code,403)
            self.assertEqual(self.edit(record,client,date='2026-11-10').status_code,200)
        finally:
            client.close()

    def test_foreign_visits_and_reassigned_own_authorship_do_not_grant_edit_right(self):
        foreign=self.visit(self.manager,assignedTo='manager')
        for status in ('planned','cancelled'):
            self.assertEqual(self.edit(foreign,status=status,cancellationReason='ДЕМО').status_code,403)
        own=self.visit(ident='own')
        response=self.edit(own,self.manager,assignedTo='manager')
        self.assertEqual(response.status_code,200,response.text)
        reassigned=next(v for v in response.json()['visits'] if v['id']=='own')
        self.assertEqual(reassigned['createdBy'],'inspector')
        self.assertEqual(self.edit(reassigned,goal='Обход').status_code,403)
        self.assertEqual(self.edit({**reassigned,'version':0},goal='Обход').status_code,403)

    def test_manager_assigned_visit_can_be_moved_by_assigned_inspector(self):
        record=self.visit(self.manager)
        self.assertEqual(record['createdBy'],'manager')
        self.assertEqual(self.edit(record,date='2026-11-07').status_code,200)
        self.assertEqual([x['actor_id'] for x in self.history(client=self.inspector)],['inspector','manager'])

    def test_manager_reassignment_cancel_and_scoped_assignee_catalogue(self):
        record=self.visit()
        changed=self.edit(record,self.manager,assignedTo='manager').json()['visits'][0]
        self.assertEqual(changed['assignee'],'ДЕМО · Андрей')
        response=self.edit(changed,self.manager,status='cancelled',cancellationReason='ДЕМО · Перепланирование')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['visits'][0]['cancelledBy'],'manager')
        self.assertTrue(self.state()['assignees'])
        self.assertEqual(self.state(self.inspector)['assignees'],[])
        for person in self.state()['assignees']:
            self.assertEqual(set(person),{'id','name','siteIds'})
        with self.app.state.db.connect() as con:
            con.execute("delete from user_sites where user_id='inspector' and site_id='s2'")
        self.assertEqual(self.change(self.manager,'visits',{'siteId':'s2','date':'2026-11-04'}).status_code,422)

    def test_cancellation_reason_and_status_shortcuts_rejected_without_audit(self):
        record=self.visit()
        for reason in ('','   ','x'*1201):
            self.assertEqual(self.edit(record,status='cancelled',cancellationReason=reason).status_code,422)
        self.assertEqual(self.edit(record,cancellationReason='Подложная отмена').status_code,403)
        self.assertEqual(self.edit(record,cancelledBy='manager').status_code,403)
        self.assertEqual(self.edit(record,status='cancelled',cancellationReason='ДЕМО',date='2026-11-08').status_code,403)
        for status in ('completed','cancelled'):
            self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2026-11-04','status':status}).status_code,403)
        self.assertEqual(self.edit(record,inspectionId='fake').status_code,422)
        self.assertEqual(self.edit(record,role='manager').status_code,422)
        self.assertEqual(len(self.history()),1)

    def test_completion_requires_new_atomic_act_and_freezes_both_roles(self):
        record=self.visit()
        self.assertEqual(self.complete(record,date='2026-11-08').status_code,403)
        self.assertEqual(self.state()['inspections'],[])
        response=self.complete(record)
        self.assertEqual(response.status_code,200,response.text)
        completed=response.json()['visits'][0]
        for client in (self.inspector,self.manager):
            self.assertEqual(self.edit(completed,client,goal='Обход').status_code,403)
            self.assertEqual(self.edit(completed,client,status='planned',inspectionId=None).status_code,403)
        self.assertEqual(self.history()[0]['after']['version'],2)
        self.assertEqual(self.history()[0]['after']['inspectionId'],'inspection')

    def test_existing_act_cannot_be_attached_or_reused(self):
        record=self.visit()
        inspection=self.inspection()
        response=self.inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[inspection]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.edit(record,status='completed',inspectionId='inspection').status_code,403)
        other=self.visit(ident='other')
        response=self.inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
            self.inspection('new-act'),*[
                {'collection':'visits','id':v['id'],'expectedVersion':1,'data':{**v,'status':'completed','inspectionId':'new-act'}}
                for v in (record,other)]]})
        self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(len(self.state()['inspections']),1)
        self.assertTrue(all(v['status']=='planned' for v in self.state()['visits']))

    def test_concurrent_edits_conflict_and_retry_is_idempotent(self):
        record=self.visit()
        def edit(goal):
            return self.edit(record,goal=goal).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(edit,['ДЕМО · А','ДЕМО · Б'])),[200,409])
        self.assertEqual(len(self.history()),2)
        record=self.state()['visits'][0]
        mutation=uuid4().hex
        for _ in range(2):
            response=self.change(self.inspector,'visits',{**record,'date':'2026-11-09'},record['id'],record['version'],mutation)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.history()),3)

    def test_batch_rolls_back_own_edit_when_foreign_visit_is_modified(self):
        own=self.visit()
        foreign=self.visit(self.manager,ident='foreign',assignedTo='manager')
        response=self.inspector.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
            {'collection':'visits','id':v['id'],'expectedVersion':1,'data':{**v,'goal':'Обход'}} for v in (own,foreign)]})
        self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(self.state()['visits'][0]['version'],1)
        self.assertEqual(len(self.history()),1)

    def test_sql_guards_block_assignment_and_terminal_bypass(self):
        record=self.visit()
        actor=self.app.state.db.user('inspector')
        for data in ({**record,'assignedTo':'manager'},{**record,'assignedTo':None},
                     {**record,'status':'cancelled','cancellationReason':'','cancelledBy':'inspector'},
                     {**record,'status':'completed','inspectionId':'fake'}):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.app.state.db.connect(actor) as con:
                    con.execute("update records set data=?,version=2 where collection='visits' and id='visit'",(json.dumps(data),))
        record=self.edit(record,status='cancelled',cancellationReason='ДЕМО').json()['visits'][0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.state.db.connect(self.app.state.db.user('manager')) as con:
                con.execute("update records set data=?,version=3 where collection='visits' and id='visit'",(json.dumps({**record,'status':'planned'}),))

    def test_existing_database_guards_upgrade_without_changing_data_or_history(self):
        record=self.visit()
        history=self.history()
        password=self.app.state.db.user('inspector')['password']
        with self.app.state.db.connect() as con:
            con.executescript("drop trigger guard_insert; create trigger guard_insert before insert on records begin select raise(abort,'legacy'); end;")
        Database(self.app.state.db.path)
        Database(self.app.state.db.path)
        self.assertEqual(self.state()['visits'],[record])
        self.assertEqual(self.history(),history)
        self.assertEqual(self.app.state.db.user('inspector')['password'],password)
        self.visit(ident='after-upgrade')
