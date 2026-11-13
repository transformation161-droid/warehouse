/* ИНСТРАКТ · Аналитика списаний. Отдельный модуль: исходные страницы и API проверок не изменяем. */
'use strict';
(()=>{
  const state={tab:'writeoffs',filters:{site:'',reportPeriod:'',reason:'',toolType:'',investigation:'',caseCategory:''},
    metric:'cost',page:1,cache:new Map(),pending:new Set(),errors:new Map(),job:null,importPeriod:'',importSheet:0,importCached:false,siteAliases:{}};
  const METRICS={inventories:'Инвентаризации по датам актов',quantity:'Списано, ед.',cost:'Стоимость, ₽',
    residual:'Остаточная стоимость, ₽',reimbursable:'К возмещению, ₽',reimbursed:'Возмещено, ₽',
    documents:'Документы',decisions:'Строки реестра'};
  const MONEY=new Set(['cost','residual','reimbursable','reimbursed','outstanding']);
  const number=v=>v===null||v===undefined?null:Number(v);
  const measure=(v,key)=>v==null?'—':MONEY.has(key)?rub(v):num(v);
  const value=(metrics,key)=>number(metrics&&metrics[key]&&metrics[key].value);
  const metricOptions=(selected)=>Object.entries(METRICS).map(([id,label])=>
    '<option value="'+attr(id)+'"'+(selected===id?' selected':'')+'>'+esc(label)+'</option>').join('');
  const input=(id,label,options,current)=>'<div class="field"><label>'+esc(label)+'</label><select data-wo-filter="'+attr(id)+'">'+
    '<option value="">Все</option>'+options.map(([v,n])=>'<option value="'+attr(v)+'"'+(v===current?' selected':'')+'>'+esc(n)+'</option>').join('')+'</select></div>';
  function query(site){
    const p=new URLSearchParams();
    const f={...state.filters};if(site)f.site=site;
    for(const [key,val] of Object.entries(f))if(val)p.set(key,val);
    p.set('chartMetric',state.metric);p.set('page',String(state.page));p.set('limit','50');
    return p;
  }
  const cacheKey=site=>query(site).toString();
  async function getJSON(url,options){
    const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options,
      headers:{'X-KSU-Expected-User':sessionUser.id,...(options&&options.headers||{})}});
    let result;try{result=await r.json()}catch{result={}};
    if(!r.ok)throw Error(typeof result.detail==='string'?result.detail:'Ошибка сервера: '+r.status);
    return result;
  }
  function refreshVisible(){
    if(!sessionUser)return;
    if(ui.page==='reports'||ui.page==='sites'&&ui.siteId)render();
  }
  function load(site){
    if(!sessionUser)return;
    const key=cacheKey(site);
    if(state.pending.has(key)||state.cache.has(key)||state.errors.has(key))return;
    state.pending.add(key);
    const p=query(site);
    getJSON('/api/writeoff-analytics?'+p.toString()).then(data=>state.cache.set(key,data))
      .catch(error=>state.errors.set(key,error.message))
      .finally(()=>{state.pending.delete(key);refreshVisible()});
  }
  function dataset(site){
    const key=cacheKey(site);
    if(!state.cache.has(key)&&!state.errors.has(key)&&!state.pending.has(key))
      queueMicrotask(()=>load(site));
    return {data:state.cache.get(key),error:state.errors.get(key),pending:state.pending.has(key)};
  }
  function kpis(d){
    const rows=[
      ['Инвентаризаций',value(d.metrics,'inventories'),'По датам актов','calendar','inventories'],
      ['Списано единиц',value(d.metrics,'quantity'),'Без нулевых строк','box','quantity'],
      ['Стоимость',value(d.metrics,'cost'),'Первоначальная стоимость','chart','cost'],
      ['Остаточная стоимость',value(d.metrics,'residual'),'Не складывается с полной','alert','residual'],
      ['К возмещению',value(d.metrics,'reimbursable'),'По данным актов','file','reimbursable'],
      ['Фактически возмещено',value(d.metrics,'reimbursed'),'По данным актов','check','reimbursed']
    ];
    return '<div class="grid grid-3 section-gap">'+rows.map(([label,v,sub,ic,key])=>metric(label,measure(v,key),sub,ic)).join('')+'</div>';
  }
  function barGroup(d,id,label,siteClickable=false){
    const items=d.charts[id]?.items||[];
    if(!items.length)return '<section class="card section-gap"><div class="card-title">'+esc(label)+'</div>'+empty('Нет данных')+'</section>';
    const high=Math.max(1,...items.map(x=>value(x.metrics,state.metric)||0));
    return '<section class="card section-gap"><div class="card-title-row"><div><div class="card-title">'+esc(label)+
      '</div><div class="card-subtitle">Показатель: '+esc(METRICS[state.metric])+'</div></div></div>'+
      '<div class="chart-list">'+items.map(x=>{
        const v=value(x.metrics,state.metric);
        const title=siteClickable?'<button class="link" data-wo-site="'+attr(x.key)+'">'+esc(x.label)+'</button>':esc(x.label);
        return '<div class="chart-item"><span class="chart-item-name">'+title+'</span>'+
        '<span class="bar-track"><span class="bar-fill" style="width:'+(v==null?0:100*v/high)+'%"></span></span>'+
        '<span class="chart-value">'+measure(v,state.metric)+'</span></div>';
      }).join('')+'</div></section>';
  }
  function siteMatrix(d){
    const matrix=d.matrix||[];
    const keys=[...new Set(matrix.map(x=>x.period))].sort();
    const periods=keys.filter(x=>x).concat(keys.includes('')?['']:[]);
    const map=new Map(matrix.map(x=>[x.siteId+'|'+x.period,x.metrics]));
    const sites=db.sites.slice().sort((a,b)=>{
      const total=id=>matrix.filter(x=>x.siteId===id).reduce((s,x)=>s+(value(x.metrics,state.metric)||0),0);
      return total(b.id)-total(a.id)||a.name.localeCompare(b.name,'ru');
    });
    return '<section class="card section-gap"><div class="card-title-row"><div><div class="card-title">Динамика по всем объектам</div>'+
      '<div class="card-subtitle">Сравнение отчётных кварталов. «—» означает отсутствие загруженных данных.</div></div></div>'+
      '<div class="table-wrap"><table class="table"><thead><tr><th>Объект</th>'+
      periods.map(x=>'<th>'+esc(x||'Без квартала')+'</th>').join('')+'<th>Всего</th><th>Изменение*</th></tr></thead><tbody>'+
      sites.map(site=>{
        const vals=periods.map(p=>value(map.get(site.id+'|'+p),state.metric));
        const known=vals.filter(x=>x!==null);
        const total=known.length?known.reduce((a,b)=>a+b,0):null;
        const pvals=periods.filter(x=>x).map(p=>value(map.get(site.id+'|'+p),state.metric));
        const last=pvals.length>1?pvals[pvals.length-1]:null,prev=pvals.length>1?pvals[pvals.length-2]:null;
        const diff=last===null||prev===null?'—':measure(last-prev,state.metric)+(prev?' ('+((last-prev)/prev*100).toFixed(1)+'%)':'');
        return '<tr><td><button class="link" data-wo-site="'+attr(site.id)+'">'+esc(site.name)+'</button></td>'+
          vals.map(v=>'<td>'+measure(v,state.metric)+'</td>').join('')+
          '<td><strong>'+measure(total,state.metric)+'</strong></td><td>'+esc(diff)+'</td></tr>';
      }).join('')+'</tbody></table></div><p class="tiny muted">* Изменение между последними двумя отображаемыми отчётными периодами при наличии данных в обоих.</p></section>';
  }
  function rowsTable(d){
    const rows=d.rows||[];
    return '<section class="card section-gap"><div class="card-title-row"><div><div class="card-title">Реестр списаний</div>'+
      '<div class="card-subtitle">Показано '+rows.length+' из '+d.totalRows+' действующих строк</div></div>'+
      '<button class="btn secondary" data-wo-export="rows">Выгрузить XLSX</button></div>'+
      '<div class="table-wrap"><table class="table"><thead><tr><th>Дата акта</th><th>Объект</th><th>Документ</th>'+
      '<th>Инструмент</th><th>Причина</th><th>Количество</th><th>Стоимость</th><th>Остаточная стоимость</th></tr></thead><tbody>'+
      rows.map(r=>'<tr><td>'+esc(fmtDate(r.date))+'</td><td>'+esc(r.siteName)+'</td><td>'+esc(r.document)+'</td>'+
        '<td>'+esc(r.toolType)+'</td><td>'+esc(r.reason)+'</td><td>'+measure(number(r.quantity),'quantity')+
        '</td><td>'+measure(number(r.cost),'cost')+'</td><td>'+measure(number(r.residual),'residual')+'</td></tr>').join('')+
      '</tbody></table></div><div class="actions-row section-gap">'+
      '<button class="btn secondary" data-wo-page="-1"'+(d.page<=1?' disabled':'')+'>Предыдущая</button>'+
      '<button class="btn secondary" data-wo-page="1"'+(d.page*d.limit>=d.totalRows?' disabled':'')+'>Следующая</button>'+
      '</div></section>';
  }
  function notices(d){
    const q=d.quality||{},m=d.metrics||{};
    const issues=[];
    if(q.overpaymentRows)issues.push('Есть строки, где фактическое возмещение превышает назначенное: '+q.overpaymentRows+'.');
    if(q.missingDateRows)issues.push('Без даты акта: '+q.missingDateRows+' строк.');
    for(const field of ['cost','residual','reimbursable','reimbursed']){
      if(m[field]?.missingRows)issues.push('Не заполнено «'+METRICS[field]+'»: '+m[field].missingRows+' строк.');
    }
    return '<div class="notice section-gap">Инвентаризация считается по уникальным датам актов внутри объекта. Отчётный квартал может отличаться от календарной даты акта. Данные предварительные.</div>'+
      (issues.length?'<div class="notice warn section-gap">'+issues.map(esc).join(' ')+'</div>':'');
  }
  function filters(d){
    const f=state.filters;
    const periods=(d?.charts?.periods?.items||[]).map(x=>[x.key,x.label]).filter(x=>x[0]);
    if(f.reportPeriod&&!periods.some(x=>x[0]===f.reportPeriod))periods.unshift([f.reportPeriod,f.reportPeriod]);
    const facet=(key)=>((d?.facets?.[key]?.values)||[]).filter(Boolean).map(x=>[x,x]);
    return '<div class="card section-gap"><div class="filterbar">'+
      input('site','Объект',db.sites.map(x=>[x.id,x.name]),f.site)+
      input('reportPeriod','Отчётный квартал',periods,f.reportPeriod)+
      input('reason','Причина',facet('reason'),f.reason)+
      input('toolType','Тип ТМЦ',facet('toolType'),f.toolType)+
      input('investigation','Расследование',facet('investigation'),f.investigation)+
      input('caseCategory','Категория',facet('caseCategory'),f.caseCategory)+
      '<div class="field"><label>Показатель</label><select data-wo-metric>'+metricOptions(state.metric)+'</select></div>'+
      '<button class="btn secondary" data-wo-action="clear">Сбросить</button>'+
      '<button class="btn secondary" data-wo-action="reload">Обновить</button></div></div>';
  }
  function reportScreen(){
    const result=dataset(null),d=result.data;
    return heading('Аналитика инвентаризаций и списаний','Сводная динамика по объектам на основе загруженных актов',
      (sessionUser?.role==='manager'?'<button class="btn primary" data-wo-action="import">Загрузить XLSX</button>':'')+
      '<button class="btn secondary" data-wo-export="summary">Сводка XLSX</button>')+
      filters(d)+(result.error?'<div class="notice warn section-gap">'+esc(result.error)+
        ' <button class="btn secondary" data-wo-action="reload">Повторить</button></div>':
      !d?'<div class="card section-gap">'+empty('Загружаем аналитику','Проверяем данные и доступные объекты')+'</div>':
      kpis(d)+notices(d)+siteMatrix(d)+
      '<div class="grid grid-2 section-gap"><div>'+barGroup(d,'periods','Динамика по отчётным кварталам')+
      barGroup(d,'months','Динамика по датам актов')+'</div><div>'+
      barGroup(d,'reasons','Причины списаний')+barGroup(d,'tools','Типы инструмента')+'</div></div>'+
      barGroup(d,'sites','Объекты по выбранному показателю',true)+rowsTable(d));
  }
  function siteScreen(id){
    const {data:d,error}=dataset(id);
    if(error)return '<section class="card section-gap"><div class="card-title">Аналитика списаний</div><div class="notice warn">'+esc(error)+'</div></section>';
    if(!d)return '<section class="card section-gap"><div class="card-title">Аналитика списаний</div>'+empty('Загружаем данные объекта')+'</section>';
    return '<section class="card section-gap"><div class="card-title-row"><div><div class="card-title">История и аналитика инвентаризаций</div>'+
      '<div class="card-subtitle">Инвентаризации по уникальным датам актов, динамика списаний и возмещений</div></div>'+
      '<button class="btn secondary" data-wo-action="show-site" data-wo-site-id="'+attr(id)+'">Все разрезы</button></div>'+
      kpis(d)+notices(d)+'</section>'+barGroup(d,'periods','Динамика объекта по кварталам')+
      rowsTable(d);
  }
  const originalReports=renderReports;
  renderReports=function(){
    const tabs='<div class="tab-line"><button type="button" class="'+(state.tab==='writeoffs'?'active':'')+
      '" data-wo-tab="writeoffs">Инвентаризации и списания</button><button type="button" class="'+
      (state.tab==='other'?'active':'')+'" data-wo-tab="other">Остальные отчёты</button></div>';
    return tabs+(state.tab==='other'?originalReports():reportScreen());
  };
  const originalSites=renderSites;
  renderSites=function(){
    const original=originalSites();
    if(!ui.siteId)return original;
    // Legacy manual count remains in DB and API; hide only its outdated view.
    const cleaned=original.replace(/<section class="card section-gap"><div class="card-title-row"><div class="card-title">История инвентаризаций<\/div><\/div>[\s\S]*?<\/section>$/,'');
    return cleaned+siteScreen(ui.siteId);
  };
  const legacy=navEntries.findIndex(x=>x[0]==='inventory');
  if(legacy>=0)navEntries.splice(legacy,1);
  const currentYear=new Date().getFullYear();
  state.importPeriod=currentYear+'-Q'+(Math.floor(new Date().getMonth()/3)+1);

  function importForm(){
    const opts=[];for(let y=currentYear;y>=2024;y--)for(let q=4;q>=1;q--)
      opts.push(y+'-Q'+q);
    openModal('<div class="notice">Загрузите лист «Ввод_списаний» из файла XLSX. Данные сначала проверяются, запись в общую базу только после подтверждения.</div>'+
      '<div class="field section-gap"><label>Файл XLSX *</label><input id="wo-file" type="file" accept=".xlsx" required></div>'+
      '<div class="form-grid section-gap"><div class="field"><label>Отчётный квартал *</label><select id="wo-period">'+
      opts.map(p=>'<option value="'+p+'"'+(p===state.importPeriod?' selected':'')+'>'+p+'</option>').join('')+
      '</select></div><div class="field"><label>Номер листа</label><select id="wo-sheet"><option value="0">Ввод_списаний (1-й лист)</option></select></div></div>'+
      '<label class="small section-gap" style="display:block"><input id="wo-cached" type="checkbox"> Разрешить сохранённые значения формул (проверить актуальность в Excel)</label>'+
      '<div id="wo-feedback" class="section-gap"></div>', 'Загрузка отчёта по списаниям','Проверка до внесения данных',true,
      '<button class="btn secondary" data-action="close-modal">Закрыть</button>'+
      '<button class="btn primary" data-wo-action="preview">Проверить файл</button>');
  }
  function feedback(message,bad=false){
    const element=document.getElementById('wo-feedback');
    if(element)element.innerHTML='<div class="notice'+(bad?' warn':'')+'">'+message+'</div>';
  }
  async function jobStatus(id,expected){
    for(let i=0;i<150;i++){
      const j=await getJSON('/api/decision-imports/'+encodeURIComponent(id));
      if(j.status==='error'||j.status==='invalid'||j.status==='cancelled'||j.status==='mapping'||j.status==='ready'||j.status==='done'){
        if(expected==='inspect'&&j.status==='uploaded')return j;
        if(expected!=='inspect')return j;
        if(['error','cancelled'].includes(j.status))return j;
      }
      if(expected==='inspect'&&j.status==='uploaded')return j;
      await new Promise(done=>setTimeout(done,900));
    }
    throw Error('Проверка файла длится слишком долго. Откройте раздел импорта повторно.');
  }
  async function previewFile(){
    const file=document.getElementById('wo-file')?.files?.[0];
    if(!file||!file.name.toLowerCase().endsWith('.xlsx'))return feedback('Выберите файл XLSX.',true);
    state.siteAliases={};
    state.importPeriod=document.getElementById('wo-period').value;
    state.importSheet=Number(document.getElementById('wo-sheet').value);
    state.importCached=!!document.getElementById('wo-cached').checked;
    if(!confirm('Проверить '+file.name+' за '+state.importPeriod+'? Ничего пока не будет записано.'))return;
    feedback('Загружаем файл и читаем структуру…');
    try{
      const response=await getJSON('/api/decision-imports',{
        method:'POST',body:file,headers:{'X-KSU-Filename':encodeURIComponent(file.name),'X-CSRF-Token':sessionUser.csrf}});
      state.job=response.id;
      const uploaded=await jobStatus(response.id,'inspect');
      if(uploaded.status==='error')throw Error(uploaded.error||'Ошибка анализа файла');
      const sheetList=uploaded.sheets||[];
      feedback('Листов в файле: '+sheetList.map(x=>esc(x.name)).join(', ')+'. Проверяем строки…');
      await doPreview({});
    }catch(e){feedback(esc(e.message),true)}
  }
  async function doPreview(mapping){
    if(!state.job)return;
    try{
      const response=await getJSON('/api/decision-imports/'+state.job+'/preview',{
        method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':sessionUser.csrf},
        body:JSON.stringify({sheet:state.importSheet,headerRow:1,firstRow:2,lastRow:100000,
          mapping,siteAliases:state.siteAliases,allowCached:state.importCached,reportPeriod:state.importPeriod})});
      const status=await jobStatus(response.id,'preview');
      if(status.status==='error')throw Error(status.error||'Ошибка проверки книги');
      const errs=(status.errors||[]).slice(0,12).map(x=>'Строка '+x.row+', '+esc(x.column)+': '+esc(x.reason)+
        '. '+esc(x.fix)).join('<div class="divider"></div>');
      if(status.status==='mapping'){
        const fields={'date':'Дата акта','site':'Объект','document':'Документ','decisionType':'Тип решения','toolType':'Тип инструмента',
          'quantity':'Кол-во','cost':'Стоимость, ₽','residual':'Остаточная стоимость, ₽','reason':'Причина списания',
          'caseCategory':'Категория случая','investigation':'Статус расследования','reimbursable':'К возмещению, ₽','reimbursed':'Фактически возмещено, ₽',
          'responsible':'Ответственное лицо / МОЛ','sourceId':'ID строки','decision':'Результат / решение','comment':'Комментарий'};
        const headers=status.headers||{};
        const selectors=Object.entries(fields).map(([key,label])=>'<div class="field"><label>'+esc(label)+'</label><select data-wo-map="'+key+'"><option value="">Не используется</option>'+
          Object.entries(headers).map(([column,name])=>'<option value="'+attr(column)+'"'+
          (status.mapping?.[key]===column?' selected':'')+'>'+esc(column+' · '+name)+'</option>').join('')+
          '</select></div>').join('');
        feedback('Не удалось однозначно сопоставить колонки: '+esc((status.missing||[]).join(', '))+
          '<div class="form-grid section-gap">'+selectors+'</div>'+
          '<button class="btn primary section-gap" data-wo-action="remap">Повторить с выбранными колонками</button>',true);
        return;
      }
      let msg='Прочитано: '+status.processed+' строк; принято: <strong>'+status.accepted+
        '</strong>; пропущено пустых: '+status.skipped+'; ошибок: '+status.rejected+'.';
      if(status.warning)msg+='<p>'+esc(status.warning)+'</p>';
      if(errs)msg+='<div class="notice warn section-gap">'+errs+'</div>';
      if(status.rejected)msg+='<a class="link" href="/api/decision-imports/'+state.job+'/errors.csv">Скачать все ошибки CSV</a>';
      if(status.unknownSites?.length){
        msg+='<p class="section-gap"><strong>Неопознанные названия объектов. Укажите, какому объекту в ИНСТРАКТ соответствует каждое название:</strong></p>';
        msg+='<div class="form-grid">'+status.unknownSites.map(name=>'<div class="field"><label>'+esc(name)+'</label><select data-wo-alias="'+attr(name)+'"><option value="">Не сопоставлен</option>'+
          db.sites.map(site=>'<option value="'+attr(site.id)+'"'+(state.siteAliases[name]===site.id?' selected':'')+'>'+esc(site.name)+'</option>').join('')+
          '</select></div>').join('')+'</div><button class="btn primary section-gap" data-wo-action="alias">Сопоставить объекты и проверить повторно</button>';
      }
      if(status.status==='ready')msg+='<p class="section-gap"><button class="btn primary" data-wo-action="commit">Сохранить '+status.accepted+' строк в общую базу</button></p>';
      feedback(msg,status.status!=='ready');
    }catch(e){feedback(esc(e.message),true)}
  }
  async function commit(){
    if(!state.job)return;
    if(!confirm('Подтвердить импорт проверенных строк за '+state.importPeriod+' в общую базу?'))return;
    feedback('Сохраняем проверенные строки…');
    try{
      const response=await getJSON('/api/decision-imports/'+state.job+'/commit',{
        method:'POST',headers:{'X-CSRF-Token':sessionUser.csrf}});
      const result=response.status==='done'?response:await jobStatus(state.job,'commit');
      if(result.status!=='done')throw Error(result.error||'Запись не подтверждена');
      feedback(result.duplicate?'Этот отчёт уже загружен — дубликат не создан.':
        'Сохранено '+result.committed+' строк за '+esc(state.importPeriod)+'.');
      state.cache.clear();state.errors.clear();state.page=1;
    }catch(e){feedback(esc(e.message),true)}
  }
  function reload(){state.cache.clear();state.errors.clear();state.page=1;refreshVisible()}
  document.addEventListener('click',event=>{
    const site=event.target.closest('[data-wo-site]');
    if(site){event.preventDefault();state.page=1;navTo('sites',site.dataset.woSite);return}
    const tab=event.target.closest('[data-wo-tab]');
    if(tab){event.preventDefault();state.tab=tab.dataset.woTab;render();return}
    const page=event.target.closest('[data-wo-page]');
    if(page){state.page=Math.max(1,state.page+Number(page.dataset.woPage));refreshVisible();return}
    const exp=event.target.closest('[data-wo-export]');
    if(exp){event.preventDefault();window.location.href='/api/writeoff-analytics/export/'+exp.dataset.woExport+
      '.xlsx?'+query(ui.page==='sites'?ui.siteId:null).toString();return}
    const action=event.target.closest('[data-wo-action]');
    if(!action)return;
    event.preventDefault();
    switch(action.dataset.woAction){
      case 'import':importForm();break;
      case 'preview':previewFile();break;
      case 'commit':commit();break;
      case 'clear':state.filters={site:'',reportPeriod:'',reason:'',toolType:'',investigation:'',caseCategory:''};reload();break;
      case 'reload':reload();break;
      case 'alias':{
        state.siteAliases=Object.fromEntries([...document.querySelectorAll('[data-wo-alias]')]
          .map(e=>[e.dataset.woAlias,e.value]).filter(([,v])=>v));
        doPreview({});break}
      case 'remap':{const mapping=Object.fromEntries([...document.querySelectorAll('[data-wo-map]')]
        .map(e=>[e.dataset.woMap,e.value]).filter(([,v])=>v));doPreview(mapping);break}
      case 'show-site':state.filters.site=action.dataset.woSiteId;state.tab='writeoffs';navTo('reports');break;
    }
  });
  document.addEventListener('change',event=>{
    if(event.target.matches('[data-wo-filter]')){
      state.filters[event.target.dataset.woFilter]=event.target.value;reload();
    }
    if(event.target.matches('[data-wo-metric]')){state.metric=event.target.value;reload()}
  });
})();