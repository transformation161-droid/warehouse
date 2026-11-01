from playwright.sync_api import sync_playwright
from pathlib import Path
import subprocess, time
root=Path(__file__).resolve().parents[1]
server=subprocess.Popen(['python','-m','http.server','8765','--bind','127.0.0.1','--directory',str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
time.sleep(.7)
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1440,'height':900},locale='ru-RU',accept_downloads=True)
  problems=[]
  page.on('pageerror',lambda e:problems.append(str(e)))
  page.evaluate("""() => { const m={}; Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:k=>m[k]??null,setItem:(k,v)=>m[k]=String(v),removeItem:k=>delete m[k]}}); }""")
  page.set_content('<html lang="ru"><head><meta charset="utf-8"><style>'+root.joinpath('styles.css').read_text()+'</style></head><body><div id="app"></div><div id="dialog-root"></div><div id="toast-root"></div><script>'+root.joinpath('app.js').read_text()+'</script></body></html>')
  page.locator('[data-nav="schedule"]').click()
  assert page.get_by_text('План выездов',exact=True).count()>0
  page.get_by_role('button',name='Запланировать выезд').click()
  f=page.locator('#visit-form')
  f.locator('select[name="siteId"]').select_option('s1')
  f.locator('input[name="date"]').fill('2026-11-04')
  f.locator('input[name="assignee"]').fill('Эльман Мамедов')
  f.locator('textarea[name="goal"]').fill('Контроль маркировки')
  page.locator('[data-action="save-visit"]').click()
  assert page.get_by_text('Контроль маркировки').count()>=1
  print('Visit saved',page.locator('[data-action="start-audit"][data-visit]').count())
  page.locator('[data-action="start-audit"][data-visit]').click()
  assert page.locator('.question-card').count()==22
  for b in page.locator('button[data-answer][data-value="ok"]').all(): b.click()
  page.get_by_role('button',name='Сохранить проверку').click()
  page.locator('[data-nav="schedule"]').click()
  assert page.get_by_text('Выполнен',exact=True).count()>=1
  assert page.locator('[data-action="audit-detail"]').count()>=1
  print('Visit completed via inspection')
  page.locator('[data-action="weekly-export"]').count()
  page.locator('[data-nav="reports"]').click()
  with page.expect_download() as download_info:
   page.locator('[data-action="weekly-export"]').click()
  dl=download_info.value
  assert '.csv' in dl.suggested_filename
  print('Weekly CSV',dl.suggested_filename)
  assert not problems,problems
  print('JS errors',problems)
  browser.close()
finally:server.terminate()
