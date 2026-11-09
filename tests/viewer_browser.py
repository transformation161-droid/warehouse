"""New branded shell and viewer/session boundaries on SQLite + native PostgreSQL."""
import argparse
import os
import shutil
from playwright.sync_api import sync_playwright,expect
from tests import offline_stage2 as flow
from server.database import password_hash


def run(backend):
    flow.RELEASE=True
    with flow.fixture(backend) as fixture,flow.server(fixture) as origin,sync_playwright() as p:
        flow.sql(fixture,'insert into users values(?,?,?,?,?,1)',('viewer','demo-viewer','ДЕМО · Viewer','viewer',password_hash('synthetic viewer password')))
        flow.sql(fixture,'insert into user_sites values(?,?)',('viewer','s1'))
        browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
        try:
            context=browser.new_context(viewport={'width':390,'height':844});page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(origin+'/');expect(page).to_have_title('ИНСТРАКТ | Складской учет')
            expect(page.locator('.logo-mark img')).to_be_visible()
            page.locator('#username').fill('demo-viewer');page.locator('#password').fill('synthetic viewer password');page.locator('#login-form button').click()
            expect(page.locator('.viewer-shell')).to_be_visible();expect(page.get_by_role('status')).to_contain_text('Только просмотр')
            assert not page.locator('input,textarea,select,a[href="/offline-visit/"]').count()
            assert 'PRIVATE-' not in page.content() and page.evaluate('document.documentElement.scrollWidth')==390
            before=flow.facts(fixture)
            result=page.evaluate("async()=>({write:(await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status,report:(await fetch('/api/reports/pdf')).status,photo:(await fetch('/api/media/unknown')).status})")
            assert result=={'write':403,'report':403,'photo':403} and before==flow.facts(fixture)
            page.locator('[data-action="logout"]').click();expect(page.locator('#login-form')).to_be_visible()
            # Reusing a browser context must not reveal the previous account's server state/queue.
            page.locator('#username').fill(fixture['logins']['inspector']);page.locator('#password').fill(fixture['accounts']['inspector']);page.locator('#login-form button').click()
            expect(page.locator('.sidebar')).to_be_visible();expect(page.locator('a[href="/offline-visit/"]')).to_have_count(1)
            page.locator('[data-action="logout"]').click();expect(page.locator('#login-form')).to_be_visible()
            page.locator('#username').fill('demo-viewer');page.locator('#password').fill('synthetic viewer password');page.locator('#login-form button').click();expect(page.locator('.viewer-shell')).to_be_visible()
            # Cookie changed in another tab AFTER this viewer page was opened.
            login=context.request.post(origin+'/api/login',headers={'Origin':origin},data={'username':fixture['logins']['manager'],'password':fixture['accounts']['manager']});assert login.status==200
            assert page.evaluate("async()=>{try{await api('state');return 0}catch(e){return e.status}}")==403
            expect(page.locator('#login-form')).to_be_visible();assert 'PRIVATE-' not in page.content()
            page.locator('#username').fill('demo-viewer');page.locator('#password').fill('synthetic viewer password');page.locator('#login-form button').click();expect(page.locator('.viewer-shell')).to_be_visible()
            # Viewer is never enabled by either rollout mode.
            fixture['switch_off']();assert context.request.get(origin+'/api/offline-visit/context').json()['enabled'] is False
            page.reload();expect(page.locator('.viewer-shell')).to_be_visible()
            assert not errors,errors
            context.close()
        finally:browser.close()
        print('PASS: '+backend+' branding, mobile viewer, direct API denies, user/session separation, off mode, no business writes')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=['sqlite','postgres'],required=True);run(parser.parse_args().backend)
