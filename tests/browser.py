"""Real HTTP and cookies, independent desktop/mobile contexts; synthetic data only."""
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
from playwright.sync_api import sync_playwright, expect
from PIL import Image
from server.database import Database, ROOT, seed_demo
from .test_api import MANAGER_PASSWORD, INSPECTOR_PASSWORD


def login(page,url,user,password):
    page.goto(url);page.locator('#username').fill(user);page.locator('#password').fill(password)
    page.get_by_role('button',name='Войти',exact=True).click();expect(page.locator('.workspace')).to_be_visible()


def wait_js(page, predicate):
    for _ in range(150):
        if page.evaluate('()=>('+predicate+')'): return
        page.wait_for_timeout(100)
    raise AssertionError('Condition not met: '+predicate+'; '+page.locator('body').inner_text()[-1500:])


def saved(page):
    wait_js(page, 'pending === null && !sending')


def nav(page,name):
    if page.viewport_size['width']<=760 and not page.locator('.sidebar').evaluate("x=>x.classList.contains('open')"):page.locator('[data-action="mobile-toggle"]').click()
    page.locator(f'[data-nav="{name}"]').first.click()


def run(full=False):
    with tempfile.TemporaryDirectory(prefix='ksu-browser-') as tmp:
        db_path=Path(tmp)/'demo.sqlite3';seed_demo(Database(db_path),MANAGER_PASSWORD,INSPECTOR_PASSWORD)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}'
        environment={**os.environ,'KSU_DB':str(db_path),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
        log=open(Path(tmp)/'server.log','w+')
        server=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],cwd=ROOT,env=environment,stdout=log,stderr=log)
        try:
            opener=build_opener(ProxyHandler({}))
            for _ in range(100):
                try:
                    if opener.open(origin+'/api/health',timeout=.2).status==200:break
                except OSError:time.sleep(.1)
            else:raise AssertionError('API did not start')
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
                manager=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU',accept_downloads=True)
                inspector=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU',accept_downloads=True)
                m=manager.new_page();i=inspector.new_page();errors=[]
                for page in (m,i):page.on('pageerror',lambda error:errors.append(str(error)))
                login(m,origin,'manager',MANAGER_PASSWORD);login(i,origin,'inspector',INSPECTOR_PASSWORD)
                assert m.locator('[data-role]').count()==0
                for name in ['sites','audits','schedule','findings','inventory','reports','settings']:
                    nav(m,name);expect(m.locator('.page-heading h2')).to_be_visible()
                    nav(i,name);expect(i.locator('.page-heading h2')).to_be_visible()
                    assert i.evaluate('document.documentElement.scrollWidth')==390,name
                m.screenshot(path='/tmp/ksu_desktop.png',full_page=True);i.screenshot(path='/tmp/ksu_mobile.png',full_page=True)
                print('PASS: login, 7 sections on desktop/mobile, no horizontal overflow',flush=True)
                nav(m,'schedule');m.locator('[data-action="add-visit"]').click()
                form=m.locator('#visit-form');form.locator('[name="siteId"]').select_option('s1')
                form.locator('[name="goal"]').fill('ДЕМО · Контроль маркировки')
                m.locator('[data-action="save-visit"]').click();saved(m)
                nav(i,'schedule');expect(i.get_by_text('ДЕМО · Контроль маркировки')).to_be_visible(timeout=8000)
                assert i.locator('[data-action="add-visit"]').count()==0
                i.locator('[data-action="start-audit"][data-visit]').click()
                assert i.locator('.question-card').count()==22
                for button in i.locator('button[data-answer][data-value="ok"]').all():button.click()
                if full:
                    i.locator('[data-answer="q1"][data-value="fail"]').click()
                    i.locator('[data-q="q1"][data-answer-field="comment"]').fill('ДЕМО · Нет маркировки')
                    image=Image.new('RGB',(20,20),'blue');image_bytes=io.BytesIO();image.save(image_bytes,format='PNG')
                    i.locator('[data-photo-q="q1"]').set_input_files({'name':'demo.png','mimeType':'image/png','buffer':image_bytes.getvalue()})
                    wait_js(i,'ui.draft.answers.q1.photo.startsWith("data:")')
                    i.wait_for_timeout(1200);i.reload()
                    expect(i.locator('.question-card')).to_have_count(22)
                    expect(i.locator('[data-q="q1"][data-answer-field="comment"]')).to_have_value('ДЕМО · Нет маркировки')
                    print('PASS: mobile checklist draft survives reload',flush=True)
                i.locator('[data-action="save-audit"]').click();saved(i)
                nav(m,'schedule');expect(m.locator('.pill').filter(has_text='Выполнен')).to_be_visible(timeout=8000)
                m.locator('[data-action="audit-detail"]').click();expect(m.get_by_role('heading',name='Акт проверки',exact=True)).to_be_visible()
                m.get_by_role('button',name='Закрыть',exact=True).first.click()
                print('PASS: manager assigns visit, mobile inspector completes checklist, shared act',flush=True)
                if full:
                    nav(i,'findings');i.locator('[data-action="finding-detail"]').first.click()
                    f=i.locator('#finding-form');f.locator('[name="status"]').select_option('closed');f.locator('[name="resolution"]').fill('ДЕМО · Маркировка восстановлена')
                    i.locator('[data-finding-upload="evidence"]').set_input_files({'name':'demo-evidence.png','mimeType':'image/png','buffer':image_bytes.getvalue()})
                    wait_js(i,'document.querySelector("#finding-form").elements.evidence.value.startsWith("/api/media/")')
                    i.locator('[data-action="save-finding"]').click();saved(i)
                    nav(m,'findings');expect(m.locator('.pill').filter(has_text='Устранено')).to_be_visible(timeout=8000)
                    m.locator('[data-action="finding-detail"]').first.click();m.locator('[data-action="history"]').click()
                    expect(m.get_by_text('ДЕМО · Эльман · Изменение · findings')).to_be_visible();m.get_by_role('button',name='Закрыть',exact=True).first.click()
                    print('PASS: closure with private evidence, shared status and actor history',flush=True)
                nav(i,'inventory');i.locator('[data-action="add-inventory"]').click()
                i.locator('[data-inv-cell="item"]').fill('ДЕМО · Перфоратор');i.locator('[data-inv-cell="expected"]').fill('2')
                i.locator('[data-inv-cell="actual"]').fill('1');i.locator('[data-inv-cell="price"]').fill('1500')
                i.locator('[data-action="save-inventory"]').click();saved(i)
                nav(m,'reports')
                for action,extension in [('export-report','.csv'),('report-xlsx','.xlsx'),('report-pdf','.pdf'),('weekly-export','.csv')]:
                    with m.expect_download() as event:m.locator(f'[data-action="{action}"]').click()
                    download=event.value;assert download.suggested_filename.endswith(extension);assert not download.failure()
                print('PASS: CSV/XLSX/PDF and weekly CSV downloads',flush=True)
                if full:
                    nav(i,'findings');i.locator('[data-action="add-finding"]').click()
                    i.locator('#finding-form [name="title"]').fill('ДЕМО · Повтор отправки')
                    inspector.route('**/api/changes',lambda route:route.abort())
                    i.locator('[data-action="save-finding"]').click()
                    expect(i.get_by_text('Изменения ещё не подтверждены сервером',exact=True)).to_be_visible()
                    i.reload();expect(i.locator('[data-action="retry-save"]')).to_be_visible()
                    inspector.unroute('**/api/changes');i.locator('[data-action="retry-save"]').click();saved(i)
                    assert i.evaluate('db.findings.filter(x=>x.title==="ДЕМО · Повтор отправки").length')==1
                    print('PASS: offline save survives reload, idempotent retry creates one record',flush=True)
                assert not errors,errors
                print('PASS: zero browser JavaScript errors',flush=True)
                browser.close()
        except Exception:
            log.flush();log.seek(0);print(log.read()[-5000:]);raise
        finally:
            server.terminate();server.wait(timeout=10);log.close()
