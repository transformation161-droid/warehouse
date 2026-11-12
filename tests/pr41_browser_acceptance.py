"""Real desktop and Android Chromium checks on synthetic PostgreSQL only."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import time

from openpyxl import Workbook,load_workbook
from playwright.sync_api import sync_playwright,expect
from pypdf import PdfReader

from inventory_browser_stage import environment,logged_page,upload_plan,workbook as large_workbook,HOST,SCREEN
from inventory_pg_smoke import MANAGER,ELMAN,ADMIN_DSN
from pr41_fixtures import seed,session,VIEWER,photo
import psycopg


def workbook():
    book=Workbook();s=book.active;s.append(['ID','Наименование','№','Стоимость','Бренд','Количество'])
    for i,qty in enumerate([1,2,0,.001],1):s.append(['PR41-'+str(i),'ДЕМО · Инструмент '+str(i),'DEMO-'+str(i),100,'ДЕМО · Бренд '+str(i),qty])
    out=BytesIO();book.save(out);return out.getvalue()


def screen(page,name):
    expect(page.locator('.modal')).to_be_visible()
    page.screenshot(path=str(SCREEN/name),full_page=True)


def matching(page,index=0):return page.locator('.ip-desktop-table [data-ip-line]').nth(index)


def main():
    SCREEN.mkdir(exist_ok=True);server,tokens=environment();os.environ['KSU_VIEWER_ACCESS']='enabled';seed();tokens[VIEWER['id']]=session(VIEWER)[0]
    errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None)
            owner,mp=logged_page(browser,tokens[MANAGER['id']]);field,ep=logged_page(browser,tokens[ELMAN['id']])
            for page in (mp,ep):page.on('pageerror',lambda error:errors.append(str(error)))
            # Counts include closed findings and respect actor's visible data.
            expect(mp.locator('.stat-card').filter(has_text='Нарушения: открыто / всего')).to_contain_text('2 / 4')
            expect(ep.locator('.stat-card').filter(has_text='Нарушения: открыто / всего')).to_contain_text('2 / 3')
            expect(mp.locator('.object-row').filter(has_text='Иной объект').first).to_contain_text('Открыто 0 / 1')
            mp.locator('.sidebar [data-nav="sites"]').click();expect(mp.locator('.object-card').filter(has_text='Иной объект')).to_contain_text('0 / 1')
            mp.locator('[data-action="site-detail"][data-id="site_alpha"]').click()
            expect(mp.locator('.stat-card').filter(has_text='Нарушения: открыто / всего')).to_contain_text('2 / 3')
            # Escape and backdrop close the top photo, preserving the underlying form.
            for page in (mp,ep):
                page.evaluate("findingForm(db.findings.find(x=>x.id==='synthetic-open'))")
                if page==mp:page.locator('#finding-form [name="description"]').fill('ДЕМО · ввод остаётся в карточке')
                page.locator('[data-action="zoom-photo"]').first.click();expect(page.locator('.ksu-photo-lightbox')).to_be_visible()
                page.keyboard.press('Escape');expect(page.locator('.ksu-photo-lightbox')).to_have_count(0);expect(page.locator('#finding-form')).to_be_visible()
                if page==mp:expect(page.locator('#finding-form [name="description"]')).to_have_value('ДЕМО · ввод остаётся в карточке')
                page.locator('[data-action="zoom-photo"]').first.click();page.locator('.ksu-photo-lightbox').click(position={'x':2,'y':2})
                expect(page.locator('.ksu-photo-lightbox')).to_have_count(0)
                page.locator('[data-action="close-modal"]').first.click()
                page.evaluate("showAudit('synthetic-act')");page.locator('[data-action="zoom-photo"]').click()
                expect(page.locator('.ksu-photo-lightbox')).to_be_visible();page.locator('.ksu-photo-lightbox button').click()
                expect(page.locator('.modal-head h3')).to_have_text('Акт проверки');page.locator('[data-action="close-modal"]').first.click()
            # Plan includes zero and fractional quantities; no new status stored in DB.
            mp.locator('.sidebar [data-nav="inventory"]').click();upload_plan(mp,workbook(),'ДЕМО · Быстрый пересчёт PR41','2026-11-22')
            ident=mp.evaluate("db.inventories.find(x=>x.title==='ДЕМО · Быстрый пересчёт PR41').id")
            lines=mp.evaluate("db.inventoryLines.filter(x=>x.inventoryId==="+json.dumps(ident)+").sort((a,b)=>a.sourceRow-b.sourceRow)")
            requests=[];mp.on('request',lambda r:requests.append(r.url) if r.method=='PATCH' and '/count' in r.url else None)
            # Hold the first response to test real repeated clicks and navigation guards.
            held=[]
            mp.route('**/inventory-lines/*/count',lambda route:held.append(route))
            matching(mp).locator('[data-action="ip-quick-ok"]').click()
            expect(mp.locator('.ip-desktop-table [data-action="ip-quick-ok"]').first).to_be_disabled()
            mp.evaluate("document.querySelector('.ip-desktop-table [data-action=ip-quick-ok]').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
            mp.keyboard.press('Escape');expect(mp.locator('.modal')).to_be_visible()
            assert len(held)==1
            held[0].continue_();mp.unroute('**/inventory-lines/*/count')
            expect(matching(mp)).to_contain_text('Соответствует')
            matching(mp).locator('[data-action="ip-quick-ok"]').click();assert len(requests)==1
            # A poll started before opening a count form must not redraw typed values.
            mp.locator('[data-action="close-modal"]').first.click()
            polled=[]
            mp.route('**/api/state',lambda route:polled.append(route))
            for _ in range(60):
                if polled:break
                mp.wait_for_timeout(100)
            assert polled
            mp.evaluate('inventoryDetail('+json.dumps(ident)+')')
            matching(mp,1).locator('[data-action="ip-quick-issue"]').click();mp.locator('#ip-comment').fill('ДЕМО · Ввод во время poll')
            ep.evaluate('''async()=>{const f=db.findings.find(x=>x.id==='synthetic-progress');await api('changes',{method:'POST',body:JSON.stringify({mutationId:crypto.randomUUID(),changes:[{collection:'findings',id:f.id,expectedVersion:f.version,data:{...cleanRecord(f),description:'ДЕМО · обновление для poll'}}]})})}''')
            with mp.expect_response(lambda r:r.url.endswith('/api/state')) as finished:polled[0].continue_()
            mp.wait_for_function('!polling');mp.unroute('**/api/state')
            expect(mp.locator('#ip-comment')).to_have_value('ДЕМО · Ввод во время poll');mp.locator('[data-action="ip-back"]').click()
            # Inspector opens the same version. Manager wins; inspector retains all input on 409.
            ep.evaluate('async()=>{db=await api("state");render();inventoryDetail('+json.dumps(ident)+')}' )
            matching(ep,1).locator('[data-action="ip-quick-issue"]').click()
            expect(ep.locator('#ip-actual')).to_have_value('')
            ep.locator('#ip-actual').fill('0');ep.locator('#ip-comment').fill('ДЕМО · Не найдено, сверить конфликт')
            ep.locator('#ip-photo').set_input_files({'name':'synthetic-count.jpg','mimeType':'image/jpeg','buffer':photo()})
            matching(mp,1).locator('[data-action="ip-quick-ok"]').click();expect(matching(mp,1)).to_contain_text('Соответствует')
            ep.locator('[data-action="ip-save-count"]').click()
            expect(ep.locator('#ip-count-feedback')).to_contain_text('Конфликт версии')
            expect(ep.locator('#ip-actual')).to_have_value('0');expect(ep.locator('#ip-comment')).to_have_value('ДЕМО · Не найдено, сверить конфликт')
            assert ep.locator('#ip-photo').evaluate('(el)=>el.files.length')==1
            with ep.expect_download() as download:ep.locator('[data-action="ip-count-copy"]').click()
            copy=json.loads(Path(download.value.path()).read_text());assert copy['actual']=='0' and copy['attachment']['name']=='synthetic-count.jpg'
            assert base64.b64decode(copy['attachment']['content'])==photo()
            # Manual reconciliation changes expectedVersion only after explicit confirmation.
            ep.once('dialog',lambda d:d.accept());ep.locator('[data-action="ip-use-current"]').click()
            ep.locator('[data-action="ip-save-count"]').click();expect(matching(ep,1)).to_contain_text('Недостача')
            changed=ep.evaluate('db.inventoryLines.find(x=>x.id==='+json.dumps(lines[1]['id'])+')')
            assert changed['actual']==0 and changed['photo'].startswith('/api/media/') and changed['version']==3
            assert all(changed[k]==lines[1][k] for k in ('expected','price','sku','sourceId','brand','inventoryNumber'))
            # A green action on a discrepancy opens editing and retains the result/photo/comment.
            matching(ep,1).locator('[data-action="ip-quick-ok"]').click();expect(ep.locator('#ip-actual')).to_have_value('0')
            expect(ep.locator('#ip-comment')).to_have_value('ДЕМО · Не найдено, сверить конфликт');ep.locator('[data-action="ip-back"]').click()
            # Zero-plan surplus also requires explicit actual and comment.
            matching(ep,2).locator('[data-action="ip-quick-issue"]').click();expect(ep.locator('#ip-actual')).to_have_value('')
            ep.locator('[data-action="ip-save-count"]').click();expect(ep.locator('#ip-count-form')).to_be_visible()
            ep.locator('#ip-actual').fill('1');ep.locator('[data-action="ip-save-count"]').click()
            expect(ep.locator('#toast-root')).to_contain_text('Укажите причину')
            ep.locator('#ip-comment').fill('ДЕМО · Найден излишек');ep.locator('[data-action="ip-save-count"]').click()
            expect(matching(ep,2)).to_contain_text('Излишек')
            # Network failure retains the original version and form until retry.
            matching(ep,3).locator('[data-action="ip-quick-issue"]').click();ep.locator('#ip-actual').fill('0')
            ep.locator('#ip-comment').fill('ДЕМО · Дробный учёт')
            ep.route('**/inventory-lines/*/count',lambda route:route.abort())
            ep.locator('[data-action="ip-save-count"]').click();expect(ep.locator('#ip-count-feedback')).to_contain_text('Сохранение не подтверждено')
            expect(ep.locator('#ip-comment')).to_have_value('ДЕМО · Дробный учёт');ep.unroute('**/inventory-lines/*/count')
            ep.locator('[data-action="ip-save-count"]').click();expect(matching(ep,3)).to_contain_text('Недостача')
            # Real report downloads keep zero/null and exact quantities, brand and statuses.
            for ext in ('xlsx','pdf'):
                with ep.expect_download() as download:ep.locator('[data-action="export-inventory-'+ext+'"]').click()
                target=SCREEN/('synthetic-pr41.'+ext);download.value.save_as(target)
                if ext=='xlsx':
                    ws=load_workbook(target,data_only=True).active
                    assert ws['C6'].value=='ДЕМО · Бренд 1' and ws['N7'].value=='Недостача' and ws['N8'].value=='Излишек'
                    assert [ws.cell(10,c).value for c in (10,11,12,13)]==[300.1,200,200.1,100]
                else:
                    text='\n'.join(p.extract_text() for p in PdfReader(target).pages)
                    assert 'ДЕМО · Бренд 4' in text and '0.001' in text and 'Недостача' in text and 'Излишек' in text
            # Manager finalizes; the closed plan exposes no write buttons.
            mp.evaluate('async()=>{db=await api("state");render();inventoryDetail('+json.dumps(ident)+')}' )
            mp.once('dialog',lambda d:d.accept());mp.locator('[data-action="ip-finalize"]').click()
            expect(mp.locator('.modal-body')).to_contain_text('Инвентаризация завершена')
            assert mp.locator('[data-action="ip-quick-ok"]').count()==0
            screen(mp,'pr41-desktop-final.png')
            # Actual Android user-agent/touch emulation, all four statuses, large targets.
            android,ap=logged_page(browser,tokens[ELMAN['id']],p.devices['Pixel 7']);ap.on('pageerror',lambda e:errors.append(str(e)))
            ap.evaluate('inventoryDetail('+json.dumps(ident)+')');expect(ap.locator('.ip-mobile-list')).to_be_visible()
            assert not ap.locator('.ip-desktop-table').is_visible()
            assert ap.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            screen(ap,'pr41-android-final.png')
            # Fresh draft keeps Android touch actions reachable and unchecked status.
            mp.locator('[data-action="close-modal"]').first.click();upload_plan(mp,workbook(),'ДЕМО · Android PR41','2026-11-23')
            fresh=mp.evaluate("db.inventories.find(x=>x.title==='ДЕМО · Android PR41').id")
            ap.evaluate('async()=>{db=await api("state");inventoryDetail('+json.dumps(fresh)+')}' )
            button=ap.locator('.ip-mobile-list [data-action="ip-quick-ok"]').first
            assert button.bounding_box()['height']>=48;button.tap()
            expect(ap.locator('.ip-mobile-card').first).to_contain_text('Соответствует')
            assert ap.locator('.toast').evaluate('el=>getComputedStyle(el).pointerEvents')=='none'
            ap.locator('.ip-mobile-list [data-action="ip-quick-issue"]').first.tap(timeout=1500)
            expect(ap.locator('#ip-actual')).to_have_value('');ap.locator('[data-action="ip-back"]').tap()
            ap.locator('.ip-mobile-list [data-action="ip-quick-issue"]').nth(1).tap();ap.locator('#ip-actual').fill('0')
            ap.locator('#ip-comment').fill('ДЕМО · Android фото');ap.locator('#ip-photo').set_input_files({'name':'synthetic.jpg','mimeType':'image/jpeg','buffer':photo()})
            ap.locator('[data-action="ip-save-count"]').tap();expect(ap.locator('.ip-mobile-card').nth(1)).to_contain_text('Недостача')
            ap.locator('.ip-mobile-card').nth(3).scroll_into_view_if_needed();top=ap.locator('.modal-body').evaluate('el=>el.scrollTop')
            ap.locator('.ip-mobile-list [data-action="ip-quick-ok"]').nth(3).tap();expect(ap.locator('.ip-mobile-card').nth(3)).to_contain_text('Соответствует')
            ap.wait_for_function('top=>Math.abs(document.querySelector(".modal-body").scrollTop-top)<3',arg=top)
            ap.locator('.ip-mobile-card').nth(1).scroll_into_view_if_needed()
            assert ap.evaluate('document.documentElement.scrollWidth<=innerWidth+1');screen(ap,'pr41-android-actions.png')
            ap.reload();expect(ap.locator('#sync-status')).to_contain_text('синхронизировано');ap.evaluate('inventoryDetail('+json.dumps(fresh)+')')
            expect(ap.locator('.ip-mobile-card').nth(1)).to_contain_text('Недостача')
            ap.locator('[data-action="close-modal"]').first.tap();ap.evaluate("findingForm(db.findings.find(x=>x.id==='synthetic-open'))")
            ap.locator('[data-action="zoom-photo"]').first.tap();expect(ap.locator('.ksu-photo-lightbox')).to_be_visible()
            box=ap.locator('.ksu-photo-lightbox').bounding_box();assert box['width']<=ap.viewport_size['width']
            ap.screenshot(path=str(SCREEN/'pr41-android-photo.png'))
            ap.locator('.ksu-photo-lightbox button').tap();expect(ap.locator('#finding-form')).to_be_visible()
            # Preserve PR40 large-plan search, page boundaries and position ordering.
            ep.locator('[data-action="close-modal"]').first.click();ep.locator('.sidebar [data-nav="inventory"]').click();upload_plan(ep,large_workbook(121),'ДЕМО · 121 позиция PR41','2026-11-24')
            expect(ep.locator('.ip-desktop-table tbody tr')).to_have_count(100)
            ep.locator('[data-action="ip-next-page"]').click();expect(ep.locator('.ip-desktop-table tbody tr')).to_have_count(21)
            expect(ep.locator('.modal-body')).to_contain_text('Страница 2 из 2')
            ep.locator('#ip-filter-name').fill('Инструмент 121');ep.locator('[data-action="ip-filter-apply"]').click()
            expect(ep.locator('.ip-desktop-table tbody tr')).to_have_count(1)
            matching(ep).locator('[data-action="ip-quick-ok"]').click();expect(matching(ep)).to_contain_text('Соответствует')
            expect(ep.locator('#ip-filter-name')).to_have_value('инструмент 121')
            screen(ep,'pr41-desktop-search-121.png')
            # Viewer can zoom permitted photos; no writes and no foreign data controls.
            viewing,vp=logged_page(browser,tokens[VIEWER['id']]);vp.on('pageerror',lambda e:errors.append(str(e)))
            vp.evaluate("findingForm(db.findings.find(x=>x.id==='synthetic-open'))")
            assert vp.locator('[data-action="save-finding"]').count()==0
            vp.locator('[data-action="zoom-photo"]').first.click();expect(vp.locator('.ksu-photo-lightbox')).to_be_visible()
            vp.screenshot(path=str(SCREEN/'pr41-viewer-photo.png'))
            vp.keyboard.press('Escape');expect(vp.locator('#finding-form')).to_be_visible()
            vp.locator('[data-action="close-modal"]').first.click();vp.evaluate('inventoryDetail('+json.dumps(ident)+')')
            assert vp.locator('[data-action="ip-quick-ok"],[data-action="ip-quick-issue"],[data-action="ip-save-count"]').count()==0
            # Closing a finding changes only the numerator in all scoped counters.
            mp.locator('[data-action="close-modal"]').first.click();mp.evaluate("findingForm(db.findings.find(x=>x.id==='synthetic-open'))")
            mp.locator('#finding-form [name="status"]').select_option('closed');mp.locator('#finding-form [name="resolution"]').fill('ДЕМО · Устранено')
            mp.locator('#finding-form [name="evidence"]').evaluate("e=>e.value='/api/media/synthetic-alpha'")
            mp.locator('[data-action="save-finding"]').click();expect(mp.locator('#finding-form')).to_have_count(0)
            mp.locator('.sidebar [data-nav="home"]').click();expect(mp.locator('.stat-card').filter(has_text='Нарушения: открыто / всего')).to_contain_text('1 / 4')
            assert not errors,errors
            for page in (mp,ep,ap,vp):
                page.evaluate('sessionUser=null');page.wait_for_function('!polling')
            for ctx in (owner,field,android,viewing):ctx.close()
            browser.close()
    finally:server.should_exit=True
    print('PASS PR41 desktop/Android: quick/double-click/conflict recovery, photos, counters, reports, viewer and persistence')


if __name__=='__main__':main()
