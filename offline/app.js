import {Vault,sha,exportJSON} from './store.js';
import {SyncEngine,STATUS,canonical} from './transport.js';
const $=s=>document.querySelector(s),vault=new Vault(),encoder=new TextEncoder();
let state=null,busy=false,revision=0,savedRevision=0,pending=0,writes=Promise.resolve(),lastWriteError=null,registration=null,releaseTab=null,urls=[],lastActive=Date.now(),photoQuestion=null;
let availableVisits=[];
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const say=(text,error=false)=>{$('#message').textContent=text;$('#message').classList.toggle('error',error)};
const dirty=()=>pending>0||state&&savedRevision<state.revision;
function status(){
  $('#local-status').textContent=state?(dirty()?'Есть изменения, ещё не подтверждённые хранилищем':`${STATUS.local} · ${new Date(state.savedAt).toLocaleString()}`):'Черновик ещё не создан';
  $('#sync-status').textContent=state?.queue?.status||'';
  $('#queue-detail').textContent=state?.queue?.detail||'';
  $('#sync-status').classList.toggle('queue-error',[STATUS.error,STATUS.conflict].includes(state?.queue?.status));
}
function queueSave(snapshot,photos=[]){
  pending++;status();
  const promise=writes.then(async()=>{await vault.save(snapshot,photos);savedRevision=Math.max(savedRevision,snapshot.revision);lastWriteError=null});
  writes=promise.catch(error=>{lastWriteError=error;say('Не сохранено на устройстве: '+error.message+'. Не закрывайте страницу; предыдущая подтверждённая копия сохранена.',true)}).finally(()=>{pending--;status()});
  return promise;
}
async function replace(next,photos=[]){next.revision=++revision;next.savedAt=Date.now();await queueSave(next,photos);state=next;status()}
async function flush(){await writes;if(state&&(savedRevision<state.revision||lastWriteError)){const next=structuredClone(state);await replace(next)}await writes;if(lastWriteError)throw lastWriteError}
const engine=new SyncEngine({getState:()=>state,commit:replace,photo:(id,type)=>vault.photo(id,type),sha});
function controls(){
  document.querySelectorAll('button,input,select,textarea').forEach(element=>element.disabled=busy);
  if(state){$('#load-visits').disabled=true;$('#prepare').disabled=true;$('#visit').disabled=true}
  if(state?.completed)document.querySelectorAll('#questions select,#questions textarea,#take-photo,#choose-photo,#photo-question,#complete').forEach(element=>element.disabled=true);
}
async function task(action){if(busy)return false;busy=true;controls();try{await action();return true}catch(error){say(error.message,true);return false}finally{busy=false;controls();status()}}
function clearPhotos(){for(const url of urls)URL.revokeObjectURL(url);urls=[];$('#photos').replaceChildren()}
async function render(){
  $('#unlock').hidden=!!vault.key;$('#editor').hidden=!vault.key;
  $('#package-status').textContent=state?`${state.site.name} · выезд ${state.visit.id} · версия ${state.visit.version} · чек-лист ${state.template.id} · до ${new Date(state.package.expiresAt).toLocaleString()}`:'Подключитесь к серверу и выберите назначенный выезд.';
  $('#visit-title').textContent=state?`Проверка · ${state.site.name}`:'Проверка КСУ';
  $('#questions').innerHTML=state?state.template.questions.map(q=>`<article class="question-card"><strong>${esc(q.title)}</strong><label>Результат<select data-answer="${esc(q.id)}"><option value="pending">Не заполнено</option><option value="ok">Соответствует</option><option value="fail">Нарушение</option><option value="na">Не применимо</option></select></label><label>Описание замечания<textarea data-comment="${esc(q.id)}" maxlength="4000"></textarea></label></article>`).join(''):'';
  if(state){for(const element of document.querySelectorAll('[data-answer]'))element.value=state.answers[element.dataset.answer].status;for(const element of document.querySelectorAll('[data-comment]'))element.value=state.answers[element.dataset.comment].comment}
  photoOptions();clearPhotos();
  if(vault.key&&state)for(const p of state.photos){const blob=await vault.photo(p.id,p.type),url=URL.createObjectURL(blob);urls.push(url);const box=document.createElement('div'),image=document.createElement('img'),text=document.createElement('p');image.src=url;image.className='photo-preview';image.alt='Фото проверки';text.textContent=`${p.questionId} · ${p.id}`;box.append(image,text);$('#photos').append(box)}
  controls();status();
}
function photoOptions(){const selected=$('#photo-question').value;$('#photo-question').innerHTML=state?state.template.questions.filter(q=>state.answers[q.id].status==='fail').map(q=>`<option value="${esc(q.id)}">${esc(q.title)}</option>`).join(''):'';if([...$('#photo-question').options].some(o=>o.value===selected))$('#photo-question').value=selected}
function editable(){if(!vault.key||!state)throw Error('Сначала сохраните назначенный выезд');if(state.completed)throw Error('Очередь зафиксирована. Изменение её содержимого и ID запрещено.');if(Date.now()>=state.package.expiresAt)throw Error('Пакет истёк. Локальная копия сохранена; новая работа заблокирована.')}
async function takeTab(){if(!navigator.locks)throw Error('Нужен браузер с Web Locks API');if(releaseTab)return;let acquired;const ready=new Promise(resolve=>acquired=resolve);navigator.locks.request('ksu-offline-release-one-visit',{ifAvailable:true},async lease=>{if(!lease){acquired(false);return}await new Promise(resolve=>{releaseTab=resolve;acquired(true)})});if(!await ready)throw Error('Этот локальный профиль открыт в другой вкладке. Закройте её.')}

