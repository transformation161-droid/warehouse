"""Shared real UI: pixel-equal navigation, same card styles, detail/read and write refusal."""
import argparse
import base64
import io
import os
from pathlib import Path
import shutil
import httpx
from PIL import Image,ImageChops
from playwright.sync_api import sync_playwright,expect
from tests import offline_stage2 as flow
from server.database import password_hash

PASSWORD='synthetic personal viewer password'


def seed(f,origin):
    for ident in ('viewer','unassigned'):
        flow.sql(f,'insert into users values(?,?,?,?,?,1)',(ident,'demo-'+ident,'ДЕМО · '+ident,'viewer',password_hash(PASSWORD)))
    flow.sql(f,'insert into user_sites values(?,?)',('viewer','s1'))
    with httpx.Client(base_url=origin,trust_env=False,headers={'Origin':origin}) as client:
        login=client.post('/api/login',json={'username':f['logins']['manager'],'password':f['accounts']['manager']});assert login.status_code==200
        client.headers['X-CSRF-Token']=login.json()['csrf']
        def change(collection,ident,data):
            response=client.post('/api/changes',json={'mutationId':'demo-shared-'+ident,'changes':[{'collection':collection,'id':ident,'expectedVersion':0,'data':data}]})
            assert response.status_code==200,response.text
        photo=io.BytesIO();Image.new('RGB',(40,30),'blue').save(photo,'PNG')
        content=base64.b64encode(photo.getvalue()).decode()
        urls=[client.post('/api/media',json={'siteId':site,'content':content}).json()['url'] for site in ('s1','s2','s1')]
        questions=client.get('/api/state').json()['questions']
        change('inspections','demo-shared-act',{'siteId':'s1','date':'2026-11-10','inspector':'PRIVATE-INSPECTOR',
            'results':[{'questionId':q['id'],'status':'ok','comment':'ДЕМО · Детали проверки','photo':urls[0] if i==0 else ''} for i,q in enumerate(questions)]})
        change('findings','demo-shared-finding',{'siteId':'s1','title':'ДЕМО · Подробное нарушение','description':'ДЕМО · Описание нарушения','photo':urls[0],'evidence':urls[0],'owner':'PRIVATE-OWNER','resolution':'ДЕМО · Результат'})
        change('findings','demo-hidden-finding',{'siteId':'s2','title':'PRIVATE-FOREIGN','photo':urls[1]})
        change('inventories','demo-shared-inv',{'siteId':'s1','date':'2026-11-10','title':'ДЕМО · Пересчёт'})
        change('inventoryLines','demo-shared-line',{'inventoryId':'demo-shared-inv','item':'ДЕМО · Инструмент','expected':3,'actual':2,'price':12.5,'owner':'PRIVATE-LINE-OWNER'})
        return urls


def login(page,origin,user,password):
    page.goto(origin+'/');expect(page.locator('#login-form')).to_be_visible()
    page.locator('#username').fill(user);page.locator('#password').fill(password);page.locator('#login-form button').click()
    expect(page.locator('.sidebar')).to_be_attached()


def styles(page):
    return page.evaluate("""()=>Object.fromEntries(['.sidebar','.topbar','.workspace','.card','.table'].map(selector=>{
      const node=document.querySelector(selector),css=getComputedStyle(node);
      return [selector,Object.fromEntries(['fontFamily','fontSize','color','backgroundColor','borderRadius','padding','boxShadow'].map(key=>[key,css[key]]))]
    }))""")


