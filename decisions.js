/* Ready write-offs are separate source facts, not calculated inventory shortages. */
'use strict';
const decisionFields={sourceId:'ID строки',date:'Дата акта',site:'Объект',document:'Документ',decisionType:'Тип решения',toolType:'Тип инструмента',quantity:'Кол-во',cost:'Стоимость, ₽',residual:'Остаточная стоимость, ₽',reason:'Причина списания',caseCategory:'Категория случая',investigation:'Статус расследования',decision:'Результат / решение',reimbursable:'К возмещению, ₽',reimbursed:'Фактически возмещено, ₽',responsible:'Ответственное лицо / МОЛ',comment:'Комментарий'};
const decisionRequired=new Set(['date','site','document','decisionType','toolType','quantity','cost']);
const decisionView={page:1,site:'',documentId:'',version:0,history:false,data:null,totals:null,revision:null,epoch:0,loading:false,error:''};
let decisionJob=null,decisionMeta=null,decisionBusy=false,decisionDirty=false,decisionAbort=null,decisionEpoch=0;
navEntries.splice(navEntries.findIndex(x=>x[0]==='reports'),0,['decisions','Списания и решения','box']);
const renderBeforeDecisions=render;
render=function(){
  renderBeforeDecisions();
  if(ui.page!=='decisions'||!sessionUser)return;
  if(decisionView.revision!==db.revision){decisionView.revision=db.revision;decisionView.data=null;decisionView.loading=false;++decisionView.epoch}
  const data=decisionView.data,rows=data?.rows||[];
  const syncNote=$('#sync-status');
  $('.workspace').innerHTML=`${heading('Списания и решения','Готовые данные источника. Стоимость, остаточная стоимость и возмещение — разные показатели.',`<button class="btn primary" data-action="decision-import">Импорт XLSX до 30 МБ</button><a class="btn secondary" href="/api/decisions/export.csv?${attr(new URLSearchParams({site:decisionView.site}).toString())}">CSV реестра</a>`)}
  <div class="notice">Записи этого реестра не превращаются в первичную недостачу, утверждённый ущерб или факт инвентаризации автоматически. Сохранённые формулы не пересчитываются сервером.</div>
  <section class="card section-gap"><div class="field"><label>Объект</label><select data-decision-site><option value="">Все доступные</option>${db.sites.map(site=>`<option value="${attr(site.id)}" ${site.id===decisionView.site?'selected':''}>${esc(site.name)}</option>`).join('')}</select></div>
  <label><input type="checkbox" data-decision-history ${decisionView.history?'checked':''}> Показать также прежние строки (архив)</label>
  ${decisionView.version?`<p class="notice">Историческая версия ${decisionView.version}. Обычный CSV содержит действующие строки.</p>`:''}
  ${decisionView.history||decisionView.version||decisionView.documentId?'<button class="btn secondary" data-action="decision-current">Вернуться к действующим</button>':''}
  ${decisionView.totals?`<p class="decision-totals">${decisionView.version?'Итоги выбранной версии':'Действующие итоги'}: ${['quantity','cost','residual','reimbursable','reimbursed'].map(k=>`${esc(decisionFields[k])}: ${esc(decisionView.totals.totals[k])}`).join(' · ')}</p>`:''}
  <p role="status">${decisionView.loading?'Загрузка…':decisionView.error?esc(decisionView.error):`Всего записей: ${data?.total||0}. Страница ${decisionView.page}`}</p>
  ${rows.length?`<div class="table-wrap"><table class="table"><thead><tr>${['Дата','Объект','Документ','Тип решения','Тип инструмента','Кол-во','Стоимость, ₽','Остаточная, ₽','К возмещению, ₽','Возмещено, ₽','Причина','Действия'].map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${['date','site','document','decisionType','toolType','quantity','cost','residual','reimbursable','reimbursed','reason'].map(k=>`<td>${esc(row[k]??'—')}</td>`).join('')}<td><small>${row.active?'Действующая':'Архивная'} · версия ${row.version||1}</small><button class="link" data-action="decision-detail" data-id="${attr(row.id)}">Источник</button><button class="link" data-action="history" data-collection="decisions" data-id="${attr(row.id)}">История</button>${row.documentId?`<button class="link" data-action="decision-versions" data-id="${attr(row.documentId)}">Версии</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('Нет импортированных решений')}
  <div class="actions-row section-gap"><button class="btn secondary" data-decision-page="${Math.max(1,decisionView.page-1)}" ${decisionView.page<=1?'disabled':''}>Назад</button><button class="btn secondary" data-decision-page="${decisionView.page+1}" ${!data||decisionView.page*50>=data.total?'disabled':''}>Далее</button></div></section>`;
  if(syncNote)$('.workspace').prepend(syncNote);
  if(!data&&!decisionView.loading&&!decisionView.error)loadDecisions();
};
async function loadDecisions(){
  const epoch=++decisionView.epoch;
  decisionView.loading=true;decisionView.error='';
  try{const params={site:decisionView.site,documentId:decisionView.documentId,version:decisionView.version};const [data,totals]=await Promise.all([api('decisions?'+new URLSearchParams({...params,page:decisionView.page,limit:50,history:decisionView.history})),api('decisions/totals?'+new URLSearchParams(params))]);if(epoch===decisionView.epoch){decisionView.data=data;decisionView.totals=totals}}
  catch(error){if(epoch===decisionView.epoch)decisionView.error=error.message}
  finally{if(epoch===decisionView.epoch){decisionView.loading=false;render()}}
}
async function decisionImport(){
  if(pending||sending)return toast('Сначала завершите отправку предыдущих изменений',true);
  const epoch=++decisionEpoch;
  let resume=null;
  try{resume=await api('decision-imports')}catch(error){return toast(error.message,true)}
  if(epoch!==decisionEpoch)return;
  decisionJob=resume?.id||null;decisionMeta=resume;decisionBusy=false;decisionDirty=false;
  openModal(`<div class="notice">Профиль: готовые списания и решения. Только XLSX до 30 МБ. Выберите входной лист, а не сводку. Для инвентаризации «учёт / факт» используйте отдельный импорт. Данные источника не смешиваются с первичными расхождениями.</div>
    <div class="field section-gap"><label>Книга XLSX</label><input id="decision-file" type="file" accept=".xlsx"></div>
    <form id="decision-settings"><div class="form-grid section-gap"><div class="field"><label>Лист</label><select name="sheet"><option value="0">Первый лист</option></select></div><div class="field"><label>Строка заголовков</label><input name="headerRow" type="number" min="1" max="99999" value="1"></div><div class="field"><label>Данные с Excel-строки</label><input name="firstRow" type="number" min="2" max="100000" value="2"></div><div class="field"><label>По Excel-строку включительно</label><input name="lastRow" type="number" min="2" max="100000" value="100000"></div></div>
    <label class="section-gap"><input name="allowCached" type="checkbox"> Использовать сохранённые в Excel результаты формул. Перед этим книгу нужно пересчитать и сохранить в Excel.</label><div id="decision-mapping" class="form-grid section-gap"></div></form>
    <p id="decision-progress" role="status">Файл остаётся в закрытом хранилище приложения на время обработки. В БД не записывается Base64.</p><div id="decision-preview"></div>`,
    'Импорт списаний и решений','Предпросмотр → исправление → атомарная запись',true,
    '<button class="btn secondary" data-action="decision-cancel">Отменить обработку</button><button class="btn secondary" data-action="decision-preview">Загрузить / проверить</button><button class="btn primary" data-action="decision-commit" disabled>Записать реестр</button>');
  installDecisionVersions(resume);
  if(resume){
    $('#decision-file').disabled=true;
    const form=$('#decision-settings');
    for(const key of ['headerRow','firstRow','lastRow'])if(resume.settings?.[key])form.elements[key].value=resume.settings[key];
    form.elements.allowCached.checked=!!resume.settings?.allowCached;
    showDecisionMeta(resume);form.elements.sheet.value=resume.settings?.sheet??0;showDecisionMeta(resume);
    if(['inspecting','checking','committing'].includes(resume.status)){
      decisionBusy=true;waitDecision().catch(error=>{if(epoch===decisionEpoch)toast(error.message,true)}).finally(()=>{if(epoch===decisionEpoch){decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)}});
    }
  }
}
function decisionSettings(){
  const form=$('#decision-settings');if(!form?.reportValidity())throw Error('Проверьте диапазон строк');
  return {sheet:Number(form.elements.sheet.value),headerRow:Number(form.elements.headerRow.value),firstRow:Number(form.elements.firstRow.value),lastRow:Number(form.elements.lastRow.value),allowCached:form.elements.allowCached.checked,
    mapping:Object.fromEntries([...form.querySelectorAll('[data-decision-map]')].filter(x=>x.value).map(x=>[x.dataset.decisionMap,x.value]))};
}
function showDecisionMeta(meta){
  decisionMeta=meta;
  const progress=$('#decision-progress');if(!progress)return;
  const labels={uploaded:'Книга загружена. Выберите лист и диапазон.',inspecting:'Проверяем структуру книги…',checking:'Проверяем строки, значения и формулы…',committing:'Записываем реестр одной транзакцией…',mapping:'Сопоставьте обязательные поля и повторите проверку.',ready:'Проверка пройдена. Можно записать реестр.',invalid:'Исправьте ошибки перед записью.',error:'Обработка не завершена.',done:meta.duplicate?'Повторный импорт: дубли не созданы.':'Импорт завершён.',cancelled:'Обработка отменена.'};
  progress.textContent=(labels[meta.status]||meta.status)+(meta.message?' '+meta.message:'');
  if(meta.sheets){
    const select=$('#decision-settings select[name="sheet"]');const current=select.value;
    select.innerHTML=meta.sheets.map(x=>`<option value="${x.index}">${esc(x.name)}</option>`).join('');select.value=current;
  }
  const area=$('#decision-mapping');
  const context=$('#decision-settings');
  if(meta.headers&&area&&!area.children.length&&Number(context.elements.sheet.value)===(meta.settings?.sheet??0)&&Number(context.elements.headerRow.value)===(meta.settings?.headerRow??1)){
    area.innerHTML=Object.entries(decisionFields).map(([field,label])=>`<div class="field"><label>${esc(label)}${decisionRequired.has(field)?' *':''}</label><select data-decision-map="${field}"><option value="">Не выбрано</option>${Object.entries(meta.headers).map(([col,name])=>`<option value="${attr(col)}" ${meta.mapping?.[field]===col?'selected':''}>${esc(col+' · '+name)}</option>`).join('')}</select></div>`).join('');
  }
  const preview=$('#decision-preview');
  preview.innerHTML=`${meta.processed!==undefined?`<p>Обработано: ${meta.processed}; принято: ${meta.accepted}; отклонено: ${meta.rejected}; пустых: ${meta.skipped||0}.</p>`:''}${meta.warning?`<div class="notice">${esc(meta.warning)}</div>`:''}${meta.missing?.length?`<p class="negative">Нужны колонки: ${esc(meta.missing.join(', '))}</p>`:''}
    ${(meta.errors||[]).map(x=>`<p class="negative">${esc(x.column||'')}${x.row}: ${esc(x.field||'')} — ${esc(x.reason)}. ${esc(x.fix)}</p>`).join('')}
    ${meta.errors?.length?`<a href="/api/decision-imports/${attr(meta.id)}/errors.csv" class="btn secondary">Все ошибки CSV</a>`:''}
    ${meta.rows?.length?`<div class="table-wrap"><table class="table"><thead><tr><th>Строка Excel</th><th>Объект</th><th>Документ</th><th>Тип</th><th>Кол-во</th><th>Стоимость, ₽</th><th>К возмещению, ₽</th><th>Возмещено, ₽</th></tr></thead><tbody>${meta.rows.map(x=>`<tr>${['row','site','document','toolType','quantity','cost','reimbursable','reimbursed'].map(k=>`<td>${esc(x[k]??'—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="tiny muted">Первые 20 строк; длинные тексты в предпросмотре сокращены, исходные значения сохраняются полностью.</p>`:''}`;
  const commit=$('[data-action="decision-commit"]');if(commit){const correction=versionMode()!=='initial';commit.disabled=decisionBusy||decisionDirty||meta.status!=='ready'||(correction&&(!meta.versionPlan?.ready||decisionVersionState.dirty));commit.textContent=correction?'Подтвердить версию':'Записать реестр'}
  showVersionSummary(meta);
}
async function waitDecision(ident=decisionJob){
  const epoch=decisionEpoch;
  for(let i=0;i<400;i++){
    if(epoch!==decisionEpoch)throw Object.assign(Error('Предыдущая обработка закрыта'),{name:'AbortError'});
    const meta=await api('decision-imports/'+ident);
    if(epoch!==decisionEpoch)throw Object.assign(Error('Предыдущая обработка закрыта'),{name:'AbortError'});
    showDecisionMeta(meta);
    if(!['uploading','inspecting','checking','committing'].includes(meta.status))return meta;
    await new Promise(resolve=>setTimeout(resolve,800));
  }
  throw Error('Обработка ещё идёт. Не начинайте повторный импорт; проверьте статус позднее.');
}
async function previewDecision(){
  if(decisionBusy)return;
  decisionBusy=true;const epoch=decisionEpoch;
  try{
    if(!decisionJob){
      const file=$('#decision-file')?.files[0];if(!file)throw Error('Выберите XLSX');
      if(!file.name.toLowerCase().endsWith('.xlsx')||file.size>30000000)throw Error('Нужен XLSX не более 30 МБ');
      $('#decision-progress').textContent='Загружаем файл…';decisionAbort=new AbortController();
      const response=await fetch('/api/decision-imports',{method:'POST',credentials:'same-origin',body:file,signal:decisionAbort.signal,
        headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':sessionUser.csrf,'X-KSU-Filename':encodeURIComponent(file.name)}});
      if(!response.ok){const error=await response.json();if(response.status===401){sessionUser=null;showLogin('Войдите заново перед загрузкой книги')}throw Error(error.detail||'Ошибка загрузки')}
      const uploaded=await response.json();
      if(epoch!==decisionEpoch){await api('decision-imports/'+uploaded.id,{method:'DELETE'});return}
      decisionJob=uploaded.id;$('#decision-file').disabled=true;
      const inspected=await waitDecision();if(inspected.status!=='uploaded')return;
    }
    const settings=decisionSettings();decisionDirty=false;
    await api('decision-imports/'+decisionJob+'/preview',{method:'POST',body:JSON.stringify(settings)});
    await waitDecision();
  }catch(error){if(epoch===decisionEpoch)toast(error.name==='AbortError'?'Загрузка отменена':error.message,true)}
  finally{if(epoch===decisionEpoch){decisionBusy=false;decisionAbort=null;if(decisionMeta)showDecisionMeta(decisionMeta)}}
}
document.addEventListener('change',event=>{
  if(event.target.closest('#decision-settings')){decisionDirty=true;const button=$('[data-action="decision-commit"]');if(button)button.disabled=true;
    if(event.target.name==='sheet'||event.target.name==='headerRow')$('#decision-mapping').innerHTML='';}
  if(event.target.matches('[data-decision-site]')){decisionView.site=event.target.value;decisionView.page=1;decisionView.data=null;loadDecisions()}
  if(event.target.matches('[data-decision-history]')){decisionView.history=event.target.checked;decisionView.version=0;decisionView.page=1;decisionView.data=null;loadDecisions()}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action],[data-decision-page]');if(!button)return;
  if(button.hasAttribute('data-decision-page')){decisionView.page=Number(button.dataset.decisionPage);decisionView.data=null;loadDecisions();return}
  const action=button.dataset.action;
  if(action==='decision-import')decisionImport();
  if(action==='decision-preview')await previewDecision();
  if(action==='decision-commit'&&!decisionBusy&&!decisionDirty){
    if(versionMode()!=='initial'){try{await confirmDecisionVersion()}catch(error){toast(error.message,true)}return}
    decisionBusy=true;button.disabled=true;const epoch=decisionEpoch;
    try{await api('decision-imports/'+decisionJob+'/commit',{method:'POST'});const meta=await waitDecision();
      if(meta.status==='done'){decisionView.data=null;toast(meta.duplicate?'Дубли не созданы':'Реестр записан');}}
    catch(error){if(epoch===decisionEpoch)toast(error.message,true)}finally{if(epoch===decisionEpoch){decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)}}
  }
  if(action==='decision-cancel'){
    const epoch=++decisionEpoch,ident=decisionJob;
    if(decisionAbort)decisionAbort.abort();decisionAbort=null;
    try{if(ident)await api('decision-imports/'+ident,{method:'DELETE'});if(epoch!==decisionEpoch)return;decisionBusy=false;decisionJob=null;decisionMeta=null;decisionView.data=null;closeModal();render()}
    catch(error){toast(error.message,true)}
  }
  if(action==='decision-detail'){
    try{const row=await api('decisions/'+button.dataset.id);openModal(`<p>Лист: ${esc(row.sheet)}, строка Excel: ${row.sourceRow}.</p><div class="table-wrap"><table class="table">${Object.entries(decisionFields).map(([key,label])=>`<tr><th>${esc(label)}</th><td>${esc(row.data[key]??'—')}</td></tr>`).join('')}</table></div><details><summary>Происхождение значений и формулы источника</summary><pre>${esc(JSON.stringify({поля:row.data.sourceCells,форматы:row.importStyles,прочиеКолонки:row.data.unmappedCells},null,2))}</pre></details>`,'Источник записи','Исходные показатели сохранены отдельно, без квалификации ущерба',true)}catch(error){toast(error.message,true)}
  }
});

// Keep the old small inventory format explicit; ready decisions use the streaming profile.
const smallInventoryImport=importCSVModal;
importCSVModal=function(){
  openModal('<p>Выберите смысл данных файла.</p><p>Готовые списания, решения, стоимость и возмещение сохраняются отдельным реестром. Учёт и факт инвентаризации требуют другого набора колонок.</p>',
    'Профиль импорта','Выбор предотвращает подмену исходных показателей',false,
    '<button class="btn secondary" data-action="small-inventory-import">Учёт / факт (старый формат до 3 МБ)</button><button class="btn primary" data-action="decision-import">Списания и решения (XLSX до 30 МБ)</button>');
};
document.addEventListener('click',event=>{if(event.target.closest('[data-action="small-inventory-import"]'))smallInventoryImport()});

// Version selection is explicit. SHA and physical Excel positions never choose a document.
let decisionVersionState={dirty:true,selected:new Set(),matches:{},oldRows:[],oldPage:1,inputPage:1,comparePage:1};
function versionMode(){return $('#decision-version-mode')?.value||'initial'}
function installDecisionVersions(resume){
  decisionVersionState={dirty:!resume?.versionPlan,selected:new Set(resume?.versionSettings?.targetIds||[]),matches:{...(resume?.versionSettings?.matches||{})},oldRows:[],oldPage:1,inputPage:1,comparePage:1};
  if(sessionUser.role!=='manager')return;
  $('#decision-settings').insertAdjacentHTML('afterend',`<fieldset id="decision-version-controls" class="card section-gap"><legend>Назначение книги — руководитель</legend>
    <div class="field"><label for="decision-version-mode">Операция</label><select id="decision-version-mode"><option value="initial">Новый документ</option><option value="full">Полностью заменить выбранный документ</option><option value="partial">Исправить выбранные строки документа</option></select></div>
    <div id="decision-version-target" hidden><p class="notice">Сравнение по объекту + «Документ» + исходному ID. При пустых/повторяющихся ID нужно ручное сопоставление. В полном замещении отсутствующие строки исключаются; при частичном остальные строки сохраняются.</p>
      <div class="form-grid"><div class="field"><label for="version-search">Найти документ по названию</label><input id="version-search"><button type="button" class="btn secondary" data-action="version-search">Найти документы</button></div><div class="field"><label for="version-document">Заменяемый документ</label><select id="version-document"><option value="">Выберите документ</option></select></div></div>
      <div class="field"><label for="version-reason">Основание исправления</label><textarea id="version-reason" maxlength="1200" placeholder="Почему первоисточник исправлен"></textarea></div>
      <details id="version-old-details"><summary>Прежние строки и выбор частичной коррекции</summary><div id="version-old-rows"></div></details><details id="version-input-details"><summary>Ручное сопоставление строк исправленной книги</summary><div id="version-input-rows"></div></details>
      <button type="button" class="btn secondary" data-action="version-compare">Сравнить версии</button>
    </div><div id="version-summary"></div><div id="version-comparison"></div></fieldset>`);
  if(resume?.versionSettings){$('#decision-version-mode').value=resume.versionSettings.mode;$('#version-reason').value=resume.versionSettings.reason;$('#decision-version-target').hidden=false}
  loadVersionDocuments(resume?.versionSettings?.documentId).catch(error=>toast(error.message,true));
}
async function loadVersionDocuments(selected=''){
  const epoch=decisionEpoch,query=$('#version-search')?.value||'';
  const result=await api('decision-documents?'+new URLSearchParams({q:query,limit:50}));
  if(epoch!==decisionEpoch||!$('#version-document'))return;
  const current=selected||$('#version-document').value;
  $('#version-document').innerHTML='<option value="">Выберите документ</option>'+result.rows.map(doc=>`<option value="${attr(doc.id)}">${esc(db.sites.find(s=>s.id===doc.site_id)?.name||doc.site_id)} · ${esc(doc.label)} · версия ${doc.version} · ${doc.active_rows} строк · ${esc(doc.created_at.slice(0,10))} · ${esc(doc.id.slice(0,8))}</option>`).join('');
  if(current&&!result.rows.some(d=>d.id===current))$('#version-document').insertAdjacentHTML('beforeend',`<option value="${attr(current)}">Выбранный документ ${esc(current.slice(0,8))}</option>`);
  $('#version-document').value=current;
  if(current){await loadVersionOldRows();if(decisionMeta?.versionPlan&&decisionMeta.status==='ready')await loadVersionComparison()}
}
async function loadVersionOldRows(){
  const id=$('#version-document')?.value,epoch=decisionEpoch;if(!id)return;
  const result=await api('decisions?'+new URLSearchParams({documentId:id,page:decisionVersionState.oldPage,limit:50}));
  if(epoch!==decisionEpoch||$('#version-document')?.value!==id)return;
  decisionVersionState.oldRows=result.rows;
  $('#version-old-rows').innerHTML=`<h3>Действующие строки документа</h3><p>${result.total} строк. Для частичной коррекции отметьте заменяемые или исключаемые строки; остальные сохранятся.</p>
    <div class="table-wrap"><table class="table"><thead><tr><th>Выбор</th><th>Исходный ID</th><th>Тип инструмента</th><th>Стоимость, ₽</th><th>ID записи</th></tr></thead><tbody>${result.rows.map(row=>`<tr><td><input aria-label="Заменить ${attr(row.sourceId||row.id)}" type="checkbox" data-version-old="${attr(row.id)}" ${decisionVersionState.selected.has(row.id)?'checked':''} ${versionMode()==='full'?'disabled':''}></td><td>${esc(row.sourceId||'Пустой ID')}</td><td>${esc(row.toolType)}</td><td>${esc(row.cost)}</td><td>${esc(row.id)}</td></tr>`).join('')}</tbody></table></div><div class="actions-row"><button class="btn secondary" data-version-old-page="${decisionVersionState.oldPage-1}" ${decisionVersionState.oldPage<=1?'disabled':''}>Предыдущие строки</button><button class="btn secondary" data-version-old-page="${decisionVersionState.oldPage+1}" ${decisionVersionState.oldPage*50>=result.total?'disabled':''}>Следующие строки</button></div>`;
  if(decisionMeta?.status==='ready')await loadVersionInputs();
}
async function loadVersionInputs(){
  const epoch=decisionEpoch,ident=decisionJob;if(!ident||!$('#version-input-rows')||versionMode()==='initial')return;
  const result=await api('decision-imports/'+ident+'/version-inputs?'+new URLSearchParams({page:decisionVersionState.inputPage,limit:20}));
  if(epoch!==decisionEpoch||ident!==decisionJob||!$('#version-input-rows'))return;
  $('#version-input-rows').innerHTML=`<h3>Строки исправленной книги</h3><p>Сопоставление по ключу используется только внутри выбранного документа. Можно явно выбрать прежнюю запись или добавление. Excel-строка здесь — указатель в загруженном файле.</p>
    ${result.rows.map(row=>{const chosen=decisionVersionState.matches[row.row]??'__auto';return `<div class="field section-gap"><label>Excel ${row.row} · ID ${esc(row.sourceId||'пустой')} · ${esc(row.toolType)} · ${esc(row.cost)} ₽</label><select aria-label="Сопоставление Excel ${row.row}" data-version-match="${row.row}"><option value="__auto">По согласованному ключу</option><option value="">Явно добавить новую строку</option>${decisionVersionState.oldRows.map(old=>`<option value="${attr(old.id)}">Прежняя: ${esc(old.sourceId||'пустой ID')} · ${esc(old.cost)} ₽ · ${esc(old.id.slice(0,8))}</option>`).join('')}${chosen!=='__auto'&&chosen&&!decisionVersionState.oldRows.some(r=>r.id===chosen)?`<option value="${attr(chosen)}">Выбрана запись ${esc(chosen)}</option>`:''}</select></div>`}).join('')}
    <div class="actions-row"><button class="btn secondary" data-version-input-page="${decisionVersionState.inputPage-1}" ${decisionVersionState.inputPage<=1?'disabled':''}>Предыдущие новые</button><button class="btn secondary" data-version-input-page="${decisionVersionState.inputPage+1}" ${decisionVersionState.inputPage*20>=result.total?'disabled':''}>Следующие новые</button></div>`;
  for(const select of document.querySelectorAll('[data-version-match]'))select.value=decisionVersionState.matches[select.dataset.versionMatch]??'__auto';
}
function showVersionSummary(meta){
  const fieldset=$('#decision-version-controls');if(!fieldset)return;fieldset.disabled=decisionBusy;
  $('#version-old-details').open=versionMode()==='partial';
  const area=$('#version-summary'),plan=meta.versionPlan;
  const inputKey=meta.id+':'+JSON.stringify(meta.settings);
  if(meta.status==='ready'&&versionMode()!=='initial'&&decisionVersionState.inputsFor!==inputKey){decisionVersionState.inputsFor=inputKey;loadVersionInputs().catch(error=>toast(error.message,true))}
  if(!plan){area.innerHTML='';return}
  if(plan.conflicts.length)$('#version-input-details').open=true;
  const summary=plan.summary,labels={added:'Добавлено',changed:'Изменено',removed:'Исключено',unchanged:'Без изменений'};
  area.innerHTML=`<h3>Сравнение: версия ${summary.version-1} → ${summary.version}</h3><p>${Object.entries(summary.counts).map(([k,v])=>`${labels[k]}: ${v}`).join(' · ')}</p>
    <p>Строк до: ${summary.beforeRows}; после: ${summary.afterRows}.</p><div class="table-wrap"><table class="table"><thead><tr><th>Показатель</th><th>До</th><th>После</th></tr></thead><tbody>${Object.keys(summary.before).map(k=>`<tr><th>${esc(decisionFields[k])}</th><td>${esc(summary.before[k])}</td><td>${esc(summary.after[k])}</td></tr>`).join('')}</tbody></table></div>
    ${plan.conflicts.map(c=>`<p class="negative">Excel ${c.row}: ${esc(c.reason)}</p>`).join('')}
    <p class="notice">${decisionVersionState.dirty?'Выбор изменён: повторите сравнение.':plan.ready?'Проверьте различия ниже и подтвердите версию отдельной кнопкой. Старые строки останутся в истории.':'Разрешите неоднозначности вручную и повторите сравнение.'}</p>`;
}
async function loadVersionComparison(){
  const epoch=decisionEpoch,ident=decisionJob;if(!ident||!$('#version-comparison'))return;
  const result=await api('decision-imports/'+ident+'/comparison?'+new URLSearchParams({page:decisionVersionState.comparePage,limit:20}));
  if(epoch!==decisionEpoch||ident!==decisionJob||!$('#version-comparison'))return;
  const labels={added:'Добавлена',changed:'Изменена',removed:'Исключена',unchanged:'Без изменений'};
  $('#version-comparison').innerHTML=result.rows.map(row=>`<details class="section-gap"><summary>${labels[row.kind]} · ID ${esc(row.after?.sourceId||row.before?.sourceId||'пустой')} · ${esc(row.before?.cost??'—')} → ${esc(row.after?.cost??'—')} ₽</summary><div class="table-wrap"><table class="table"><thead><tr><th>Поле</th><th>До</th><th>После</th></tr></thead><tbody>${Object.entries(decisionFields).map(([k,label])=>`<tr><th>${esc(label)}${row.fields.includes(k)?' *':''}</th><td>${esc(row.before?.[k]??'—')}</td><td>${esc(row.after?.[k]??'—')}</td></tr>`).join('')}</tbody></table></div></details>`).join('')+`<div class="actions-row"><button class="btn secondary" data-version-compare-page="${decisionVersionState.comparePage-1}" ${decisionVersionState.comparePage<=1?'disabled':''}>Предыдущие различия</button><button class="btn secondary" data-version-compare-page="${decisionVersionState.comparePage+1}" ${decisionVersionState.comparePage*20>=result.total?'disabled':''}>Следующие различия</button></div>`;
}
async function compareDecisionVersion(){
  if(decisionBusy||decisionDirty||decisionMeta?.status!=='ready')throw Error('Сначала загрузите и проверьте книгу');
  const id=$('#version-document').value,reason=$('#version-reason').value.trim();if(!id||!reason)throw Error('Выберите документ и укажите основание исправления');
  const versions=await api('decision-documents/'+id+'/versions'),epoch=decisionEpoch;
  const settings={documentId:id,expectedVersion:versions[0].number,mode:versionMode(),reason,targetIds:[...decisionVersionState.selected],matches:decisionVersionState.matches};
  decisionBusy=true;
  try{await api('decision-imports/'+decisionJob+'/version-preview',{method:'POST',body:JSON.stringify(settings)});const meta=await waitDecision();if(meta.status==='ready'){decisionVersionState.dirty=false;decisionVersionState.comparePage=1;showDecisionMeta(meta);await loadVersionComparison()}}
  finally{if(epoch===decisionEpoch){decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)}}
}
async function confirmDecisionVersion(){
  if(decisionVersionState.dirty||!decisionMeta?.versionPlan?.ready)throw Error('Повторите сравнение версий');
  decisionBusy=true;const epoch=decisionEpoch;
  try{await api('decision-imports/'+decisionJob+'/version-confirm',{method:'POST',body:JSON.stringify({token:decisionMeta.versionPlan.token})});const meta=await waitDecision();if(meta.status==='done'){decisionView.data=null;decisionView.revision=null;toast('Версия подтверждена. В итогах только действующие строки')}}
  catch(error){if(epoch===decisionEpoch)toast(error.message,true)}
  finally{if(epoch===decisionEpoch){decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)}}
}
document.addEventListener('change',event=>{
  if(!event.target.closest('#decision-version-controls'))return;
  decisionVersionState.dirty=true;
  if(event.target.id==='decision-version-mode'){$('#decision-version-target').hidden=versionMode()==='initial';if(versionMode()==='initial')decisionDirty=true;else loadVersionOldRows().catch(error=>toast(error.message,true))}
  if(event.target.id==='version-document'){decisionVersionState.selected.clear();decisionVersionState.matches={};decisionVersionState.oldPage=1;loadVersionOldRows().catch(error=>toast(error.message,true))}
  if(event.target.matches('[data-version-old]')){const id=event.target.dataset.versionOld;event.target.checked?decisionVersionState.selected.add(id):decisionVersionState.selected.delete(id)}
  if(event.target.matches('[data-version-match]')){const row=event.target.dataset.versionMatch;if(event.target.value==='__auto')delete decisionVersionState.matches[row];else{decisionVersionState.matches[row]=event.target.value;if(event.target.value&&versionMode()==='partial')decisionVersionState.selected.add(event.target.value)}}
  if(decisionMeta)showDecisionMeta(decisionMeta);
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action],[data-version-old-page],[data-version-input-page],[data-version-compare-page]');if(!button)return;
  try{
    if(button.dataset.action==='version-search')await loadVersionDocuments();
    if(button.dataset.action==='version-compare')await compareDecisionVersion();
    if(button.hasAttribute('data-version-old-page')){decisionVersionState.oldPage=Number(button.dataset.versionOldPage);await loadVersionOldRows()}
    if(button.hasAttribute('data-version-input-page')){decisionVersionState.inputPage=Number(button.dataset.versionInputPage);await loadVersionInputs()}
    if(button.hasAttribute('data-version-compare-page')){decisionVersionState.comparePage=Number(button.dataset.versionComparePage);await loadVersionComparison()}
    if(button.dataset.action==='decision-current'){Object.assign(decisionView,{documentId:'',version:0,history:false,page:1,data:null});await loadDecisions()}
    if(button.dataset.action==='decision-versions'){
      const versions=await api('decision-documents/'+button.dataset.id+'/versions');
      openModal(versions.map(v=>`<article class="timeline-item"><strong>Версия ${v.number} · ${esc(v.actor)}</strong><p>${esc(v.reason)} · ${esc(v.created_at)}</p>${v.summary.before?`<p>Стоимость: ${esc(v.summary.before.cost)} → ${esc(v.summary.after.cost)} ₽; добавлено ${v.summary.counts.added}, изменено ${v.summary.counts.changed}, исключено ${v.summary.counts.removed}.</p>`:'<p>Первичный состав документа</p>'}<button class="btn secondary" data-action="decision-show-version" data-id="${attr(v.document_id)}" data-version="${v.number}">Показать строки версии ${v.number}</button></article>`).join(''),'Версии документа','Прежние данные сохранены; обычные итоги учитывают действующий состав',true);
    }
    if(button.dataset.action==='decision-show-version'){Object.assign(decisionView,{documentId:button.dataset.id,version:Number(button.dataset.version),history:false,page:1,data:null});closeModal();await loadDecisions()}
  }catch(error){toast(error.message,true)}
});
