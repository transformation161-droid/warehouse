/* PR40. Shared inventory plans. No client-side authority over access or totals. */
'use strict';
(()=>{
  let upload=null,preview=null,countBusy=false,activeInventory=null;
  const pageSize=100;
  const mapFields=[['item','Наименование'],['externalId','ID'],['inventoryNumber','Инвентарный №'],
    ['price','Цена закупки'],['expected','Учётное количество'],['brand','Бренд'],
    ['category','Категория'],['owner','МОЛ'],['warehouse','Склад'],
    ['unit','Единица'],['note','Примечание']];
  const allowed=()=>!!sessionUser&&['manager','inspector'].includes(sessionUser.role);
  const ready=()=>!pending&&!sending&&!countBusy;
  const get=(id)=>document.getElementById(id);
  const field=(id)=>get(id)?.value||'';
  const safe=(v)=>esc(v??'');
  const amount=(n)=>rub(n);
  const hasCount=(x)=>x.actual!==null&&x.actual!==undefined;
  const countLines=(id)=>db.inventoryLines.filter(x=>x.inventoryId===id);
  const sizeWarning=()=>toast('Сначала завершите предыдущую отправку',true);
  const sourceData=async(file)=>{
    if(file.size>3000000)throw Error('Максимальный размер файла 3 МБ');
    const bytes=new Uint8Array(await file.arrayBuffer());let content='';
    for(let i=0;i<bytes.length;i+=8192)content+=String.fromCharCode(...bytes.subarray(i,i+8192));
    return {name:file.name,content:btoa(content)};
  };
  const showError=(error)=>toast(error?.message||'Не удалось выполнить операцию',true);

  inventoryPlanPreviewModal=function(){
    upload=null;preview=null;
    const selected=(ui.invFilter?.site!=='all'?ui.invFilter?.site:null)||db.sites[0]?.id||'';
    const content='<div class="notice">Создать план может руководитель или КСУ с доступом к объекту. Все позиции сначала имеют статус «Не проверено».</div>'+
      '<form id="inv-plan-form" class="form-grid section-gap">'+
      '<div class="field"><label>Объект *</label><select id="ip-site" required>'+siteOptions(selected,false)+'</select></div>'+
      '<div class="field"><label>Дата *</label><input id="ip-date" type="date" value="'+today()+'" required></div>'+
      '<div class="field span2"><label>Название *</label><input id="ip-title" maxlength="230" required value="Инвентаризация инструмента"></div>'+
      '<div class="field span2"><label>Файл XLSX/CSV *</label><input id="ip-file" type="file" accept=".xlsx,.csv" required></div></form>'+
      '<div id="ip-mapping" class="section-gap"></div><div id="ip-preview" class="section-gap"></div>';
    openModal(content,'Загрузка плана инвентаризации','Список без результатов пересчёта',true,
      '<button class="btn secondary" data-action="close-modal">Отмена</button>'+
      '<button class="btn secondary" data-action="inventory-plan-check">Проверить файл</button>'+
      '<button class="btn primary" data-action="ip-commit" disabled>Создать план</button>');
  };

  inventoryPlanCheck=async function(){
    if(!allowed())return toast('Недостаточно прав',true);
    if(!ready())return sizeWarning();
    if(!get('inv-plan-form')?.reportValidity())return;
    const file=get('ip-file')?.files?.[0];if(!file)return toast('Выберите файл',true);
    const button=document.querySelector('[data-action="ip-commit"]');
    if(button)button.disabled=true;
    const target=get('ip-preview');if(target)target.textContent='Проверяем файл…';
    try{
      const selected=[...document.querySelectorAll('[data-ip-map]')];
      const mapping=Object.fromEntries(selected.map(x=>[x.dataset.ipMap,x.value]).filter(([,v])=>v));
      upload={...await sourceData(file),...(selected.length?{mapping}:{})};
      preview=await api('inventory-plan/preview',{method:'POST',body:JSON.stringify(upload)});
      if(!selected.length){
        const options=preview.headers.map(x=>'<option value="'+attr(x)+'">'+safe(x)+'</option>').join('');
        get('ip-mapping').innerHTML='<div class="small-heading">Колонки (измените сопоставление и проверьте повторно)</div>'+
          '<div class="form-grid">'+mapFields.map(([key,label])=>{
            const current=preview.mapping?.[key]||'';
            return '<div class="field"><label>'+safe(label)+'</label><select data-ip-map="'+key+'">'+
              '<option value="">Не используется</option>'+preview.headers.map(x=>'<option value="'+attr(x)+'"'+
              (current===x?' selected':'')+'>'+safe(x)+'</option>').join('')+'</select></div>';
          }).join('')+'</div>';
      }
      const rows=preview.rows.slice(0,15).map(row=>'<tr><td>'+row.row+'</td><td>'+safe(row.item)+'</td><td>'+
          safe(row.inventoryNumber||row.sku)+'</td><td>'+safe(row.expected)+'</td><td>'+
          amount(row.purchasePrice)+'</td><td>Не проверено</td></tr>').join('');
      target.innerHTML='<div class="notice">Строка заголовков: '+preview.headerRow+
        '; позиций: <strong>'+preview.positions+'</strong>; стоимость по закупочным ценам: <strong>'+
        amount(preview.totalPurchaseValue)+'</strong>. Результатов пересчёта: 0.</div>'+
        '<div class="table-wrap"><table class="table"><thead><tr><th>Строка</th><th>Наименование</th><th>Номер</th>'+
        '<th>Учёт</th><th>Цена за ед.</th><th>Факт</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
        '<div class="tiny muted">Показаны первые '+Math.min(15,preview.positions)+' позиций. Ничего не сохранено.</div>';
      if(button)button.disabled=false;
    }catch(e){preview=null;if(target)target.textContent='';showError(e)}
  };

  async function commitPlan(){
    if(!ready())return sizeWarning();
    if(!upload||!preview)return toast('Сначала проверьте Excel',true);
    if(!get('inv-plan-form')?.reportValidity())return;
    const btn=document.querySelector('[data-action="ip-commit"]');btn.disabled=true;countBusy=true;
    try{
      const response=await api('inventory-plan/commit',{method:'POST',body:JSON.stringify({
        ...upload,siteId:field('ip-site'),date:field('ip-date'),title:field('ip-title')})});
      db=response.state;upload=null;preview=null;closeModal();render();
      toast(response.duplicate?'Этот план уже был загружен, дубликат не создан':
        'План создан: '+response.positions+' позиций');
      inventoryDetail(response.inventoryId);
    }catch(e){showError(e);btn.disabled=false}finally{countBusy=false}
  }

  function totals(rows){
    const processed=rows.filter(hasCount);
    const sum=(a)=>a.reduce((v,x)=>v+x,0);
    const val=(v)=>Number(v)||0;
    return {checked:processed.length,unchecked:rows.length-processed.length,
      planned:sum(rows.map(x=>lineMoney(val(x.expected),val(x.price)))),
      actual:sum(processed.map(x=>lineMoney(val(x.actual),val(x.price)))),
      short:sum(processed.map(x=>lineMoney(Math.max(0,val(x.expected)-val(x.actual)),val(x.price)))),
      surplus:sum(processed.map(x=>lineMoney(Math.max(0,val(x.actual)-val(x.expected)),val(x.price))))};
  }
  function planScreen(id){
    const inv=db.inventories.find(x=>x.id===id);if(!inv)return;
    activeInventory=id;
    const rows=countLines(id);
    const t=totals(rows),completed=rows.length>0&&rows.length===inv.plannedPositions&&t.unchecked===0;
    const mayCount=allowed()&&inv.status!=='final'&&inv.stage==='primary';
    const mayFinalize=mayCount&&(sessionUser.role==='manager'||inv.createdBy===sessionUser.id);
    const filterState=field('ip-filter-status')||'all',query=field('ip-filter-name').toLowerCase();
    const filtered=rows.filter(x=>(filterState==='all'||(filterState==='unchecked'?!hasCount(x):
      filterState==='differences'?hasCount(x)&&Number(x.actual)!==Number(x.expected):hasCount(x)))&&
      (!query||[x.item,x.sku,x.inventoryNumber,x.sourceId].some(v=>String(v||'').toLowerCase().includes(query))));
    const slice=filtered.slice(0,pageSize);
    const tr=slice.map(x=>{
      const status=!hasCount(x)?'Не проверено':x.actual<x.expected?'Недостача':
         x.actual>x.expected?'Излишек':'Соответствует';
      return '<tr><td>'+safe(x.inventoryNumber||x.sku||x.sourceId)+'</td><td><strong>'+
        safe(x.item)+'</strong>'+(x.reason?'<div class="tiny muted">'+safe(x.reason)+'</div>':'')+
        '</td><td>'+safe(x.expected)+'</td><td>'+(hasCount(x)?safe(x.actual):'—')+
        '</td><td>'+amount(x.price)+'</td><td>'+status+'</td><td>'+
        (mayCount?'<button class="link" data-action="ip-count-open" data-id="'+attr(x.id)+'">'+
          (hasCount(x)?'Изменить':'Проверить')+'</button>':'')+'</td></tr>';
    }).join('');
    const html='<div class="notice">'+safe(inv.sourceName)+' · План по закупочной стоимости. '+
      (inv.status==='final'?'Инвентаризация завершена.':'Данные и суммы предварительные.')+'</div>'+
      '<div class="grid grid-4 section-gap">'+
      metric('Проверено',t.checked+' / '+rows.length,'позиций','check')+
      metric('Не проверено',t.unchecked,'без факта','clock')+
      metric('Недостачи',amount(t.short),'закупочная стоимость','alert')+
      metric('Излишки',amount(t.surplus),'закупочная стоимость','box')+'</div>'+
      '<div class="notice section-gap">План: <b>'+amount(t.planned)+'</b>. Известный факт: <b>'+
      amount(t.actual)+'</b> (только проверенные). Не является расчётом ущерба.</div>'+
      '<div class="filterbar section-gap"><div class="field"><label>Поиск</label><input id="ip-filter-name" value="'+
      attr(query)+'" placeholder="Наименование или ID"></div><div class="field"><label>Показать</label>'+
      '<select id="ip-filter-status"><option value="all"'+(filterState==='all'?' selected':'')+
      '>Все</option><option value="unchecked"'+(filterState==='unchecked'?' selected':'')+
      '>Не проверено</option><option value="checked"'+(filterState==='checked'?' selected':'')+
      '>Проверено</option><option value="differences"'+(filterState==='differences'?' selected':'')+
      '>Расхождения</option></select></div><button class="btn secondary" data-action="ip-filter-apply">Применить</button></div>'+
      '<div class="table-wrap"><table class="table"><thead><tr><th>Инв. № / ID</th><th>Наименование</th>'+
      '<th>Учёт</th><th>Факт</th><th>Закупочная цена</th><th>Статус</th><th></th></tr></thead><tbody>'+
      tr+'</tbody></table></div><div class="tiny muted">Показано '+slice.length+' из '+filtered.length+
      ' позиций. Для других позиций используйте поиск или фильтр.</div>';
    const footer='<button class="btn secondary" data-action="history" data-collection="inventories" data-id="'+attr(id)+'">История</button>'+
      (sessionUser.role==='manager'&&(typeof stageNext!=='undefined')&&
        (stageNext[inv.stage||'primary']||[]).length?
        '<button class="btn secondary" data-action="change-stage" data-id="'+attr(id)+'">Стадия расхождений</button>':'')+
      '<button class="btn secondary" data-action="export-inventory-xlsx" data-id="'+attr(id)+'">XLSX</button>'+
      '<button class="btn secondary" data-action="export-inventory-pdf" data-id="'+attr(id)+'">PDF / печать</button>'+
      (mayFinalize?'<button class="btn primary" data-action="ip-finalize" data-id="'+attr(id)+'"'+
        (completed?'':' disabled')+'>Завершить ('+t.checked+'/'+rows.length+')</button>':'')+
      '<button class="btn secondary" data-action="close-modal">Закрыть</button>';
    openModal(html,inv.title,nameSite(inv.siteId)+' · '+fmtDate(inv.date),true,footer);
  }
  const previous=inventoryDetail;
  inventoryDetail=function(id){
    const inv=db.inventories.find(x=>x.id===id);
    if(inv?.sourceHash)return planScreen(id);
    return previous(id);
  };

  function countScreen(lineId){
    const x=db.inventoryLines.find(x=>x.id===lineId);if(!x)return;
    const inv=db.inventories.find(v=>v.id===x.inventoryId);
    if(!inv||inv.status==='final')return toast('Пересчёт завершён',true);
    activeInventory=inv.id;
    const html='<form id="ip-count-form"><div class="notice">Учёт: '+safe(x.expected)+' '+
      safe(x.unit)+'; цена закупки: '+amount(x.price)+'. Пустое поле = «Не проверено», 0 = «Не найден».</div>'+
      '<div class="form-grid section-gap"><div class="field"><label>Фактическое количество</label>'+
      '<input id="ip-actual" type="number" step="any" min="0" max="100000000" value="'+
      (hasCount(x)?attr(x.actual):'')+'" placeholder="Не проверено"></div>'+
      '<div class="field span2"><label>Комментарий (обязателен при расхождении)</label>'+
      '<textarea id="ip-comment" maxlength="4000">'+safe(x.reason)+'</textarea></div>'+
      '<div class="field span2"><label>Фото (необязательно)</label>'+
      '<input id="ip-photo" type="file" accept="image/png,image/jpeg,image/webp">'+
      (x.photo?'<div class="tiny muted">Ранее загруженное фото сохранится, если не выбирать новое.</div>':'')+
      '</div></div></form>';
    const footer='<button class="btn secondary" data-action="ip-back">Назад</button>'+
      '<button class="btn primary" data-action="ip-save-count" data-id="'+attr(lineId)+'">Сохранить факт</button>';
    openModal(html,'Пересчёт: '+x.item,nameSite(inv.siteId),true,footer);
  }
  async function saveCount(lineId){
    if(!ready())return sizeWarning();
    const x=db.inventoryLines.find(x=>x.id===lineId);if(!x)return;
    const form=get('ip-count-form');if(!form?.reportValidity())return;
    const string=field('ip-actual'),actual=string===''?null:Number(string);
    const reason=field('ip-comment').trim();
    if(actual!==null&&(!Number.isFinite(actual)||actual<0||actual>100000000))return toast('Некорректный факт',true);
    if(actual!==null&&actual!==Number(x.expected)&&!reason)
      return toast('Укажите причину расхождения или комментарий о проверке',true);
    const inv=db.inventories.find(v=>v.id===x.inventoryId);if(!inv)return;
    const button=document.querySelector('[data-action="ip-save-count"]');if(button)button.disabled=true;
    countBusy=true;
    try{
      let photo=x.photo||'',upload=get('ip-photo')?.files?.[0];
      if(upload){
        if(upload.size>3000000)throw Error('Фото больше 3 МБ');
        const reader=new FileReader();
        const data=await new Promise((resolve,reject)=>{reader.onload=()=>resolve(reader.result);
          reader.onerror=reject;reader.readAsDataURL(upload)});
        photo=await uploadPhoto(data,inv.siteId);
      }
      const result=await api('inventory-lines/'+encodeURIComponent(lineId)+'/count',{
        method:'PATCH',body:JSON.stringify({expectedVersion:x.version,actual,reason,photo}),
      });
      db=result.state;render();planScreen(inv.id);toast('Результат пересчёта сохранён');
    }catch(e){
      if(e?.status===409){try{db=await api('state');render();planScreen(inv.id)}catch{}}
      showError(e);if(button)button.disabled=false;
    }finally{countBusy=false}
  }
  async function finalize(id){
    if(!ready())return sizeWarning();
    const inv=db.inventories.find(x=>x.id===id);if(!inv)return;
    const lines=countLines(id);
    if(!lines.length||lines.length!==inv.plannedPositions||lines.some(x=>!hasCount(x)))
      return toast('Нельзя завершить: остались непроверенные позиции',true);
    if(!confirm('Завершить инвентаризацию? Результаты пересчёта будут закрыты для изменений.'))return;
    countBusy=true;
    try{
      const result=await api('inventories/'+encodeURIComponent(id)+'/finalize',{
        method:'POST',body:JSON.stringify({expectedVersion:inv.version}),
      });
      db=result.state;render();planScreen(id);toast('Инвентаризация завершена');
    }catch(e){showError(e)}finally{countBusy=false}
  }

  document.addEventListener('change',e=>{
    if(e.target?.id==='ip-file'||e.target?.hasAttribute('data-ip-map')){
      upload=null;preview=null;
      const b=document.querySelector('[data-action="ip-commit"]');if(b)b.disabled=true;
      const t=get('ip-preview');if(t)t.textContent='Файл или сопоставление изменено — проверьте снова.';
      if(e.target.id==='ip-file'){const target=get('ip-mapping');if(target)target.innerHTML=''}
    }
  });
  document.addEventListener('click',e=>{
    const b=e.target.closest('[data-action]');if(!b)return;
    const mode=b.dataset.action,id=b.dataset.id;
    if(mode==='ip-commit')void commitPlan();
    if(mode==='ip-count-open')countScreen(id);
    if(mode==='ip-back'&&activeInventory)planScreen(activeInventory);
    if(mode==='ip-save-count')void saveCount(id);
    if(mode==='ip-finalize')void finalize(id);
    if(mode==='ip-filter-apply'&&activeInventory){
      // Search the current page using user-controlled filters, not an implicit data reset.
      const name=field('ip-filter-name'),status=field('ip-filter-status');
      const inv=db.inventories.find(x=>x.id===activeInventory);
      if(inv){const prior=planScreen; // small state restoration around modal redraw
        activeInventory=inv.id;
        planScreen(inv.id);
        const nameInput=get('ip-filter-name'),statusInput=get('ip-filter-status');
        if(nameInput)nameInput.value=name;if(statusInput)statusInput.value=status;
      }
    }
  });
})();