$('#open').onclick=()=>task(async()=>{
  await takeTab();state=await vault.unlock($('#phrase').value);$('#phrase').value='';
  if(state&&state.schema!==1){vault.lock();state=null;throw Error('Неизвестная версия локальной копии; она не удалена')}
  revision=savedRevision=state?.revision||0;lastWriteError=null;lastActive=Date.now();$('#minutes').value=state?.lockMinutes||10;
  if(state?.queue?.status===STATUS.sending){const next=structuredClone(state);next.queue.status=STATUS.waiting;next.queue.detail='Отправка была прервана. Сначала проверим receipt, ID не изменены.';await replace(next)}
  await render();say(state?'Локальный выезд восстановлен. Серверный вход для чтения не нужен.':'Локальный профиль создан. Войдите инспектором и выберите выезд.');
});
$('#login-form').onsubmit=event=>{event.preventDefault();task(async()=>{await flush();let user;try{user=await engine.request('/api/login',{body:{username:$('#username').value,password:$('#password').value},bind:false})}finally{$('#password').value=''}engine.session=null;engine.assertUser(user);engine.session=user;const {context}=await engine.identity();$('#environment').textContent=context.mode==='staging'?'Проверочный стенд · отдельная копия базы':'Офлайн-доступ: '+(context.enabled?'включён':'отключён');$('#session-status').textContent='Сессия инспектора: '+user.name;say('Вход подтверждён. Локальная очередь не изменена.')})};
$('#logout').onclick=()=>task(async()=>{await flush();await engine.identity();await engine.request('/api/logout',{body:{}});engine.session=null;$('#session-status').textContent='Серверная сессия закрыта. Локальные данные сохранены.';say('Выход выполнен. Можно читать и продолжать незавершённую проверку без сети.')});
$('#load-visits').onclick=()=>task(async()=>{if(state)throw Error('Локальная копия уже существует и не будет перезаписана');const {user,context}=await engine.identity();if(!context.enabled)throw Error('Подготовка новых офлайн-выездов сейчас отключена');const current=await engine.request('/api/state');availableVisits=current.visits.filter(v=>v.assignedTo===user.id&&v.status==='planned');$('#visit').innerHTML=availableVisits.map(v=>`<option value="${esc(v.id)}">${esc(v.date)} · ${esc(current.sites.find(s=>s.id===v.siteId)?.name||v.siteId)} · ${esc(v.goal)}</option>`).join('');say(availableVisits.length?'Выберите один выезд и сохраните его для работы без сети.':'Назначенных запланированных выездов нет.')});
$('#prepare').onclick=()=>task(async()=>{
  if(state)throw Error('Подготовка не перезаписывает существующую локальную копию');
  const visit=availableVisits.find(v=>v.id===$('#visit').value);if(!visit)throw Error('Сначала загрузите назначенные выезды');
  const {user,context}=await engine.identity();if(!context.enabled)throw Error('Подготовка новых офлайн-выездов сейчас отключена');const pack=await engine.request('/api/offline/packages',{body:{siteIds:[visit.siteId],ttlHours:24}});
  const pinned=pack.visits.find(v=>v.id===visit.id);
  if(pack.format!=='ksu-offline-package-v1'||pack.userId!==user.id||!pinned||pinned.assignedTo!==user.id||await sha(encoder.encode(canonical(pack.checklist.questions)))!==pack.checklist.id)throw Error('Пакет изменился или не соответствует выбранному выезду. Повторите подготовку.');
  const next={schema:1,revision:0,user:{id:user.id,name:user.name,username:user.username},deploymentId:context.deploymentId,package:{id:pack.id,expiresAt:Date.parse(pack.expiresAt)},site:pack.sites.find(s=>s.id===pinned.siteId),visit:pinned,template:pack.checklist,inspectionId:crypto.randomUUID(),operationId:crypto.randomUUID(),lockMinutes:Number($('#minutes').value),completed:false,photos:[],answers:Object.fromEntries(pack.checklist.questions.map(q=>[q.id,{status:'pending',comment:'',findingId:crypto.randomUUID()}])),savedAt:Date.now()};
  await replace(next);await render();say('Выезд и историческая версия чек-листа сохранены. Дождитесь активной офлайн-оболочки.');
});
$('#questions').addEventListener('input',event=>{try{editable();const id=event.target.dataset.answer||event.target.dataset.comment;if(!id)return;state.answers[id][event.target.dataset.answer?'status':'comment']=event.target.value;state.revision=++revision;state.savedAt=Date.now();queueSave(structuredClone(state)).catch(()=>{});photoOptions();status()}catch(error){say(error.message,true)}});
$('#save').onclick=()=>task(async()=>{await flush();if(!state)throw Error('Выезд не подготовлен');await replace(structuredClone(state));say('Изменения подтверждены хранилищем устройства.')});
$('#complete').onclick=()=>task(async()=>{editable();await flush();const answers=Object.values(state.answers);if(answers.some(a=>a.status==='pending'||a.status==='fail'&&!a.comment.trim()))throw Error('Ответьте на все пункты и опишите каждое нарушение');if(answers.filter(a=>a.status==='fail').length>98)throw Error('Предел одного серверного batch — 98 нарушений; локальная копия сохранена.');const next=structuredClone(state);next.completed=true;next.queue={status:STATUS.waiting,detail:'Фотографии и единый акт ждут отправки. ID уже сохранены.',photos:next.photos.map(p=>({...p,receipt:null})),batch:null,receipt:null};await replace(next);await render();say('Очередь зашифрована и сохранена. Теперь можно отправлять или закрыть браузер.')});
$('#sync').onclick=()=>task(async()=>{await flush();await engine.sync();status();say('Операция подтверждена сервером; локальная копия и receipt сохранены.')});

