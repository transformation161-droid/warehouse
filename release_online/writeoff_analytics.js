/* ИНСТРАКТ. Аналитика только по построчным отчётам; общие страницы PR41 сохранены. */
'use strict';
(()=>{
  const FIELDS={sourceId:'ID строки',date:'Дата акта',site:'Объект',document:'Документ',decisionType:'Тип решения',toolType:'Тип инструмента',quantity:'Кол-во',cost:'Стоимость',residual:'Остаточная стоимость',reason:'Причина',caseCategory:'Категория',investigation:'Статус расследования',decision:'Результат / решение',reimbursable:'К возмещению',reimbursed:'Фактически возмещено',responsible:'Ответственный / МОЛ',comment:'Комментарий'};
  const METRICS={quantity:'Количество, ед.',cost:'Стоимость, ₽',residual:'Остаточная стоимость, ₽',reimbursable:'К возмещению, ₽',reimbursed:'Фактически возмещено, ₽',inventories:'Инвентаризации по датам',documents:'Документы',decisions:'Строки реестра'};
  const DIMS={reason:'Причина',toolType:'Тип ТМЦ',decisionType:'Тип решения',caseCategory:'Категория',investigation:'Расследование',responsible:'Ответственный / МОЛ'};
  const GROUP_FILTER={sites:'site',periods:'reportPeriod',months:'month',reasons:'reason',tools:'toolType',categories:'caseCategory',decisionTypes:'decisionType',responsibles:'responsible',investigations:'investigation'};
  const state={owner:'',tab:'writeoffs',filters:{},metric:'cost',page:1,top:10,sort:'value',cache:new Map(),pending:new Set(),errors:new Map(),job:null,busy:false,importOptions:null,documents:[],documentPage:1,comparisonPage:1,chartTop:5,chartHighlight:'',chartSites:null,chartStructure:'reasons',rowScope:''};
  const allowed=()=>sessionUser&&['manager','inspector'].includes(sessionUser.role);
  const money=k=>['cost','residual','reimbursable','reimbursed'].includes(k);
  const format=(v,k)=>v==null?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:money(k)?4:6}).format(v)+(money(k)?' ₽':'');
  const value=(m,k)=>m?.[k]?.value??null;
  const btn=(text,action,extra='',primary=false)=>`<button type="button" class="btn ${primary?'primary':'secondary'}" data-wo-action="${action}" ${extra}>${esc(text)}</button>`;
  const option=(v,text,selected)=>`<option value="${attr(v??'')}"${String(v??'')===String(selected??'')?' selected':''}>${esc(text)}</option>`;
  const select=(key,label,items,current,extra='')=>`<div class="field"><label>${esc(label)}</label><select ${extra} data-wo-filter="${attr(key)}">${option('','Все',current)}${items.map(([v,t])=>option(v,t,current)).join('')}</select></div>`;
  function identity(){return sessionUser?sessionUser.id+':'+sessionUser.csrf:''}
  function syncOwner(){
    if(state.owner===identity())return;
    state.owner=identity();state.cache.clear();state.pending.clear();state.errors.clear();state.filters={};state.page=1;state.job=null;state.importOptions=null;state.busy=false;state.documents=[];state.chartTop=5;state.chartHighlight='';state.chartSites=null;state.chartStructure='reasons';state.rowScope='';
  }
  function query(site){
    const p=new URLSearchParams();for(const [k,v] of Object.entries({...state.filters,...(site?{site}:{})}))if(v!==''&&v!=null)p.set(k,v);
    p.set('chartMetric',state.metric);p.set('page',state.page);p.set('limit','50');return p;
  }
  const key=site=>identity()+'|'+db.revision+'|'+query(site);
  async function request(url,options={}){
    const owner=identity(),actor=sessionUser;
    if(!actor)throw Error('Войдите в свою учётную запись');
    const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options,headers:{'X-KSU-Expected-User':actor.id,'X-CSRF-Token':actor.csrf,...options.headers}});
    let data;try{data=await r.json()}catch{data={}}
    if(identity()!==owner)throw Error('Учётная запись изменилась; повторите действие');
    if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'Ошибка сервера: '+r.status);
    return data;
  }
  const post=(url,data)=>request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function dataset(site){
    syncOwner();const scope=site||state.filters.site||'all';if(state.rowScope!==scope){state.rowScope=scope;state.page=1}
    const k=key(site),owner=identity();
    if(!state.cache.has(k)&&!state.pending.has(k)&&!state.errors.has(k)){
      state.pending.add(k);
      queueMicrotask(()=>request('/api/writeoff-analytics?'+query(site)).then(d=>{
        if(owner!==identity())return;
        state.cache.set(k,d);while(state.cache.size>8)state.cache.delete(state.cache.keys().next().value);
      }).catch(e=>{if(owner===identity())state.errors.set(k,e.message)}).finally(()=>{
        state.pending.delete(k);if(owner===identity()&&!ui.modal&&['home','reports','sites'].includes(ui.page))render();
      }));
    }
    return {data:state.cache.get(k),error:state.errors.get(k)};
  }
  function reload(){state.cache.clear();state.errors.clear();render()}
  function kpis(d){
    return '<div class="grid grid-4 section-gap wo-primary-kpis">'+['inventories','decisions','documents','quantity','cost','residual','reimbursable','reimbursed'].map(k=>metric(METRICS[k],format(d.totalRows?value(d.metrics,k):null,k),`${d.metrics[k]?.missingRows||0} незаполненных строк`,k==='inventories'?'calendar':'chart')).join('')+'</div><div class="wo-mobile-kpi-summary">'+['inventories','decisions','documents','reimbursable'].map(k=>'<span>'+esc(METRICS[k])+': <strong>'+format(d.totalRows?value(d.metrics,k):null,k)+'</strong></span>').join('')+'</div>';
  }
  function notices(d){
    const q=d.quality,issues=[];
    if(!d.totalRows)issues.push('Нет загруженных данных для выбранного среза. Незагруженный квартал не считается нулевым результатом.');
    if(q.outsideReportPeriodRows)issues.push(`${q.outsideReportPeriodRows} строк: дата акта вне отчётного квартала. Квартал задан при импорте, даты сохранены.`);
    if(q.fractionalQuantityRows)issues.push(`Дробное количество: ${q.fractionalQuantityRows} строк; единицы требуют проверки.`);
    for(const k of ['cost','residual','reimbursable','reimbursed'])if(d.metrics[k].missingRows)issues.push(`${METRICS[k]}: ${d.metrics[k].missingRows} незаполненных. Пустое поле не означает ноль.`);
    return `<div class="notice section-gap">${esc(d.definitions.inventories.definition)} Отчётный квартал и дата акта независимы. Несколько инвентаризаций в один день могут требовать отдельного уточнения.</div>${issues.length?`<div class="notice warn section-gap">${issues.map(esc).join('<br>')}</div>`:''}`;
  }
  function filters(d,site){
    const f=state.filters,periods=d?.reportPeriods||[],years=[...new Set([...periods.map(p=>p.slice(0,4)),String(new Date().getFullYear()),f.reportYear].filter(Boolean))].sort();
    const year=f.reportYear||f.reportPeriod?.slice(0,4)||years.at(-1),quarters=[1,2,3,4].map(n=>[year+'-Q'+n,year+' · '+n+' квартал']);
    const advanced=Object.entries(f).some(([k,v])=>v&& !['site','reportYear','reportPeriod'].includes(k));
    return `<section class="card section-gap wo-filters"><div class="filterbar">${site?'':select('site','Объект',db.sites.map(s=>[s.id,s.name]),f.site)}${select('reportYear','Отчётный год',years.map(y=>[y,y]),f.reportYear)}${select('reportPeriod','Отчётный квартал',quarters,f.reportPeriod)}${btn('Сбросить','clear')}${btn('Обновить','reload')}</div><details data-wo-filter-details${advanced?' open':''}><summary>Все фильтры${advanced?' · применены дополнительные условия':''}</summary><div class="filterbar section-gap">${['start','end'].map((k,i)=>`<div class="field"><label>Дата акта ${i?'по':'с'}</label><input type="date" data-wo-filter="${k}" value="${attr(f[k]||'')}"></div>`).join('')}${Object.entries(DIMS).map(([k,label])=>select(k,label,(d?.facets[k]?.values||[]).filter(v=>v!=null).map(v=>[v,v]),f[k])).join('')}<div class="field"><label>Поиск по реестру</label><input type="search" data-wo-filter="q" value="${attr(f.q||'')}" maxlength="200" placeholder="Документ, ТМЦ, комментарий"></div></div></details><p class="tiny muted wo-filter-note">Основная динамика — отчётный квартал. Интервал дат актов — в дополнительных фильтрах.</p>${f.month||f.documentId?`<div class="notice">Дополнительный срез: ${esc(f.month||f.documentId)}. «Сбросить» снимает все фильтры.</div>`:''}</section>`;
  }
  function matrix(d){
    const periods=d.matrixPeriods,lookup=new Map(d.matrix.map(x=>[x.siteId+'|'+x.period,x.metrics]));
    const sites=[...d.matrixSites].sort((a,b)=>{
      const av=state.sort==='value'?value(a.metrics,state.metric):a.delta,bv=state.sort==='value'?value(b.metrics,state.metric):b.delta;
      return av==null?bv==null?a.name.localeCompare(b.name,'ru'):1:bv==null?-1:(Number(bv)-Number(av))*(state.sort==='decrease'?-1:1);
    });
    return `<section class="card section-gap"><div class="card-title-row"><div><div class="card-title">Динамика объектов</div><div class="card-subtitle">Все ${sites.length} объектов. «—» — нет загруженных данных; 0 — подтверждённый ноль. Δ к предыдущему календарному кварталу.</div></div></div><div class="filterbar"><div class="field"><label>Сортировка</label><select data-wo-sort>${[['value','По величине'],['growth','По росту'],['decrease','По снижению']].map(([k,v])=>option(k,v,state.sort)).join('')}</select></div><div class="field"><label>Диаграммы</label><select data-wo-top>${[[10,'Топ-10'],[20,'Топ-20'],[0,'Все группы']].map(([k,v])=>option(k,v,state.top)).join('')}</select></div></div><div class="table-wrap"><table class="table"><thead><tr><th>Объект</th>${periods.map(p=>`<th>${esc(p)}</th>`).join('')}<th>Всего</th><th>Δ / %</th><th>Тренд</th></tr></thead><tbody>${sites.map(s=>`<tr><td><button class="link" data-wo-site="${attr(s.id)}">${esc(s.name)}</button></td>${periods.map(p=>`<td><button class="link" data-wo-cross="site" data-wo-value="${attr(s.id)}" data-wo-period="${attr(p)}">${format(value(lookup.get(s.id+'|'+p),state.metric),state.metric)}</button></td>`).join('')}<td>${format(value(s.metrics,state.metric),state.metric)}</td><td title="${attr((s.previousPeriod||'')+' → '+(s.currentPeriod||''))}">${format(s.delta,state.metric)}${s.deltaPercent==null?'':` / ${esc(s.deltaPercent)}%`}</td><td>${s.delta==null?'—':Number(s.delta)>0?'↗':Number(s.delta)<0?'↘':'→'}</td></tr>`).join('')}</tbody></table></div></section>`;
  }
  function barsGroup(d,group,label){
    const all=d.charts[group]?.items||[],items=state.top?all.slice(0,state.top):all;
    return `<section class="card section-gap"><div class="card-title">${esc(label)}</div><div class="card-subtitle">${esc(METRICS[state.metric])} · показано ${items.length} из ${all.length}. Нажмите группу для детализации.</div>${!items.length?empty('Нет загруженных данных','Загрузите построчный отчёт или измените фильтры'):`<div class="chart-list">${items.map(x=>`<div class="chart-item"><span class="chart-item-name"><button class="link" data-wo-cross="${GROUP_FILTER[group]}" data-wo-value="${attr(x.key??'')}" title="${attr(x.label)}" data-wo-missing="${x.key==null?'1':''}">${esc(x.label)}</button></span><span class="bar-track"><span class="bar-fill" style="display:block;width:${Number(x.width)||0}%"></span></span><span class="chart-value">${format(value(x.metrics,state.metric),state.metric)}${x.sharePercent==null?'':` <small class="muted">${esc(x.sharePercent)}%</small>`}</span></div>`).join('')}</div>`}</section>`;
  }
  function registry(d,site){
    const sourceSite=site||d.filters.site;
    if(sourceSite)return window.WriteoffStatement.render(d,{siteName:nameSite(sourceSite),format,date:fmtDate});
    return `<section class="card section-gap" id="wo-registry"><div class="card-title-row"><div><div class="card-title">Реестр списаний</div><div class="card-subtitle">${d.totalRows} действующих строк · страница ${d.page}. Откройте строку для исходных полей и истории акта.</div></div><div class="page-actions">${btn('Строки XLSX','export','data-wo-kind="rows" data-wo-ext="xlsx"')}${btn('CSV','export','data-wo-kind="rows" data-wo-ext="csv"')}</div></div><div class="table-wrap"><table class="table"><thead><tr><th>Дата акта / квартал</th><th>Объект / документ</th><th>Тип / причина</th><th>Кол-во</th><th>Стоимость</th><th>Остаточная</th><th>К возмещению</th><th>Фактически возмещено</th><th></th></tr></thead><tbody>${d.rows.map(r=>`<tr><td>${esc(fmtDate(r.date))}<br><small>${esc(r.reportPeriod||'Без квартала')}</small></td><td><button class="link" data-wo-site="${attr(r.siteId)}">${esc(r.siteName)}</button><br>${esc(r.document)}</td><td>${esc(r.toolType)}<br><small>${esc(r.reason)}</small></td><td>${format(r.quantity,'quantity')}</td><td>${format(r.cost,'cost')}</td><td>${format(r.residual,'residual')}</td><td data-wo-field="reimbursable">${format(r.reimbursable,'cost')}</td><td data-wo-field="reimbursed">${format(r.reimbursed,'cost')}</td><td>${btn('Открыть','detail',`data-wo-id="${attr(r.id)}"`)}</td></tr>`).join('')}</tbody></table></div><div class="actions-row section-gap">${btn('Предыдущая','page',`data-wo-step="-1"${d.page<=1?' disabled':''}`)}${btn('Следующая','page',`data-wo-step="1"${d.page*d.limit>=d.totalRows?' disabled':''}`)}</div></section>`;
  }
  function recovery(d){
    return `<section class="card section-gap" data-wo-recovery><div class="card-title">Возмещения</div><p class="small"><strong>К возмещению:</strong> <span data-wo-sum="reimbursable">${format(value(d.metrics,'reimbursable'),'cost')}</span></p><p class="small"><strong>Фактически возмещено:</strong> <span data-wo-sum="reimbursed">${format(value(d.metrics,'reimbursed'),'cost')}</span></p><p class="tiny muted">Два самостоятельных поля источника. Пустые значения не означают ноль.</p><div class="actions-row">${btn('Не заполнено «К возмещению»','coverage','data-wo-field="reimbursable"')}${btn('Не заполнено «Фактически возмещено»','coverage','data-wo-field="reimbursed"')}</div></section>`;
  }
  function analytics(site){
    const {data:d,error}=dataset(site),workspace=document.querySelector('.workspace'),style=workspace?getComputedStyle(workspace):null;
    const available=workspace?workspace.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight):Math.min(1100,innerWidth-32);
    const width=Math.max(300,(available-(innerWidth>900?16:0))/(innerWidth>900?2:1)-40);
    return `<div data-wo-panel>${filters(d,site)}${error?`<div class="notice warn">${esc(error)} ${btn('Повторить','reload')}</div>`:!d?'<div class="card section-gap">Загружаем аналитику…</div>':(site?'':kpis(d))+window.WriteoffCharts.render(d,{metric:state.metric,top:state.chartTop,highlight:state.chartHighlight,sites:state.chartSites,structure:state.chartStructure,site,width,structureWidth:site?width:Math.max(300,available-40)})+(site?kpis(d):'')+notices(d)+matrix(d)+`<details class="wo-detail-charts"><summary>Дополнительные разрезы: даты актов, категории, решения и ответственные</summary><div class="grid grid-2">${Object.entries({months:'Месяцы по датам актов',categories:'Категории случаев',decisionTypes:'Типы решений',responsibles:'Ответственные / МОЛ',investigations:'Статус расследования'}).map(([k,v])=>barsGroup(d,k,v)).join('')}</div></details>`+recovery(d)+registry(d,site)}</div>`;
  }
  const baseReports=renderReports;
  renderReports=function(){
    if(!allowed())return baseReports();
    syncOwner();const tabs=`<div class="tab-line"><button class="${state.tab==='writeoffs'?'active':''}" data-wo-tab="writeoffs">Аналитика списаний</button><button class="${state.tab==='other'?'active':''}" data-wo-tab="other">Остальные отчёты</button></div>`;
    return tabs+(state.tab==='other'?baseReports():heading('Аналитика списаний','По загруженным построчным отчётам · независимые отчётные периоды',`${sessionUser.role==='manager'?btn('Загрузить отчёт XLSX','import','',true):''}${btn('Сводка XLSX','export','data-wo-kind="summary" data-wo-ext="xlsx"')}${btn('Сводка CSV','export','data-wo-kind="summary" data-wo-ext="csv"')}`)+analytics());
  };
  const baseHeader=header;
  header=function(){
    const html=baseHeader();if(!allowed())return html;
    return html.replace(/<button class="nav-item[^"]*" data-nav="inventory">[\s\S]*?<\/button>/,'');
  };
  function replaceInventoryStats(html,site){
    const root=document.createElement('div');root.innerHTML=html;const {data}=dataset(site);
    for(const card of root.querySelectorAll('.stat-card')){
      const label=card.querySelector('.stat-label')?.textContent;
      const kind=['Недостачи по пересчётам','Сумма недостач'].includes(label)?'cost':label==='Инвентаризаций'?'inventories':null;
      if(kind)card.outerHTML=metric(METRICS[kind],data?format(value(data.metrics,kind),kind):'…','По загруженным отчётам','chart');
    }
    for(const title of root.querySelectorAll('.card-title'))if(title.textContent==='История инвентаризаций')title.closest('section')?.remove();
    return root.innerHTML;
  }
  const baseOverview=renderOverview;
  renderOverview=function(){const html=baseOverview();return allowed()?replaceInventoryStats(html):html};
  const baseSites=renderSites;
  renderSites=function(){
    const html=baseSites();if(!allowed()||!ui.siteId)return html;
    const root=document.createElement('div');root.innerHTML=replaceInventoryStats(html,ui.siteId);
    const block=document.createElement('div');block.innerHTML=analytics(ui.siteId);
    const visuals=document.createElement('div');visuals.className='wo-site-visuals';visuals.innerHTML=heading('Аналитика списаний объекта','Те же расчёты, фильтры и права доступа, что в общем отчёте',btn('Полная ведомость','statement'));
    for(const node of [block.querySelector('.wo-filters'),block.querySelector('#wo-charts')])if(node)visuals.append(node);
    const summary=root.querySelector('.grid.grid-4');
    if(summary)summary.after(visuals);else root.append(visuals);
    // Contacts, findings and inspection history stay ahead of the long analytic registry.
    root.append(block);return root.innerHTML;
  };

  // Dialog HTML is kept in ui.modal so the existing sync renderer cannot reset edits.
  function feedback(html,bad=false){
    const el=document.getElementById('wo-feedback');if(el)el.innerHTML=`<div class="notice${bad?' warn':''}">${html}</div>`;
    if(ui.modal&&document.querySelector('.modal-body'))ui.modal.content=document.querySelector('.modal-body').innerHTML;
  }
  function busy(value){state.busy=value;for(const el of document.querySelectorAll('[data-wo-action]'))if(el.closest('.modal'))el.disabled=value}
  async function exclusive(action){if(state.busy)return;busy(true);try{await action()}catch(e){feedback(esc(e.message),true);toast(e.message,true)}finally{busy(false)}}
  async function waitJob(id){
    for(let n=0;n<180;n++){
      const job=await request('/api/decision-imports/'+id);
      if(!['uploading','inspecting','checking','committing'].includes(job.status)){state.job=job;return job}
      await new Promise(resolve=>setTimeout(resolve,600));
    }
    throw Error('Обработка продолжается. Закройте окно и откройте импорт снова: текущая задача будет восстановлена.');
  }
  function importDialog(job){
    const s=job?.settings||{sheet:0,headerRow:1,firstRow:2,lastRow:100000,reportPeriod:'',mapping:{},allowCached:false,siteAliases:{}},year=new Date().getFullYear();
    const body=`<div class="notice">Только построчный «Отчёт по списаниям». «Аналитика инвентаризаций» — образец результата, её загружать нельзя. Запись выполняется после сверки и подтверждения.</div>${job?`<p class="small">${esc(job.name)} · ${esc(job.status)}</p>`:'<div class="field section-gap"><label>Файл XLSX до 30 МБ</label><input id="wo-file" type="file" accept=".xlsx"></div>'}<div class="form-grid section-gap"><div class="field"><label>Отчётный квартал *</label><input id="wo-period" placeholder="2026-Q2" pattern="[0-9]{4}-Q[1-4]" value="${attr(s.reportPeriod)}" required><small>Например ${year}-Q2. Не выводится из даты акта.</small></div>${job?`<div class="field"><label>Лист</label><select id="wo-sheet">${job.sheets.map((x,i)=>option(i,x.name,s.sheet)).join('')}</select></div>${[['headerRow','Заголовки: строка'],['firstRow','Первая строка данных'],['lastRow','Последняя строка данных']].map(([k,v])=>`<div class="field"><label>${v}</label><input type="number" id="wo-${k}" min="1" max="100000" value="${s[k]}"></div>`).join('')}<div class="field"><label>Только документ (для замещения одного акта)</label><input id="wo-document" value="${attr(s.sourceDocument||'')}" placeholder="Пусто — все строки диапазона"></div>`:''}</div><label class="small section-gap" style="display:block"><input id="wo-cached" type="checkbox"${s.allowCached?' checked':''}> Разрешить сохранённые результаты формул (проверить в Excel)</label><div id="wo-mapping">${job?.headers?mappingHtml(job):''}</div><div id="wo-aliases">${job&&(job.unknownSites?.length||Object.keys(job.settings?.siteAliases||{}).length)?aliasesHtml(job):''}</div><div id="wo-feedback" class="section-gap"></div>`;
    openModal(body,'Импорт отчёта по списаниям','Предварительный просмотр не изменяет рабочие записи',true,`${btn('Закрыть','close')}${job?btn('Отменить задачу','cancel'):''}${btn(job?'Проверить строки':'Загрузить и распознать','preview','',true)}${job?.status==='ready'?btn('Подтвердить новый импорт','commit','',true)+btn('Исправление существующего акта','replace'):''}`);
    if(job&&['ready','invalid','mapping','error','done'].includes(job.status))showPreview(job);
  }
  function mappingHtml(job){return `<details class="section-gap"${job.status==='mapping'?' open':''}><summary>Сопоставление 17 полей</summary><div class="form-grid">${Object.entries(FIELDS).map(([k,label])=>`<div class="field"><label>${esc(label)}</label><select data-wo-map="${k}">${option('','Не импортировать',job.mapping?.[k])}${Object.entries(job.headers||{}).map(([col,name])=>option(col,col+' · '+name,job.mapping?.[k])).join('')}</select></div>`).join('')}</div></details>`}
  function aliasesHtml(job){
    const aliases={...job.settings?.siteAliases};for(const name of job.unknownSites||[])if(!aliases[name])aliases[name]=state.importOptions.suggestedAliases[name]||'';
    return `<div class="notice warn section-gap">Подтвердите связь с существующими объектами. Новые объекты не создаются.</div><div class="form-grid">${Object.entries(aliases).map(([name,id])=>`<div class="field"><label>${esc(name)}</label><select data-wo-alias="${attr(name)}">${option('','Выберите объект',id)}${state.importOptions.sites.map(s=>option(s.id,s.name,id)).join('')}</select></div>`).join('')}</div><label><input id="wo-alias-confirm" type="checkbox"> Сопоставления проверены и подтверждены</label>`;
  }
  function showPreview(job){
    const s=job.summary||{},errors=job.errors||[];
    feedback(`${job.message?`<p>${esc(job.message)}</p>`:''}<p>Принято ${job.accepted||0}; ошибок строк ${job.rejected||0}; пустых ${job.skipped||0}; вне выбранного документа ${job.excludedDocumentRows||0}.</p>${s.rows!=null?`<p>Количество: ${format(s.quantity,'quantity')}; стоимость: ${format(s.cost,'cost')}; остаточная: ${format(s.residual,'residual')}. К возмещению: ${format(s.reimbursable,'cost')}; фактически возмещено: ${format(s.reimbursed,'cost')}. Нулевых строк: ${s.zeroQuantityRows}. Документов: ${s.documents}.</p>`:''}${job.warning?`<p>${esc(job.warning)}</p>`:''}${job.excelRoundingCells?`<p>Устранена погрешность хранения чисел Excel в ${job.excelRoundingCells} ячейках (до 0,00000001 ₽). Исходные значения сохранены.</p>`:''}${errors.length?`<p>${btn('Скачать все ошибки CSV','errors')}</p><div class="table-wrap"><table class="table"><thead><tr><th>Лист / ячейка</th><th>Причина / исправление</th></tr></thead><tbody>${errors.map(e=>`<tr><td>${esc(e.sheet||job.sheets?.[job.settings?.sheet||0]?.name)} · ${esc(e.column)}${e.row}</td><td>${esc(e.reason)}<br>${esc(e.fix)}</td></tr>`).join('')}</tbody></table></div>`:''}${job.rows?.length?`<details open><summary>Первые ${job.rows.length} принятых строк из ${job.accepted}</summary><div class="table-wrap"><table class="table"><thead><tr><th>Строка</th>${Object.values(FIELDS).map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${job.rows.map(r=>`<tr><td>${r.row}</td>${Object.keys(FIELDS).map(k=>`<td>${esc(r[k]??'—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`:''}`,job.status==='error'||job.status==='invalid');
  }
  async function openImport(){
    syncOwner();state.importOptions=await request('/api/writeoff-analytics/import-options');
    const current=await request('/api/decision-imports');state.job=current;importDialog(current);
  }
  async function preview(){
    let job=state.job;
    if(!job){
      const file=document.getElementById('wo-file')?.files[0],period=document.getElementById('wo-period').value;
      if(!file)throw Error('Выберите построчный XLSX');
      if(!/^[0-9]{4}-Q[1-4]$/.test(period))throw Error('Укажите отчётный квартал, например 2026-Q2');
      if(file.size>30*1024*1024)throw Error('Файл больше 30 МБ');
      const upload=await request('/api/decision-imports',{method:'POST',headers:{'X-KSU-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});
      job=await waitJob(upload.id);job.settings={sheet:0,headerRow:1,firstRow:2,lastRow:100000,reportPeriod:period,allowCached:document.getElementById('wo-cached').checked,mapping:{},siteAliases:{}};
      importDialog(job);if(job.status==='error')return;
    }
    const settings={sheet:Number(document.getElementById('wo-sheet').value),headerRow:Number(document.getElementById('wo-headerRow').value),firstRow:Number(document.getElementById('wo-firstRow').value),lastRow:Number(document.getElementById('wo-lastRow').value),reportPeriod:document.getElementById('wo-period').value,sourceDocument:document.getElementById('wo-document').value.trim(),allowCached:document.getElementById('wo-cached').checked,mapping:{},siteAliases:{},aliasesConfirmed:false};
    for(const el of document.querySelectorAll('[data-wo-map]'))if(el.value)settings.mapping[el.dataset.woMap]=el.value;
    // Selecting a different sheet/header requests fresh automatic detection.
    if(settings.sheet!==(job.settings?.sheet||0)||settings.headerRow!==(job.settings?.headerRow||1))settings.mapping={};
    for(const el of document.querySelectorAll('[data-wo-alias]')){if(!el.value)throw Error('Сопоставьте все неизвестные объекты');settings.siteAliases[el.dataset.woAlias]=el.value}
    settings.aliasesConfirmed=!!document.getElementById('wo-alias-confirm')?.checked;
    if(Object.keys(settings.siteAliases).length&&!settings.aliasesConfirmed)throw Error('Подтвердите сопоставление объектов');
    await post('/api/decision-imports/'+job.id+'/preview',settings);job=await waitJob(job.id);importDialog(job);
  }
  async function commit(){
    const j=state.job;if(j?.status!=='ready')throw Error('Нужна успешная проверка без ошибок');
    if(j.versionPlan){await post('/api/decision-imports/'+j.id+'/version-confirm',{token:j.versionPlan.token})}
    else{if(!confirm('Сверили суммы, период и все объекты? Записать новые строки? Для исправления используйте замещение акта.'))return;await post('/api/decision-imports/'+j.id+'/commit',{})}
    const done=await waitJob(j.id);if(done.status!=='done'){showPreview(done);return}
    closeModal();state.job=null;state.page=1;reload();toast(done.duplicate?'Импорт уже выполнен, дубли не созданы':'Отчёт записан. Суммы пересчитаны по действующим версиям.');
  }
  async function replacement(page=1){
    const q=new URLSearchParams({page,limit:50,q:document.getElementById('wo-doc-search')?.value||''});
    const result=await request('/api/decision-documents?'+q);state.documents=result.rows;state.documentPage=page;
    openModal(`<div class="notice warn">Полное замещение одного акта: новые строки заменят его действующий набор; старые версии останутся в истории. В предпросмотре должны быть строки только этого акта и объекта. При необходимости вернитесь и укажите «Только документ» или диапазон строк.</div><div class="field section-gap"><label>Поиск документа</label><input id="wo-doc-search" value="${attr(q.get('q'))}"></div>${btn('Найти','replace-search')}<div class="field section-gap"><label>Документ (${result.total} всего)</label><select id="wo-target">${result.rows.map(r=>option(r.id,`${nameSite(r.site_id)} · ${r.label} · версия ${r.version}`,'')).join('')}</select></div><div class="field section-gap"><label>Причина исправления *</label><textarea id="wo-reason" maxlength="1200"></textarea></div><div id="wo-feedback"></div>`, 'Исправление существующего акта','Сначала сравнение, затем отдельное подтверждение',true,`${btn('Назад','import-back')}${page>1?btn('Предыдущие','replace-page','data-wo-step="-1"'):''}${page*50<result.total?btn('Следующие','replace-page','data-wo-step="1"'):''}${btn('Сравнить','compare','',true)}`);
  }
  async function compare(){
    const target=state.documents.find(d=>d.id===document.getElementById('wo-target').value),reason=document.getElementById('wo-reason').value.trim();
    if(!target||!reason)throw Error('Выберите акт и укажите причину');
    state.versionSettings={documentId:target.id,expectedVersion:target.version,mode:'full',reason,matches:{}};
    await compareAgain();
  }
  async function compareAgain(){
    await post('/api/decision-imports/'+state.job.id+'/version-preview',state.versionSettings);const j=await waitJob(state.job.id);
    if(j.status!=='ready')throw Error(j.message||'Не удалось сравнить. Вернитесь к предпросмотру.');
    await comparisonDialog(1);
  }
  async function comparisonDialog(page){
    const j=state.job,p=j.versionPlan,c=await request(`/api/decision-imports/${j.id}/comparison?page=${page}&limit=50`);state.comparisonPage=page;
    let conflicts='';
    if(p.conflicts.length){
      const old=await request('/api/decisions?'+new URLSearchParams({documentId:p.settings.documentId,limit:100}));
      conflicts=`<div class="notice warn">Нужны явные соответствия исходных ID. Если в акте больше 100 строк, исправьте ID в исходном XLSX.</div>${p.conflicts.map(x=>`<div class="field"><label>Строка ${x.row}: ${esc(x.reason)}</label><select data-wo-match="${x.row}">${option('','Выберите соответствие','')}${option('__new__','Добавить как самостоятельную новую строку','')}${old.rows.map(r=>option(r.id,`${r.sourceId||'Без ID'} · ${r.toolType} · ${r.quantity} · строка ${r.sourceRow}`,'')).join('')}</select></div>`).join('')}`;
    }
    const counts=p.summary.counts;
    openModal(`<div class="notice">Добавлено ${counts.added}; изменено ${counts.changed}; удалено из действующего набора ${counts.removed}; без изменений ${counts.unchanged}. После замещения: ${p.summary.afterRows} строк.</div><p>Количество: ${esc(p.summary.before.quantity)} → ${esc(p.summary.after.quantity)}. Стоимость: ${esc(p.summary.before.cost)} → ${esc(p.summary.after.cost)}.</p>${conflicts}<div class="table-wrap"><table class="table"><thead><tr><th>Строка</th><th>Изменение</th><th>Поля</th></tr></thead><tbody>${c.rows.map(r=>`<tr><td>${r.row??'—'}</td><td>${esc(r.kind)}</td><td>${r.fields.map(f=>esc(FIELDS[f]||f)).join(', ')}</td></tr>`).join('')}</tbody></table></div><div id="wo-feedback"></div>`, 'Сверка замещения','Исходные версии не удаляются; конфликт версии отменит всю запись',true,`${btn('Назад','import-back')}${page>1?btn('Предыдущая','comparison-page','data-wo-step="-1"'):''}${page*50<c.total?btn('Следующая','comparison-page','data-wo-step="1"'):''}${p.ready?btn('Подтвердить замещение','commit','',true):btn('Подтвердить соответствия и пересчитать','match','',true)}`);
  }
  async function detail(id){
    const r=await request('/api/decisions/'+id),versions=await request('/api/decision-documents/'+r.documentId+'/versions');state.detail={r,version:versions[0].number};
    openModal(`<div class="meta-list" style="display:block">${Object.entries(FIELDS).map(([k,v])=>`<p><small class="muted">${esc(v)}</small><br><strong>${k==='decision'?window.WriteoffStatement.text(r.data[k]):esc(r.data[k]??'Не указано')}</strong></p>`).join('')}</div><p class="tiny muted">Лист ${esc(r.sheet)} · строка ${r.sourceRow}. Отчётный квартал: ${esc(r.data.reportPeriod||'Не задан')}. SHA-256 ${esc(r.fileSha256)}.</p><details><summary>История версий документа</summary>${versions.map(v=>`<p>Версия ${v.number} · ${esc(v.actor)} · ${esc(v.created_at)} · ${esc(v.reason)}<br><small>${esc(JSON.stringify(v.summary.inventoryEventOverride||v.summary.counts||{}))}</small></p>`).join('')}</details>${sessionUser.role==='manager'?`<div class="divider"></div><div class="field"><label>Уточнение даты инвентаризации для всего документа</label><select id="wo-event-mode">${option('default','По исходной дате акта','default')}${option('date','Подтверждённая другая дата','')}${option('exclude','Исключить документ из счётчика событий','')}</select></div><div class="field"><label>Подтверждённая дата (только для другой даты)</label><input id="wo-event-date" type="date"></div><div class="field"><label>Основание уточнения *</label><textarea id="wo-event-reason" maxlength="1200"></textarea></div>`:''}<div id="wo-feedback"></div>`, 'Данные строки и акта','Исходные поля сохранены; история доступна в рамках прав объекта',true,`${btn('Закрыть','close')}${btn('Все строки акта','document',`data-wo-id="${attr(r.documentId)}"`)}${sessionUser.role==='manager'?btn('Подтвердить уточнение','event','',true):''}`);
  }
  function download(url){
    // Check identity via fetch as well as response headers before starting a download.
    const owner=identity(),actor=sessionUser;
    return fetch(url,{credentials:'same-origin',cache:'no-store',headers:{'X-KSU-Expected-User':actor.id}}).then(async r=>{
      if(!r.ok){let d={};try{d=await r.json()}catch{}throw Error(d.detail||'Ошибка выгрузки: '+r.status)}
      const blob=await r.blob();if(identity()!==owner)return;
      const link=document.createElement('a'),href=URL.createObjectURL(blob);link.href=href;link.download=(r.headers.get('Content-Disposition')?.match(/filename="?([^";]+)/)||[])[1]||'instrakt-report.xlsx';link.click();setTimeout(()=>URL.revokeObjectURL(href),30000);
    });
  }
  document.addEventListener('click',event=>{
    const structure=event.target.closest('[data-wo-chart-structure]');if(structure&&allowed()){state.chartStructure=structure.dataset.woChartStructure;render();return}
    const el=event.target.closest('[data-wo-action],[data-wo-tab],[data-wo-site],[data-wo-cross]');if(!el||!allowed())return;
    if(el.dataset.woTab){state.tab=el.dataset.woTab;render();return}
    if(el.dataset.woSite){navTo('sites',el.dataset.woSite);return}
    if(el.dataset.woCross){
      const f=el.dataset.woCross,v=el.dataset.woValue;delete state.filters[f+'Missing'];
      if(el.dataset.woMissing&&DIMS[f]){delete state.filters[f];state.filters[f+'Missing']='true'}else state.filters[f]=v;
      if(el.dataset.woPeriod)state.filters.reportPeriod=el.dataset.woPeriod;
      if(f==='reportPeriod')state.filters.reportYear=v.slice(0,4);
      state.page=1;reload();document.getElementById('wo-registry')?.scrollIntoView({block:'start'});return;
    }
    const a=el.dataset.woAction;
    if(a==='close'){if(!state.busy)closeModal();return}
    if(a==='clear'){state.filters={};state.page=1;reload();return}
    if(a==='reload'){reload();return}
    if(a==='statement'){const target=document.getElementById('wo-registry');if(target)window.scrollTo({top:target.getBoundingClientRect().top+scrollY-90,behavior:'smooth'});return}
    if(a==='page'){state.page+=Number(el.dataset.woStep);reload();return}
    if(a==='document'){state.filters.documentId=el.dataset.woId;state.page=1;closeModal();reload();return}
    if(a==='coverage'){state.filters.metricField=el.dataset.woField;state.filters.coverage='missing';state.page=1;reload();return}
    exclusive(async()=>{
      if(a==='import')await openImport();
      if(a==='preview')await preview();
      if(a==='import-back')importDialog(state.job);
      if(a==='cancel'){await request('/api/decision-imports/'+state.job.id,{method:'DELETE'});state.job=null;importDialog(null)}
      if(a==='commit')await commit();
      if(a==='replace'||a==='replace-search')await replacement();
      if(a==='replace-page')await replacement(state.documentPage+Number(el.dataset.woStep));
      if(a==='compare')await compare();
      if(a==='comparison-page')await comparisonDialog(state.comparisonPage+Number(el.dataset.woStep));
      if(a==='match'){
        for(const s of document.querySelectorAll('[data-wo-match]')){if(!s.value)throw Error('Укажите все соответствия');state.versionSettings.matches[s.dataset.woMatch]=s.value==='__new__'?'':s.value}
        await compareAgain();
      }
      if(a==='detail')await detail(el.dataset.woId);
      if(a==='event'){
        const mode=document.getElementById('wo-event-mode').value,reason=document.getElementById('wo-event-reason').value.trim(),date=document.getElementById('wo-event-date').value;
        if(!reason||mode==='date'&&!date)throw Error('Укажите основание и подтверждённую дату');
        const payload={expectedVersion:state.detail.version,mode,date:mode==='date'?date:null,reason,mutationId:crypto.randomUUID().replaceAll('-','')};
        await post('/api/writeoff-analytics/documents/'+state.detail.r.documentId+'/inventory-date',payload);closeModal();reload();toast('Уточнение записано в журнал; исходные даты актов сохранены');
      }
      if(a==='errors')await download('/api/decision-imports/'+state.job.id+'/errors.csv');
      if(a==='export'){
        const site=ui.page==='sites'?ui.siteId:null,d=dataset(site).data;if(!d)throw Error('Дождитесь данных панели');
        const q=query(site);q.set('expectedSnapshot',d.snapshot);await download(`/api/writeoff-analytics/export/${el.dataset.woKind}.${el.dataset.woExt}?${q}`);
      }
    });
  });
  let resizeFrame=0;
  window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{if(allowed()&&!ui.modal&&(ui.page==='reports'||ui.page==='sites'&&ui.siteId))render()})});
  document.addEventListener('change',event=>{
    const el=event.target;if(!allowed())return;
    if(el.matches('[data-wo-filter]')){const k=el.dataset.woFilter;state.filters[k]=el.value;delete state.filters[k+'Missing'];if(k==='reportYear')delete state.filters.reportPeriod;if(k==='reportPeriod'&&el.value)state.filters.reportYear=el.value.slice(0,4);state.page=1;reload()}
    if(el.matches('[data-wo-chart-top]')){state.chartTop=Number(el.value);render()}
    if(el.matches('[data-wo-chart-highlight]')){state.chartHighlight=el.value;render()}
    if(el.matches('[data-wo-chart-site]')){state.chartSites=[...document.querySelectorAll('[data-wo-chart-site]:checked')].map(s=>s.dataset.woChartSite);render()}
    if(el.matches('[data-wo-metric]')){state.metric=el.value;reload()}
    if(el.matches('[data-wo-sort]')){state.sort=el.value;render()}
    if(el.matches('[data-wo-top]')){state.top=Number(el.value);render()}
  });
})();
