/* One shared, versioned checklist. All writes use the existing queued /api/changes contract. */
'use strict';
function orderedQuestions(){return [...db.questions].sort((a,b)=>{
 const pa=Number.isInteger(a.position)?a.position:100000+(Number(a.id.replace(/^q/,''))||0);
 const pb=Number.isInteger(b.position)?b.position:100000+(Number(b.id.replace(/^q/,''))||0);
 return pa-pb||a.id.localeCompare(b.id);
})}
function activeQuestions(){return orderedQuestions().filter(q=>!q.archived)}
function draftQuestions(){return ui.draft?.questions||activeQuestions()}
function checklistSettings(){
 const active=activeQuestions(),all=orderedQuestions(),sections=[...new Set(active.map(q=>q.section))];
 if(ui.role!=='manager')return `<section class="card setting-card"><h3>Шаблон чек-листа</h3><p>Общий шаблон. Изменять вопросы может только руководитель. Акты и офлайн-пакеты сохраняют свою версию.</p></section>`;
 return `<section class="card setting-card" id="checklist-editor"><h3>Общий шаблон чек-листа</h3><p>${active.length} действующих · ${all.filter(q=>q.archived).length} в архиве. Изменения применяются к новым проверкам. История актов и выданные пакеты сохраняются.</p><div class="actions-row"><button class="btn secondary" data-action="checklist-add">Добавить пункт</button><button class="btn secondary" data-action="checklist-archives">${ui.checklistArchives?'Скрыть':'Показать'} архив</button></div>${sections.map((section,index)=>`<div class="section-gap" data-checklist-section="${attr(section)}"><div class="card-title-row"><h4>${esc(section)}</h4><div><button class="btn secondary" data-action="checklist-section-up" data-section="${attr(section)}" ${index===0?'disabled':''} aria-label="Раздел выше">↑</button><button class="btn secondary" data-action="checklist-section-down" data-section="${attr(section)}" ${index===sections.length-1?'disabled':''} aria-label="Раздел ниже">↓</button></div></div>${active.filter(q=>q.section===section).map((q,i,rows)=>`<div class="object-row" data-checklist-id="${attr(q.id)}"><div style="flex:1">${esc(q.title)}</div><div class="actions-row"><button class="btn secondary" data-action="checklist-up" data-id="${attr(q.id)}" ${i===0?'disabled':''} aria-label="Вопрос выше">↑</button><button class="btn secondary" data-action="checklist-down" data-id="${attr(q.id)}" ${i===rows.length-1?'disabled':''} aria-label="Вопрос ниже">↓</button><button class="btn secondary" data-action="checklist-edit" data-id="${attr(q.id)}">Изменить</button><button class="btn secondary" data-action="checklist-archive" data-id="${attr(q.id)}">В архив</button></div></div>`).join('')}</div>`).join('')}${ui.checklistArchives?`<div class="section-gap"><h4>Архив</h4>${all.filter(q=>q.archived).map(q=>`<div class="object-row" data-checklist-id="${attr(q.id)}"><div style="flex:1"><small>${esc(q.section)}</small><br>${esc(q.title)}</div><button class="btn secondary" data-action="checklist-restore" data-id="${attr(q.id)}">Вернуть</button></div>`).join('')||'<p>Архив пуст</p>'}</div>`:''}</section>`;
}
function editChecklistQuestion(id){
 const question=db.questions.find(q=>q.id===id);
 openModal(`<form id="checklist-question-form"><div class="field"><label>Раздел *</label><input name="section" required maxlength="120" list="checklist-sections" value="${attr(question?.section||'')}"><datalist id="checklist-sections">${[...new Set(activeQuestions().map(q=>q.section))].map(s=>`<option value="${attr(s)}"></option>`).join('')}</datalist></div><div class="field section-gap"><label>Формулировка *</label><textarea name="title" required maxlength="500">${esc(question?.title||'')}</textarea></div></form>`,question?'Изменить пункт':'Добавить пункт','Исторические формулировки и выданные пакеты не изменятся',false,`<button class="btn secondary" data-action="close-modal">Отмена</button><button class="btn primary" data-action="checklist-save" data-id="${attr(id||'')}">Сохранить</button>`);
}
async function changeChecklist(change){
 if(ui.role!=='manager'||isReadOnly())return toast('Изменение шаблона доступно только руководителю',true);
 return mutate(()=>{activeQuestions().forEach((q,i)=>{q.position=i+1});change();const rows=activeQuestions();[...new Set(rows.map(q=>q.section))].flatMap(section=>rows.filter(q=>q.section===section)).forEach((q,i)=>{q.position=i+1});closeModal()});
}
// Capture only our controls before legacy delegation. No bypass of api/mutate/CSRF/ACL.
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-action]');const action=button?.dataset.action;
 if(!action?.startsWith('checklist-'))return;
 event.preventDefault();event.stopImmediatePropagation();
 if(action==='checklist-draft-copy'&&!isReadOnly()&&ui.draft){const url=URL.createObjectURL(new Blob([JSON.stringify(ui.draft)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='instrakt-checklist-draft.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);return}
 if(ui.role!=='manager'||isReadOnly())return toast('Только руководитель изменяет шаблон',true);
 const id=button.dataset.id;
 if(action==='checklist-archives'){ui.checklistArchives=!ui.checklistArchives;render();return}
 if(action==='checklist-add'||action==='checklist-edit'){if(action==='checklist-add'&&activeQuestions().length>=200)return toast('Допускается не более 200 действующих пунктов',true);editChecklistQuestion(id);return}
 if(action==='checklist-save'){
  const form=document.getElementById('checklist-question-form');if(!form?.reportValidity())return;
  const data=Object.fromEntries(new FormData(form));
  await changeChecklist(()=>{const q=db.questions.find(q=>q.id===id);if(q)Object.assign(q,data);else db.questions.push({id:uid(),...data,position:activeQuestions().length+1,archived:false,templateId:'global'})});return;
 }
 const q=db.questions.find(q=>q.id===id);
 if(action==='checklist-archive'){
  if(activeQuestions().length<=1)return toast('Оставьте хотя бы один действующий пункт',true);
  if(q&&confirm('Архивировать пункт? Акты и ранее выданные пакеты сохранят его.'))await changeChecklist(()=>{q.archived=true});return;
 }
 if(action==='checklist-restore'){if(activeQuestions().length>=200)return toast('Допускается не более 200 действующих пунктов',true);if(q)await changeChecklist(()=>{q.position=activeQuestions().length+1;q.archived=false});return}
 if(action==='checklist-up'||action==='checklist-down'){
  if(!q)return;const rows=activeQuestions().filter(item=>item.section===q.section);const index=rows.findIndex(item=>item.id===id),next=index+(action==='checklist-up'?-1:1);
  if(next<0||next>=rows.length)return;
  await changeChecklist(()=>{[rows[index].position,rows[next].position]=[rows[next].position,rows[index].position]});return;
 }
 if(action.startsWith('checklist-section-')){
  const rows=activeQuestions(),sections=[...new Set(rows.map(q=>q.section))],index=sections.indexOf(button.dataset.section),next=index+(action.endsWith('-up')?-1:1);
  if(index<0||next<0||next>=sections.length)return;
  [sections[index],sections[next]]=[sections[next],sections[index]];
  await changeChecklist(()=>{sections.flatMap(section=>rows.filter(q=>q.section===section)).forEach((q,i)=>{q.position=i+1})});
 }
},true);
// Pin both content and version before starting an online draft. The same snapshot is
// persisted by sync.js; pending request bodies retain their explicit checklistVersion.
startWizard=async function(siteId){
 try{
  const version=await api('checklists/current');ui.wizard=true;ui.page='audits';
  ui.draft={siteId:siteId||db.sites[0]?.id,date:db.visits.find(v=>v.id===ui.activeVisitId)?.date||today(),inspector:sessionUser.name,checklistVersion:version.id,questions:structuredClone(version.questions),answers:Object.fromEntries(version.questions.map(q=>[q.id,{status:'pending',comment:'',severity:'medium',owner:'Кладовщик объекта',dueDate:dateOffset(7),photo:''}]))};
  render();window.scrollTo(0,0);
 }catch(error){toast('Не удалось закрепить версию чек-листа: '+error.message,true)}
};