async function processPhoto(file,questionId){
  editable();await flush();if(!questionId||!state.answers[questionId])throw Error('Выберите нарушение для фотографии');
  if(state.photos.some(p=>p.questionId===questionId))throw Error('Для этого пункта фото уже сохранено с устойчивым ID. Оно не заменяется автоматически.');
  if(state.photos.length>=20)throw Error('Предел выезда — 20 фото. Сохранённые снимки не удалены.');
  if(!['image/jpeg','image/png','image/webp'].includes(file.type)||file.size>10000000)throw Error('Нужен JPEG/PNG/WebP до 10 МБ');
  const bitmap=await createImageBitmap(file);let blob;
  try{if(bitmap.width*bitmap.height>16000000)throw Error('Изображение превышает 16 Мп');const scale=Math.min(1,1600/Math.max(bitmap.width,bitmap.height)),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.8))}finally{bitmap.close()}
  if(!blob||blob.size>3000000)throw Error('Не удалось сжать фото до 3 МБ');
  const p={id:crypto.randomUUID(),questionId,type:blob.type,size:blob.size,sha256:await sha(await blob.arrayBuffer())};const next=structuredClone(state);next.photos.push(p);await replace(next,[[p.id,blob]]);await render();say('Фотография и её SHA-256 зашифрованы и подтверждены хранилищем.');
}
async function requestPhoto(input){if(busy)return;try{editable();if(dirty()){if(!await task(flush))return;if(!navigator.userActivation||!navigator.userActivation.isActive){say('Текст сохранён. Нажмите кнопку фото ещё раз.');return}}photoQuestion=$('#photo-question').value;if(!photoQuestion)throw Error('Сначала отметьте нарушение и выберите его для фото');input.value='';lastActive=Date.now();try{if(typeof input.showPicker==='function')input.showPicker();else input.click()}catch{if(navigator.userActivation&&!navigator.userActivation.isActive){say('Текст сохранён. Нажмите кнопку фото ещё раз.');return}input.click()}}catch(error){say(error.message,true)}}
$('#take-photo').onclick=()=>requestPhoto($('#camera-photo'));$('#choose-photo').onclick=()=>requestPhoto($('#photo'));
for(const input of [$('#camera-photo'),$('#photo')]){input.addEventListener('cancel',()=>{input.value='';photoQuestion=null;say('Выбор отменён. Подтверждённые данные и фото не изменены.')});input.addEventListener('change',()=>{const file=input.files?.[0],qid=photoQuestion;input.value='';photoQuestion=null;lastActive=Date.now();if(file)task(()=>processPhoto(file,qid))})}
async function lock(force=false){try{await flush()}catch(error){if(!force)throw error;say('Последние правки не подтвердились хранилищем. Сохранена предыдущая копия; локальный профиль блокируется.',true)}vault.lock();engine.session=null;state=null;$('#phrase').value='';$('#password').value='';clearPhotos();await render()}
$('#lock').onclick=()=>task(()=>lock());
$('#minutes').onchange=()=>task(async()=>{await flush();const value=Number($('#minutes').value);if(!Number.isInteger(value)||value<1||value>60)throw Error('Интервал 1–60 минут');if(state){const next=structuredClone(state);next.lockMinutes=value;await replace(next)}});
$('#export').onclick=()=>task(async()=>{await flush();const url=URL.createObjectURL(new Blob([exportJSON(await vault.export())],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ksu-offline-encrypted.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);say('Скачана зашифрованная копия. Фразу храните отдельно; автоматический импорт не реализован.')});
for(const event of ['pointerdown','keydown','input','change'])document.addEventListener(event,()=>lastActive=Date.now(),{passive:true});
setInterval(()=>{if(vault.key&&!busy&&Date.now()-lastActive>=(state?.lockMinutes||10)*60000)task(()=>lock(true))},1000);
window.addEventListener('beforeunload',event=>{if(dirty()||busy){event.preventDefault();event.returnValue=''}});
function workerStatus(){$('#update').hidden=!registration?.waiting;$('#worker-status').textContent=registration?.waiting?'Новая оболочка ожидает безопасного обновления':navigator.serviceWorker.controller?'Офлайн-оболочка активна. Перед отключением сети дождитесь сохранения выезда.':'Офлайн-оболочка готовится; сеть пока нужна.'}
$('#update').onclick=()=>task(async()=>{if(!vault.key)throw Error('Сначала разблокируйте профиль');await flush();await lock();registration?.waiting?.postMessage({type:'ACTIVATE'});say('Обновление разрешено. Перезагрузите страницу и разблокируйте сохранённую копию.')});
navigator.serviceWorker?.addEventListener('controllerchange',workerStatus);
(async()=>{try{if(!isSecureContext||!crypto.subtle||!indexedDB||!navigator.serviceWorker)throw Error('Нужен Chrome и localhost/127.0.0.1 либо доверенный HTTPS');registration=await navigator.serviceWorker.register('./sw.js',{scope:'/offline-visit/',updateViaCache:'none'});registration.addEventListener('updatefound',()=>registration.installing?.addEventListener('statechange',workerStatus));await navigator.serviceWorker.ready;workerStatus()}catch(error){say(error.message,true)}})();
