"""Synthetic two-session, mobile manager acceptance of explicit XLSX corrections."""
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener,ProxyHandler
from openpyxl import Workbook
from playwright.sync_api import sync_playwright,expect
from server.database import Database,ROOT,seed_demo
from server.decision_import import FIELDS
from tests.pg_environment import prepare_browser_database
from visits_flow_test import login,nav,MANAGER_PASSWORD,INSPECTOR_PASSWORD


def workbook(cost):
    book=Workbook();sheet=book.active;sheet.title='DEMO versions';sheet.append(list(FIELDS.values()))
    row={'sourceId':'DEMO-A','date':'2026-11-01','site':'ДЕМО · Нова','document':'ДЕМО · Исправляемый акт',
         'decisionType':'ДЕМО · Списание','toolType':'ДЕМО · ТМЦ','quantity':'1','cost':cost,'residual':'0',
         'reason':'ДЕМО','caseCategory':'ДЕМО','investigation':'ДЕМО','decision':'ДЕМО','reimbursable':'0',
         'reimbursed':'0','responsible':'ДЕМО','comment':'ДЕМО · <img src=x onerror=alert(1)>'}
    sheet.append([row[k] for k in FIELDS]);output=io.BytesIO();book.save(output);return output.getvalue()


def upload(page,data):
    page.locator('[data-action="decision-import"]').click()
    page.locator('#decision-file').set_input_files({'name':'DEMO-versions.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':data})
    page.locator('[data-action="decision-preview"]').click()
    expect(page.locator('#decision-progress')).to_contain_text('Проверка пройдена',timeout=20000)


def run():
    with tempfile.TemporaryDirectory(prefix='ksu-versions-ui-') as folder:
        path=Path(folder)/'synthetic.sqlite3';seed_demo(Database(path),MANAGER_PASSWORD,INSPECTOR_PASSWORD);prepare_browser_database(path)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}';env={**os.environ,'KSU_DB':str(path),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
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
                    manager=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU',accept_downloads=True)
                    inspector=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU',accept_downloads=True)
                    m=manager.new_page();i=inspector.new_page();errors=[]
                    for page in (m,i):page.on('pageerror',lambda error:errors.append(str(error)))
                    login(i,origin,'inspector',INSPECTOR_PASSWORD);login(m,origin,'manager',MANAGER_PASSWORD)
                    nav(i,'decisions');upload(i,workbook('10'));expect(i.locator('#decision-version-controls')).to_have_count(0)
                    i.locator('[data-action="decision-commit"]').click();expect(i.locator('#decision-progress')).to_contain_text('Импорт завершён',timeout=15000)
                    i.get_by_role('button',name='Закрыть',exact=True).first.click()
                    nav(m,'decisions');upload(m,workbook('12'))
                    m.locator('#decision-version-mode').select_option('full')
                    expect(m.locator('#version-document option')).to_have_count(2)
                    doc=m.locator('#version-document option').nth(1).get_attribute('value');m.locator('#version-document').select_option(doc)
                    m.locator('#version-reason').fill('ДЕМО · Исправлена сумма с 10 на 12')
                    m.locator('[data-action="version-compare"]').click()
                    expect(m.locator('#version-summary')).to_contain_text('Изменено: 1',timeout=15000)
                    expect(m.locator('#version-comparison')).to_contain_text('10 → 12',timeout=15000)
                    m.screenshot(path='/tmp/ksu_versions_mobile.png',full_page=True)
                    assert m.evaluate('document.documentElement.scrollWidth')==390
                    m.locator('[data-action="decision-commit"]').click();expect(m.locator('#decision-progress')).to_contain_text('Импорт завершён',timeout=15000)
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    expect(m.locator('.workspace')).to_contain_text('Всего записей: 1',timeout=10000)
                    expect(m.locator('.decision-totals')).to_contain_text('Стоимость, ₽: 12')
                    expect(i.locator('.decision-totals')).to_contain_text('Стоимость, ₽: 12',timeout=10000)
                    m.locator('[data-action="decision-versions"]').first.click()
                    expect(m.locator('.timeline-item')).to_have_count(2)
                    m.screenshot(path='/tmp/ksu_versions_history.png',full_page=True)
                    m.locator('[data-action="decision-show-version"][data-version="1"]').click()
                    expect(m.locator('.decision-totals')).to_contain_text('Стоимость, ₽: 10')
                    m.locator('[data-action="decision-current"]').click()
                    expect(m.locator('.decision-totals')).to_contain_text('Стоимость, ₽: 12')
                    with m.expect_download() as downloaded:m.locator('a[href^="/api/decisions/export.csv"]').click()
                    assert not downloaded.value.failure()
                    upload(m,workbook('14.0001'));m.locator('#decision-version-mode').select_option('partial');m.locator('#version-document').select_option(doc)
                    expect(m.locator('[data-version-old]')).to_have_count(1);m.locator('[data-version-old]').check()
                    m.locator('#version-reason').fill('ДЕМО · Частичная коррекция выбранной строки')
                    m.locator('[data-action="version-compare"]').click();expect(m.locator('#version-summary')).to_contain_text('14.0001',timeout=15000)
                    m.locator('[data-action="decision-commit"]').click();expect(m.locator('#decision-progress')).to_contain_text('Импорт завершён',timeout=15000)
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    expect(m.locator('.decision-totals')).to_contain_text('14.0001',timeout=10000)
                    assert not errors,errors;browser.close()
                print('PASS: mobile manager full/partial explicit correction, 10 to 12, diff and exact totals, inspector scope, history, current CSV and preserved source text')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-3500:]);raise
            finally:process.terminate();process.wait(timeout=15)


if __name__=='__main__':run()
