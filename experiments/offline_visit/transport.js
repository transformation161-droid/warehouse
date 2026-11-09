export const STATUS={local:'Сохранено на устройстве',waiting:'Ожидает отправки',sending:'Отправляется',accepted:'Принято сервером',conflict:'Конфликт',error:'Ошибка'};
export const canonical=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
export class ApiError extends Error{constructor(status,message,body=null){super(message);this.status=status;this.body=body}}

export class SyncEngine{
  session=null;
  constructor({getState,commit,photo,sha}){Object.assign(this,{getState,commit,photo,sha})}
  async request(path,{body,bind=true}={}){
    const headers={},state=this.getState();
    if(bind&&state){headers['X-KSU-Expected-User']=state.user.id;headers['X-KSU-Offline-Deployment']=state.deploymentId}
    if(body!==undefined){headers['Content-Type']='application/json';if(this.session?.csrf)headers['X-CSRF-Token']=this.session.csrf}
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    try{
      const response=await fetch(path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal});
      let data;try{data=await response.json()}catch{throw new ApiError(502,'Сервер вернул неполный ответ; подтверждение нужно проверить')}
      if(!response.ok)throw new ApiError(response.status,typeof data.detail==='string'?data.detail:`Сервер отказал: ${response.status}`,data);
      return data;
    }catch(error){if(error instanceof ApiError)throw error;throw new ApiError(0,'Нет подтверждённого ответа. Очередь сохранена; повтор проверит receipt.')}
    finally{clearTimeout(timer)}
  }
  assertUser(user){const state=this.getState();if(user.role!=='inspector'||state&&user.id!==state.user.id)throw new ApiError(403,'Нужна сессия того инспектора, которому принадлежит локальный выезд. Данные сохранены.')}
  async identity(){
    const user=await this.request('/api/me');this.assertUser(user);this.session=user;
    const context=await this.request('/api/offline-visit/context');
    if(context.synthetic!==true||context.stage!==2||context.userId!==user.id||context.role!=='inspector')throw new ApiError(403,'Нужна изолированная ДЕМО-установка этапа 2');
    if(this.getState()&&context.deploymentId!==this.getState().deploymentId)throw new ApiError(409,'Другая тестовая установка. Локальная копия сохранена.');
    return {user,context};
  }
  async receipt(id,kind){
    try{const value=await this.request('/api/receipts/'+encodeURIComponent(id));if(value.mutationId!==id||value.kind!==kind||![200,409].includes(value.status))throw new ApiError(502,'Некорректное подтверждение сервера');return value}
    catch(error){if(error.status===404)return null;throw error}
  }
  async postWithReceipt(path,body,id,kind){
    try{return await this.request(path,{body})}
    catch(error){if(error.status===0||error.status>=500){try{const saved=await this.receipt(id,kind);if(saved)return saved}catch{} }throw error}
  }
  async transition(status,detail='',extras={}){const next=structuredClone(this.getState());Object.assign(next.queue,{status,detail,...extras});next.savedAt=Date.now();await this.commit(next)}
  verifyPhoto(value,operation){
    if(value.kind!=='media'||value.mutationId!==operation.id||value.status!==200||value.committed!==true||value.sha256!==operation.sha256||!/^\/api\/media\/[a-f0-9]{48}$/.test(value.url)||!value.records?.some(r=>r.siteId===this.getState().visit.siteId))throw new ApiError(409,'Подтверждение фотографии не соответствует очереди');
  }
  async accept(value){
    const state=this.getState(),batch=state.queue.batch;
    if(!batch||value.kind!=='changes'||value.mutationId!==state.operationId||value.status!==200||value.committed!==true||!batch.changes.every(c=>value.records?.some(r=>r.collection===c.collection&&r.id===c.id&&r.version===c.expectedVersion+1)))throw new ApiError(409,'Подтверждение выезда не соответствует локальной операции');
    await this.transition(STATUS.accepted,'Акт, нарушения и завершение выезда подтверждены одним receipt.',{receipt:value});
  }
  buildBatch(){
    const state=this.getState(),siteId=state.visit.siteId;
    const photos=new Map(state.queue.photos.map(p=>[p.questionId,p.receipt?.url||'']));
    const results=state.template.questions.map(q=>({questionId:q.id,status:state.answers[q.id].status,comment:state.answers[q.id].comment,photo:photos.get(q.id)||''}));
    const changes=[{collection:'inspections',id:state.inspectionId,expectedVersion:0,data:{siteId,date:state.visit.date,inspector:state.user.name,checklistVersion:state.template.id,results}}];
    for(const q of state.template.questions){const a=state.answers[q.id];if(a.status==='fail')changes.push({collection:'findings',id:a.findingId,expectedVersion:0,data:{siteId,inspectionId:state.inspectionId,questionId:q.id,section:q.section,title:q.title,description:a.comment,photo:photos.get(q.id)||''}})}
    changes.push({collection:'visits',id:state.visit.id,expectedVersion:state.visit.version,data:{...state.visit,status:'completed',inspectionId:state.inspectionId}});
    return {mutationId:state.operationId,packageId:state.package.id,changes};
  }
  async sync(){
    if(!this.getState()?.queue)throw new ApiError(422,'Сначала завершите проверку и сохраните очередь');
    if(this.getState().queue.receipt?.committed===true)return;
    try{
      await this.transition(STATUS.sending,'Проверяем текущую сессию и сохранённый результат');
      await this.identity();
      let saved=await this.receipt(this.getState().operationId,'changes');
      if(saved){if(saved.status===409)throw new ApiError(409,saved.detail,saved);await this.accept(saved);return}
      const state=this.getState(),current=await this.request('/api/state');
      if(!current.sites.some(s=>s.id===state.visit.siteId))throw new ApiError(403,'Доступ к объекту отозван. Локальные данные сохранены.');
      const visit=current.visits.find(v=>v.id===state.visit.id);
      if(!visit||visit.assignedTo!==state.user.id)throw new ApiError(403,'Выезд больше не назначен этому инспектору. Копия сохранена.');
      if(visit.status!=='planned'||visit.version!==state.visit.version||visit.siteId!==state.visit.siteId)throw new ApiError(409,'Выезд изменён на сервере. Нужна ручная сверка; автоматической перезаписи нет.');
      if(Date.now()>=state.package.expiresAt)throw new ApiError(409,'Пакет истёк. Не создавайте новую операцию без сверки локальной копии.');
      for(let index=0;index<this.getState().queue.photos.length;index++){
        const operation=this.getState().queue.photos[index];if(operation.receipt)continue;
        let receipt=await this.receipt(operation.id,'media');
        if(!receipt){
          const blob=await this.photo(operation.id,operation.type),bytes=new Uint8Array(await blob.arrayBuffer());
          if(await this.sha(bytes)!==operation.sha256)throw new ApiError(409,'Повреждена локальная фотография. Копия не удалена.');
          let binary='';for(let offset=0;offset<bytes.length;offset+=32768)binary+=String.fromCharCode(...bytes.subarray(offset,offset+32768));
          receipt=await this.postWithReceipt('/api/media',{siteId:state.visit.siteId,content:btoa(binary),mutationId:operation.id,sha256:operation.sha256},operation.id,'media');
        }
        this.verifyPhoto(receipt,operation);
        const next=structuredClone(this.getState());next.queue.photos[index].receipt=receipt;next.queue.detail=`Фото подтверждены: ${index+1} из ${next.queue.photos.length}`;await this.commit(next);
      }
      if(!this.getState().queue.batch){const next=structuredClone(this.getState());next.queue.batch=this.buildBatch();await this.commit(next)}
      const batch=this.getState().queue.batch;
      saved=await this.postWithReceipt('/api/offline/changes',batch,batch.mutationId,'changes');
      if(saved.status===409)throw new ApiError(409,saved.detail,saved);
      await this.accept(saved);
    }catch(error){
      const status=error.status===409?STATUS.conflict:error.status===0?STATUS.waiting:STATUS.error;
      const detail=error.status===401?'Сессия истекла. Войдите тем же инспектором и повторите; очередь сохранена.':error.message;
      try{await this.transition(status,detail,error.body?.kind==='changes'?{receipt:error.body}:{})}catch{throw new Error('Не удалось сохранить подтверждение на устройстве. Не удаляйте профиль; повторите проверку receipt после освобождения места.')}
      throw error;
    }
  }
}
