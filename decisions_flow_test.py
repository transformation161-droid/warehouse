"""Two-browser acceptance of the new profile; synthetic XLSX only."""
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from openpyxl import Workbook
from playwright.sync_api import sync_playwright, expect
from server.database import Database, ROOT, seed_demo
from server.decision_import import FIELDS
from tests.test_api import MANAGER_PASSWORD, INSPECTOR_PASSWORD
from tests.browser import login, nav


def run():
    book=Workbook();sheet=book.active;sheet.title='DEMO decisions';sheet.append(list(FIELDS.values()))
    for ident in ('DEMO-1','DEMO-2'):
        row={'sourceId':ident,'date':'2026-11-01','site':'ДЕМО · Нова','document':'ДЕМО · Акт',
          'decisionType':'ДЕМО · Списание','toolType':'ДЕМО · Инструмент','quantity':2,'cost':'100.50',
          'residual':'80.25','reason':'ДЕМО · Проверяется','caseCategory':'ДЕМО','investigation':'ДЕМО · В работе',
          'decision':'ДЕМО · Решение','reimbursable':'25.50','reimbursed':'10','responsible':'ДЕМО · МОЛ','comment':'ДЕМО · <b>обычный текст</b>'}
        sheet.append([row[k] for k in FIELDS])
    out=io.BytesIO();book.save(out)
    with tempfile.TemporaryDirectory(prefix='ksu-decisions-ui-') as folder:
        database=Path(folder)/'synthetic.sqlite3';seed_demo(Database(database),MANAGER_PASSWORD,INSPECTOR_PASSWORD)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}'
        env={**os.environ,'KSU_DB':str(database),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
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
                    manager=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU',accept_downloads=True)
                    inspector=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU',accept_downloads=True)
                    m=manager.new_page();i=inspector.new_page();errors=[]
                    for page in (m,i):page.on('pageerror',lambda error:errors.append(str(error)))
                    login(i,origin,'inspector',INSPECTOR_PASSWORD);login(m,origin,'manager',MANAGER_PASSWORD)
                    nav(i,'decisions')
                    i.locator('[data-action="decision-import"]').click()
                    i.locator('#decision-file').set_input_files({'name':'DEMO.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':out.getvalue()})
                    i.locator('[data-action="decision-preview"]').click()
                    expect(i.locator('#decision-progress')).to_contain_text('Проверка пройдена',timeout=15000)
                    expect(i.locator('#decision-mapping select')).to_have_count(17)
                    # The dialog can be closed/reopened without abandoning the owned staged file.
                    # A delayed poll from a closed dialog must not overwrite a newly opened preview.
                    i.evaluate("""() => {
                      window.originalDecisionApi=api;
                      const ident=decisionJob, meta=structuredClone(decisionMeta);
                      const delayed=new Promise(resolve=>window.releaseOldDecisionPoll=()=>resolve({...meta,status:'done',committed:999}));
                      api=(path,options)=>path==='decision-imports/'+ident?delayed:window.originalDecisionApi(path,options);
                      window.oldDecisionResult=null;
                      waitDecision().then(()=>window.oldDecisionResult='unexpected').catch(error=>window.oldDecisionResult=error.name);
                    }""")
                    i.get_by_role('button',name='Закрыть',exact=True).first.click()
                    i.locator('[data-action="decision-import"]').click()
                    expect(i.locator('#decision-progress')).to_contain_text('Проверка пройдена',timeout=15000)
                    expect(i.locator('#decision-file')).to_be_disabled()
                    i.evaluate('() => { api=window.originalDecisionApi; window.releaseOldDecisionPoll(); }')
                    i.wait_for_function('window.oldDecisionResult!==null')
                    assert i.evaluate('window.oldDecisionResult')=='AbortError'
                    expect(i.locator('#decision-progress')).to_contain_text('Проверка пройдена')
                    i.locator('[data-action="decision-commit"]').click()
                    expect(i.locator('#decision-progress')).to_contain_text('Импорт завершён',timeout=15000)
                    i.get_by_role('button',name='Закрыть',exact=True).first.click()
                    expect(i.locator('.workspace')).to_contain_text('Всего записей: 2',timeout=8000)
                    expect(i.locator('.workspace table')).to_contain_text('100.50')
                    expect(i.locator('.workspace table')).to_contain_text('80.25')
                    expect(i.locator('.workspace table')).to_contain_text('25.50')
                    assert i.evaluate('document.documentElement.scrollWidth')==390
                    nav(m,'decisions');expect(m.locator('.workspace')).to_contain_text('Всего записей: 2',timeout=8000)
                    m.locator('[data-action="decision-detail"]').first.click()
                    expect(m.locator('.modal')).to_contain_text('<b>обычный текст</b>')
                    assert m.locator('.modal img').count()==0
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    m.locator('[data-action="history"]').first.click()
                    expect(m.locator('.timeline-item')).to_contain_text('ДЕМО · Эльман · Импорт · decisions')
                    m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    with m.expect_download() as download:m.locator('a[href^="/api/decisions/export.csv"]').click()
                    assert not download.value.failure()
                    # Same bytes and same source range are idempotent, including in the UI.
                    i.locator('[data-action="decision-import"]').click()
                    i.locator('#decision-file').set_input_files({'name':'DEMO.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':out.getvalue()})
                    i.locator('[data-action="decision-preview"]').click()
                    expect(i.locator('#decision-progress')).to_contain_text('Проверка пройдена',timeout=15000)
                    i.locator('[data-action="decision-commit"]').click()
                    expect(i.locator('#decision-progress')).to_contain_text('дубли не созданы',timeout=15000)
                    assert not errors,errors
                    browser.close()
                print('PASS: mobile streamed import, mapping/preview, dialog resume, exact separate amounts, manager view/history/CSV, escaped source text and repeat without duplicates')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-4000:]);raise
            finally:
                process.terminate();process.wait(timeout=15)


if __name__=='__main__':run()
