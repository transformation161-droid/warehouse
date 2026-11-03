"""Synthetic desktop/mobile analytics, shared filters, exact downloads and scoped users."""
import csv
from decimal import Decimal
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
                    m.locator('[data-wa-action="reset"]').click();ready(m)
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
                    assert not errors,errors;browser.close()
                print('PASS: desktop/390/320px analytics, synchronized filters/KPIs/charts/rows, exact four downloads, current12/history10, scoped inspector, no JS errors')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-2000:]);raise
            finally:process.terminate();process.wait(timeout=15)
    finally:case.tearDown()


if __name__=='__main__':run()
