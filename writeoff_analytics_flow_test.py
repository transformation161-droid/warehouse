"""Synthetic desktop/mobile analytics, shared filters, exact downloads and scoped users."""
import csv
from decimal import Decimal
import io
import os
import json
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener,ProxyHandler
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright,expect
from server.database import ROOT
from tests import test_writeoff_analytics as contracts
from tests import test_api
from tests.analytics_fixture import insert,records,oracle
from tests.pg_environment import prepare_browser_database
from visits_flow_test import login,nav


def ready(page):expect(page.locator('.wa-status')).to_contain_text('Действующих строк:',timeout=20000)


def run():
    case=contracts.Analytics();case.setUp()
    try:
        old=case.initial()[0];ident,meta=case.correction([case.row(cost='12',residual='5')],old['documentId'])
        assert case.confirm(ident,meta)['status']=='done'
        rows=insert(case.app.state.db,records(64))
        # Independent oracle is also asserted in the real browser's filtered slice.
        selected=[r for r in rows if r['_site']=='s2'];totals,_=oracle(selected)
        with case.app.state.db.connect() as con:con.execute("delete from user_sites where user_id='inspector' and site_id<>'s1'")
        prepare_browser_database(case.app.state.db.path)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://localhost:{port}';env={**os.environ,'KSU_DB':str(case.app.state.db.path),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0'}
        with (Path(case.temp.name)/'browser.log').open('w+') as log:
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
                    manager=browser.new_context(viewport={'width':1440,'height':1000},locale='ru-RU',accept_downloads=True)
                    inspector=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU',accept_downloads=True)
                    m=manager.new_page();i=inspector.new_page();errors=[]
                    for page in (m,i):page.on('pageerror',lambda error:errors.append(str(error)))
                    login(m,origin,'manager',test_api.MANAGER_PASSWORD);login(i,origin,'inspector',test_api.INSPECTOR_PASSWORD)
                    nav(m,'writeoff-analytics');ready(m)
                    expect(m.locator('[data-wa-kpi]')).to_have_count(8);expect(m.locator('[data-wa-chart]')).to_have_count(6)
                    expect(m.locator('.wa-provisional')).to_contain_text('Не официальная отчётность')
                    expect(m.locator('#wa-start')).to_be_visible();expect(m.locator('#wa-site')).to_be_visible()
                    expect(m.locator('[data-wa-kpi="quantity"]')).to_contain_text('Единицы неоднородны')
                    expect(m.locator('[data-wa-kpi="quantity"] .wa-quantity')).to_have_attribute('aria-label','104,000013')
                    expect(m.locator('[data-wa-kpi="quantity"] .wa-fraction')).to_have_text(',000\u202f013')
                    ui_samples=m.evaluate('''async()=>{const values=[];for(let i=0;i<10;i++){const start=performance.now();render();const html=performance.now()-start;await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));values.push({html_ms:html,through_two_frames_ms:performance.now()-start})}return values}''')
                    Path('/tmp/ksu_prerelease_ui.json').write_text(json.dumps({'synthetic_only':True,'backend':'postgresql' if os.getenv('KSU_DATABASE_URL') else 'sqlite','visible_rows':25,'fixture_active_rows':65,'samples':ui_samples},indent=2)+'\n')
                    m.screenshot(path='/tmp/ksu_analytics_desktop.png',full_page=True)
                    m.locator('.wa-filter-panel').evaluate('(element)=>element.open=false')
                    m.set_viewport_size({'width':1440,'height':1400})
                    m.screenshot(path='/tmp/ksu_analytics_desktop_overview.png',animations='disabled')
                    m.set_viewport_size({'width':1440,'height':1000})
                    m.locator('.wa-filter-panel summary').click()
                    m.locator('#wa-site').select_option('s2');m.locator('#wa-filters button[type="submit"]').click();ready(m)
                    expect(m.locator('[data-wa-kpi="decisions"] .wa-kpi-value')).to_contain_text(str(len(selected)))
                    assert Decimal(m.evaluate('waView.data.metrics.cost.value'))==Decimal(totals['cost'])
                    expect(m.locator('[data-wa-chart="sites"] [data-wa-group]')).to_have_count(1)
                    assert set(m.locator('[data-wa-row]').evaluate_all('(rows)=>rows.map(r=>r.children[1].textContent)'))=={'ДЕМО · Дом Горизонтов'}
                    for spec in ('rows.csv','rows.xlsx','summary.csv','summary.xlsx'):
                        with m.expect_download() as download:m.locator(f'[data-wa-export="{spec}"]').click()
                        item=download.value;assert not item.failure();assert item.suggested_filename.endswith('.'+spec.split('.')[1])
                        raw=Path(item.path()).read_bytes()
                        if spec=='rows.csv':assert len(list(csv.reader(io.StringIO(raw.decode('utf-8-sig')),delimiter=';')))-1==len(selected)
                        if spec=='rows.xlsx':
                            book=load_workbook(io.BytesIO(raw),read_only=True);assert len(list(book.worksheets[0].values))-1==len(selected);book.close()
                    # One continuous user path: period/object -> chart -> source -> current composition.
                    m.locator('#wa-start').fill('2026-02-01');m.locator('#wa-end').fill('2026-04-30')
                    m.locator('#wa-filters button[type="submit"]').click();ready(m)
                    period_rows=[r for r in selected if r['date'] and '2026-02-01'<=r['date']<='2026-04-30']
                    assert m.evaluate('waView.data.totalRows')==len(period_rows)
                    m.locator('[data-wa-group="months"]').first.click();ready(m)
                    m.locator('[data-action="decision-detail"]:visible').first.click();expect(m.get_by_role('dialog')).to_contain_text('ДЕМО')
                    m.locator('[data-action="close-modal"]').first.click();m.locator('[data-wa-versions]:visible').first.click()
                    m.locator('[data-wa-show-version][data-wa-current="true"]').click()
                    expect(m.locator('.decision-totals')).to_contain_text('Действующие итоги');assert m.evaluate('decisionView.version')==0
                    nav(m,'writeoff-analytics');ready(m)
                    m.locator('[data-wa-clear="month"]').click();ready(m);assert not m.evaluate('waView.filters.month')
                    m.locator('[data-wa-clear="period"]').click();ready(m);assert m.evaluate('waView.data.totalRows')==len(selected)
                    m.locator('[data-wa-action="reset"]').click();ready(m)
                    m.locator('[data-wa-action="fractional"]').click();ready(m)
                    assert m.evaluate('waView.data.totalRows')==13
                    assert all(Decimal(r['quantity'])%1 for r in m.evaluate('waView.data.rows'))
                    m.locator('[data-wa-clear="metric"]').click();ready(m)
                    m.locator('[data-wa-action="reports"]').click()
                    expect(m.locator('.workspace')).to_contain_text('Проверки, нарушения и первичные расхождения')
                    expect(m.locator('.nav-item[data-nav="reports"]')).to_have_text('Операционные отчёты')
                    nav(m,'writeoff-analytics');ready(m)
                    m.locator('[data-wa-group="months"]').first.click();ready(m);assert m.evaluate('waView.filters.month')
                    m.locator('[data-wa-action="reset"]').click();ready(m)
                    m.locator('[data-wa-missing-metric="residual"]').click();ready(m)
                    assert all(r['residual'] is None for r in m.evaluate('waView.data.rows'))
                    m.locator('[data-wa-action="reset"]').click();ready(m)
                    # Drill to corrected document by explicitly known logical ID.
                    m.evaluate('(doc)=>waDrill({documentId:doc})',old['documentId']);ready(m)
                    expect(m.locator('[data-wa-kpi="cost"] .wa-kpi-value')).to_have_text('12 ₽')
                    m.locator('[data-action="decision-detail"]:visible').click();expect(m.get_by_role('dialog')).to_contain_text('DEMO-1')
                    m.locator('[data-action="close-modal"]').first.click()
                    m.locator('[data-wa-versions]:visible').click();expect(m.locator('.timeline-item')).to_have_count(2)
                    m.locator('[data-wa-show-version="1"]').click();expect(m.locator('.decision-totals')).to_contain_text('Стоимость, ₽: 10')
                    nav(m,'writeoff-analytics');ready(m);m.locator('[data-wa-action="reset"]').click();ready(m)
                    m.set_viewport_size({'width':390,'height':844});m.locator('.wa-filter-panel').evaluate('(element)=>element.open=false')
                    expect(m.locator('#wa-start')).to_be_visible();expect(m.locator('#wa-site')).to_be_visible()
                    m.screenshot(path='/tmp/ksu_analytics_mobile.png',full_page=True,animations='disabled')
                    m.locator('.wa-kpis').evaluate('(el)=>window.scrollTo(0,el.getBoundingClientRect().top+window.scrollY-80)')
                    m.screenshot(path='/tmp/ksu_analytics_mobile_overview.png',animations='disabled')
                    assert m.evaluate('document.documentElement.scrollWidth')==390
                    expect(m.locator('.wa-mobile-row')).to_have_count(25)
                    m.locator('.wa-mobile-row [data-action="decision-detail"]').first.click();expect(m.get_by_role('dialog')).to_be_visible()
                    m.locator('[data-action="close-modal"]').first.click()
                    m.set_viewport_size({'width':320,'height':740});assert m.evaluate('document.documentElement.scrollWidth')==320
                    nav(i,'writeoff-analytics');ready(i);expect(i.locator('#wa-site option')).to_have_count(2)
                    i.locator('.wa-filter-panel summary').click();i.locator('#wa-dateMode').select_option('missing')
                    i.locator('#wa-filters button[type="submit"]').click();ready(i)
                    assert all(not r['date'] for r in i.evaluate('waView.data.rows'))
                    i.evaluate("waView.filters={...waDefaults};waView.data=null;waLoad()");ready(i)
                    assert {p['key'] for p in i.evaluate('waView.data.charts.sites.items')}=={'s1'}
                    assert i.evaluate('waView.data.totalRows')==len([r for r in rows if r['_site']=='s1'])+1
                    # Client cannot broaden scope by constructing a request.
                    assert i.evaluate("async()=> (await (await fetch('/api/writeoff-analytics?site=s2')).json()).totalRows")==0
                    expect(i.locator('[data-wa-action="registry"]')).to_be_visible()
                    # A backend failure and an actual aborted fetch cannot leave an endless spinner.
                    pattern=re.compile(r'/api/writeoff-analytics\?')
                    m.route(pattern,lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"ДЕМО · Сервер временно недоступен"}'))
                    m.evaluate('()=>{waView.data=null;void waLoad()}')
                    expect(m.locator('.wa-status')).to_contain_text('Сервер временно недоступен')
                    expect(m.get_by_role('button',name='Повторить загрузку')).to_be_visible();m.unroute(pattern)
                    m.get_by_role('button',name='Повторить загрузку').click();ready(m)
                    m.clock.install();held=[];m.route(pattern,lambda route:held.append(route))
                    m.evaluate('()=>{waView.data=null;void waLoad()}');expect(m.locator('.wa-status')).to_contain_text('Загрузка')
                    m.clock.fast_forward(31000);expect(m.locator('.wa-status')).to_contain_text('не загрузился за 30 секунд')
                    assert m.evaluate('waView.loading') is False
                    for route in held:route.abort()
                    m.unroute(pattern)
                    assert not errors,errors;browser.close()
                print('PASS: desktop/390/320px, primary filters, period/object/chart/source/current/history, exact quantity/fractional review, four downloads, scoped rights, UI timing, failure/retry/30s timeout; no JS errors')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-2000:]);raise
            finally:process.terminate();process.wait(timeout=15)
    finally:case.tearDown()


if __name__=='__main__':run()
