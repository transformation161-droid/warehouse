export const DB_NAME='ksu-offline-release-encrypted-v1';
const enc=new TextEncoder(),dec=new TextDecoder();
const scope='ksu-offline-release:v1';
export const sha=async data=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data)),b=>b.toString(16).padStart(2,'0')).join('');
const random=n=>crypto.getRandomValues(new Uint8Array(n));
function openDB(){return new Promise((resolve,reject)=>{const r=indexedDB.open(DB_NAME,1);r.onupgradeneeded=()=>{r.result.createObjectStore('meta');r.result.createObjectStore('records')};r.onerror=()=>reject(r.error);r.onblocked=()=>reject(Error('Закройте другие вкладки офлайн-выезда'));r.onsuccess=()=>resolve(r.result)})}
async function read(store,key){const db=await openDB();try{return await new Promise((resolve,reject)=>{const tx=db.transaction(store);const r=tx.objectStore(store).get(key);tx.oncomplete=()=>resolve(r.result);tx.onabort=()=>reject(tx.error||Error('Транзакция чтения прервана'));tx.onerror=()=>{}})}finally{db.close()}}
async function commit(store,entries){const db=await openDB();try{return await new Promise((resolve,reject)=>{let tx;try{tx=db.transaction(store,'readwrite',{durability:'strict'})}catch{tx=db.transaction(store,'readwrite')};const target=tx.objectStore(store);for(const [id,value] of entries)target.put(value,id);tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error||Error('Транзакция не завершена'));tx.onerror=()=>{}})}finally{db.close()}}
async function derive(phrase,salt,iterations){const material=await crypto.subtle.importKey('raw',enc.encode(phrase),'PBKDF2',false,['deriveKey']);return crypto.subtle.deriveKey({name:'PBKDF2',hash:'SHA-256',salt,iterations},material,{name:'AES-GCM',length:256},false,['encrypt','decrypt'])}
async function encrypt(key,id,bytes){const iv=random(12);return {iv,cipher:await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:enc.encode(scope+':'+id)},key,bytes)}}
async function decrypt(key,id,value){return crypto.subtle.decrypt({name:'AES-GCM',iv:value.iv,additionalData:enc.encode(scope+':'+id)},key,value.cipher)}
export class Vault {
  key=null;
  async unlock(phrase){
    if(!isSecureContext||!crypto.subtle)throw Error('Нужен localhost или доверенный HTTPS');
    if(phrase.length<12)throw Error('Локальная фраза — не менее 12 символов');
    let meta=await read('meta','profile');
    if(!meta){
      const salt=random(16),iterations=310000,raw=random(32),wrapper=await derive(phrase,salt,iterations);
      const wrapped=await encrypt(wrapper,'key',raw);const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt','decrypt']);raw.fill(0);
      meta={schema:1,salt,iterations,wrapped};
      // Single-tab profile ownership also guards concurrent initialisation.
      await commit('meta',[['profile',meta]]);this.key=key;
    }else{
      if(meta.schema!==1||meta.iterations!==310000)throw Error('Несовместимая версия хранилища');
      try{const wrapper=await derive(phrase,meta.salt,meta.iterations);const raw=new Uint8Array(await decrypt(wrapper,'key',meta.wrapped));this.key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt','decrypt']);raw.fill(0)}catch{throw Error('Неверная фраза или повреждённый профиль')}
    }
    const value=await read('records','state');return value?JSON.parse(dec.decode(await decrypt(this.key,'state',value))):null;
  }
  lock(){this.key=null}
  async save(state,photos=[]){
    if(!this.key)throw Error('Профиль заблокирован');
    const total=state.photos.reduce((n,p)=>n+p.size,0)+(state.reference?.size||0);
    if(total>100*1024*1024)throw new DOMException('Превышен бюджет 100 МиБ','QuotaExceededError');
    const entries=[['state',await encrypt(this.key,'state',enc.encode(JSON.stringify(state)))]];
    for(const [id,blob] of photos)entries.push(['photo:'+id,await encrypt(this.key,'photo:'+id,await blob.arrayBuffer())]);
    // Crypto happens BEFORE opening IDB transaction. State and photo writes commit together.
    await commit('records',entries);
  }
  async photo(id,type){const value=await read('records','photo:'+id);if(!value)throw Error('Фото отсутствует');return new Blob([await decrypt(this.key,'photo:'+id,value)],{type})}
  async export(){const db=await openDB();try{return await new Promise((resolve,reject)=>{const tx=db.transaction(['meta','records']);const data={format:'ksu-offline-release-encrypted-v1',stores:{}};for(const name of ['meta','records']){const values=[];data.stores[name]=values;tx.objectStore(name).openCursor().onsuccess=e=>{const c=e.target.result;if(c){values.push([c.key,c.value]);c.continue()}}}tx.oncomplete=()=>resolve(data);tx.onabort=()=>reject(tx.error)})}finally{db.close()}}
}
export function exportJSON(data){return JSON.stringify(data,(_,v)=>v instanceof ArrayBuffer?{bytes:Array.from(new Uint8Array(v))}:v instanceof Uint8Array?{bytes:Array.from(v)}:v)}
