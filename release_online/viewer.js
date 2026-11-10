/* Shared UI permissions. All roles use app.js renderers and the same stylesheet. */
'use strict';
function isReadOnly(){return sessionUser?.role==='viewer'}
function roleLabel(){return ui.role==='manager'?'руководитель':ui.role==='viewer'?'Только просмотр':'инспектор'}
const readActions=new Set(['mobile-toggle','mobile-close','site-detail','sites-back','finding-detail','audit-detail','print-audit','inventory-detail','export-inventory','export-report','print-report','weekly-report','weekly-export','report-xlsx','report-pdf','close-modal','logout']);
const viewFilters='[data-visit-filter],[data-audit-filter],[data-issue-filter],[data-inv-filter],[data-report-field]';
function applyReadPermissions(root=document){
 if(!isReadOnly())return;
 root.querySelectorAll('[data-action]').forEach(button=>{
  if(!readActions.has(button.dataset.action))button.remove();
  else if(button.dataset.action==='finding-detail'&&button.textContent==='Изменить')button.textContent='Просмотр';
  else if(button.dataset.action==='close-modal'&&button.textContent==='Отмена')button.textContent='Закрыть';
 });
 root.querySelectorAll('form input,form select,form textarea').forEach(input=>{input.disabled=true});
 root.querySelectorAll('input[type="file"],[data-answer]').forEach(input=>input.remove());
}
// Fail closed for delegated or injected controls; read navigation/filter controls remain usable.
document.addEventListener('click',event=>{
 if(!isReadOnly())return;
 const action=event.target.closest('[data-action]');
 if(action?.dataset.action==='print-report'){
  event.stopImmediatePropagation();event.preventDefault();
  api('state').then(state=>{db=state;render();window.print()}).catch(error=>toast(error.message,true));return;
 }
 if(event.target.closest('[data-answer]')||(action&&!readActions.has(action.dataset.action))){event.stopImmediatePropagation();event.preventDefault()}
},true);
for(const kind of ['change','input'])document.addEventListener(kind,event=>{
 if(isReadOnly()&&!event.target.matches(viewFilters)){event.stopImmediatePropagation();event.preventDefault()}
},true);
document.addEventListener('submit',event=>{
 if(isReadOnly()){event.stopImmediatePropagation();event.preventDefault()}
},true);
