"""Issue #17: independent desktop/mobile sessions, timezone boundaries and real visits API."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from server.app import create_app
from server.database import Database, ROOT, seed_demo
from tests.test_api import MANAGER_PASSWORD, INSPECTOR_PASSWORD
from tests.browser import login, nav, saved


def fixture(database):
    app=create_app(database,'http://testserver',False)
    def client(role,password):
        c=TestClient(app,headers={'Origin':'http://testserver'})
        response=c.post('/api/login',json={'username':role,'password':password})
        c.headers['X-CSRF-Token']=response.json()['csrf'];return c
    def change(client, ident, data, version=0, extra=None):
        response=client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':(extra or [])+[
            {'collection':'visits','id':ident,'expectedVersion':version,'data':data}]})
        assert response.status_code==200,response.text
    with client('manager',MANAGER_PASSWORD) as m,client('inspector',INSPECTOR_PASSWORD) as i:
        for ident,date,assignee in [('DEMO-other','2026-11-01','manager'),('DEMO-second','2026-11-01','inspector'),
                                   ('DEMO-past','2026-10-30','inspector'),('DEMO-cancel','2026-11-01','inspector'),
                                   ('DEMO-done','2026-11-01','inspector')]:
            change(m,ident,{'siteId':'s1','date':date,'assignedTo':assignee,'goal':'ДЕМО · '+ident})
        visits={v['id']:v for v in m.get('/api/state').json()['visits']}
        change(i,'DEMO-cancel',{**visits['DEMO-cancel'],'status':'cancelled','cancellationReason':'ДЕМО · Исходная отмена'},1)
        questions=i.get('/api/state').json()['questions']
        act={'collection':'inspections','id':'DEMO-act','expectedVersion':0,'data':{'siteId':'s1','date':'2026-11-01',
             'inspector':'ДЕМО','results':[{'questionId':q['id'],'status':'ok'} for q in questions]}}
        change(i,'DEMO-done',{**visits['DEMO-done'],'status':'completed','inspectionId':'DEMO-act'},1,[act])
    app.state.import_jobs.close()


def run():
    with tempfile.TemporaryDirectory(prefix='ksu-calendar-') as folder:
        path=Path(folder)/'DEMO.sqlite3';seed_demo(Database(path),MANAGER_PASSWORD,INSPECTOR_PASSWORD);fixture(path)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}'
        env={**os.environ,'KSU_DB':str(path),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
        with (Path(folder)/'server.log').open('w+') as log:
            server=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=build_opener(ProxyHandler({}))
                for _ in range(100):
                    try:
                        if opener.open(origin+'/api/health',timeout=.2).status==200:break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Local API did not start')
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
                    manager=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU',timezone_id='America/Los_Angeles')
                    inspector=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU',timezone_id='Pacific/Kiritimati')
                    m=manager.new_page();i=inspector.new_page();errors=[]
                    for page in (m,i):
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.clock.set_fixed_time(datetime(2026,11,1,0,30,tzinfo=timezone.utc))
                    login(m,origin,'manager',MANAGER_PASSWORD);login(i,origin,'inspector',INSPECTOR_PASSWORD)
                    for page in (m,i):
                        nav(page,'schedule');page.locator('[data-schedule-view="calendar"]').click()
                        page.locator('#calendar-date').fill('2026-11-01')
                        expect(page.locator('[data-calendar-day="2026-11-01"] .calendar-count')).to_have_text('4')
                        expect(page.locator('.calendar-day-visits [data-visit-id]')).to_have_count(4)
                    assert i.locator('#schedule-assignee').count()==0
                    # Explicit site choice, date prefilled; create in the same authoritative visits collection.
                    i.locator('[data-calendar-day="2026-11-01"]').click()
                    i.locator('[data-calendar-create]').click()
                    expect(i.locator('#visit-form [name="date"]')).to_have_value('2026-11-01')
                    expect(i.locator('#visit-form [name="siteId"]')).to_have_value('')
                    i.locator('#visit-form [name="siteId"]').select_option('s1')
                    i.locator('#visit-form [name="goal"]').fill('ДЕМО · Из календаря <b>текст</b>')
                    i.locator('[data-action="save-visit"]').click();saved(i)
                    own=i.evaluate('db.visits.find(v=>v.goal.startsWith("ДЕМО · Из календаря")).id')
                    row=i.locator(f'[data-visit-id="{own}"]')
                    expect(row).to_contain_text('<b>текст</b>')
                    assert row.locator('b').count()==0
                    expect(m.locator(f'[data-visit-id="{own}"]')).to_be_visible(timeout=8000)
                    i.screenshot(path='/tmp/ksu_calendar_mobile_month.png',full_page=True)
                    m.screenshot(path='/tmp/ksu_calendar_desktop.png',full_page=True)
                    # Open details, move with the existing versioned editor, then cancel with reason.
                    row.locator('[data-calendar-visit]').click()
                    i.locator('.modal [data-action="edit-visit"]').click()
                    i.locator('#visit-form [name="date"]').fill('2026-11-02')
                    i.locator('[data-action="save-visit"]').click();saved(i)
                    expect(i.locator(f'[data-visit-id="{own}"]')).to_have_count(0)
                    for page in (m,i):page.locator('#calendar-date').fill('2026-11-02')
                    expect(m.locator(f'[data-visit-id="{own}"]')).to_be_visible(timeout=8000)
                    row.locator('[data-action="cancel-visit"]').click()
                    i.locator('[data-action="save-cancel-visit"]').click()
                    expect(i.locator('#cancel-visit-form')).to_be_visible()
                    i.locator('#cancel-visit-form textarea').fill('ДЕМО · Перенос работ')
                    i.locator('[data-action="save-cancel-visit"]').click();saved(i)
                    expect(m.locator(f'[data-visit-id="{own}"]')).to_contain_text('ДЕМО · Перенос работ',timeout=8000)
                    m.locator(f'[data-visit-id="{own}"] [data-action="history"]').click()
                    expect(m.locator('.timeline-item')).to_have_count(3)
                    expect(m.locator('.timeline-item').first).to_contain_text('ДЕМО · Эльман')
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    # Manager-only assignee filter, shared status/site filters and preserved list.
                    m.locator('#calendar-date').fill('2026-11-01')
                    m.locator('#schedule-assignee').select_option('manager')
                    expect(m.locator('.calendar-day-visits [data-visit-id]')).to_have_count(1)
                    m.locator('[data-schedule-view="list"]').click()
                    expect(m.locator('[data-visit-id]')).to_have_count(1)
                    m.locator('[data-schedule-reset]').click()
                    m.locator('#schedule-site').select_option('s2')
                    expect(m.locator('[data-visit-id]')).to_have_count(0)
                    m.locator('[data-schedule-reset]').click()
                    m.locator('[data-schedule-view="calendar"]').click()
                    for page in (m,i):
                        page.locator('#schedule-status').select_option('overdue')
                        page.locator('#calendar-date').fill('2026-10-30')
                        expect(page.locator('.calendar-day-visits')).to_contain_text('Просрочен')
                        assert page.evaluate('db.visits.find(v=>v.id==="DEMO-past").status')=='planned'
                        page.locator('[data-schedule-reset]').click()
                    # Modes and local date arithmetic across Sunday/month/year/leap/DST boundaries.
                    i.locator('#calendar-date').fill('2026-11-01')
                    i.locator('[data-calendar-mode="week"]').click()
                    expect(i.locator('[data-calendar-day]')).to_have_count(7)
                    expect(i.locator('[data-calendar-day="2026-10-26"]')).to_be_visible()
                    i.screenshot(path='/tmp/ksu_calendar_mobile_week.png',full_page=True)
                    i.locator('[data-calendar-step="1"]').click();expect(i.locator('#calendar-date')).to_have_value('2026-11-08')
                    i.locator('[data-calendar-mode="day"]').click()
                    i.locator('#calendar-date').fill('2026-12-31')
                    i.locator('[data-calendar-step="1"]').click();expect(i.locator('#calendar-date')).to_have_value('2027-01-01')
                    i.locator('[data-calendar-mode="month"]').click()
                    i.locator('#calendar-date').fill('2028-01-31')
                    i.locator('[data-calendar-step="1"]').click();expect(i.locator('#calendar-date')).to_have_value('2028-02-29')
                    assert i.locator('[data-calendar-day="2028-02-29"]').count()==1
                    i.locator('#calendar-date').fill('2026-12-31')
                    i.locator('[data-calendar-day="2026-12-31"]').focus()
                    i.keyboard.press('ArrowRight')
                    expect(i.locator('[data-calendar-day="2027-01-01"]')).to_be_focused()
                    # Reload restores preferences for this account when returning to the plan.
                    i.reload();expect(i.locator('.workspace')).to_be_visible();nav(i,'schedule')
                    expect(i.locator('[data-schedule-view="calendar"]')).to_have_attribute('aria-pressed','true')
                    expect(i.locator('#calendar-date')).to_have_value('2027-01-01')
                    # The header action also uses the selected calendar date, never tomorrow.
                    i.locator('[data-action="add-visit"]').click()
                    expect(i.locator('#visit-form [name="date"]')).to_have_value('2027-01-01')
                    expect(i.locator('#visit-form [name="siteId"]')).to_have_value('')
                    i.keyboard.press('Escape')
                    # Today is local: same instant is different calendar dates in the two contexts.
                    for page,date in ((i,'2026-11-01'),(m,'2026-10-31')):
                        page.locator('[data-calendar-today]').click();expect(page.locator('#calendar-date')).to_have_value(date)
                    i.locator('#calendar-date').fill('2026-11-01')
                    for ident in ('DEMO-other','DEMO-done','DEMO-cancel'):
                        protected=i.locator(f'[data-visit-id="{ident}"]')
                        expect(protected.locator('[data-action="edit-visit"],[data-action="cancel-visit"],[data-action="start-audit"]')).to_have_count(0)
                    # Complete an assigned visit from the day agenda using the unchanged checklist.
                    i.locator('[data-visit-id="DEMO-second"] [data-action="start-audit"]').click()
                    for answer in i.locator('button[data-answer][data-value="ok"]').all():answer.click()
                    i.locator('[data-action="save-audit"]').click();saved(i);nav(i,'schedule')
                    expect(i.locator('[data-visit-id="DEMO-second"]')).to_contain_text('Выполнен')
                    m.locator('#calendar-date').fill('2026-11-01')
                    expect(m.locator('[data-visit-id="DEMO-second"]')).to_contain_text('Выполнен',timeout=8000)
                    # Completed act and site navigation work from the calendar detail.
                    i.locator('[data-visit-id="DEMO-done"] [data-calendar-visit]').click()
                    i.locator('.modal [data-action="audit-detail"]').click()
                    expect(i.get_by_role('heading',name='Акт проверки',exact=True)).to_be_visible()
                    i.get_by_role('button',name='Закрыть',exact=True).first.click()
                    i.locator('[data-visit-id="DEMO-other"] [data-calendar-visit]').click()
                    i.keyboard.press('Shift+Tab')
                    assert i.locator('.modal').evaluate('el=>el.contains(document.activeElement)')
                    i.locator('.modal [data-action="site-detail"]').click()
                    expect(i.locator('.page-heading h2')).to_have_text('ДЕМО · Нова')
                    nav(i,'schedule');i.locator('[data-calendar-mode="day"]').click()
                    i.screenshot(path='/tmp/ksu_calendar_mobile_day.png',full_page=True)
                    for width in (320,390,760):
                        i.set_viewport_size({'width':width,'height':844})
                        for mode in ('month','week','day'):
                            i.locator('[data-calendar-mode="'+mode+'"]').click()
                            assert i.evaluate('document.documentElement.scrollWidth')==width,(width,mode)
                    # No calendar preferences leak to a different account in the same tab.
                    i.locator('[data-action="logout"]').click()
                    expect(i.locator('#login-form')).to_be_visible()
                    login(i,origin,'manager',MANAGER_PASSWORD);nav(i,'schedule')
                    expect(i.locator('[data-schedule-view="list"]')).to_have_attribute('aria-pressed','true')
                    assert not errors,errors
                    browser.close()
                print('PASS: calendar month/week/day, independent sessions, mobile create/move/cancel and audit, filters/list, ACL actions, act/site navigation, keyboard, reload, leap/year/DST/local dates, 320/390/760px, escaped text; no JS errors')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-3500:]);raise
            finally:
                server.terminate();server.wait(timeout=15)


if __name__=='__main__':run()
