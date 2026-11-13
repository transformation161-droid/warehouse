/* SVG charts of the existing actor-scoped API snapshot. No network, storage or writes. */
'use strict';
(()=>{
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const COLORS=['#2269ad','#15948c','#d38a25','#8c64b2','#c35f72','#577b3e','#956b45','#457f9f','#7563a3','#a34f73'];
  const METRICS={quantity:'Количество, ед.',cost:'Первоначальная стоимость, ₽',residual:'Остаточная стоимость, ₽',reimbursable:'К возмещению, ₽',reimbursed:'Фактически возмещено, ₽',inventories:'Инвентаризации по датам',documents:'Документы',decisions:'Строки реестра'};
  const val=(m,k)=>m?.[k]?.value??null;
  const fmt=(v,k)=>v==null?'Нет загруженных данных':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:6}).format(v)+(['cost','residual','reimbursable','reimbursed'].includes(k)?' ₽':'');
  const opt=(v,t,current)=>`<option value="${esc(v)}"${String(v)===String(current)?' selected':''}>${esc(t)}</option>`;
  const quarterNumber=p=>Number(p.slice(0,4))*4+Number(p.at(-1))-1;
  const quarterLabel=p=>`${['I','II','III','IV'][Number(p.at(-1))-1]} кв. ${p.slice(0,4)}`;
  function periods(d){
    const loaded=[...new Set([...(d.matrixPeriods||[]),...(d.charts.periods?.items||[]).map(x=>x.key)].filter(p=>/^\d{4}-Q[1-4]$/.test(p)))].sort();
    if(!loaded.length)return {items:[],clipped:false};
    const first=quarterNumber(loaded[0]),last=quarterNumber(loaded.at(-1)),start=Math.max(first,last-23),items=[];
    for(let n=start;n<=last;n++)items.push(Math.floor(n/4)+'-Q'+(n%4+1));
    return {items,clipped:start>first};
  }
  function compareDecimal(a,b){
    if(a==null||b==null)return a==null?(b==null?0:-1):1;
    const parts=v=>{const [i,f='']=String(v).split('.');return [i.replace(/^0+(?=\d)/,''),f]};
    const [ai,af]=parts(a),[bi,bf]=parts(b);return ai.length-bi.length||ai.localeCompare(bi)||af.padEnd(6,'0').localeCompare(bf.padEnd(6,'0'));
  }
  function series(d,o){
    const visible=(d.matrixSites||[]).filter(s=>o.sites==null||o.sites.includes(s.id)).sort((a,b)=>compareDecimal(val(b.metrics,o.metric),val(a.metrics,o.metric))||a.name.localeCompare(b.name,'ru'));
    const selected=o.top?visible.slice(0,o.top):visible,lookup=new Map((d.matrix||[]).map(r=>[r.siteId+'|'+r.period,r.metrics]));
    return selected.map((s,i)=>({...s,color:COLORS[i%COLORS.length],points:periods(d).items.map(period=>({period,value:val(lookup.get(s.id+'|'+period),o.metric)}))}));
  }
  function scale(values,w=640,h=280,bottomPad=48){
    const max=values.reduce((m,v)=>v==null?m:Math.max(m,Number(v)),0),power=10**Math.floor(Math.log10(max||1)),high=max?Math.ceil(max/power/2)*power*2:1;
    return {w,h,left:w<420?54:76,right:w-24,top:36,bottom:h-bottomPad,high,y:v=>h-bottomPad-(h-bottomPad-36)*Number(v)/high};
  }
  function axes(a,labels){
    const short=new Intl.NumberFormat('ru-RU',{notation:'compact',maximumFractionDigits:1});
    return Array.from({length:5},(_,i)=>{
      const n=a.high*i/4,y=a.y(n);return `<line class="wo-plot-grid" x1="${a.left}" y1="${y}" x2="${a.right}" y2="${y}"/><text class="wo-axis-label" x="${a.left-10}" y="${y+4}" text-anchor="end">${esc(short.format(n))}</text>`;
    }).join('')+`<line class="wo-plot-axis" x1="${a.left}" y1="${a.bottom}" x2="${a.right}" y2="${a.bottom}"/>`+labels.map((label,i)=>`<text class="wo-axis-label" x="${xAt(a,i,labels.length)}" y="${a.bottom+24}" text-anchor="middle">${(Array.isArray(label)?label:[label]).map((line,n)=>`<tspan x="${xAt(a,i,labels.length)}" dy="${n?13:0}">${esc(line)}</tspan>`).join('')}</text>`).join('');
  }
  const xAt=(a,i,n)=>a.left+(a.right-a.left)*(i+.5)/Math.max(1,n);
  const action=attrs=>Object.entries(attrs).map(([k,v])=>` ${k}="${esc(v)}"`).join('');
  function mark(a,x,value,tip,color,attrs={},label=false){
    const missing=value==null,y=missing?a.bottom:a.y(value),text=missing?'—':fmt(value,attrs['data-wo-metric-key']);
    const plot=missing?`<text x="${x}" y="${a.bottom-8}" text-anchor="middle" class="wo-missing">—</text>`:`<circle cx="${x}" cy="${y}" r="5" fill="${color}" stroke="white" stroke-width="2"/>${label?`<text class="wo-value-label" x="${x}" y="${Math.max(a.top-12,y-13)}" text-anchor="middle">${esc(text)}</text>`:''}`;
    return `<g class="wo-chart-point" tabindex="0" role="button" aria-label="${esc(tip)}" data-wo-tip="${esc(tip)}" data-wo-chart-value="${esc(value??'')}" data-wo-chart-state="${missing?'missing':Number(value)===0?'zero':'known'}"${action(attrs)}><title>${esc(tip)}</title><circle cx="${x}" cy="${y}" r="16" fill="transparent"/>${plot}</g>`;
  }
  function linePath(a,values){
    let open=false;return values.map((v,i)=>{if(v==null){open=false;return ''}const p=`${xAt(a,i,values.length)},${a.y(v)}`;const code=open?'L':'M';open=true;return code+p}).filter(Boolean).join(' ');
  }
  function svg(a,body,label){return `<div class="wo-plot-scroll" tabindex="0" aria-label="${esc(label)}"><svg class="wo-plot" style="min-width:${a.w}px" viewBox="0 0 ${a.w} ${a.h}" role="img" aria-label="${esc(label)}">${body}</svg></div>`}
  function empty(){return '<div class="wo-chart-empty">Нет загруженных данных для выбранного среза</div>'}
  function trend(d,o,ps){
    const lookup=new Map(d.charts.periods.items.map(r=>[r.key,r.metrics])),values=ps.map(p=>val(lookup.get(p),o.metric)),a=scale(values,Math.max(o.width,ps.length*78));
    return `<section class="card wo-chart" data-wo-chart="trend"><div class="card-title">${o.site?'Тренд объекта':'Динамика по отчётным кварталам'}</div><p class="card-subtitle">${esc(METRICS[o.metric])} · отчётный квартал из импорта</p>${!ps.length?empty():svg(a,axes(a,ps.map(quarterLabel))+`<path d="${linePath(a,values)}" fill="none" stroke="${COLORS[0]}" stroke-width="3"/>`+ps.map((p,i)=>mark(a,xAt(a,i,ps.length),values[i],`${quarterLabel(p)}. ${METRICS[o.metric]}: ${fmt(values[i],o.metric)}. Открыть строки квартала.`,COLORS[0],{'data-wo-cross':'reportPeriod','data-wo-value':p,'data-wo-metric-key':o.metric},ps.length<8)).join(''),'Квартальная динамика')}<div class="wo-chart-caption">Разрыв и «—» — нет данных. Точка на нуле — подтверждённый 0. Даты актов не меняют отчётный квартал.</div></section>`;
  }
  function comparison(d,o,ps,items){
    const a=scale(items.flatMap(s=>s.points.map(p=>p.value)),Math.max(o.width,ps.length*78));
    const controls=`<div class="wo-comparison-controls"><div class="field"><label>Объекты на графике</label><select data-wo-chart-top>${[[5,'Топ-5'],[10,'Топ-10'],[0,'Все']].map(([v,t])=>opt(v,t,o.top)).join('')}</select></div><div class="field"><label>Выделить объект</label><select data-wo-chart-highlight>${opt('','Все линии','')}${items.map(s=>opt(s.id,s.name,o.highlight)).join('')}</select></div></div>`;
    return `<section class="card wo-chart" data-wo-chart="comparison"><div class="card-title">Сравнение объектов по кварталам</div>${controls}${!items.length||!ps.length?empty():svg(a,axes(a,ps.map(quarterLabel))+items.map(s=>{
      const faded=o.highlight&&o.highlight!==s.id,values=s.points.map(p=>p.value);
      return `<g data-wo-series="${esc(s.id)}" opacity="${faded?'0.18':1}"><path d="${linePath(a,values)}" fill="none" stroke="${s.color}" stroke-width="${o.highlight===s.id?4:2.5}"/>${s.points.map((p,i)=>mark(a,xAt(a,i,ps.length),p.value,`${s.name}. ${quarterLabel(p.period)}. ${METRICS[o.metric]}: ${fmt(p.value,o.metric)}. Открыть паспорт объекта.`,s.color,{'data-wo-site':s.id})).join('')}</g>`;
    }).join(''),'Сравнение объектов на единой оси кварталов')}<div class="wo-chart-legend">${items.map(s=>`<button type="button" class="link" data-wo-site="${esc(s.id)}"><i style="background:${s.color}"></i>${esc(s.name)}</button>`).join('')}</div><div class="wo-chart-caption">Нажмите точку или название — откроется существующий паспорт объекта. Выбор линий и Top-N меняют отображение; общие фильтры и выгрузки остаются едиными.</div></section>`;
  }
  function structure(d,o){
    const group=o.structure==='tools'?'tools':'reasons',field=group==='tools'?'toolType':'reason',all=d.charts[group].items,items=o.top?all.slice(0,o.top):all;
    const a=scale(items.map(x=>val(x.metrics,o.metric)),Math.max(o.structureWidth||o.width,items.length*96),320,80),barWidth=Math.min(62,(a.right-a.left)/Math.max(1,items.length)*.55);
    const label=x=>{
      const limit=Math.max(12,Math.floor((a.right-a.left)/Math.max(1,items.length)/6)),lines=[''];
      for(const word of x.label.split(/\s+/)){
        const last=lines.length-1;if((lines[last]+' '+word).trim().length<=limit)lines[last]=(lines[last]+' '+word).trim();
        else if(lines.length<2)lines.push(word.length>limit?word.slice(0,limit-1)+'…':word);
        else{lines[last]=lines[last].slice(0,limit-1)+'…';break}
      }
      return lines.filter(Boolean);
    };
    const body=axes(a,items.map(label))+items.map((item,i)=>{
      const v=val(item.metrics,o.metric),x=xAt(a,i,items.length),y=v==null?a.bottom:a.y(v),tip=`${item.label}. ${METRICS[o.metric]}: ${fmt(v,o.metric)}${item.sharePercent==null?'':`. Доля: ${item.sharePercent}%`}. Открыть отфильтрованный реестр.`;
      return `${v==null?'':`<g class="wo-chart-bar" data-wo-tip="${esc(tip)}"${action({'data-wo-cross':field,'data-wo-value':item.key??'','data-wo-missing':item.key==null?'1':''})}><rect x="${x-barWidth/2}" y="${y}" width="${barWidth}" height="${a.bottom-y}" rx="4" fill="${COLORS[i%COLORS.length]}"/></g>`}${mark(a,x,v,tip,COLORS[i%COLORS.length],{'data-wo-cross':field,'data-wo-value':item.key??'','data-wo-missing':item.key==null?'1':'','data-wo-metric-key':o.metric},items.length<=10)}${item.sharePercent==null?'':`<text class="wo-axis-label" x="${x}" y="${a.bottom+62}" text-anchor="middle">${esc(item.sharePercent)}%</text>`}`;
    }).join('');
    return `<section class="card wo-chart wo-structure" data-wo-chart="structure"><div class="card-title-row"><div><div class="card-title">Структура списаний${o.site?' объекта':''}</div><p class="card-subtitle">${esc(METRICS[o.metric])} · ${items.length} из ${all.length} групп · нажмите точку для детализации</p></div><div class="tab-line"><button type="button" data-wo-chart-structure="reasons" class="${group==='reasons'?'active':''}">Причины</button><button type="button" data-wo-chart-structure="tools" class="${group==='tools'?'active':''}">Типы ТМЦ</button></div></div>${items.length?svg(a,body,group==='reasons'?'Диаграмма причин списаний':'Диаграмма типов ТМЦ'):empty()}</section>`;
  }
  function render(d,options){
    const o={metric:'cost',top:5,highlight:'',structure:'reasons',sites:null,site:null,width:560,...options},range=periods(d),items=series(d,o),hasUnknown=d.charts.periods.items.some(p=>!p.key);
    return `<section class="wo-chart-section section-gap" id="wo-charts"><div class="wo-charts-heading"><div><h3>Графики аналитики</h3><p>Единый срез отчёта · ${o.site?'динамика объекта':'все разрешённые объекты'}</p></div><div class="field"><label>Показатель графиков и матрицы</label><select data-wo-metric>${Object.entries(METRICS).map(([k,v])=>opt(k,v,o.metric)).join('')}</select></div>${o.site?'':`<details class="wo-site-picker"><summary>Выбрать объекты для сравнения</summary><div>${(d.matrixSites||[]).map(s=>`<label><input type="checkbox" data-wo-chart-site="${esc(s.id)}"${o.sites==null||o.sites.includes(s.id)?' checked':''}>${esc(s.name)}</label>`).join('')}</div></details>`}</div>${range.clipped||hasUnknown?`<p class="notice">${range.clipped?'На графике последние 24 квартала; полный диапазон сохранён в матрице и выгрузке. ':''}${hasUnknown?'Строки без отчётного квартала не распределены по оси времени.':''}</p>`:''}<div class="wo-charts-grid${o.site?' wo-site-charts':''}">${trend(d,o,range.items)}${o.site?'':comparison(d,o,range.items,items)}${structure(d,o)}</div><div class="wo-chart-tooltip" role="tooltip" hidden></div></section>`;
  }
  function tooltip(target,clientX,clientY){
    const root=target?.closest('.wo-chart-section'),tip=root?.querySelector('.wo-chart-tooltip');if(!tip)return;
    tip.textContent=target.dataset.woTip;tip.hidden=false;
    const rect=target.getBoundingClientRect(),x=clientX??rect.left,y=clientY??rect.top;
    tip.style.left=Math.max(12,Math.min(innerWidth-tip.offsetWidth-12,x+14))+'px';tip.style.top=Math.max(12,Math.min(innerHeight-tip.offsetHeight-12,y+18))+'px';
  }
  document.addEventListener('pointerover',e=>{const p=e.target.closest('[data-wo-tip]');if(p)tooltip(p,e.clientX,e.clientY)});
  document.addEventListener('focusin',e=>{const p=e.target.closest('[data-wo-tip]');if(p)tooltip(p)});
  for(const name of ['pointerout','focusout'])document.addEventListener(name,e=>{if(e.target.closest('[data-wo-tip]'))for(const p of document.querySelectorAll('.wo-chart-tooltip'))p.hidden=true});
  document.addEventListener('keydown',e=>{const p=e.target.closest('g.wo-chart-point[role="button"]');if(p&&['Enter',' '].includes(e.key)){e.preventDefault();p.dispatchEvent(new MouseEvent('click',{bubbles:true}))}});
  window.WriteoffCharts=Object.freeze({render,periods,series,linePath,scale,compareDecimal});
})();
