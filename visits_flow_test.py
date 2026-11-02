"""Issue #11 browser acceptance: two sessions and synthetic data, no cloud APIs."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright, expect
from server.database import Database, ROOT, seed_demo
from tests.test_api import MANAGER_PASSWORD, INSPECTOR_PASSWORD
from tests.browser import login, nav, saved


def run():
    with tempfile.TemporaryDirectory(prefix='ksu-visit-ui-') as folder:
        path=Path(folder)/'synthetic.sqlite3'
        seed_demo(Database(path),MANAGER_PASSWORD,INSPECTOR_PASSWORD)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}'
        env={**os.environ,'KSU_DB':str(path),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
        with (Path(folder)/'server.log').open('w+') as log:
            process=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=build_opener(ProxyHandler({}))
                for _ in range(100):
                    try:
                        if opener.open(origin+'/api/health',timeout=.2).status==200:break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Local API did not start')
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
                    manager=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU')
                    inspector=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU')
                    m=manager.new_page();i=inspector.new_page();errors=[]
                    for page in (m,i):page.on('pageerror',lambda error:errors.append(str(error)))
                    login(m,origin,'manager',MANAGER_PASSWORD);login(i,origin,'inspector',INSPECTOR_PASSWORD)
                    nav(m,'schedule');nav(i,'schedule')
                    i.locator('[data-action="add-visit"]').click()
                    form=i.locator('#visit-form')
                    expect(form.locator('[name="assignee"]')).to_have_value('ДЕМО · Эльман')
                    expect(form.locator('select[name="assignedTo"]')).to_have_count(0)
                    form.locator('[name="date"]').fill('2026-11-04')
                    form.locator('[name="goal"]').fill('ДЕМО · Самостоятельный выезд')
                    i.locator('[data-action="save-visit"]').click();saved(i)
                    own=i.evaluate('db.visits.find(v=>v.goal==="ДЕМО · Самостоятельный выезд").id')
                    expect(m.locator(f'[data-visit-id="{own}"]')).to_contain_text('ДЕМО · Самостоятельный выезд',timeout=8000)
                    row=i.locator(f'[data-visit-id="{own}"]')
                    row.locator('[data-action="edit-visit"]').click()
                    form=i.locator('#visit-form')
                    form.locator('[name="date"]').fill('2026-11-06')
                    form.locator('[name="siteId"]').select_option('s2')
                    form.locator('[name="kind"]').select_option('followup')
                    form.locator('[name="goal"]').fill('ДЕМО · Перенесённый выезд')
                    i.locator('[data-action="save-visit"]').click();saved(i)
                    expect(m.locator(f'[data-visit-id="{own}"]')).to_contain_text('ДЕМО · Перенесённый выезд',timeout=8000)
                    row.locator('[data-action="cancel-visit"]').click()
                    i.locator('[data-action="save-cancel-visit"]').click()
                    expect(i.locator('#cancel-visit-form')).to_be_visible()
                    i.locator('#cancel-visit-form textarea').fill('ДЕМО · Объект закрыт')
                    i.locator('[data-action="save-cancel-visit"]').click();saved(i)
                    expect(row).to_contain_text('Причина отмены: ДЕМО · Объект закрыт')
                    expect(row.locator('[data-action="edit-visit"],[data-action="cancel-visit"]')).to_have_count(0)
                    manager_row=m.locator(f'[data-visit-id="{own}"]')
                    expect(manager_row).to_contain_text('ДЕМО · Объект закрыт',timeout=8000)
                    manager_row.locator('[data-action="history"]').click()
                    expect(m.locator('.timeline-item')).to_have_count(3)
                    expect(m.locator('.timeline-item').first).to_contain_text('ДЕМО · Эльман')
                    m.locator('.timeline-item').first.locator('summary').click()
                    expect(m.locator('.timeline-item').first.locator('pre')).to_contain_text('"version": 3')
                    expect(m.locator('.timeline-item').first.locator('pre')).to_contain_text('ДЕМО · Объект закрыт')
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    # Manager can still plan and reassign; another assignee's visit has no inspector actions.
                    m.locator('[data-action="add-visit"]').click()
                    form=m.locator('#visit-form')
                    form.locator('[name="assignedTo"]').select_option('manager')
                    form.locator('[name="goal"]').fill('ДЕМО · Чужой выезд')
                    m.locator('[data-action="save-visit"]').click();saved(m)
                    foreign=m.evaluate('db.visits.find(v=>v.goal==="ДЕМО · Чужой выезд").id')
                    foreign_row=i.locator(f'[data-visit-id="{foreign}"]')
                    expect(foreign_row).to_contain_text('ДЕМО · Чужой выезд',timeout=8000)
                    expect(foreign_row.locator('[data-action="edit-visit"],[data-action="cancel-visit"],[data-action="start-audit"]')).to_have_count(0)
                    m.locator(f'[data-visit-id="{foreign}"] [data-action="edit-visit"]').click()
                    m.locator('#visit-form [name="assignedTo"]').select_option('inspector')
                    m.locator('[data-action="save-visit"]').click();saved(m)
                    expect(foreign_row.locator('[data-action="edit-visit"]')).to_be_visible(timeout=8000)
                    foreign_row.locator('[data-action="start-audit"]').click()
                    for button in i.locator('button[data-answer][data-value="ok"]').all():button.click()
                    i.locator('[data-action="save-audit"]').click();saved(i)
                    nav(i,'schedule')
                    expect(foreign_row).to_contain_text('Выполнен')
                    expect(foreign_row.locator('[data-action="edit-visit"],[data-action="cancel-visit"]')).to_have_count(0)
                    expect(m.locator(f'[data-visit-id="{foreign}"]')).to_contain_text('Выполнен',timeout=8000)
                    expect(m.locator(f'[data-visit-id="{foreign}"] [data-action="edit-visit"]')).to_have_count(0)
                    assert not errors,errors
                    browser.close()
                print('PASS: mobile inspector creates, moves, cancels with reason; manager sees audit/versions; foreign/terminal actions hidden; reassignment and completion preserved')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-4000:]);raise
            finally:
                process.terminate();process.wait(timeout=10)


if __name__=='__main__':run()
