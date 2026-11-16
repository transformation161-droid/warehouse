"""Focused real integrated UI/PG17 checks. Screenshots contain invented data only."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
import unittest
import uvicorn
from playwright.sync_api import sync_playwright,expect
from tests.pg_environment import unused_port
from tests.writeoff_environment import pilot
from tests.writeoff_fixture import workbook,rows


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    port=unused_port();origin=f'http://127.0.0.1:{port}';errors=[]
    with pilot(origin) as (app,accounts,pg):
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.1)
        else:raise AssertionError('Isolated UI did not start')
        try:
            with sync_playwright() as p:
                options={'headless':True}
                if os.getenv('CHROMIUM_EXECUTABLE'):options['executable_path']=os.environ['CHROMIUM_EXECUTABLE']
                browser=p.chromium.launch(**options);ctx=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True);page=ctx.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)))
                def login(role):
                    page.goto(origin);page.locator('input[name=username]').fill(role);page.locator('input[name=password]').fill(accounts[role]['password']);page.locator('button[type=submit]').click();expect(page.locator('[data-nav=reports]')).to_be_visible()
                login('manager');expect(page.locator('[data-nav=inventory]')).to_have_count(0)
                page.locator('[data-nav=reports]').click();expect(page.locator('[data-wo-panel]')).to_be_visible();expect(page.locator('#wo-registry')).to_be_visible()
                expect(page.locator('.notice.warn')).to_contain_text('Нет загруженных данных')
                page.locator('[data-wo-action=import]').click();expect(page.locator('#wo-file')).to_be_visible()
                page.locator('#wo-file').set_input_files({'name':'DEMO.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':workbook()})
                page.locator('#wo-period').fill('2026-Q2');page.locator('[data-wo-action=preview]').click()
                expect(page.locator('[data-wo-alias]')).to_have_count(2,timeout=30000)
                for el in page.locator('[data-wo-alias]').all():el.select_option('s1' if 'Север' in el.get_attribute('data-wo-alias') else 's2')
                page.locator('#wo-alias-confirm').check();page.locator('[data-wo-action=preview]').click()
                expect(page.locator('[data-wo-action=commit]')).to_be_visible(timeout=30000);expect(page.locator('#wo-feedback')).to_contain_text('Принято 39')
                page.on('dialog',lambda d:d.accept());page.locator('[data-wo-action=commit]').dblclick()
                expect(page.locator('.modal')).to_have_count(0,timeout=30000);expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих',timeout=30000)
                expect(page.locator('.workspace > [data-wo-panel]')).to_be_visible()
                page.screenshot(path=str(output/'desktop.png'),full_page=True)
                page.locator('[data-wo-filter=reportYear]').select_option('2026');page.locator('[data-wo-filter=reportPeriod]').select_option('2026-Q1')
                expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('0 действующих',timeout=10000)
                expect(page.locator('.notice.warn')).to_contain_text('Нет загруженных данных')
                page.locator('[data-wo-action=clear]').click();expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих')
                page.locator('[data-wo-cross=reason]').first.click();expect(page.locator('[data-wo-filter=reason]')).not_to_have_value('');page.locator('[data-wo-action=clear]').click()
                with page.expect_download() as event:page.locator('#wo-registry [data-wo-ext=xlsx]').click()
                path=event.value.path();assert Path(path).stat().st_size>1000
                # Correct one existing act through the real comparison UI, not by summing two books.
                page.locator('[data-wo-action=import]').click();items=rows();items[0]['quantity']='4'
                page.locator('#wo-file').set_input_files({'name':'DEMO-corrected.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':workbook(items)})
                page.locator('#wo-period').fill('2026-Q2');page.locator('[data-wo-action=preview]').click()
                expect(page.locator('[data-wo-alias]')).to_have_count(2,timeout=30000)
                page.locator('#wo-document').fill('ДЕМО · Акт 1')
                for el in page.locator('[data-wo-alias]').all():el.select_option('s1' if 'Север' in el.get_attribute('data-wo-alias') else 's2')
                page.locator('#wo-alias-confirm').check();page.locator('[data-wo-action=preview]').click()
                expect(page.locator('[data-wo-action=replace]')).to_be_visible(timeout=30000);page.locator('[data-wo-action=replace]').click()
                expect(page.locator('#wo-target')).to_be_visible()
                target=next(x.get_attribute('value') for x in page.locator('#wo-target option').all() if 'Акт 1' in x.text_content());page.locator('#wo-target').select_option(target)
                page.locator('#wo-reason').fill('ДЕМО · Исправленный источник');page.locator('[data-wo-action=compare]').click()
                expect(page.locator('.modal')).to_contain_text('Изменено',ignore_case=True,timeout=30000)
                page.locator('[data-wo-action=commit]').click();expect(page.locator('.modal')).to_have_count(0,timeout=30000)
                expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('39 действующих',timeout=30000)
                assert page.evaluate("Number(document.querySelectorAll('[data-wo-panel] .stat-value')[3].textContent.replace(/[^0-9]/g,''))")==58
                page.locator('[data-wo-action=detail]').first.click();expect(page.locator('.modal')).to_contain_text('История версий')
                page.locator('#wo-event-mode').select_option('date');page.locator('#wo-event-date').fill('2026-05-20');page.locator('#wo-event-reason').fill('ДЕМО · Уточнено при приёмке');page.locator('[data-wo-action=event]').click();expect(page.locator('.modal')).to_have_count(0)
                page.locator('[data-wo-site]').first.click();expect(page.locator('h2').first).to_contain_text('Склад');expect(page.locator('[data-wo-panel]')).to_be_visible();expect(page.locator('#wo-registry')).to_be_visible()
                # Real mobile layout: table scrolls inside its container, not the entire page.
                page.set_viewport_size({'width':412,'height':915});page.wait_for_timeout(400);page.screenshot(path=str(output/'mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2'), 'Mobile page overflows'
                expect(page.locator('.mobile-menu')).to_be_visible()
                page.set_viewport_size({'width':1440,'height':1000});page.locator('[data-action=logout]').click();login('inspector');page.locator('[data-nav=reports]').click()
                expect(page.locator('#wo-registry .card-subtitle')).to_contain_text('20 действующих',timeout=20000);expect(page.locator('[data-wo-action=import]')).to_have_count(0)
                expect(page.locator('[data-wo-filter=site] option')).to_have_count(2)
                page.locator('[data-action=logout]').click();login('viewer');page.locator('[data-nav=reports]').click();expect(page.locator('[data-wo-panel]')).to_have_count(0)
                assert not errors,errors
                browser.close()
                result={'passed':True,'backend':'postgresql17','integrated_ui':True,'upload_preview_alias_commit_dashboard_xlsx':True,'mobile_width':412,'account_cache_isolation':True,'source':'synthetic only','browser_errors':errors}
                (output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
        finally:server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
