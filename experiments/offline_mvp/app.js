import {Vault,sha,exportJSON} from './store.js';
const $=s=>document.querySelector(s),vault=new Vault();
let state=null,busy=false,dirty=false,urls=[],registration=null,lastActive=Date.now(),lockMinutes=10,releaseTab=null;
const say=(text,error=false)=>{$('#message').textContent=text;$('#message').classList.toggle('error',error)};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function ready(){return state&&Date.now()<state.package.expiresAt}
function setBusy(value){busy=value;document.querySelectorAll('#editor button,#editor input,#editor select,#editor textarea,#open,#update').forEach(x=>x.disabled=value)}
async function task(fn){if(busy)return false;setBusy(true);try{await fn();return true}catch(error){say(error.name==='QuotaExceededError'?'Не сохранено: хранилище заполнено. Предыдущая подтверждённая копия сохранена.':error.message,true);return false}finally{setBusy(false)}}
function failQuota(){const value=$('#quota').checked;$('#quota').checked=false;return value}
function clearPhotos(){for(const url of urls)URL.revokeObjectURL(url);urls=[];$('#photos').replaceChildren()}
async function photos(){clearPhotos();for(const p of [state?.reference,...(state?.photos||[])].filter(Boolean)){const blob=await vault.photo(p.id,p.type);const url=URL.createObjectURL(blob);urls.push(url);const item=document.createElement('div');const img=document.createElement('img');img.className='photo-preview';img.src=url;img.alt=p.reference?'ДЕМО · загруженный образец':'ДЕМО · фото нарушения';const meta=document.createElement('p');meta.className='photo-meta';meta.textContent=(p.reference?'Образец · ':'Фото · ')+p.id;item.append(img,meta);$('#photos').append(item)}}
async function render(){
  $('#unlock').hidden=!!vault.key;$('#editor').hidden=!vault.key;
  $('#local-status').textContent=state?(dirty?'Есть изменения, ещё не сохранённые на устройстве':`Сохранено на устройстве · ${state.completed?'завершено локально':'черновик'} · ${state.inspectionId}`):'Черновик ещё не создан';
  $('#package-status').textContent=state?`${ready()?'Пакет доступен':'24 часа истекли: новая работа заблокирована; данные сохранены'} · чек-лист ${state.template.id} / v${state.template.version} · до ${new Date(state.package.expiresAt).toLocaleString()}`:'Сначала загрузите ДЕМО-пакет онлайн';
  $('#questions').innerHTML=state?state.template.questions.map(q=>`<article class="question-card"><strong>${esc(q.title)}</strong><label>Результат<select data-answer="${esc(q.id)}"><option value="pending">Не заполнено</option><option value="ok">Соответствует</option><option value="fail">Нарушение</option><option value="na">Не применимо</option></select></label><label>Описание замечания<textarea data-comment="${esc(q.id)}" maxlength="4000"></textarea></label></article>`).join(''):'';
  if(state)for(const q of state.template.questions){$(`[data-answer="${q.id}"]`).value=state.answers[q.id].status;$(`[data-comment="${q.id}"]`).value=state.answers[q.id].comment}
  if(busy)setBusy(true);
  if(vault.key)await photos();else clearPhotos();
  const estimate=await navigator.storage?.estimate?.();$('#storage-status').textContent=estimate?`Браузер: занято ${Math.round(estimate.usage/1024/1024)} МиБ из ≈${Math.round(estimate.quota/1024/1024)} МиБ. Квота не гарантирует сохранность.`:'Оценка квоты недоступна';
}
function editable(){if(!ready())throw Error('Пакет не загружен или истёк. Подтверждённые данные сохранены.');if(state.completed)throw Error('ДЕМО-проверка уже завершена локально; повторная отправка отсутствует')}
async function save(){if(!state)return;const next=structuredClone(state);next.savedAt=Date.now();await vault.save(next,[],failQuota());state=next;dirty=false;await render();say('Черновик и ссылки на фото сохранены на устройстве')}
async function lock(force=false){if(!vault.key)return;if(dirty){try{await save()}catch(error){if(!force)throw error;say('Автоблокировка: последние неподтверждённые правки не сохранены. Предыдущий черновик сохранён.',true)}}vault.lock();state=null;dirty=false;$('#phrase').value='';$('#questions').replaceChildren();clearPhotos();$('#unlock').hidden=false;$('#editor').hidden=true;$('#local-status').textContent='';$('#package-status').textContent='';}
async function takeTab(){if(!navigator.locks)throw Error('Для безопасного single-tab прототипа нужен Web Locks API');if(releaseTab)return;let acquired;const result=new Promise(r=>acquired=r);navigator.locks.request('ksu-stage0-vault',{ifAvailable:true},async lease=>{if(!lease){acquired(false);return}await new Promise(resolve=>{releaseTab=resolve;acquired(true)})});if(!await result)throw Error('Прототип уже открыт в другой вкладке. Закройте её перед разблокировкой.')}
$('#open').onclick=()=>task(async()=>{await takeTab();try{state=await vault.unlock($('#phrase').value);$('#phrase').value='';lockMinutes=state?.lockMinutes||10;$('#minutes').value=lockMinutes;lastActive=Date.now();dirty=false;await render();say(state?'Подтверждённый локальный черновик восстановлен':'Профиль создан: загрузите ДЕМО-пакет')}catch(error){vault.lock();state=null;dirty=false;$('#questions').replaceChildren();clearPhotos();$('#unlock').hidden=false;$('#editor').hidden=true;throw error}});
$('#prepare').onclick=()=>task(async()=>{
  if(state)throw Error('Пакет уже загружен: прототип не перезаписывает существующий черновик');
  const response=await fetch('/demo-package.json',{cache:'no-store',credentials:'omit'});if(!response.ok)throw Error('Нет сети для подготовки');const pack=await response.json();
  if(pack.demo!==true||pack.deploymentId!=='ksu-stage0-synthetic'||await sha(new TextEncoder().encode(pack.templateJson))!==pack.templateSha256)throw Error('Некорректный ДЕМО-пакет');
  const imageResponse=await fetch(pack.photo.url,{cache:'no-store',credentials:'omit'});if(!imageResponse.ok)throw Error('Не загружено тестовое фото');const blob=await imageResponse.blob();if(await sha(await blob.arrayBuffer())!==pack.photo.sha256)throw Error('Не совпал SHA-256 фото');
  const template=JSON.parse(pack.templateJson);const next={package:pack,template,inspectionId:crypto.randomUUID(),operationId:crypto.randomUUID(),lockMinutes,completed:false,photos:[],reference:{id:pack.photo.id,sha256:pack.photo.sha256,size:blob.size,type:blob.type,reference:true},answers:Object.fromEntries(template.questions.map(q=>[q.id,{status:'pending',comment:'',findingId:crypto.randomUUID()}])),savedAt:Date.now()};
  await vault.save(next,[[pack.photo.id,blob]],failQuota());state=next;dirty=false;await render();say('ДЕМО-пакет и фото сохранены. Дождитесь готовности офлайн-оболочки.');
});
$('#questions').addEventListener('input',e=>{try{editable();const id=e.target.dataset.answer||e.target.dataset.comment;if(!id)return;state.answers[id][e.target.dataset.answer?'status':'comment']=e.target.value;dirty=true;$('#local-status').textContent='Есть изменения, ещё не сохранённые на устройстве'}catch(error){say(error.message,true);render()}});
$('#save').onclick=()=>task(save);
$('#complete').onclick=()=>task(async()=>{editable();if(Object.values(state.answers).some(a=>a.status==='pending'||a.status==='fail'&&!a.comment.trim()))throw Error('Ответьте на все вопросы и опишите каждое нарушение');const next=structuredClone(state);next.completed=true;next.savedAt=Date.now();await vault.save(next,[],failQuota());state=next;dirty=false;await render();say('Завершено только на устройстве. Сервер не получал эту проверку.')});
// Both native pickers share one validation/compression/encryption/atomic-write path.
async function processPhoto(file){
  editable();
    if($('#memory').checked){$('#memory').checked=false;throw Error('ДЕМО: отказ бюджета памяти до декодирования. Фото не добавлено; сохранённый черновик цел.')}
    if(!['image/jpeg','image/png','image/webp'].includes(file.type)||file.size>10_000_000)throw Error('Нужен JPEG/PNG/WebP до 10 МБ');
    const sourceHash=await sha(await file.arrayBuffer());if(state.photos.some(p=>p.sourceHash===sourceHash)){say('Это фото уже сохранено: его ID не изменён');return}
    const bitmap=await createImageBitmap(file);
    let blob;try{if(bitmap.width*bitmap.height>16000000)throw Error('Изображение больше 16 Мп; уменьшите размер до добавления');const scale=Math.min(1,1600/Math.max(bitmap.width,bitmap.height));const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.8))}finally{bitmap.close()}
    if(!blob||blob.size>3_000_000)throw Error('Не удалось подготовить фото до 3 МБ');
    const p={id:crypto.randomUUID(),sourceHash,sha256:await sha(await blob.arrayBuffer()),size:blob.size,type:blob.type};const next=structuredClone(state);next.photos.push(p);next.savedAt=Date.now();await vault.save(next,[[p.id,blob]],failQuota());state=next;dirty=false;await render();say('Фото зашифровано и сохранено на устройстве. ID устойчив.')
}