def run(backend):
    flow.RELEASE=True
    output=Path(os.getenv('KSU_VIEWER_SCREENSHOTS','var/viewer-ui'))/backend;output.mkdir(parents=True,exist_ok=True)
    with flow.fixture(backend) as f,flow.server(f) as origin,sync_playwright() as p:
        photo,foreign,orphan=seed(f,origin);before=flow.facts(f)
        browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
        errors=[]
        try:
            for width in (1280,390):
                reference=None;reference_styles=None
                for role in ('manager','inspector','viewer'):
                    context=browser.new_context(viewport={'width':width,'height':900},locale='ru-RU',accept_downloads=True)
                    context.add_init_script("window.__idbOpens=0;const originalOpen=indexedDB.open.bind(indexedDB);indexedDB.open=(...args)=>{window.__idbOpens++;return originalOpen(...args)}")
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    login(page,origin,'demo-viewer' if role=='viewer' else f['logins'][role],PASSWORD if role=='viewer' else f['accounts'][role])
                    expect(page).to_have_title('ИНСТРАКТ | Складской учет')
                    assert page.locator('.nav [data-nav]').count()==8 and page.locator('.viewer-shell').count()==0
                    page.evaluate("navTo('audits')")
                    if width==390:page.locator('[data-action="mobile-toggle"]').click()
                    # Hide only the dynamic count, not navigation or layout, for pixel comparison.
                    page.add_style_tag(content='.nav-count{visibility:hidden}')
                    image_path=output/f'{role}-{width}-navigation.png'
                    page.locator('.nav').screenshot(path=str(image_path),animations='disabled')
                    screenshot=Image.open(image_path).convert('RGB')
                    if reference is None:reference=screenshot.copy();reference_styles=styles(page)
                    else:
                        assert reference.size==screenshot.size and ImageChops.difference(reference,screenshot).getbbox() is None,(role,width,'navigation pixels differ')
                        assert styles(page)==reference_styles,(role,width,'shared component styles differ')
                    if width==390:page.mouse.click(width-10,20)
                    if role!='viewer':context.close();continue
                    expect(page.locator('.role-select')).to_contain_text('Только просмотр')
                    assert page.evaluate('window.__idbOpens')==0
                    assert 'PRIVATE-' not in page.content()
                    for name in ('home','sites','audits','schedule','findings','inventory','reports','settings'):
                        page.evaluate('(name)=>navTo(name)',name)
                        assert not page.locator('[data-action="start-audit"],[data-action="add-finding"],[data-action="import-csv"],[data-action="add-inventory"],[data-action="history"]').count()
                        assert page.locator('.workspace').is_visible()
                        if width==390:assert page.evaluate('document.documentElement.scrollWidth')==width
                    page.evaluate("navTo('sites','s1')");expect(page.locator('.page-heading h2')).to_contain_text('Нова')
                    page.evaluate("navTo('audits')");page.locator('[data-action="audit-detail"][data-id="demo-shared-act"]').click()
                    expect(page.locator('.modal')).to_contain_text('ДЕМО · Детали проверки')
                    expect(page.locator('.modal .photo-preview')).to_have_js_property('naturalWidth',40,timeout=10000)
                    page.locator('[data-action="close-modal"]').first.click()
                    page.evaluate("navTo('findings')");expect(page.locator('[data-action="finding-detail"][data-id="demo-shared-finding"]').first).to_have_text('ДЕМО · Подробное нарушение');page.locator('[data-action="finding-detail"][data-id="demo-shared-finding"]').first.click()
                    expect(page.locator('#finding-form textarea[name="description"]')).to_have_value('ДЕМО · Описание нарушения')
                    assert page.locator('.modal input:enabled,.modal select:enabled,.modal textarea:enabled').count()==0
                    assert page.locator('.modal input[type="file"],[data-action="save-finding"]').count()==0
                    assert page.locator('.modal .photo-preview').count()==2
                    page.screenshot(path=str(output/f'viewer-{width}-finding.png'),full_page=True)
                    page.locator('[data-action="close-modal"]').first.click()
                    page.evaluate("navTo('inventory')");page.locator('[data-action="inventory-detail"][data-id="demo-shared-inv"]').click()
                    expect(page.locator('.modal')).to_contain_text('ДЕМО · Инструмент')
                    assert not page.locator('[data-action="toggle-inv-status"],[data-action="add-inv-line"],[data-action="change-stage"]').count()
                    with page.expect_download():page.locator('[data-action="export-inventory"]').click()
                    page.locator('[data-action="close-modal"]').first.click()
                    page.evaluate("navTo('reports')")
                    page.locator('[data-report-field="kind"]').select_option('audits')
                    for action in ('export-report','report-xlsx','report-pdf'):
                        with page.expect_download():page.locator('[data-action="'+action+'"]').click()
                    # Injecting a write control and calling mutate directly still cannot write/queue.
                    page.evaluate("document.querySelector('.workspace').insertAdjacentHTML('beforeend','<button id=forged data-action=start-audit>forged</button>')")
                    page.locator('#forged').click();assert page.locator('.wizard-layout').count()==0
                    assert page.evaluate("async()=>{let ran=false;await mutate(()=>{ran=true});return !ran&&pending===null}")
                    result=page.evaluate("async()=>({write:(await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status,offline:(await fetch('/api/offline/packages',{method:'POST',body:'{}'})).status})")
                    assert result=={'write':403,'offline':403}
                    assert context.request.get(origin+photo).status==200
                    assert context.request.get(origin+foreign).status==404 and context.request.get(origin+orphan).status==404
                    # A different account's cookie must not authorize a viewer download or state request.
                    pinned=page.evaluate("db.inspections.find(row=>row.id==='demo-shared-act').results[0].photo")
                    changed=context.request.post(origin+'/api/login',headers={'Origin':origin},data={'username':f['logins']['manager'],'password':f['accounts']['manager']});assert changed.status==200
                    assert context.request.get(origin+pinned).status==403
                    assert page.evaluate("async()=>{try{await api('state');return 0}catch(e){return e.status}}") ==403
                    expect(page.locator('#login-form')).to_be_visible()
                    assert 'PRIVATE-' not in page.content()
                    context.close()
            context=browser.new_context();page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            login(page,origin,'demo-unassigned',PASSWORD)
            assert page.evaluate('db.sites.length+db.inspections.length+db.questions.length')==0
            assert page.locator('.nav [data-nav]').count()==8
            f['switch_off']();page.reload();expect(page.locator('.sidebar')).to_be_attached()
            assert not context.request.get(origin+'/api/offline-visit/context').json()['enabled']
            context.close()
            assert not errors,errors
        finally:browser.close()
        assert before==flow.facts(f),'Read-only browser scenario changed business tables'
        print('PASS: '+backend+' shared desktop/mobile UI, pixel-identical navigation and card CSS across 3 roles; details/photos/reports, personal-field redaction, empty ACL, no writes/IDB, session race, off mode',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=['sqlite','postgres'],required=True);run(parser.parse_args().backend)
