/* Ready write-offs are separate source facts, not calculated inventory shortages. */
'use strict';
const decisionFields={sourceId:'ID строки',date:'Дата акта',site:'Объект',document:'Документ',decisionType:'Тип решения',toolType:'Тип инструмента',quantity:'Кол-во',cost:'Стоимость, ₽',residual:'Остаточная стоимость, ₽',reason:'Причина списания',caseCategory:'Категория случая',investigation:'Статус расследования',decision:'Результат / решение',reimbursable:'К возмещению, ₽',reimbursed:'Фактически возмещено, ₽',responsible:'Ответственное лицо / МОЛ',comment:'Комментарий'};
const decisionRequired=new Set(['date','site','document','decisionType','toolType','quantity','cost']);
const decisionView={page:1,site:'',data:null,loading:false,error:''};
let decisionJob=null,decisionMeta=null,decisionBusy=false,decisionDirty=false,decisionAbort=null;
navEntries.splice(navEntries.findIndex(x=>x[0]==='reports'),0,['decisions','Списания и решения','box']);
const renderBeforeDecisions=render;
render=function(){
  renderBeforeDecisions();
  if(ui.page!=='decisions'||!sessionUser)return;
  const data=decisionView.data,rows=data?.rows||[];
  $('.workspace').innerHTML=`${heading('Списания и решения','Готовые данные источника. Стоимость, остаточная стоимость и возмещение — разные показатели.',`<button class="btn primary" data-action="decision-import">Импорт XLSX до 30 МБ</button><a class="btn secondary" href="/api/decisions/export.csv?${attr(new URLSearchParams({site:decisionView.site}).toString())}">CSV реестра</a>`)}
  <div class="notice">Записи этого реестра не превращаются в первичную недостачу, утверждённый ущерб или факт инвентаризации автоматически. Сохранённые формулы не пересчитываются сервером.</div>
  <section class="card section-gap"><div class="field"><label>Объект</label><select data-decision-site><option value="">Все доступные</option>${db.sites.map(site=>`<option value="${attr(site.id)}" ${site.id===decisionView.site?'selected':''}>${esc(site.name)}</option>`).join('')}</select></div>
  <p role="status">${decisionView.loading?'Загрузка…':decisionView.error?esc(decisionView.error):`Всего записей: ${data?.total||0}. Страница ${decisionView.page}`}</p>
  ${rows.length?`<div class="table-wrap"><table class="table"><thead><tr>${['Дата','Объект','Документ','Тип решения','Тип инструмента','Кол-во','Стоимость, ₽','Остаточная, ₽','К возмещению, ₽','Возмещено, ₽','Причина','Действия'].map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${['date','site','document','decisionType','toolType','quantity','cost','residual','reimbursable','reimbursed','reason'].map(k=>`<td>${esc(row[k]??'—')}</td>`).join('')}<td><button class="link" data-action="decision-detail" data-id="${attr(row.id)}">Источник</button><button class="link" data-action="history" data-collection="decisions" data-id="${attr(row.id)}">История</button></td></tr>`).join('')}</tbody></table></div>`:empty('Нет импортированных решений')}
  <div class="actions-row section-gap"><button class="btn secondary" data-decision-page="${Math.max(1,decisionView.page-1)}" ${decisionView.page<=1?'disabled':''}>Назад</button><button class="btn secondary" data-decision-page="${decisionView.page+1}" ${!data||decisionView.page*50>=data.total?'disabled':''}>Далее</button></div></section>`;
  if(!data&&!decisionView.loading&&!decisionView.error)loadDecisions();
};
async function loadDecisions(){
  if(decisionView.loading)return;
  decisionView.loading=true;decisionView.error='';
  try{decisionView.data=await api('decisions?'+new URLSearchParams({page:decisionView.page,limit:50,site:decisionView.site}))}
  catch(error){decisionView.error=error.message}
  finally{decisionView.loading=false;render()}
}
async function decisionImport(){
  let resume=null;
  try{resume=await api('decision-imports')}catch(error){return toast(error.message,true)}
  decisionJob=resume?.id||null;decisionMeta=resume;decisionBusy=false;decisionDirty=false;
  openModal(`<div class="notice">Профиль: готовые списания и решения. Только XLSX до 30 МБ. Выберите входной лист, а не сводку. Для инвентаризации «учёт / факт» используйте отдельный импорт. Данные источника не смешиваются с первичными расхождениями.</div>
    <div class="field section-gap"><label>Книга XLSX</label><input id="decision-file" type="file" accept=".xlsx"></div>
    <form id="decision-settings"><div class="form-grid section-gap"><div class="field"><label>Лист</label><select name="sheet"><option value="0">Первый лист</option></select></div><div class="field"><label>Строка заголовков</label><input name="headerRow" type="number" min="1" max="99999" value="1"></div><div class="field"><label>Данные с Excel-строки</label><input name="firstRow" type="number" min="2" max="100000" value="2"></div><div class="field"><label>По Excel-строку включительно</label><input name="lastRow" type="number" min="2" max="100000" value="100000"></div></div>
    <label class="section-gap"><input name="allowCached" type="checkbox"> Использовать сохранённые в Excel результаты формул. Перед этим книгу нужно пересчитать и сохранить в Excel.</label><div id="decision-mapping" class="form-grid section-gap"></div></form>
    <p id="decision-progress" role="status">Файл остаётся в закрытом хранилище приложения на время обработки. В БД не записывается Base64.</p><div id="decision-preview"></div>`,
    'Импорт списаний и решений','Предпросмотр → исправление → атомарная запись',true,
    '<button class="btn secondary" data-action="decision-cancel">Отменить обработку</button><button class="btn secondary" data-action="decision-preview">Загрузить / проверить</button><button class="btn primary" data-action="decision-commit" disabled>Записать реестр</button>');  if(resume){
    $('#decision-file').disabled=true;
    const form=$('#decision-settings');
    for(const key of ['headerRow','firstRow','lastRow'])if(resume.settings?.[key])form.elements[key].value=resume.settings[key];
    form.elements.allowCached.checked=!!resume.settings?.allowCached;
    showDecisionMeta(resume);form.elements.sheet.value=resume.settings?.sheet??0;showDecisionMeta(resume);
    if(['inspecting','checking','committing'].includes(resume.status)){
      decisionBusy=true;waitDecision().catch(error=>toast(error.message,true)).finally(()=>{decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)});
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
  const commit=$('[data-action="decision-commit"]');if(commit)commit.disabled=decisionBusy||decisionDirty||meta.status!=='ready';
}
async function waitDecision(){
  for(let i=0;i<400;i++){
    const meta=await api('decision-imports/'+decisionJob);showDecisionMeta(meta);
    if(!['uploading','inspecting','checking','committing'].includes(meta.status))return meta;
    await new Promise(resolve=>setTimeout(resolve,800));
  }
  throw Error('Обработка ещё идёт. Не начинайте повторный импорт; проверьте статус позднее.');
}
async function previewDecision(){
  if(decisionBusy)return;
  decisionBusy=true;
  try{
    if(!decisionJob){
      const file=$('#decision-file')?.files[0];if(!file)throw Error('Выберите XLSX');
      if(!file.name.toLowerCase().endsWith('.xlsx')||file.size>30000000)throw Error('Нужен XLSX не более 30 МБ');
      $('#decision-progress').textContent='Загружаем файл…';decisionAbort=new AbortController();
      const response=await fetch('/api/decision-imports',{method:'POST',credentials:'same-origin',body:file,signal:decisionAbort.signal,
        headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':sessionUser.csrf,'X-KSU-Filename':encodeURIComponent(file.name)}});
      if(!response.ok){const error=await response.json();if(response.status===401){sessionUser=null;showLogin('Войдите заново перед загрузкой книги')}throw Error(error.detail||'Ошибка загрузки')}
      const uploaded=await response.json();decisionJob=uploaded.id;$('#decision-file').disabled=true;
      const inspected=await waitDecision();if(inspected.status!=='uploaded')return;
    }
    const settings=decisionSettings();decisionDirty=false;
    await api('decision-imports/'+decisionJob+'/preview',{method:'POST',body:JSON.stringify(settings)});
    await waitDecision();
  }catch(error){toast(error.name==='AbortError'?'Загрузка отменена':error.message,true)}
  finally{decisionBusy=false;decisionAbort=null;if(decisionMeta)showDecisionMeta(decisionMeta)}
}
document.addEventListener('change',event=>{
  if(event.target.closest('#decision-settings')){decisionDirty=true;const button=$('[data-action="decision-commit"]');if(button)button.disabled=true;
    if(event.target.name==='sheet'||event.target.name==='headerRow')$('#decision-mapping').innerHTML='';}
  if(event.target.matches('[data-decision-site]')){decisionView.site=event.target.value;decisionView.page=1;decisionView.data=null;loadDecisions()}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action],[data-decision-page]');if(!button)return;
  if(button.hasAttribute('data-decision-page')){decisionView.page=Number(button.dataset.decisionPage);decisionView.data=null;loadDecisions();return}
  const action=button.dataset.action;
  if(action==='decision-import')decisionImport();
  if(action==='decision-preview')await previewDecision();
  if(action==='decision-commit'&&!decisionBusy&&!decisionDirty){
    decisionBusy=true;button.disabled=true;
    try{await api('decision-imports/'+decisionJob+'/commit',{method:'POST'});const meta=await waitDecision();
      if(meta.status==='done'){decisionView.data=null;toast(meta.duplicate?'Дубли не созданы':'Реестр записан');}}
    catch(error){toast(error.message,true)}finally{decisionBusy=false;if(decisionMeta)showDecisionMeta(decisionMeta)}
  }
  if(action==='decision-cancel'){
    if(decisionAbort)decisionAbort.abort();
    try{if(decisionJob)await api('decision-imports/'+decisionJob,{method:'DELETE'});decisionBusy=false;closeModal();render()}
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
