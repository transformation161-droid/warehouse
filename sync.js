/* Server boundary for the preserved v1.1 interface. No client-assigned roles. */
'use strict';
let sessionUser=null, pending=null, sending=false, connected=true, initialized=false;
const collections=['sites','questions','inspections','findings','inventories','inventoryLines','visits'];
const metadata=new Set(['id','version','createdBy','createdAt','updatedAt']);
const cleanRecord=x=>Object.fromEntries(Object.entries(x).filter(([k])=>!metadata.has(k)));
const stable=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
const lineMoney=(quantity,price)=>Math.round((quantity*Number(price)+Number.EPSILON)*100)/100;

async function api(path,options={}){
  const response=await fetch('/api/'+path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(sessionUser?{'X-CSRF-Token':sessionUser.csrf}:{}),...options.headers}});
  if(!response.ok){let error;try{error=await response.json()}catch{error={detail:'Сервер недоступен'}}
    if(response.status===401&&sessionUser){sessionUser=null;showLogin('Сессия завершилась. Войдите снова; неподтверждённые изменения сохранены для вашей учётной записи.')}
    const failure=Error(typeof error.detail==='string'?error.detail:'Проверьте введённые значения');failure.status=response.status;throw failure;
  }
  return response.json();
}

// Only the recovery queue, not the database, is retained on the device. It is isolated by account.
async function queueStore(value,read=false,kind='pending'){
  const user=sessionUser?.id;if(!user) return null;
  return new Promise((resolve,reject)=>{
    const open=indexedDB.open('ksu-recovery-v2',1);
    open.onupgradeneeded=()=>open.result.createObjectStore('pending');open.onerror=()=>reject(open.error);
    open.onsuccess=()=>{
      const database=open.result,tx=database.transaction('pending',read?'readonly':'readwrite'),store=tx.objectStore('pending');
      const key=user+':'+kind;
      const req=read?store.get(key):value?store.put(value,key):store.delete(key);
      let result=null;req.onsuccess=()=>result=req.result;
      tx.oncomplete=()=>{database.close();resolve(result)};tx.onerror=()=>{database.close();reject(tx.error)};
    };
  });
}

function showLogin(message=''){
  initialized=false;
  $('#dialog-root').innerHTML='';
  $('#app').innerHTML=`<main class="login-shell"><section class="card login-card"><div class="logo-mark">И</div><h1>ИНСТРАКТ · КСУ Контроль</h1><p class="muted-line">Совместная работа · демонстрационные данные</p><form id="login-form"><div class="field"><label for="username">Логин</label><input id="username" name="username" autocomplete="username" required maxlength="120"></div><div class="field section-gap"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="200"></div><p id="login-error" role="alert">${esc(message)}</p><button class="btn primary" type="submit">Войти</button></form><p class="tiny muted">Роль и доступ к объектам назначаются на сервере. Только тестовые данные.</p></section></main>`;
}

async function enter(){
  db=await api('state');ui.role=sessionUser.role;ui.page='home';ui.siteId=null;ui.wizard=false;ui.modal=null;
  pending=await queueStore(null,true);
  const draft=await queueStore(null,true,'draft');
  if(draft&&!pending){ui.draft=draft.data;ui.activeVisitId=draft.visit;ui.wizard=true;ui.page='audits'}
  connected=true;initialized=true;render();
  if(pending)toast('Есть неподтверждённые изменения. Повторите отправку или скачайте копию.',true);
}

