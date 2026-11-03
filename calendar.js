/* Calendar projection of visits. Preferences only; all writes use the existing API/ACL. */
'use strict';
const scheduleCache=new Map();
const calendarModes={month:'Месяц',week:'Неделя',day:'День'};
function calendarDate(value){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(value||''))return null;
  // Local noon survives UTC offsets and DST; calendar dates are never UTC timestamps.
  const date=new Date(value+'T12:00:00');
  return Number.isFinite(+date)&&date.toLocaleDateString('sv-SE')===value?date:null;
}
function calendarISO(date){return date.toLocaleDateString('sv-SE')}
function calendarShift(value,days){const date=calendarDate(value);date.setDate(date.getDate()+days);return calendarISO(date)}
function calendarLabel(value){return calendarDate(value).toLocaleDateString('ru-RU',{weekday:'long',day:'numeric',month:'long',year:'numeric'})}
function scheduleState(){
  const user=sessionUser.id;
  if(!scheduleCache.has(user)){
    let saved={};try{saved=JSON.parse(sessionStorage.getItem('ksu-schedule:'+user))||{}}catch{}
    const state={view:['list','calendar'].includes(saved.view)?saved.view:'list',
      mode:Object.hasOwn(calendarModes,saved.mode)?saved.mode:'month',
      date:calendarDate(saved.date)&&saved.date>='1900-01-01'&&saved.date<='9998-12-31'?saved.date:today(),
      status:['all','planned','completed','cancelled','overdue'].includes(saved.status)?saved.status:'all',
      site:typeof saved.site==='string'?saved.site:'all',
      assignee:typeof saved.assignee==='string'?saved.assignee:'all'};
    scheduleCache.set(user,state);
  }
  const state=scheduleCache.get(user);
  if(state.site!=='all'&&!db.sites.some(s=>s.id===state.site))state.site='all';
  if(sessionUser.role!=='manager'||(state.assignee!=='all'&&!(db.assignees||[]).some(p=>p.id===state.assignee)))state.assignee='all';
  return state;
}
function setScheduleState(patch,focus){
  Object.assign(scheduleState(),patch);
  try{sessionStorage.setItem('ksu-schedule:'+sessionUser.id,JSON.stringify(scheduleState()))}catch{}
  render();
  if(focus)document.querySelector(focus)?.focus({preventScroll:true});
}
function filteredScheduleVisits(){
  const state=scheduleState();
  return db.visits.filter(v=>(state.site==='all'||v.siteId===state.site)&&
    (state.assignee==='all'||v.assignedTo===state.assignee)&&
    (state.status==='all'||(state.status==='overdue'?v.status==='planned'&&v.date<today():v.status===state.status)))
    .slice().sort((a,b)=>a.date.localeCompare(b.date)||a.id.localeCompare(b.id));
}
function scheduleControls(){
  const state=scheduleState();
  return `<section class="card section-gap schedule-controls" aria-label="Вид и фильтры плана">
    <div class="chip-choice" role="group" aria-label="Представление плана">${[['calendar','Календарь'],['list','Список']].map(([value,label])=>`<button type="button" data-schedule-view="${value}" aria-pressed="${state.view===value}" class="${state.view===value?'active':''}">${label}</button>`).join('')}</div>
    <div class="filterbar section-gap">
      <div class="field"><label for="schedule-site">Объект</label><select id="schedule-site" data-schedule-filter="site">${siteOptions(state.site)}</select></div>
      ${sessionUser.role==='manager'?`<div class="field"><label for="schedule-assignee">Исполнитель</label><select id="schedule-assignee" data-schedule-filter="assignee">${selectOptions([['all','Все исполнители'],...(db.assignees||[]).map(p=>[p.id,p.name])],state.assignee)}</select></div>`:''}
      <div class="field"><label for="schedule-status">Статус выезда</label><select id="schedule-status" data-visit-filter>${selectOptions([['all','Все'],...Object.entries(visitStatuses),['overdue','Просрочен']],state.status)}</select></div>
      <button type="button" class="btn secondary" data-schedule-reset>Сбросить фильтры</button>
    </div><p class="tiny muted">Фильтры общие для календаря и списка. Показатели выше — по всему доступному плану.</p>
  </section>`;
}
function calendarRange(state){
  const selected=calendarDate(state.date);
  if(state.mode==='day')return [state.date];
  if(state.mode==='month')selected.setDate(1);
  selected.setDate(selected.getDate()-(selected.getDay()+6)%7);
  const start=calendarISO(selected);
  if(state.mode==='week')return Array.from({length:7},(_,n)=>calendarShift(start,n));
  const end=new Date(calendarDate(state.date));end.setMonth(end.getMonth()+1,0);
  const count=Math.ceil(((end.getDate()+(calendarDate(state.date.slice(0,8)+'01').getDay()+6)%7))/7)*7;
  return Array.from({length:count},(_,n)=>calendarShift(start,n));
}
function visitCalendarStatus(v){return (visitStatuses[v.status]||v.status)+(v.status==='planned'&&v.date<today()?' · Просрочен':'')}
function visitCalendarActions(v,history=false){
  return `<div class="actions-row">
    <button class="link" data-calendar-visit="${attr(v.id)}">Подробнее</button>
    ${v.status==='planned'&&v.assignedTo===sessionUser.id?`<button class="btn primary" data-action="start-audit" data-site="${attr(v.siteId)}" data-visit="${attr(v.id)}">Проверить</button>`:''}
    ${canEditVisit(v)?`<button class="btn secondary" data-action="edit-visit" data-id="${attr(v.id)}">Изменить / перенести</button><button class="link" data-action="cancel-visit" data-id="${attr(v.id)}">Отменить</button>`:''}
    ${v.inspectionId?`<button class="link" data-action="audit-detail" data-id="${attr(v.inspectionId)}">Акт проверки</button>`:''}
    ${history?`<button class="link" data-action="history" data-collection="visits" data-id="${attr(v.id)}">История</button>`:''}
  </div>`;
}
function visitCalendarCard(v){
  return `<article class="calendar-visit" data-visit-id="${attr(v.id)}">
    <div class="calendar-card-heading"><strong>${esc(nameSite(v.siteId))}</strong>${pill(visitCalendarStatus(v),v.status==='completed'?'green':v.status==='cancelled'?'neutral':v.date<today()?'red':'amber')}</div>
    <p>${esc(visitKinds[v.kind]||v.kind)} · ${esc(v.assignee||'—')}</p>
    ${v.goal?`<p>${esc(v.goal)}</p>`:''}${v.cancellationReason?`<p>Причина отмены: ${esc(v.cancellationReason)}</p>`:''}
    ${visitCalendarActions(v)}</article>`;
}
function renderVisitCalendar(visits){
  const state=scheduleState(),dates=calendarRange(state);
  const groups=new Map();for(const visit of visits){if(!groups.has(visit.date))groups.set(visit.date,[]);groups.get(visit.date).push(visit)}
  const label=state.mode==='month'?calendarDate(state.date).toLocaleDateString('ru-RU',{month:'long',year:'numeric'}):state.mode==='week'?`${fmtDate(dates[0])} — ${fmtDate(dates[6])}`:calendarLabel(state.date);
  return `<section class="card section-gap calendar" aria-label="Календарь выездов">
    <div class="calendar-toolbar"><div class="chip-choice" role="group" aria-label="Период календаря">${Object.entries(calendarModes).map(([key,name])=>`<button type="button" data-calendar-mode="${key}" aria-pressed="${state.mode===key}" class="${state.mode===key?'active':''}">${name}</button>`).join('')}</div>
      <div class="calendar-navigation"><button class="btn secondary" data-calendar-step="-1" aria-label="Предыдущий период">←</button><button class="btn secondary" data-calendar-today>Сегодня</button><button class="btn secondary" data-calendar-step="1" aria-label="Следующий период">→</button></div>
      <div class="field"><label for="calendar-date">Выбранная дата</label><input id="calendar-date" type="date" min="1900-01-01" max="9998-12-31" value="${attr(state.date)}"></div>
    </div><h3 class="calendar-period" aria-live="polite">${esc(label)}</h3>
    ${state.mode!=='day'?`<div class="calendar-grid calendar-${state.mode}">${state.mode==='month'?['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(d=>`<div class="calendar-weekday" aria-hidden="true">${d}</div>`).join(''):''}${dates.map(date=>{
      const rows=groups.get(date)||[];
      return `<div class="calendar-cell ${date.slice(0,7)!==state.date.slice(0,7)?'calendar-outside':''} ${date===state.date?'calendar-selected':''}">
        <button type="button" class="calendar-date" data-calendar-day="${date}" aria-pressed="${date===state.date}" ${date===today()?'aria-current="date"':''} aria-label="${attr(calendarLabel(date))}. Выездов: ${rows.length}"><span>${state.mode==='week'?esc(calendarDate(date).toLocaleDateString('ru-RU',{weekday:'short',day:'numeric',month:'short'})):Number(date.slice(-2))}</span><span class="calendar-count">${rows.length||'—'}</span></button>
        <div class="calendar-mini-visits">${rows.slice(0,3).map(v=>`<button type="button" class="calendar-event calendar-event-${v.status}" data-calendar-visit="${attr(v.id)}" title="${attr(visitCalendarStatus(v)+' · '+nameSite(v.siteId)+' · '+v.goal)}">${esc(nameSite(v.siteId))}<small>${esc(visitCalendarStatus(v))}</small></button>`).join('')}${rows.length>3?`<span class="tiny">Ещё ${rows.length-3} · выберите дату</span>`:''}</div>
        <span class="calendar-mobile-status" aria-hidden="true">${rows.some(v=>v.status==='planned'&&v.date<today())?'!':''}</span></div>`;
    }).join('')}</div><p class="tiny muted">Нажмите дату, чтобы увидеть все выезды дня. Число — количество по фильтрам; ! — есть просроченный выезд.</p>`:''}
    <section class="calendar-agenda" aria-labelledby="calendar-day-title"><div class="calendar-day-heading"><h3 id="calendar-day-title" tabindex="-1">${esc(calendarLabel(state.date))}</h3><button class="btn primary" data-calendar-create="${state.date}" ${!db.sites.length?'disabled':''}>Запланировать выезд</button></div>
      <div class="calendar-day-visits">${(groups.get(state.date)||[]).map(visitCalendarCard).join('')||'<p class="calendar-empty">На выбранную дату выездов по фильтрам нет.</p>'}</div>
    </section></section>`;
}
function createCalendarVisit(date){
  visitForm(null,date);
  const form=$('#visit-form');if(!form)return;
  // Require an explicit site choice for a newly planned visit from the calendar.
  form.elements.siteId.insertAdjacentHTML('afterbegin','<option value="">Выберите объект</option>');
  form.elements.siteId.value='';form.elements.siteId.dispatchEvent(new Event('change'));
  form.elements.siteId.focus();
}
function openCalendarVisit(id){
  const v=db.visits.find(v=>v.id===id);if(!v)return;
  openModal(`<p><strong>${esc(nameSite(v.siteId))}</strong> · ${fmtDate(v.date)}</p><p>${esc(visitCalendarStatus(v))}</p><p>${esc(visitKinds[v.kind]||v.kind)} · ${esc(v.assignee||'—')}</p><p>${esc(v.goal)}</p>${v.cancellationReason?`<p>Причина отмены: ${esc(v.cancellationReason)}</p>`:''}<p><button class="link" data-action="site-detail" data-id="${attr(v.siteId)}">Открыть объект</button></p>${visitCalendarActions(v,true)}`,'Выезд', 'Данные общего плана',false,'<button class="btn secondary" data-action="close-modal">Закрыть</button>');
  // The detail is a view; the existing editors/act replace or close it normally.
  document.querySelector('.modal [data-calendar-visit]')?.remove();
  document.querySelector('.modal [data-action="close-modal"]')?.focus();
  document.querySelector('.modal [data-action="site-detail"]').addEventListener('click',closeModal);
  document.querySelector('.modal [data-action="start-audit"]')?.addEventListener('click',closeModal);
  document.querySelector('.modal [data-action="audit-detail"]')?.addEventListener('click',closeModal);
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-schedule-view],[data-calendar-mode],[data-calendar-day],[data-calendar-step],[data-calendar-today],[data-calendar-create],[data-calendar-visit],[data-schedule-reset]');
  if(!button||!sessionUser)return;
  if(button.hasAttribute('data-schedule-view'))return setScheduleState({view:button.dataset.scheduleView},`[data-schedule-view="${button.dataset.scheduleView}"]`);
  if(button.hasAttribute('data-calendar-mode'))return setScheduleState({mode:button.dataset.calendarMode},`[data-calendar-mode="${button.dataset.calendarMode}"]`);
  if(button.hasAttribute('data-schedule-reset'))return setScheduleState({site:'all',assignee:'all',status:'all'},'[data-schedule-reset]');
  if(button.hasAttribute('data-calendar-create'))return createCalendarVisit(button.dataset.calendarCreate);
  if(button.hasAttribute('data-calendar-visit'))return openCalendarVisit(button.dataset.calendarVisit);
  if(button.hasAttribute('data-calendar-day')){
    setScheduleState({date:button.dataset.calendarDay},'#calendar-day-title');
    $('#calendar-day-title').scrollIntoView({block:'nearest'});return;
  }
  let date=today();
  if(button.hasAttribute('data-calendar-step')){
    const state=scheduleState(),step=Number(button.dataset.calendarStep);
    if(state.mode==='month'){
      const selected=calendarDate(state.date),day=selected.getDate();selected.setDate(1);selected.setMonth(selected.getMonth()+step);
      const end=new Date(selected);end.setMonth(end.getMonth()+1,0);selected.setDate(Math.min(day,end.getDate()));date=calendarISO(selected);
    }else date=calendarShift(state.date,step*(state.mode==='week'?7:1));
  }
  if(calendarDate(date)&&date>='1900-01-01'&&date<='9998-12-31')setScheduleState({date},button.hasAttribute('data-calendar-today')?'[data-calendar-today]':`[data-calendar-step="${button.dataset.calendarStep}"]`);
});
document.addEventListener('change',event=>{
  const target=event.target;
  if(target.id==='calendar-date'){
    if(calendarDate(target.value)&&target.value>='1900-01-01'&&target.value<='9998-12-31')setScheduleState({date:target.value},'#calendar-date');
    else target.value=scheduleState().date;
  }else if(target.hasAttribute('data-schedule-filter'))setScheduleState({[target.dataset.scheduleFilter]:target.value},'#'+target.id);
});
document.addEventListener('keydown',event=>{
  const button=event.target.closest('[data-calendar-day]');if(!button)return;
  const offset={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[event.key];if(!offset)return;
  event.preventDefault();const date=calendarShift(button.dataset.calendarDay,offset);
  if(date>='1900-01-01'&&date<='9998-12-31')setScheduleState({date},`[data-calendar-day="${date}"]`);
});

// Keep keyboard navigation inside calendar dialogs; Escape restores the selected day.
const closeBeforeCalendar=closeModal;
closeModal=function(){
  const restore=ui.modal&&ui.page==='schedule'&&sessionUser&&scheduleState().view==='calendar';
  closeBeforeCalendar();
  if(restore)document.querySelector('[data-calendar-day="'+scheduleState().date+'"]')?.focus({preventScroll:true});
};
document.addEventListener('keydown',event=>{
  if(event.key!=='Tab'||!ui.modal||ui.page!=='schedule'||scheduleState().view!=='calendar')return;
  const elements=[...document.querySelectorAll('.modal button:not([disabled]),.modal input:not([type="hidden"]):not([disabled]),.modal select:not([disabled]),.modal textarea:not([disabled]),.modal a[href]')];
  const first=elements[0],last=elements.at(-1);if(!first)return;
  if(event.shiftKey&&(document.activeElement===first||!document.activeElement.closest('.modal'))){event.preventDefault();last.focus()}
  else if(!event.shiftKey&&(document.activeElement===last||!document.activeElement.closest('.modal'))){event.preventDefault();first.focus()}
});
