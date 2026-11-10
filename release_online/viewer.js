/* No editor, financial report, media, IDB or offline queue in the viewer surface. */
'use strict';
function renderViewer(){
 const names=new Map(db.sites.map(s=>[s.id,s.name]));
 const rows=(key)=>db[key].map(r=>`<tr><td>${esc(names.get(r.siteId)||r.name||'')}</td><td>${esc(r.title||r.date||'')}</td><td>${esc(r.status||'')}</td></tr>`).join('');
 $('#app').innerHTML=`<main class="workspace viewer-shell"><header class="page-heading"><img src="/assets/instrakt-logo.svg" width="100" height="48" alt="ИНСТРАКТ"><h1>ИНСТРАКТ | Складской учет</h1><span class="pill" role="status">Только просмотр</span><span>${esc(sessionUser.name)}</span><button class="btn secondary" data-action="logout">Выйти</button></header><p>Только разрешённые объекты. Вложения, личные поля, история, финансовые данные и выгрузки недоступны.</p>${[['sites','Объекты'],['visits','Выезды'],['inspections','Проверки'],['findings','Нарушения']].map(([key,title])=>`<section class="card"><h2>${title}</h2><div class="table-wrap"><table><thead><tr><th>Объект</th><th>Сведения</th><th>Статус</th></tr></thead><tbody>${rows(key)}</tbody></table></div></section>`).join('')}</main>`;
}
document.addEventListener('click',event=>{
 if(sessionUser?.role==='viewer'&&event.target.closest('[data-action],[data-nav]')?.dataset.action!=='logout'){
  event.stopImmediatePropagation();event.preventDefault();
 }
},true);
