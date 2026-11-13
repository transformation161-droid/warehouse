"""Real PostgreSQL + integrated UI: three invented quarterly reports and actual screenshots."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
from decimal import Decimal
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
from tests.pg_environment import unused_port
from tests.writeoff_environment import pilot
from tests import test_writeoff_pilot as api
from tools.writeoff_demo.fixture import workbook,quarter_rows


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);port=unused_port();origin=f'http://127.0.0.1:{port}';errors=[]
    with pilot(origin) as (app,accounts,pg):
        qa=api.Pilot();qa.app=app;qa.alias={'ДЕМО · Север':'s1','ДЕМО · Юг':'s2'}
        with TestClient(app,base_url=origin,headers={'Origin':origin}) as client:
            login=client.post('/api/login',json={'username':'manager','password':accounts['manager']['password']});assert login.status_code==200
            client.headers['X-CSRF-Token']=login.json()['csrf'];qa.manager=client
            qa.imported()
            for period in ('2026-Q1','2026-Q3'):qa.imported(workbook(quarter_rows(period)),reportPeriod=period)
            source=qa.panel();assert source['totalRows']==48
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False));thread=threading.Thread(target=server.run,daemon=True);thread.start()
            for _ in range(100):
                if server.started:break
                time.sleep(.1)
            else:raise AssertionError('Synthetic UI did not start')
            try:
                with sync_playwright() as p:
                    opts={'headless':True}
                    if os.getenv('CHROMIUM_EXECUTABLE'):opts['executable_path']=os.environ['CHROMIUM_EXECUTABLE']
                    browser=p.chromium.launch(**opts);ctx=browser.new_context(viewport={'width':1440,'height':1080},accept_downloads=True);page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    def login(role):
                        page.goto(origin);page.locator('input[name=username]').fill(role);page.locator('input[name=password]').fill(accounts[role]['password']);page.locator('button[type=submit]').click();expect(page.locator('[data-nav=reports]')).to_be_visible()
                    def ready():expect(page.locator('#wo-charts [data-wo-chart=trend] svg')).to_be_visible(timeout=15000)
                    login('manager');page.locator('[data-nav=reports]').click();ready()
                    expect(page.locator('#wo-charts svg')).to_have_count(3)
                    assert page.locator('#wo-charts').evaluate('e=>e.getBoundingClientRect().top+scrollY')<700,'Graphs must be above the long matrix'
                    assert page.locator('#wo-charts').evaluate('e=>e.compareDocumentPosition(document.querySelector("#wo-registry"))&Node.DOCUMENT_POSITION_FOLLOWING')
                    expect(page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]')).to_have_count(1)
                    expect(page.locator('[data-wo-series=s2] [data-wo-chart-state=missing]')).to_have_count(1)
                    expect(page.locator('[data-wo-series=s2] [data-wo-chart-state=zero]')).to_have_count(1)
                    assert len(page.locator('[data-wo-series=s2] path').get_attribute('d').split('M'))==2
                    page.screenshot(path=str(output/'dashboard-top.png'))
                    page.locator('[data-wo-chart=structure]').screenshot(path=str(output/'structure-reasons.png'))
                    page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]').hover();expect(page.locator('.wo-chart-tooltip')).to_be_visible();expect(page.locator('.wo-chart-tooltip')).to_contain_text('II кв. 2026')
                    for top in ('10','0','5'):page.locator('[data-wo-chart-top]').select_option(top);expect(page.locator('[data-wo-series]')).to_have_count(2)
                    page.locator('[data-wo-chart-highlight]').select_option('s1');expect(page.locator('[data-wo-series=s2]')).to_have_attribute('opacity','0.18');page.locator('[data-wo-chart-highlight]').select_option('')
                    page.locator('.wo-site-picker summary').click();page.locator('[data-wo-chart-site=s2]').uncheck();expect(page.locator('[data-wo-series]')).to_have_count(1);expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('48 действующих')
                    page.locator('.wo-site-picker summary').click();page.locator('[data-wo-chart-site=s2]').check();expect(page.locator('[data-wo-series]')).to_have_count(2)
                    for metric in ('quantity','residual','reimbursable','reimbursed','cost'):
                        with page.expect_response(lambda r:'/api/writeoff-analytics?' in r.url and 'chartMetric='+metric in r.url):page.locator('[data-wo-metric]').select_option(metric)
                        ready();expected=qa.panel(chartMetric=metric)['charts']['periods']['items'][1]['metrics'][metric]['value']
                        expect(page.locator(f'[data-wo-chart=trend] [data-wo-chart-value="{expected}"]')).to_have_count(1)
                    page.locator('[data-wo-chart-structure=tools]').click();expect(page.locator('[data-wo-chart=structure] svg')).to_have_attribute('aria-label','Диаграмма типов ТМЦ');page.locator('[data-wo-chart=structure]').screenshot(path=str(output/'structure-tools.png'))
                    page.locator('[data-wo-chart-structure=reasons]').click();reason=page.locator('.wo-chart-bar').first.get_attribute('data-wo-value');page.locator('.wo-chart-bar').first.click();ready()
                    expect(page.locator('[data-wo-filter=reason]')).to_have_value(reason);assert '48 действующих' not in page.locator('#wo-registry .card-subtitle').text_content()
                    with page.expect_download() as dl:page.locator('#wo-registry [data-wo-ext=csv]').click()
                    assert reason in Path(dl.value.path()).read_text(encoding='utf-8-sig')
                    page.locator('[data-wo-action=clear]').click();ready()
                    page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]').click();ready();expect(page.locator('[data-wo-filter=reportPeriod]')).to_have_value('2026-Q2');expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих')
                    page.locator('[data-wo-action=clear]').click();ready()
                    page.locator('[data-wo-chart=comparison] .wo-chart-legend [data-wo-site=s1]').click();ready();expect(page.locator('[data-wo-chart=comparison]')).to_have_count(0);expect(page.locator('#wo-charts')).to_contain_text('Тренд объекта');expect(page.locator('#wo-charts svg')).to_have_count(2)
                    assert page.locator('#wo-charts').evaluate('e=>e.compareDocumentPosition([...document.querySelectorAll(".card-title")].find(t=>t.textContent==="Ответственные"))&Node.DOCUMENT_POSITION_FOLLOWING')
                    page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(output/'site-top.png'));page.locator('#wo-charts').screenshot(path=str(output/'site-charts.png'))
                    page.locator('[data-nav=reports]').click();ready();page.set_viewport_size({'width':412,'height':915});page.wait_for_timeout(400);ready();page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(output/'mobile-top.png'))
                    page.evaluate('window.scrollTo(0,document.querySelector("#wo-charts").getBoundingClientRect().top+scrollY-85)');page.screenshot(path=str(output/'mobile-charts.png'))
                    page.evaluate('window.scrollTo(0,document.querySelector("[data-wo-chart=comparison]").getBoundingClientRect().top+scrollY-85)');page.screenshot(path=str(output/'mobile-comparison.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'),'No horizontal page overflow'
                    # Touch-sized viewport: the time axis for three quarters fits the chart itself.
                    trend=page.locator('[data-wo-chart=trend] .wo-plot-scroll');assert trend.evaluate('e=>e.scrollWidth<=e.clientWidth+2'),'Mobile three-quarter trend must fit'
                    page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]').click();ready();expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих')
                    page.set_viewport_size({'width':1440,'height':1080});page.locator('[data-action=logout]').click();login('inspector');page.locator('[data-nav=reports]').click();ready();expect(page.locator('[data-wo-series]')).to_have_count(1);expect(page.locator('[data-wo-series]')).to_have_attribute('data-wo-series','s1')
                    expect(page.locator('[data-wo-chart-site]')).to_have_count(1);assert 'Склад Юг' not in page.locator('#wo-charts').inner_text()
                    page.locator('[data-action=logout]').click();login('viewer');page.locator('[data-nav=reports]').click();expect(page.locator('#wo-charts')).to_have_count(0)
                    assert not errors,errors;browser.close()
                    result={'passed':True,'postgresql17':True,'source':'three invented detailed quarterly reports','quarter_reports':['2026-Q1','2026-Q2','2026-Q3'],'missing_vs_zero':True,'top_5_10_all':True,'metric_switch_and_drilldown':True,'shared_filter_export':True,'desktop_mobile_site_screenshots':True,'role_acl':True,'browser_errors':errors}
                    (output/'charts-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
            finally:server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
