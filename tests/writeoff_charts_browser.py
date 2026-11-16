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
from tests.test_writeoff_reimbursements import FORBIDDEN


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
                    def independent_reimbursements():
                        text=page.locator('[data-wo-panel]').inner_text().casefold()
                        for term in FORBIDDEN:assert term.casefold() not in text,term
                        expect(page.locator('[data-wo-recovery] [data-wo-sum]')).to_have_count(2)
                        expect(page.locator('#wo-registry th',has_text='К возмещению')).to_have_count(1)
                        expect(page.locator('#wo-registry th',has_text='Фактически возмещено')).to_have_count(1)
                    login('manager');page.locator('[data-nav=reports]').click();ready()
                    expect(page.locator('#wo-charts svg')).to_have_count(3)
                    assert page.locator('#wo-charts').evaluate('e=>e.getBoundingClientRect().top+scrollY')<700,'Graphs must be above the long matrix'
                    assert page.locator('#wo-charts').evaluate('e=>e.compareDocumentPosition(document.querySelector("#wo-registry"))&Node.DOCUMENT_POSITION_FOLLOWING')
                    expect(page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]')).to_have_count(1)
                    expect(page.locator('[data-wo-series=s2] [data-wo-chart-state=missing]')).to_have_count(1)
                    expect(page.locator('[data-wo-series=s2] [data-wo-chart-state=zero]')).to_have_count(1)
                    assert len(page.locator('[data-wo-series=s2] path').get_attribute('d').split('M'))==2
                    independent_reimbursements()
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
                        expect(page.locator(f'[data-wo-chart=trend] [data-wo-chart-value="{expected}"]')).to_have_count(1);independent_reimbursements()
                    page.locator('[data-wo-chart-structure=tools]').click();expect(page.locator('[data-wo-chart=structure] svg')).to_have_attribute('aria-label','Диаграмма типов ТМЦ');page.locator('[data-wo-chart=structure]').screenshot(path=str(output/'structure-tools.png'))
                    page.locator('[data-wo-chart-structure=reasons]').click();reason=page.locator('.wo-chart-bar').first.get_attribute('data-wo-value');page.locator('.wo-chart-bar').first.click();ready()
                    expect(page.locator('[data-wo-filter=reason]')).to_have_value(reason);assert '48 действующих' not in page.locator('#wo-registry .card-subtitle').text_content()
                    with page.expect_download() as dl:page.locator('#wo-registry [data-wo-ext=csv]').click()
                    assert reason in Path(dl.value.path()).read_text(encoding='utf-8-sig')
                    page.locator('[data-wo-action=clear]').click();ready()
                    page.locator('[data-wo-chart=trend] [data-wo-chart-value="356787.50"]').click();ready();expect(page.locator('[data-wo-filter=reportPeriod]')).to_have_value('2026-Q2');expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих')
                    independent_reimbursements()
                    amounts=page.locator('[data-wo-recovery] [data-wo-sum]').evaluate_all("els=>els.map(e=>e.textContent.replace(/\\s/g,'').replace('₽','').replace(',','.'))")
                    assert [Decimal(v) for v in amounts]==[Decimal('113827.87'),Decimal('116992.36')]
                    page.locator('[data-wo-recovery]').screenshot(path=str(output/'reimbursements.png'))
                    greater=next(r for r in source['rows'] if Decimal(r['reimbursed'])>Decimal(r['reimbursable']))
                    page.locator('#wo-registry [data-wo-id="'+greater['id']+'"]').click();expect(page.locator('.modal')).to_be_visible()
                    for term in FORBIDDEN:assert term.casefold() not in page.locator('.modal').inner_text().casefold(),term
                    page.locator('.modal [data-wo-action=close]').click()
                    page.locator('[data-wo-action=clear]').click();ready()
                    page.locator('[data-wo-chart=comparison] .wo-chart-legend [data-wo-site=s1]').click();ready();expect(page.locator('[data-wo-chart=comparison]')).to_have_count(0);expect(page.locator('#wo-charts')).to_contain_text('Тренд объекта');expect(page.locator('#wo-charts svg')).to_have_count(2);independent_reimbursements()
                    assert page.locator('#wo-charts').evaluate('e=>e.compareDocumentPosition([...document.querySelectorAll(".card-title")].find(t=>t.textContent==="Ответственные"))&Node.DOCUMENT_POSITION_FOLLOWING')
                    expect(page.locator('[data-wo-statement]')).to_be_visible()
                    expect(page.locator('.wo-source-table thead th')).to_have_count(18)
                    expect(page.locator('.wo-source-table tbody tr')).to_have_count(26)
                    expect(page.locator('.wo-source-table [data-wo-column=comment]').filter(has_text='позиция 1 —')).to_have_count(1)
                    page.locator('[data-wo-action=statement]').click();page.wait_for_timeout(400)
                    page.screenshot(path=str(output/'site-statement-prices.png'))
                    task=page.locator('.wo-source-table .wo-task-link').first;href=task.get_attribute('href')
                    page.locator('.wo-source-scroll').evaluate("e=>{const c=e.querySelector('td[data-wo-column=decision]');e.scrollLeft=c.offsetLeft-210}")
                    page.screenshot(path=str(output/'site-statement-tasks.png'))
                    requests=[]
                    def fake_task(route):
                        requests.append(route.request.headers);route.fulfill(status=200,content_type='text/html',body='<title>DEMO task</title><p>DEMO synthetic task only</p>')
                    ctx.route('https://bitrix.demo.invalid/**',fake_task)
                    with page.expect_popup() as popup_event:task.click()
                    popup=popup_event.value;popup.wait_for_load_state();assert popup.url==href;assert popup.evaluate('window.opener===null');popup.close()
                    assert requests and all('referer' not in r for r in requests)
                    page.locator('.wo-source-table [data-wo-action=detail]').first.click();expect(page.locator('.modal')).to_contain_text('История версий документа');page.locator('.modal [data-wo-action=close]').click()
                    page.locator('[data-wo-filter-details] summary').click();page.locator('[data-wo-filter=q]').fill('позиция 1 —');page.locator('[data-wo-filter=q]').press('Tab');ready()
                    expect(page.locator('.wo-source-table tbody tr')).to_have_count(1)
                    page.locator('[data-wo-action=clear]').click();ready();expect(page.locator('.wo-source-table tbody tr')).to_have_count(26)
                    page.set_viewport_size({'width':412,'height':915});page.wait_for_timeout(400);ready()
                    expect(page.locator('.wo-source-scroll')).not_to_be_visible();expect(page.locator('.wo-source-cards')).to_be_visible()
                    expect(page.locator('.wo-source-card')).to_have_count(26);expect(page.locator('.wo-source-card').first.locator('dl>div')).to_have_count(17)
                    expect(page.locator('.wo-source-cards .wo-task-link').first).to_be_visible()
                    page.evaluate('window.scrollTo(0,document.querySelector("[data-wo-statement]").getBoundingClientRect().top+scrollY-85)');page.screenshot(path=str(output/'mobile-statement.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
                    page.set_viewport_size({'width':1440,'height':1080});page.wait_for_timeout(300);ready()
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
                    page.locator('[data-wo-chart=comparison] .wo-chart-legend [data-wo-site=s1]').click();ready();expect(page.locator('.wo-source-table tbody tr')).to_have_count(26)
                    assert 'Юг' not in page.locator('[data-wo-statement]').inner_text()
                    assert page.request.get(origin+'/api/writeoff-analytics?site=s2').json()['totalRows']==0
                    page.locator('[data-action=logout]').click();login('viewer');page.locator('[data-nav=reports]').click();expect(page.locator('#wo-charts')).to_have_count(0)
                    assert page.request.get(origin+'/api/writeoff-analytics?site=s1').status==403
                    assert not errors,errors;browser.close()
                    result={'passed':True,'postgresql17':True,'source':'three invented detailed quarterly reports','quarter_reports':['2026-Q1','2026-Q2','2026-Q3'],'missing_vs_zero':True,'top_5_10_all':True,'metric_switch_and_drilldown':True,'shared_filter_export':True,'desktop_mobile_site_screenshots':True,'role_acl':True,'independent_source_reimbursements':True,'full_statement_17_columns':True,'safe_task_link_popup':True,'mobile_source_cards':True,'browser_errors':errors}
                    (output/'charts-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
            finally:server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