document.addEventListener('submit',async e=>{
  if(e.target.id!=='login-form')return;e.preventDefault();
  const button=e.target.querySelector('button');button.disabled=true;
  try{sessionUser=await api('login',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await enter()}
  catch(error){const target=$('#login-error');if(target)target.textContent=error.message;button.disabled=false}
});

const originalRender=render;
render=function(){
  if(!sessionUser)return;
  originalRender();
  const note=document.createElement('div');note.id='sync-status';note.className='sync-status no-print';note.setAttribute('role','status');
  note.innerHTML=pending?`<strong>${sending?'Сохраняем…':'Изменения ещё не подтверждены сервером'}</strong> <button class="btn secondary" data-action="retry-save">Повторить</button><button class="btn secondary" data-action="recover-save">Скачать копию</button><button class="btn secondary" data-action="discard-save">Отменить отправку</button>`:connected?'Общая база · синхронизировано':'Нет связи · редактирование доступно с сохранением запроса для повторной отправки';
  $('.workspace').prepend(note);
  if(ui.role!=='manager')document.querySelectorAll('[data-action="add-site"],[data-action="edit-site"],[data-action="add-question"],[data-action="add-visit"],[data-action="edit-visit"],[data-action="cancel-visit"],[data-action="change-stage"]').forEach(x=>x.remove());
  document.querySelectorAll('[data-action="start-audit"][data-visit]').forEach(x=>{if(db.visits.find(v=>v.id===x.dataset.visit)?.assignedTo!==sessionUser.id)x.remove()});
  document.querySelectorAll('[data-action="finding-detail"]').forEach(x=>{const f=db.findings.find(f=>f.id===x.dataset.id);if(ui.role!=='manager'&&f?.createdBy!==sessionUser.id)x.textContent='Просмотр'});
  if(ui.page==='reports'){
    const actions=$('.page-actions');actions.insertAdjacentHTML('beforeend','<button class="btn secondary" data-action="report-xlsx">XLSX</button><button class="btn secondary" data-action="report-pdf">Скачать PDF</button>');
  }
};

async function uploadPhoto(photo,siteId){
  if(!photo?.startsWith('data:'))return photo||'';
  return (await api('media',{method:'POST',body:JSON.stringify({siteId,content:photo.split(',')[1]})})).url;
}

async function sendPending(){
  if(!pending||sending||!sessionUser)return;
  sending=true;render();
  try{
    // Persist the exact request before sending. Retries use the same mutation id.
    for(const change of pending.changes){
      if(change.data.photo)change.data.photo=await uploadPhoto(change.data.photo,change.data.siteId);
      for(const result of change.data.results||[])if(result.photo)result.photo=await uploadPhoto(result.photo,change.data.siteId);
    }
    await queueStore(pending);
    db=await api('changes',{method:'POST',body:JSON.stringify(pending)});
    pending=null;await queueStore(null);connected=true;toast('Изменения сохранены в общей базе');
  }catch(error){connected=!!error.status;toast(error.status===409?'Конфликт версий. Скачайте свою копию, отмените отправку и сравните с актуальной записью.':error.message||'Нет связи: запрос сохранён для повторной отправки',true)}
  finally{sending=false;render()}
}

async function mutate(fn,msg){
  if(!sessionUser||pending||sending)return toast('Сначала завершите отправку предыдущих изменений',true);
  const before=structuredClone(db),recovery={draft:structuredClone(ui.draft),invRows:structuredClone(ui.invRows),invMeta:structuredClone(ui.invMeta)};
  try{
    fn();const changes=[];
    for(const collection of collections){
      const old=new Map(before[collection].map(x=>[x.id,x]));
      for(const record of db[collection]){
        const previous=old.get(record.id);
        if(!previous||stable(cleanRecord(previous))!==stable(cleanRecord(record)))changes.push({collection,id:record.id,expectedVersion:previous?.version||0,data:cleanRecord(record)});
      }
    }
    db=before;
    if(!changes.length){render();return}
    pending={mutationId:crypto.randomUUID(),changes};
    await queueStore(pending);await sendPending();
  }catch(error){db=before;Object.assign(ui,recovery);toast('Не удалось сохранить копию на устройстве: '+error.message+'. Скачайте запрос перед закрытием страницы.',true);render()}
}

function downloadServerReport(extension,kind){
  const params=new URLSearchParams({...ui.report,...(kind?{kind}:{})});
  const link=document.createElement('a');link.href='/api/reports/'+extension+'?'+params;link.download='';document.body.append(link);link.click();link.remove();
}
exportReport=()=>downloadServerReport('csv');
exportWeeklyReport=()=>downloadServerReport('csv','weekly');
csvTemplate=()=>downloadCSV(['object','date','title','item','sku','category','unit','expected','actual','price','owner','reason'],[[db.sites[0]?.name,today(),'ДЕМО · Пересчёт','ДЕМО · Перфоратор','DEMO-001','Инструмент','шт.',3,2,8900,'ДЕМО · Кладовщик','Проверяется']],'КСУ_ДЕМО_шаблон.csv');

let importPayload=null;
function importCSVModal(){
  importPayload=null;
  openModal('<div class="notice">CSV UTF-8 или XLSX, до 3 МБ и 2000 строк. Сопоставьте колонки и проверьте предпросмотр. Ошибки блокируют запись всего файла; повтор того же файла не создаёт дубли.</div><div class="field section-gap"><label>Файл CSV / XLSX</label><input id="csv-upload" type="file" accept=".csv,.xlsx"></div><div id="import-preview" class="section-gap"></div>','Импорт инвентаризации','Только демонстрационные данные',true,'<button class="btn secondary" data-action="close-modal">Отмена</button><button class="btn secondary" data-action="process-csv">Проверить файл</button><button class="btn primary" data-action="commit-import" disabled>Сохранить импорт</button>');
}

async function processCSV(){
  const file=$('#csv-upload')?.files[0];if(!file)return toast('Выберите файл',true);
  if(file.size>3_000_000)return toast('Файл больше 3 МБ',true);
  try{
    const data=new Uint8Array(await file.arrayBuffer());let binary='';for(const byte of data)binary+=String.fromCharCode(byte);
    const mapping=Object.fromEntries([...document.querySelectorAll('[data-import-column]')].map(x=>[x.dataset.importColumn,x.value]).filter(([,v])=>v));
    importPayload={name:file.name,content:btoa(binary),...(Object.keys(mapping).length?{mapping}:{})};
    const result=await api('import/preview',{method:'POST',body:JSON.stringify(importPayload)});
    const keys=['object','date','item','expected','actual','price','title','sku','category','unit','owner','reason'];
    const labels={object:'Объект',date:'Дата',item:'ТМЦ',expected:'Учёт',actual:'Факт',price:'Цена'};
    $('#import-preview').innerHTML=`<div class="form-grid">${keys.map(key=>`<div class="field"><label>${esc(labels[key]||key)}</label><select data-import-column="${key}"><option value="">Не выбрано</option>${result.headers.map(header=>`<option value="${attr(header)}" ${(mapping[key]||key)===header?'selected':''}>${esc(header)}</option>`).join('')}</select></div>`).join('')}</div><p>Корректных строк: ${result.count}. Предпросмотр первых 20.</p>${result.missing.length?`<div class="notice danger">Сопоставьте: ${esc(result.missing.join(', '))}, затем нажмите «Проверить файл».</div>`:''}${result.errors.map(x=>`<p class="negative">Строка ${x.row}: ${esc(x.message)}</p>`).join('')}<div class="table-wrap"><table class="table"><thead><tr><th>Объект</th><th>ТМЦ</th><th>Учёт</th><th>Факт</th><th>Цена</th></tr></thead><tbody>${result.rows.map(x=>`<tr><td>${esc(x.object)}</td><td>${esc(x.item)}</td><td>${num(x.expected)}</td><td>${num(x.actual)}</td><td>${rub(x.price)}</td></tr>`).join('')}</tbody></table></div>`;
    $('[data-action="commit-import"]').disabled=!!(result.missing.length||result.errors.length||!result.count);
  }catch(error){toast(error.message,true)}
}

const originalFindingForm=findingForm;
findingForm=function(f){
  originalFindingForm(f);
  const form=$('#finding-form');form.insertAdjacentHTML('beforeend',`<input name="photo" type="hidden" value="${attr(f?.photo||'')}"><input name="evidence" type="hidden" value="${attr(f?.evidence||'')}"><div class="field section-gap"><label>Фото при выявлении</label><input type="file" accept="image/jpeg,image/png,image/webp" data-finding-upload="photo"></div><div class="field section-gap"><label>Фото-подтверждение устранения (обязательно для закрытия)</label><input type="file" accept="image/jpeg,image/png,image/webp" data-finding-upload="evidence">${f?.evidence?`<img class="photo-preview" src="${attr(f.evidence)}" alt="Подтверждение устранения">`:''}</div>`);
  if(f){$('.modal-footer').insertAdjacentHTML('afterbegin',`<button class="btn secondary" data-action="history" data-collection="findings" data-id="${attr(f.id)}">История</button>`)}
  if(f&&ui.role!=='manager'&&f.createdBy!==sessionUser.id){form.querySelectorAll('input,select,textarea').forEach(x=>x.disabled=true);$('[data-action="save-finding"]').remove()}
};

const stages={primary:'Первичное расхождение',confirmed:'Подтверждено',corrected:'Снято корректировкой',investigation:'Расследование',approved:'Утверждённый итог',recovered:'Взыскано'};
const stageNext={primary:['confirmed','corrected'],confirmed:['investigation','corrected'],investigation:['approved','corrected'],approved:['recovered'],corrected:[],recovered:[]};
const baseInventoryDetail=inventoryDetail;
inventoryDetail=function(id){
  baseInventoryDetail(id);const inv=db.inventories.find(x=>x.id===id);if(!inv)return;
  $('.modal-body').insertAdjacentHTML('afterbegin',`<div class="notice">Стадия: <strong>${esc(stages[inv.stage||'primary'])}</strong>${inv.decision?` · ${esc(inv.decision)}`:''}</div>`);
  $('.modal-footer').insertAdjacentHTML('afterbegin',`<button class="btn secondary" data-action="history" data-collection="inventories" data-id="${attr(id)}">История</button>${ui.role==='manager'&&(stageNext[inv.stage||'primary']||[]).length?`<button class="btn secondary" data-action="change-stage" data-id="${attr(id)}">Стадия расхождений</button>`:''}`);
};

async function showHistory(collection='',id=''){
  try{
    const entries=await api('history?'+new URLSearchParams({collection,record_id:id}));
    openModal(entries.map(entry=>`<article class="timeline-item"><strong>${esc(entry.actor)} · ${esc(entry.operation==='create'?'Создание':entry.operation==='import'?'Импорт':'Изменение')} · ${esc(entry.collection)}</strong><small>${esc(entry.at)} UTC · ${esc(entry.record_id)}</small><details><summary>Исходные и новые значения</summary><pre>${esc(JSON.stringify({до:entry.before,после:entry.after},null,2))}</pre></details></article>`).join('')||empty('Изменений нет'),'Журнал изменений','Последние 100 событий. Более ранние события доступны через API с параметром before.',true,'<button class="btn primary" data-action="close-modal">Закрыть</button>');
  }catch(error){toast(error.message,true)}
}

document.addEventListener('change',async e=>{
  if(e.target.matches('[data-import-column]')){const button=$('[data-action="commit-import"]');if(button)button.disabled=true}
  if(!e.target.matches('[data-finding-upload]'))return;
  const input=e.target,file=input.files[0],form=$('#finding-form');if(!file)return;
  if(file.size>3_000_000)return toast('Фото больше 3 МБ',true);
  const save=$('[data-action="save-finding"]');if(save)save.disabled=true;
  try{const reader=new FileReader();const photo=await new Promise((resolve,reject)=>{reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file)});
    form.elements[input.dataset.findingUpload].value=await uploadPhoto(photo,form.elements.siteId.value);toast('Фото загружено в закрытое хранилище')
  }catch(error){toast(error.message,true)}finally{if(save)save.disabled=false}
});

