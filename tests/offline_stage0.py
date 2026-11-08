"""Persistent Chromium profile, server really stopped, encrypted IDB, controlled failures."""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener,ProxyHandler

from PIL import Image
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
PHRASE='stage zero local synthetic phrase'

@contextmanager
def sentinel(backend,folder):
    from server.database import Database,seed_demo
    from server.maintenance import snapshot_database
    live=folder/'sentinel.sqlite3';source=folder/'copy.sqlite3'
    seed_demo(Database(live),'demo-synthetic-manager-pass','demo-synthetic-inspector-pass');snapshot_database(live,source)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    if backend=='postgres':
        import psycopg
        from tests.pg_environment import local_cluster
        from server.pg_admin import initialize,migrate_sqlite,fingerprints
        with local_cluster() as cluster:
            dsn=cluster['source']['admin'];initialize(dsn,'ksu_runtime','ksu_backup');migrate_sqlite(source,dsn)
            with psycopg.connect(dsn) as con:before=fingerprints(con,True)
            yield {'KSU_DB':str(source),'KSU_DATABASE_URL':dsn}
            with psycopg.connect(dsn) as con:assert fingerprints(con,True)==before
    else:
        yield {'KSU_DB':str(source),'KSU_DATABASE_URL':'postgresql://not-used.invalid/no-contact'}
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    print('PASS: '+backend+' sentinel unchanged; standalone server never accesses application databases',flush=True)


