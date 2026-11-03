/* Provisional management view. All values, filters and chart scales come from the server. */
'use strict';
navEntries.splice(navEntries.findIndex(x=>x[0]==='reports'),0,['writeoff-analytics','Списания и аналитика','chart']);
pages['writeoff-analytics']='Списания и аналитика';
const waDimensions={toolType:'Вид инструмента',reason:'Причина списания',decisionType:'Тип решения',responsible:'МОЛ / ответственное лицо',investigation:'Статус расследования',caseCategory:'Категория случая'};
const waDefaults={start:'',end:'',site:'',toolType:'',reason:'',decisionType:'',responsible:'',investigation:'',caseCategory:'',dateMode:'all',month:'',documentId:'',metricField:'',coverage:'all',chartMetric:'cost'};
const waView={filters:{...waDefaults},data:null,error:'',loading:false,epoch:0,page:1,scope:'',identity:'',exporting:false,filtersOpen:!window.matchMedia('(max-width:700px)').matches};
const renderBeforeWriteoffAnalytics=render;
function waFormat(value){
  if(value===null||value===undefined)return 'Не указано';
  const [integer,fraction]=String(value).split('.');return integer.replace(/\B(?=(\d{3})+(?!\d))/g,' ')+(fraction!==undefined?','+fraction:'');
}
function waMetric(key,value,definitions){return waFormat(value?.value)+(value?.value!==null&&value?.value!==undefined?' '+definitions[key].unit:'')}
function waCoverage(value){return value.missingRows?`По заполненным: ${value.knownRows}; нет значения: ${value.missingRows}${value.invalidRows?`; некорректно: ${value.invalidRows}`:''}`:`Заполнено строк: ${value.knownRows}`}
function waParams(extra={}){return new URLSearchParams({...waView.filters,page:waView.page,limit:25,...extra})}
function waScope(){return sessionUser?JSON.stringify([sessionUser.id,sessionUser.role,sessionUser.csrf,db.revision,db.sites.map(s=>s.id)]):''}
setInterval(()=>{if(sessionUser&&ui.page==='writeoff-analytics'&&waScope()!==waView.scope)render();else if(!sessionUser&&waView.scope){waView.data=null;waView.scope='';waView.epoch++}},2000);
render=function(){
  renderBeforeWriteoffAnalytics();
  if(ui.page!=='writeoff-analytics'||!sessionUser)return;
  const identity=sessionUser.id+sessionUser.csrf;if(identity!==waView.identity){waView.identity=identity;waView.filters={...waDefaults};waView.page=1}
  const scope=waScope();if(scope!==waView.scope){waView.scope=scope;waView.data=null;waView.loading=false;waView.error='';waView.epoch++}
  const data=waView.data,f=waView.filters,definitions=data?.definitions;
  $('.workspace').innerHTML=`${heading('Списания и аналитика','Готовые результаты источника. Стоимость, остаточная стоимость и возмещение показаны отдельно.',
    '<button class="btn secondary" data-wa-action="registry">Реестр и импорт</button><button class="btn secondary" data-wa-action="refresh">Обновить</button>')}
    <div class="notice wa-provisional" role="note">Предварительная управленческая витрина. Не официальная отчётность: бизнес-ключ идентификации документов и исторические пересечения требуют проверки владельцем процесса. Только действующие версии документов; суммы не являются автоматически установленным ущербом.</div>
    <details class="card section-gap wa-filter-panel" ${waView.filtersOpen?'open':''}><summary>Фильтры показателей, графиков и строк</summary><form id="wa-filters"><div class="form-grid">
      <div class="field"><label for="wa-start">Период с (дата акта)</label><input id="wa-start" name="start" type="date" value="${attr(f.start)}"></div>
      <div class="field"><label for="wa-end">По дату включительно</label><input id="wa-end" name="end" type="date" value="${attr(f.end)}"></div>
      <div class="field"><label for="wa-site">Объект</label><select id="wa-site" name="site"><option value="">Все доступные</option>${db.sites.map(s=>`<option value="${attr(s.id)}" ${f.site===s.id?'selected':''}>${esc(s.name)}</option>`).join('')}</select></div>
      <div class="field"><label for="wa-dateMode">Полнота даты</label><select id="wa-dateMode" name="dateMode">${[['all','Все, включая без даты'],['dated','Только с корректной датой'],['missing','Без корректной даты']].map(([k,v])=>`<option value="${k}" ${f.dateMode===k?'selected':''}>${v}</option>`).join('')}</select></div>
      ${Object.entries(waDimensions).map(([key,label])=>`<div class="field"><label for="wa-${key}">${label}</label><input id="wa-${key}" name="${key}" list="wa-options-${key}" value="${attr(f[key])}" maxlength="500" placeholder="Любое; либо точное значение"><datalist id="wa-options-${key}">${(data?.facets[key].values||[]).filter(v=>v!==null).map(v=>`<option value="${attr(v)}"></option>`).join('')}</datalist><label class="wa-missing"><input type="checkbox" name="${key}Missing" ${f[key+'Missing']?'checked':''}> Не указано</label>${data?.facets[key].truncated?'<small>Первые 100 вариантов; можно ввести другое точное значение.</small>':''}</div>`).join('')}
      <div class="field"><label for="wa-chartMetric">Показатель на графиках</label><select id="wa-chartMetric" name="chartMetric">${Object.entries(definitions||{cost:{label:'Стоимость списаний'}}).map(([key,v])=>`<option value="${key}" ${f.chartMetric===key?'selected':''}>${esc(v.label)}</option>`).join('')}</select></div>
    </div><div class="actions-row section-gap"><button class="btn primary" type="submit">Применить фильтры</button><button class="btn secondary" type="button" data-wa-action="reset">Сбросить всё</button></div></form></details>
    <p class="tiny wa-applied">Срез: ${esc(waFilterLabel(f))}</p>
    <p class="wa-status" role="status">${waView.loading?'Загрузка серверного среза…':waView.error?esc(waView.error):data?`Действующих строк: ${data.totalRows}; страница ${data.page}.`:'Нет загруженных данных'}</p>
    ${f.month||f.documentId||f.metricField?`<p class="notice wa-drill">Детализация: ${esc(f.month||'')}${f.documentId?' документ '+esc(f.documentId.slice(0,8)):''}${f.metricField?' '+esc(definitions?.[f.metricField]?.label||f.metricField)+' · '+(f.coverage==='missing'?'неизвестные значения':'заполненные значения'):''} <button class="link" data-wa-action="clear-drill">Снять детализацию</button></p>`:''}
    ${data&&!waView.loading&&!waView.error?waDashboard(data):''}`;
  if(!waView.data&&!waView.loading&&!waView.error)waLoad();
};
function waDashboard(data){
  const defs=data.definitions;
  const cards=Object.entries(defs).map(([key,definition])=>{const metric=data.metrics[key];return `<article class="card wa-kpi" data-wa-kpi="${key}"><h3>${esc(definition.label)}</h3><button class="wa-kpi-value" data-wa-metric="${key}" aria-label="Строки: ${attr(definition.label)}">${esc(waMetric(key,metric,defs))}</button><p>${esc(waCoverage(metric))}</p>${metric.missingRows?`<button class="link" data-wa-missing-metric="${key}">Показать строки без значения</button>`:''}<details><summary>Определение показателя</summary><p>${esc(definition.definition)}</p></details></article>`}).join('');
  const chart=(group,title,axis)=>{const set=data.charts[group];return `<section class="card wa-chart" data-wa-chart="${group}"><h3>${title}</h3><p class="muted">${esc(axis)} · ${esc(defs[waView.filters.chartMetric].label)} (${esc(defs[waView.filters.chartMetric].unit)})</p>${set.items.length?set.items.map((item,index)=>`<button class="wa-bar" data-wa-group="${group}" data-wa-index="${index}" title="${attr(item.label+' · '+waMetric(waView.filters.chartMetric,item.metrics[waView.filters.chartMetric],defs))}"><span class="wa-bar-label">${esc(item.label)}</span><span class="wa-track"><span class="wa-fill" style="width:${item.width}%"></span></span><strong>${esc(waMetric(waView.filters.chartMetric,item.metrics[waView.filters.chartMetric],defs))}</strong><small>${item.metrics.decisions.value} строк · ${esc(waCoverage(item.metrics[waView.filters.chartMetric]))}</small></button>`).join(''):empty('Нет данных в выбранном срезе')}${set.truncated?`<p class="tiny">Показано ${set.items.length} из ${set.totalGroups} групп. Все группы доступны в сводной выгрузке; суммы карточек включают весь срез.</p>`:''}</section>`};
  const recovery=`<section class="card wa-chart wa-recovery" data-wa-chart="recovery"><h3>К возмещению и фактически возмещено</h3><p class="muted">Сравнение по объектам · ₽. Покрытие полей может различаться; остаток считается построчно.</p>${data.charts.sites.items.map((item,index)=>`<div class="wa-recovery-site"><button class="link" data-wa-group="sites" data-wa-index="${index}">${esc(item.label)}</button>${['reimbursable','reimbursed'].map(key=>`<button class="wa-bar wa-pair" data-wa-recovery="${key}" data-wa-site="${attr(item.key)}"><span class="wa-bar-label">${esc(defs[key].label)}</span><span class="wa-track"><span class="wa-fill ${key==='reimbursed'?'wa-paid':''}" style="width:${item[key+'Width']}%"></span></span><strong>${esc(waMetric(key,item.metrics[key],defs))}</strong><small>${esc(waCoverage(item.metrics[key]))}</small></button>`).join('')}</div>`).join('')||empty('Нет данных')}</section>`;
  const quality=`<p class="notice wa-quality">Без корректной даты в выбранном срезе: ${data.quality.missingDateRows} (некорректных: ${data.quality.invalidDateRows}). При фильтре периода такие строки исключены; их можно посмотреть через «Полнота даты». Возмещено больше назначения: ${data.quality.overpaymentRows} строк, ${esc(waFormat(data.quality.overpaymentValue))} ₽; это не уменьшает остаток других строк.</p>`;
  const exports=`<section class="card section-gap wa-exports"><h3>Предварительные выгрузки выбранного среза</h3><p>CSV/XLSX содержат те же фильтры и действующие строки. В XLSX десятичные суммы записаны точным текстом, без float; определения и ограничения находятся на отдельном листе.</p><div class="actions-row">${['summary','rows'].flatMap(kind=>['csv','xlsx'].map(ext=>`<button class="btn secondary" data-wa-export="${kind}.${ext}" ${waView.exporting?'disabled':''}>${kind==='summary'?'Сводки':'Строки'} ${ext.toUpperCase()}</button>`)).join('')}</div><p id="wa-export-status" role="status"></p></section>`;
  return `<div class="wa-kpis section-gap">${cards}</div>${quality}<div class="wa-charts section-gap">${chart('months','Динамика по месяцам','Месяц по дате акта')}${chart('sites','Сравнение объектов','Объект')}${chart('reasons','Структура причин списания','Причина')}${chart('tools','Виды инструмента','Вид инструмента')}${recovery}${chart('categories','Категории случаев','Категория')}</div>
    <section class="card section-gap" id="wa-rows"><h3>Детализация до исходных строк</h3><p>Строки ${data.rows.length?((data.page-1)*data.limit+1):0}–${Math.min(data.page*data.limit,data.totalRows)} из ${data.totalRows}. Только действующие записи; текущая версия документа может включать строки, введённые более ранней частичной версией.</p>
      ${data.rows.length?`<div class="table-wrap"><table class="table wa-rows-table"><thead><tr>${['Дата акта','Объект','Документ / версия','ID источника','Инструмент','Кол-во','Стоимость, ₽','Остаточная, ₽','К возмещению, ₽','Возмещено, ₽','Остаток возмещения, ₽','Причина / решение / МОЛ','Источник и исправления'].map(v=>`<th>${v}</th>`).join('')}</tr></thead><tbody>${data.rows.map(row=>`<tr data-wa-row="${attr(row.id)}"><td>${esc(row.date||'Без даты')}</td><td>${esc(row.siteName)}</td><td><button class="link" data-wa-document="${attr(row.documentId)}">${esc(row.document)}</button><small>Действующая версия ${row.currentVersion}; строка введена в ${row.introducedVersion}</small></td><td>${esc(row.sourceId||'Не указано')}</td><td>${esc(row.toolType||'Не указано')}</td>${['quantity','cost','residual','reimbursable','reimbursed','outstanding'].map(k=>`<td>${esc(waFormat(row[k]))}${row.invalidFields.includes(k)?'<small>Некорректно в источнике</small>':''}</td>`).join('')}<td>${esc(row.reason||'Не указано')}<small>${esc(row.decisionType||'Не указано')} · ${esc(row.responsible||'Не указано')} · ${esc(row.investigation||'Не указано')}</small></td><td><button class="link" data-action="decision-detail" data-id="${attr(row.id)}">Исходная строка</button><small>${esc(row.sheet)} · Excel ${row.sourceRow}</small><button class="link" data-wa-versions="${attr(row.documentId)}">Действующая версия и история</button></td></tr>`).join('')}</tbody></table></div>`:empty('Нет строк по выбранным фильтрам')}
      ${waMobileRows(data)}<div class="actions-row"><button class="btn secondary" data-wa-page="${data.page-1}" ${data.page<=1?'disabled':''}>Назад</button><button class="btn secondary" data-wa-page="${data.page+1}" ${data.page*data.limit>=data.totalRows?'disabled':''}>Далее</button></div></section>${exports}`;
}
function waFilterLabel(f){
  const labels=[];if(f.start||f.end)labels.push((f.start||'начало')+' — '+(f.end||'сегодня и далее'));
  if(f.site)labels.push(db.sites.find(s=>s.id===f.site)?.name||f.site);if(f.dateMode==='missing')labels.push('Без даты');if(f.dateMode==='dated')labels.push('С датой');
  for(const [key,label] of Object.entries(waDimensions))if(f[key]||f[key+'Missing'])labels.push(label+': '+(f[key]||'не указано'));
  return labels.join(' · ')||'Все доступные действующие строки';
}
function waMobileRows(data){return `<div class="wa-mobile-rows">${data.rows.map(row=>`<article class="wa-mobile-row" data-wa-mobile-row="${attr(row.id)}"><strong>${esc(row.siteName)} · ${esc(row.date||'Без даты')}</strong><p><button class="link" data-wa-document="${attr(row.documentId)}">${esc(row.document)}</button> · версия ${row.currentVersion}</p><p>${esc(row.sourceId||'ID не указан')} · ${esc(row.toolType||'Вид инструмента не указан')}</p><dl>${['quantity','cost','residual','reimbursable','reimbursed','outstanding'].map(k=>`<div><dt>${esc(data.definitions[k].label)}</dt><dd>${esc(waFormat(row[k]))} ${row[k]!==null&&row[k]!==undefined?esc(data.definitions[k].unit):''}</dd></div>`).join('')}</dl><p>${esc(row.reason||'Причина не указана')} · ${esc(row.decisionType||'Решение не указано')} · ${esc(row.responsible||'МОЛ не указан')} · ${esc(row.investigation||'Статус не указан')}</p><p class="tiny">${esc(row.sheet)} · Excel ${row.sourceRow} · строка введена в версии ${row.introducedVersion}</p><div class="actions-row"><button class="link" data-action="decision-detail" data-id="${attr(row.id)}">Исходная строка</button><button class="link" data-wa-versions="${attr(row.documentId)}">Версия и история</button></div></article>`).join('')}</div>`}
document.addEventListener('toggle',event=>{if(event.target.matches('.wa-filter-panel'))waView.filtersOpen=event.target.open},true);
async function waLoad(scroll=false){
  const epoch=++waView.epoch,scope=waScope();waView.loading=true;waView.error='';render();
  try{const data=await api('writeoff-analytics?'+waParams());if(epoch!==waView.epoch||scope!==waScope())return;waView.data=data}
  catch(error){if(epoch===waView.epoch)waView.error=error.message}
  finally{if(epoch===waView.epoch){waView.loading=false;render();if(scroll)$('#wa-rows')?.scrollIntoView({block:'start'})}}
}
function waDrill(changes){Object.assign(waView.filters,changes);waView.page=1;waView.data=null;waLoad(true)}
document.addEventListener('submit',event=>{
  if(event.target.id!=='wa-filters')return;event.preventDefault();const form=event.target;if(!form.reportValidity())return;
  const next={...waDefaults};
  for(const key of ['start','end','site','dateMode','chartMetric',...Object.keys(waDimensions)])next[key]=form.elements[key].value;
  for(const key of Object.keys(waDimensions)){next[key+'Missing']=form.elements[key+'Missing'].checked;if(next[key+'Missing'])next[key]=''}
  if(next.dateMode==='missing'){next.start='';next.end=''}
  if(next.start&&next.end&&next.start>next.end)return toast('Начало периода позже окончания',true);
  if(window.matchMedia('(max-width:700px)').matches)waView.filtersOpen=false;
  waView.filters=next;waView.page=1;waView.data=null;waLoad();
});
async function waDownload(spec){
  if(waView.exporting||!waView.data)return;
  const epoch=waView.epoch,snapshot=waView.data.snapshot;waView.exporting=true;
  const status=$('#wa-export-status');if(status)status.textContent='Готовим серверную выгрузку…';
  try{
    const response=await fetch('/api/writeoff-analytics/export/'+spec+'?'+waParams({expectedSnapshot:snapshot}),{credentials:'same-origin'});
    if(!response.ok){const error=await response.json();throw Error(error.detail||'Выгрузка не завершена')}
    const blob=await response.blob();if(epoch!==waView.epoch||!sessionUser)throw Error('Срез изменился во время выгрузки; повторите её');
    const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='КСУ_предварительно_'+spec;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    if(status?.isConnected)status.textContent='Готово. Это предварительная аналитика, не официальная отчётность.';
  }catch(error){if(status?.isConnected)status.textContent=error.message;toast(error.message,true)}
  finally{waView.exporting=false}
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-wa-action],[data-wa-page],[data-wa-metric],[data-wa-missing-metric],[data-wa-group],[data-wa-recovery],[data-wa-document],[data-wa-versions],[data-wa-show-version],[data-wa-export]');if(!button)return;
  try{
    const action=button.dataset.waAction;
    if(action==='registry'){ui.page='decisions';render()}
    if(action==='refresh'){waView.data=null;waLoad()}
    if(action==='reset'){waView.filters={...waDefaults};waView.page=1;waView.data=null;waLoad()}
    if(action==='clear-drill')waDrill({month:'',documentId:'',metricField:'',coverage:'all'});
    if(button.hasAttribute('data-wa-page')){waView.page=Number(button.dataset.waPage);waView.data=null;waLoad(true)}
    if(button.dataset.waMetric){const key=button.dataset.waMetric;if(['documents','decisions'].includes(key))$('#wa-rows')?.scrollIntoView();else waDrill({metricField:key,coverage:'known'})}
    if(button.dataset.waMissingMetric)waDrill({metricField:button.dataset.waMissingMetric,coverage:'missing'});
    if(button.dataset.waDocument)waDrill({documentId:button.dataset.waDocument});
    if(button.dataset.waGroup){
      const group=button.dataset.waGroup,item=waView.data.charts[group].items[Number(button.dataset.waIndex)];
      if(group==='months')waDrill(item.key===null?{month:'',dateMode:'missing',start:'',end:''}:{month:item.key,dateMode:'dated'});
      else if(group==='sites')waDrill({site:item.key});
      else{const field={tools:'toolType',reasons:'reason',categories:'caseCategory'}[group];waDrill({[field]:item.key||'',[field+'Missing']:item.key===null})}
    }
    if(button.dataset.waRecovery)waDrill({site:button.dataset.waSite,metricField:button.dataset.waRecovery,coverage:'known'});
    if(button.dataset.waVersions){
      const versions=await api('decision-documents/'+button.dataset.waVersions+'/versions');
      openModal(versions.map(v=>`<article class="timeline-item"><strong>Версия ${v.number} · ${esc(v.actor)}</strong><p>${esc(v.reason)} · ${esc(v.created_at)}</p>${v.summary.before?`<p>Стоимость: ${esc(waFormat(v.summary.before.cost))} → ${esc(waFormat(v.summary.after.cost))} ₽. Изменено ${v.summary.counts.changed}; добавлено ${v.summary.counts.added}; исключено ${v.summary.counts.removed}.</p>`:'<p>Первичный состав документа</p>'}<button class="btn secondary" data-wa-show-version="${v.number}" data-document="${attr(v.document_id)}">${v===versions[0]?'Действующий состав':'Исторические строки'} · версия ${v.number}</button></article>`).join(''),'Документ и история исправлений','Прежние версии доступны для проверки; в аналитике только действующие строки',true);
    }
    if(button.dataset.waShowVersion){ui.page='decisions';Object.assign(decisionView,{site:'',documentId:button.dataset.document,version:Number(button.dataset.waShowVersion),history:false,page:1,data:null,revision:null});closeModal();render()}
    if(button.dataset.waExport)await waDownload(button.dataset.waExport);
  }catch(error){toast(error.message,true)}
});
