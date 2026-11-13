/* Read-only source statement. URLs come from the decision field; never constructed or fetched. */
'use strict';
(()=>{
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const FIELDS={sourceId:'ID строки',date:'Дата акта',site:'Объект',document:'Документ',decisionType:'Тип решения',toolType:'Тип инструмента',quantity:'Кол-во',cost:'Полная стоимость, ₽',residual:'Остаточная стоимость, ₽',reason:'Причина',caseCategory:'Категория',investigation:'Расследование',decision:'Результат / решение',reimbursable:'К возмещению, ₽',reimbursed:'Фактически возмещено, ₽',responsible:'МОЛ',comment:'Комментарий'};
  const NUMBERS=new Set(['quantity','cost','residual','reimbursable','reimbursed']);
  function safeURL(raw){
    if(/[\u0000-\u0020\u007f]/.test(raw))return null;
    try{
      const url=new URL(raw);
      if(!['http:','https:'].includes(url.protocol)||!url.hostname||url.username||url.password)return null;
      return url.href;
    }catch{return null}
  }
  function text(value){
    if(value==null||value==='')return '<span class="muted">—</span>';
    const source=String(value),pattern=/https?:\/\/[^\s<>"']+/gi;let end=0,result='',match;
    while((match=pattern.exec(source))){
      result+=esc(source.slice(end,match.index));let token=match[0],suffix='';
      while(/[.,;!?]$/.test(token)){suffix=token.slice(-1)+suffix;token=token.slice(0,-1)}
      for(const [open,close] of [['(',')'],['[',']'],['{','}']])while(token.endsWith(close)&&token.split(close).length>token.split(open).length){suffix=close+suffix;token=token.slice(0,-1)}
      const url=safeURL(token);
      result+=url?`<a class="wo-task-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="Открыть исходную ссылку на задачу в новой вкладке">${esc(token)}</a>`:esc(token);
      result+=esc(suffix);end=pattern.lastIndex;
    }
    return result+esc(source.slice(end));
  }
  function cell(row,key,format,date){
    const v=row[key];
    if(key==='decision')return text(v);
    if(NUMBERS.has(key))return esc(format(v,key));
    if(key==='date')return `${esc(date(v))}<small class="wo-source-context">${esc(row.reportPeriod||'Без отчётного квартала')}</small>`;
    return v==null||v===''?'<span class="muted">—</span>':esc(v);
  }
  const history=r=>`<button type="button" class="btn secondary" data-wo-action="detail" data-wo-id="${esc(r.id)}">Источник / версии</button><small class="wo-source-context">${esc(r.sheet)} · строка ${esc(r.sourceRow)} · версия ${esc(r.currentVersion)}</small>`;
  function render(d,{siteName,format,date}){
    const keys=Object.keys(FIELDS),start=d.totalRows&&(d.page-1)*d.limit<d.totalRows?(d.page-1)*d.limit+1:0,end=start?Math.min(d.page*d.limit,d.totalRows):0;
    return `<section class="card section-gap wo-statement" id="wo-registry" data-wo-statement><div class="card-title-row"><div><div class="card-title">Полная ведомость списаний · ${esc(siteName)}</div><div class="card-subtitle">${d.totalRows} действующих строк · показано ${start}–${end} · страница ${d.page}</div><p class="tiny muted">17 исходных полей A:Q. Каждая позиция показана отдельно; нулевые строки сохранены. «—» — поле не заполнено. Все страницы входят в выгрузку.</p></div><div class="page-actions"><button class="btn secondary" data-wo-action="export" data-wo-kind="rows" data-wo-ext="xlsx">Строки XLSX</button><button class="btn secondary" data-wo-action="export" data-wo-kind="rows" data-wo-ext="csv">CSV</button></div></div><div class="table-wrap wo-source-scroll" tabindex="0" role="region" aria-label="Полная ведомость: прокрутка по строкам и столбцам"><table class="table wo-source-table"><thead><tr>${keys.map(k=>`<th data-wo-column="${k}" scope="col">${esc(FIELDS[k])}</th>`).join('')}<th scope="col">Источник и история</th></tr></thead><tbody>${d.rows.map(r=>`<tr data-wo-source-row="${esc(r.id)}">${keys.map(k=>`<td data-wo-column="${k}"${['reimbursable','reimbursed'].includes(k)?` data-wo-field="${k}"`:''}>${cell(r,k,format,date)}</td>`).join('')}<td>${history(r)}</td></tr>`).join('')}</tbody></table></div><div class="wo-source-cards">${d.rows.map(r=>`<article class="wo-source-card" data-wo-source-card="${esc(r.id)}"><h4>${esc(r.toolType??'Тип инструмента не указан')}</h4><dl>${keys.map(k=>`<div><dt>${esc(FIELDS[k])}</dt><dd data-wo-column="${k}">${cell(r,k,format,date)}</dd></div>`).join('')}</dl>${history(r)}</article>`).join('')}</div>${d.totalRows?'':'<p class="notice">Нет загруженных данных для выбранного объекта и фильтров.</p>'}<div class="actions-row section-gap"><button class="btn secondary" data-wo-action="page" data-wo-step="-1"${d.page<=1?' disabled':''}>Предыдущая</button><span class="small muted">${start}–${end} из ${d.totalRows}</span><button class="btn secondary" data-wo-action="page" data-wo-step="1"${d.page*d.limit>=d.totalRows?' disabled':''}>Следующая</button></div></section>`;
  }
  window.WriteoffStatement=Object.freeze({text,safeURL,render,fields:FIELDS});
})();
