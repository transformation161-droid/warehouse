"""Calendar uses the existing visits contract; date-only values, ACL and audit stay authoritative."""
import unittest
from tests import test_api


class Calendar(unittest.TestCase):
    setUp=test_api.API.setUp
    tearDown=test_api.API.tearDown
    client=test_api.API.client
    state=test_api.API.state
    change=test_api.API.change

    def test_date_only_leap_boundary_multiple_visits_and_audited_move(self):
        for ident,date in [('DEMO-a','2028-02-29'),('DEMO-b','2028-02-29'),('DEMO-c','2028-03-01')]:
            result=self.change(self.inspector,'visits',{'siteId':'s1','date':date,'goal':'ДЕМО · Календарь'},ident)
            self.assertEqual(result.status_code,200,result.text)
        visits={v['id']:v for v in self.state()['visits']}
        self.assertEqual(visits['DEMO-a']['date'],'2028-02-29')
        self.assertEqual(len([v for v in visits.values() if v['date']=='2028-02-29']),2)
        moved=self.change(self.inspector,'visits',{**visits['DEMO-a'],'date':'2028-03-01'},'DEMO-a',1)
        self.assertEqual(moved.status_code,200,moved.text)
        self.assertEqual(self.change(self.inspector,'visits',{**visits['DEMO-a'],'date':'2028-03-02'},'DEMO-a',1).status_code,409)
        history=self.manager.get('/api/history?collection=visits&record_id=DEMO-a')
        self.assertEqual(history.status_code,200)
        self.assertEqual([(r['before'] or {}).get('date') for r in history.json()],['2028-02-29',None])
        self.assertEqual(history.json()[0]['after']['date'],'2028-03-01')
        self.assertEqual(history.json()[0]['actor_id'],'inspector')
        self.assertEqual(self.state(self.inspector)['visits'],self.state()['visits'])

    def test_invalid_dates_rollback_and_overdue_is_not_a_server_status(self):
        for date in ['2027-02-29','2026-11-31','2026-11-01T23:30:00-08:00']:
            self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':date},'DEMO-invalid').status_code,422)
        self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2020-01-01','status':'overdue'},'DEMO-invalid').status_code,422)
        self.assertEqual(self.state()['visits'],[])
        self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2020-01-01'},'DEMO-past').status_code,200)
        before=self.manager.get('/api/history?collection=visits&record_id=DEMO-past').json()
        for _ in range(2):
            past=self.state()['visits'][0]
            self.assertEqual((past['date'],past['status'],past['version']),('2020-01-01','planned',1))
        self.assertEqual(self.manager.get('/api/history?collection=visits&record_id=DEMO-past').json(),before)

    def test_calendar_cannot_bypass_scope_assignment_or_terminal_rules(self):
        result=self.change(self.manager,'visits',{'siteId':'s1','date':'2026-12-31','assignedTo':'manager'},'DEMO-foreign')
        self.assertEqual(result.status_code,200)
        foreign=next(v for v in result.json()['visits'] if v['id']=='DEMO-foreign')
        self.assertEqual(self.change(self.inspector,'visits',{**foreign,'date':'2027-01-01'},'DEMO-foreign',1).status_code,403)
        result=self.change(self.inspector,'visits',{'siteId':'s1','date':'2026-12-31'},'DEMO-own')
        own=next(v for v in result.json()['visits'] if v['id']=='DEMO-own')
        cancelled=self.change(self.inspector,'visits',{**own,'status':'cancelled','cancellationReason':'ДЕМО · Отмена'},'DEMO-own',1)
        self.assertEqual(cancelled.status_code,200)
        for client in (self.manager,self.inspector):
            self.assertEqual(self.change(client,'visits',{**own,'date':'2027-01-01'},'DEMO-own',2).status_code,403)
        with self.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id='s1'")
        self.assertEqual(self.state(self.inspector)['visits'],[])
        self.assertEqual(self.change(self.inspector,'visits',{'siteId':'s1','date':'2027-01-01'},'DEMO-denied').status_code,403)
        self.assertEqual(self.inspector.get('/api/history?collection=visits&record_id=DEMO-own').json(),[])
        self.assertEqual(len(self.state()['visits']),2)