function cancelledPhoto(input){
  input.value='';lastActive=Date.now();
  say((input.id==='camera-photo'?'Съёмка отменена.':'Выбор фото отменён.')+' Сохранённый черновик и фотографии не изменены.');
}
function openPhotoPicker(input){
  editable();input.value='';lastActive=Date.now();
  try{if(typeof input.showPicker==='function')input.showPicker();else input.click()}
  catch(error){
    // Older browsers may require a fresh synchronous tap after an asynchronous save.
    if(navigator.userActivation&&!navigator.userActivation.isActive){say('Черновик сохранён. Нажмите кнопку фото ещё раз, чтобы открыть выбор.');return}
    input.click();
  }
}
async function requestPhoto(input){
  if(busy)return;
  try{
    editable();
    if(dirty){
      // Persist text before handing control to an external camera/gallery. Never open on failure.
      if(!await task(save)||dirty||!vault.key)return;
      if(navigator.userActivation&&!navigator.userActivation.isActive){say('Черновик сохранён. Нажмите кнопку фото ещё раз, чтобы открыть выбор.');return}
    }
    openPhotoPicker(input);
  }catch(error){say(error.message,true)}
}
$('#take-photo').onclick=()=>requestPhoto($('#camera-photo'));
$('#choose-photo').onclick=()=>requestPhoto($('#photo'));
for(const input of [$('#camera-photo'),$('#photo')]){
  input.addEventListener('cancel',()=>cancelledPhoto(input));
  input.addEventListener('change',()=>{
    const file=input.files?.[0];
    if(!file){cancelledPhoto(input);return}
    lastActive=Date.now();
    // Keep only the captured File object; release the native input even if another task is busy.
    input.value='';
    task(()=>processPhoto(file));
  });
}
$('#lock').onclick=()=>task(()=>lock());
$('#minutes').onchange=()=>task(async()=>{const value=Number($('#minutes').value);if(!Number.isInteger(value)||value<1||value>60)throw Error('Интервал 1–60 минут');lockMinutes=value;if(state){const next=structuredClone(state);next.lockMinutes=value;await vault.save(next,[],failQuota());state=next;dirty=false}say('Интервал автоблокировки: '+value+' мин')});
$('#export').onclick=()=>task(async()=>{if(dirty)throw Error('Сначала сохраните черновик; экспорт содержит только подтверждённые записи');const content=exportJSON(await vault.export());const url=URL.createObjectURL(new Blob([content],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ksu-stage0-encrypted.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);say('Скачана только зашифрованная копия. Импорт — вне этапа 0.')});
$('#persist').onclick=()=>task(async()=>say(await navigator.storage?.persist?.()?'Браузер предоставил persistent storage; удаление данных пользователем всё равно возможно.':'Persistent storage не предоставлен. Это не отменяет локальные записи.'));
for(const event of ['pointerdown','keydown','input','change'])document.addEventListener(event,()=>lastActive=Date.now(),{passive:true});
setInterval(()=>{if(vault.key&&!busy&&Date.now()-lastActive>=lockMinutes*60000)task(()=>lock(true))},1000);
function workerStatus(){const waiting=registration?.waiting;$('#update').hidden=!waiting;$('#worker-status').textContent=waiting?'Новая оболочка ожидает сохранения черновика':navigator.serviceWorker.controller?'Офлайн-оболочка активна. Полный холодный запуск проверяйте без сети.':'Оболочка ещё готовится: не отключайте сеть'}
async function boot(){$('#worker-status').textContent='Подготовка офлайн-оболочки…';try{if(!isSecureContext||!crypto.subtle||!indexedDB||!('serviceWorker' in navigator))throw Error('Нужен поддерживаемый браузер и localhost/доверенный HTTPS');registration=await navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'});registration.addEventListener('updatefound',()=>registration.installing?.addEventListener('statechange',workerStatus));await navigator.serviceWorker.ready;workerStatus()}catch(error){say(error.message,true)}}
navigator.serviceWorker?.addEventListener('controllerchange',workerStatus);
$('#check-update').onclick=()=>task(async()=>{if(!navigator.onLine)throw Error('Для проверки обновления нужна сеть');await registration.update();workerStatus();say('Проверка обновления запрошена')});
$('#update').onclick=()=>task(async()=>{if(!registration?.waiting)return;if(!vault.key)throw Error('Разблокируйте профиль перед обновлением');if(dirty)await save();await lock();registration.waiting.postMessage({type:'ACTIVATE'});say('Обновление разрешено после сохранения. Перезагрузите страницу и разблокируйте профиль.')});
window.addEventListener('beforeunload',e=>{if(dirty||busy){e.preventDefault();e.returnValue=''}});
boot();