document.addEventListener('click',async e=>{
  const action=e.target.closest('[data-action]');if(!action)return;const a=action.dataset.action,id=action.dataset.id;
  if(a==='logout'){
    if(pending||sending)return toast('Сначала сохраните, скачайте или отмените неподтверждённые изменения',true);
    if(ui.wizard&&!confirm('Завершить сеанс? Несохранённый чек-лист будет закрыт.'))return;
    try{await api('logout',{method:'POST'});await queueStore(null,false,'draft');sessionUser=null;db=freshData(false);ui.draft=null;ui.modal=null;showLogin()}catch(error){toast(error.message,true)}
  }
  if(a==='retry-save')await sendPending();
  if(a==='recover-save'&&pending)downloadFile(JSON.stringify(pending,null,2),'КСУ_ДЕМО_неподтверждённые_изменения.json','application/json');
  if(a==='discard-save'&&!sending&&confirm('Отменить отправку? Сначала скачайте копию, если введённые данные нужны.')){pending=null;await queueStore(null);db=await api('state');render()}
  if(a==='history')await showHistory(action.dataset.collection||'',id||'');
  if(a==='report-xlsx')downloadServerReport('xlsx');
  if(a==='report-pdf')downloadServerReport('pdf');
  if(a==='commit-import'&&importPayload){
    if(pending||sending)return toast('Сначала завершите отправку изменений',true);
    action.disabled=true;
    try{const result=await api('import/commit',{method:'POST',body:JSON.stringify(importPayload)});db=result.state;closeModal();render();toast(result.duplicate?'Этот файл уже импортирован — дубли не созданы':'Импорт сохранён в общей базе')}
    catch(error){toast(error.message,true);action.disabled=false}
  }
  if(a==='change-stage'&&ui.role==='manager'){
    const inv=db.inventories.find(x=>x.id===id),next=stageNext[inv.stage||'primary'];
    openModal(`<form id="stage-form"><div class="field"><label>Следующая стадия</label><select name="stage">${selectOptions(next.map(x=>[x,stages[x]]),next[0])}</select></div><div class="field section-gap"><label>Основание решения *</label><textarea name="decision" required maxlength="4000"></textarea></div></form>`,'Стадия расхождений','Первичные суммы не являются установленным ущербом',false,`<button class="btn primary" data-action="save-stage" data-id="${attr(id)}">Сохранить решение</button>`);
  }
  if(a==='save-stage'){
    const form=$('#stage-form');if(!form?.reportValidity())return;
    const values=Object.fromEntries(new FormData(form));await mutate(()=>{Object.assign(db.inventories.find(x=>x.id===id),values);closeModal()});
  }
});

let polling=false;
setInterval(async()=>{
  if(!sessionUser||!initialized||sending||pending||ui.modal||ui.wizard||polling)return;
  polling=true;
  try{const state=await api('state');const changed=state.revision!==db.revision||!connected;db=state;connected=true;if(changed)render()}
  catch(error){if(connected){connected=false;render()}}
  finally{polling=false}
},3000);
let lastDraft='';
setInterval(async()=>{
  if(!sessionUser||!initialized||sending)return;
  const draft=ui.wizard&&ui.draft?{data:ui.draft,visit:ui.activeVisitId}:null;
  const serialized=stable(draft);
  if(serialized===lastDraft)return;
  try{await queueStore(draft,false,'draft');lastDraft=serialized}
  catch{toast('Не удалось сохранить черновик на устройстве. Не закрывайте страницу до отправки.',true)}
},1000);
window.addEventListener('beforeunload',e=>{if(pending||sending||ui.wizard||document.querySelector('.modal form')){e.preventDefault();e.returnValue=''}});
(async()=>{try{sessionUser=await api('me');await enter()}catch{showLogin()}})();