def run(backend):
    with tempfile.TemporaryDirectory(prefix='ksu-stage0-test-') as tmp:
        folder=Path(tmp)
        with sentinel(backend,folder) as env:
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            origin=f'http://localhost:{port}';process=None
            log=(folder/'server.log').open('w')
            def start(build='stage0-v1',fail=None):
                nonlocal process
                command=[sys.executable,'experiments/offline_mvp/server.py','--port',str(port),'--build',build]
                if fail:command+=['--fail-asset',fail]
                process=subprocess.Popen(command,cwd=ROOT,env={**os.environ,**env},stdout=log,stderr=log)
                opener=build_opener(ProxyHandler({}))
                for _ in range(100):
                    try:
                        if opener.open(origin+'/build.json',timeout=.5).status==200:return
                    except OSError:time.sleep(.1)
                raise AssertionError('Prototype did not start')
            def stop():
                nonlocal process
                if process:process.terminate();process.wait(timeout=10);process=None
            try:
                start()
                with sync_playwright() as p:
                    def browser(offline=False):
                        context=p.chromium.launch_persistent_context(str(folder/'profile'),headless=True,
                            executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),
                            viewport={'width':390,'height':844},locale='ru-RU',args=['--no-sandbox'])
                        context.set_offline(offline);return context
                    context=browser();page=context.pages[0];errors=[]
                    def setup(page):
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.on('dialog',lambda d:d.accept())
                    setup(page);page.clock.install(time=time.time());page.goto(origin)
                    expect(page.locator('#worker-status')).to_contain_text('активна')
                    assert page.evaluate('document.documentElement.scrollWidth')==390
                    page.locator('#phrase').fill(PHRASE);page.locator('#open').click()
                    expect(page.locator('#editor')).to_be_visible()
                    context.set_offline(True);page.locator('#prepare').click();expect(page.locator('#message')).to_have_class('error')
                    assert page.locator('.question-card').count()==0
                    context.set_offline(False);page.locator('#prepare').click()
                    expect(page.locator('.question-card')).to_have_count(3)
                    expect(page.locator('#photos img')).to_have_count(1)
                    print('PASS: package + reference photo verified; failed preparation never creates partial package',flush=True)
                    stop();context.set_offline(True)
                    for select in page.locator('[data-answer]').all():select.select_option('ok')
                    page.locator('[data-answer="demo-q1"]').select_option('fail')
                    page.locator('[data-comment="demo-q1"]').fill('ДЕМО · Нет маркировки · offline-secret-marker')
                    expect(page.get_by_role('button',name='Сделать фото',exact=True)).to_be_visible()
                    expect(page.get_by_role('button',name='Из галереи',exact=True)).to_be_visible()
                    assert page.locator('#camera-photo').get_attribute('capture')=='environment'
                    assert page.locator('#camera-photo').get_attribute('accept')=='image/*'
                    assert page.locator('#photo').get_attribute('capture') is None
                    def pick(button,element,payload):
                        with page.expect_file_chooser() as chosen:
                            page.get_by_role('button',name=button,exact=True).click()
                        chooser=chosen.value
                        assert chooser.element.get_attribute('id')==element
                        chooser.set_files(payload)
                    # Dirty text must be committed BEFORE the native camera opens; cancellation adds no image.
                    pick('Сделать фото','camera-photo',[])
                    expect(page.locator('#message')).to_contain_text('Съёмка отменена')
                    expect(page.locator('#local-status')).to_contain_text('Сохранено на устройстве')
                    expect(page.locator('#photos img')).to_have_count(1)
                    expect(page.locator('[data-comment="demo-q1"]')).to_have_value('ДЕМО · Нет маркировки · offline-secret-marker')
                    # A real offline process restart after cancel must retain the formerly dirty text.
                    context.close();context=browser(True);page=context.pages[0];setup(page);page.clock.install(time=time.time());page.goto(origin)
                    page.locator('#phrase').fill(PHRASE);page.locator('#open').click()
                    expect(page.locator('[data-comment="demo-q1"]')).to_have_value('ДЕМО · Нет маркировки · offline-secret-marker')
                    expect(page.locator('#photos img')).to_have_count(1)
                    def image(color):
                        data=io.BytesIO();Image.new('RGB',(64,48),color).save(data,format='PNG');return {'name':'demo.png','mimeType':'image/png','buffer':data.getvalue()}
                    pick('Сделать фото','camera-photo',image('blue'));expect(page.locator('#photos img')).to_have_count(2)
                    ids=page.locator('.photo-meta').all_text_contents()
                    pick('Из галереи','photo',image('blue'));expect(page.locator('#message')).to_contain_text('уже сохранено')
                    assert page.locator('.photo-meta').all_text_contents()==ids
                    def raw_state():
                        return page.evaluate("""async()=>{const {DB_NAME}=await import('/store.js');return await new Promise((resolve,reject)=>{const r=indexedDB.open(DB_NAME);r.onsuccess=()=>{const d=r.result,t=d.transaction('records'),q=t.objectStore('records').get('state'),count=t.objectStore('records').count();t.oncomplete=()=>{d.close();resolve({count:count.result,keys:Object.keys(q.result),cipher:Array.from(new Uint8Array(q.result.cipher))})};};r.onerror=()=>reject(r.error)})}""")
                    raw=raw_state();assert set(raw['keys'])=={'iv','cipher'};assert b'offline-secret-marker' not in bytes(raw['cipher'])
                    # Both cancel event and empty selection preserve the encrypted record and stable IDs.
                    pick('Из галереи','photo',[]);expect(page.locator('#message')).to_contain_text('Выбор фото отменён')
                    page.locator('#camera-photo').dispatch_event('cancel')
                    expect(page.locator('#message')).to_contain_text('Съёмка отменена')
                    assert raw_state()==raw and page.locator('.photo-meta').all_text_contents()==ids
                    # Legacy fallback without showPicker/capture uses the ordinary chooser, not another save path.
                    page.locator('#camera-photo').evaluate("e=>{e.removeAttribute('capture');e.showPicker=undefined}")
                    pick('Сделать фото','camera-photo',[]);expect(page.locator('#message')).to_contain_text('Съёмка отменена')
                    page.evaluate("Object.defineProperty(navigator,'userActivation',{configurable:true,value:undefined})")
                    page.locator('[data-comment="demo-q1"]').fill('ДЕМО · Нет маркировки · offline-secret-marker')
                    page.get_by_role('button',name='Сделать фото',exact=True).click()
                    expect(page.locator('#message')).to_contain_text('Нажмите кнопку фото ещё раз')
                    pick('Сделать фото','camera-photo',[]);expect(page.locator('#message')).to_contain_text('Съёмка отменена')
                    page.evaluate('delete navigator.userActivation')
                    raw=raw_state()
                    page.locator('#camera-photo').evaluate("e=>{e.setAttribute('capture','environment');delete e.showPicker}")
                    for button,element in (('Сделать фото','camera-photo'),('Из галереи','photo')):
                        pick(button,element,{'name':'unsupported.heic','mimeType':'image/heic','buffer':b'DEMO unsupported'})
                        expect(page.locator('#message')).to_contain_text('JPEG/PNG/WebP до 10 МБ')
                        assert raw_state()==raw
                    pick('Сделать фото','camera-photo',{'name':'too-large.jpg','mimeType':'image/jpeg','buffer':b'0'*10000001})
                    expect(page.locator('#message')).to_contain_text('JPEG/PNG/WebP до 10 МБ')
                    assert raw_state()==raw
                    print('PASS: separate rear-camera/gallery controls; cancel + restart preserves dirty text; legacy chooser fallback; cross-picker dedup and common validation',flush=True)
                    page.locator('summary').click()
                    page.locator('[data-comment="demo-q1"]').fill('ДЕМО · unsaved text before camera quota failure')
                    chooser_events=[]
                    def unexpected_chooser(event):chooser_events.append(event)
                    page.on('filechooser',unexpected_chooser)
                    page.locator('#quota').check();page.get_by_role('button',name='Сделать фото',exact=True).click()
                    expect(page.locator('#message')).to_contain_text('хранилище заполнено')
                    page.remove_listener('filechooser',unexpected_chooser)
                    assert not chooser_events and raw_state()==raw
                    expect(page.locator('[data-comment="demo-q1"]')).to_have_value('ДЕМО · unsaved text before camera quota failure')
                    page.locator('[data-comment="demo-q1"]').fill('ДЕМО · Нет маркировки · offline-secret-marker')
                    page.locator('#save').click();expect(page.locator('#message')).to_contain_text('сохранены на устройстве')
                    raw=raw_state()
                    page.locator('#quota').check();pick('Сделать фото','camera-photo',image('red'))
                    expect(page.locator('#message')).to_contain_text('хранилище заполнено')
                    assert raw_state()==raw;expect(page.locator('#photos img')).to_have_count(2)
                    page.locator('#memory').check();pick('Из галереи','photo',image('red'))
                    expect(page.locator('#message')).to_contain_text('отказ бюджета памяти')
                    assert raw_state()==raw
                    # Configured capacity guard, distinct from a physical disk-full experiment.
                    budget_error=page.evaluate("""async phrase=>{const {Vault}=await import('/store.js');const v=new Vault(),s=await v.unlock(phrase);s.photos.push({id:crypto.randomUUID(),size:101*1024*1024,type:'image/jpeg'});try{await v.save(s);return 'NO_ERROR'}catch(e){return e.name}finally{v.lock()}}""",PHRASE)
                    assert budget_error=='QuotaExceededError',budget_error
                    assert raw_state()==raw
                    print('PASS: encrypted state, stable photo IDs, atomic transaction abort, configured 100 MiB budget guard, memory-budget simulation',flush=True)
                    pick('Из галереи','photo',image('green'));expect(page.locator('#photos img')).to_have_count(3)
                    gallery_ids=page.locator('.photo-meta').all_text_contents();assert gallery_ids[:len(ids)]==ids
                    ids=gallery_ids
                    context.close()
                    context=browser(True);page=context.pages[0];setup(page);page.clock.install(time=time.time());page.goto(origin)
                    page.locator('#phrase').fill('incorrect local phrase');page.locator('#open').click();expect(page.locator('#message')).to_contain_text('Неверная фраза')
                    page.locator('#phrase').fill(PHRASE);page.locator('#open').click();expect(page.locator('#photos img')).to_have_count(3)
                    expect(page.locator('[data-comment="demo-q1"]')).to_have_value('ДЕМО · Нет маркировки · offline-secret-marker')
                    assert page.locator('.photo-meta').all_text_contents()==ids
                    print('PASS: full browser restart with server stopped + offline; phrase required; draft and photo IDs restored',flush=True)
                    # A second tab cannot race the vault writer.
                    second=context.new_page();second.goto(origin);second.locator('#phrase').fill(PHRASE);second.locator('#open').click();expect(second.locator('#message')).to_contain_text('другой вкладке');second.close()
                    start('stage0-v2');context.set_offline(False)
                    page.locator('summary').click()
                    page.locator('#check-update').click();expect(page.locator('#update')).to_be_visible(timeout=15000)
                    page.locator('[data-comment="demo-q1"]').fill('ДЕМО · saved before SW update')
                    context.set_offline(True);page.locator('#update').click();expect(page.locator('#unlock')).to_be_visible()
                    page.wait_for_function("async()=>{const r=await navigator.serviceWorker.getRegistration();return r.active?.state==='activated'&&!r.waiting}")
                    page.reload();page.locator('#phrase').fill(PHRASE);page.locator('#open').click()
                    expect(page.locator('[data-comment="demo-q1"]')).to_have_value('ДЕМО · saved before SW update')
                    expect(page.locator('#photos img')).to_have_count(3)
                    stop();start('stage0-v3','/prototype.css');context.set_offline(False)
                    page.locator('summary').click()
                    page.locator('#check-update').click()
                    page.wait_for_timeout(2500)
                    assert not page.evaluate('(async()=>!!(await navigator.serviceWorker.getRegistration()).waiting)()')
                    stop();context.set_offline(True);page.reload();page.locator('#phrase').fill(PHRASE);page.locator('#open').click()
                    expect(page.locator('#photos img')).to_have_count(3)
                    cached=page.evaluate("async()=>{let a=[];for(const n of await caches.keys())for(const r of await (await caches.open(n)).keys())a.push(new URL(r.url).pathname);return a}")
                    assert '/demo-package.json' not in cached and '/demo-photo.png' not in cached and not any(x.startswith('/api/') for x in cached)
                    # Expiry is a local policy; no deletion on expiry. Clock rollback is a documented limitation.
                    page.locator('#minutes').fill('1');page.locator('#minutes').dispatch_event('change')
                    expect(page.locator('#message')).to_contain_text('Интервал автоблокировки: 1')
                    page.clock.fast_forward(60001);expect(page.locator('#unlock')).to_be_visible()
                    page.clock.set_system_time(time.time()+25*3600)
                    page.locator('#phrase').fill(PHRASE);page.locator('#open').click()
                    expect(page.locator('#package-status')).to_contain_text('24 часа истекли')
                    page.locator('[data-comment="demo-q1"]').fill('blocked after expiry')
                    expect(page.locator('#message')).to_contain_text('истёк')
                    expect(page.locator('#photos img')).to_have_count(3)
                    assert not errors,errors
                    output=Path(os.getenv('KSU_STAGE0_OUTPUT',str(Path(tempfile.gettempdir())/'ksu-stage0-output')));output.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(output/f'mobile-{backend}.png'),full_page=True)
                    context.close()
                    print('PASS: single-tab lock; configurable auto-lock (default 10m); update saved dirty draft; broken SW install retained old shell; no private Cache API data; expiry retains photos',flush=True)
            finally:
                stop();log.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=['sqlite','postgres'],default='sqlite');run(parser.parse_args().backend)
