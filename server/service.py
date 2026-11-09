import json
import sqlite3
import psycopg
from datetime import date
from fastapi import HTTPException
from pydantic import ValidationError
from .database import digest, now
from .models import MODELS


def fail(message, code=422):
    raise HTTPException(code, message)


def get_record(con, collection, ident):
    return con.execute('select * from records where collection=? and id=?', (collection, ident)).fetchone()


def accessible(con, actor, site):
    return actor['role']=='manager' or bool(con.execute('select 1 from user_sites where user_id=? and site_id=?',
                                                     (actor['id'],site)).fetchone())


def apply_changes(db, actor, batch, receipt_only=False, package_id=None):
    if actor['role']=='viewer':fail('Только просмотр',403)
    from .offline import current_checklist, receipt, store_receipt, validate_package, canonical
    legacy_hash=digest(batch.model_dump_json())
    body_hash = digest(canonical(batch.model_dump(mode='json')))
    try:
        with db.connect(actor) as con:
            con.execute('begin immediate')
            saved=receipt(con,actor,batch.mutationId,'changes',body_hash)
            if saved:
                if receipt_only:return saved
                if saved['status']!=200:fail(saved['detail'],saved['status'])
                return db.snapshot(con,actor)
            previous = con.execute('select body_hash from mutations where user_id=? and id=?', (actor['id'],batch.mutationId)).fetchone()
            if previous:
                if previous[0] != legacy_hash:
                    fail('Идентификатор запроса уже использован с другим содержимым',409)
                if receipt_only:fail('Для старой операции нет сохранённого receipt',409)
                return db.snapshot(con, actor)
            con.execute('savepoint operation_work')
            status=200;result={}
            try:
                if package_id:validate_package(con,actor,package_id,batch)
                if any(c.collection=='questions' for c in batch.changes):current_checklist(con)
                if db.backend=='postgresql':
                    db.check_capacity(con,len(batch.model_dump_json().encode())*4+len(batch.changes)*16384+65536)
                seen = set()
                for change in batch.changes:
                    key = (change.collection, change.id)
                    if key in seen:
                        fail('Запись повторяется в одном запросе')
                    seen.add(key)
                    save_record(con, actor, change.collection, change.id, change.data, change.expectedVersion)
                # Completion is a plan-to-fact transition, not a reusable status shortcut.
                new_inspections = {c.id for c in batch.changes if c.collection=='inspections' and c.expectedVersion==0}
                for change in batch.changes:
                    if change.collection=='visits' and change.data.get('status')=='completed':
                        visit=json.loads(get_record(con,'visits',change.id)['data'])
                        if visit['inspectionId'] not in new_inspections:
                            fail('Выполнить выезд можно только вместе с новым актом проверки',403)
                # All submitted inspection failures must have exactly one linked finding.
                for change in batch.changes:
                    if change.collection != 'inspections':
                        continue
                    ins = json.loads(get_record(con,'inspections',change.id)['data'])
                    linked = [json.loads(r[0]) for r in con.execute("select data from records where collection='findings' and json_extract(data,'$.inspectionId')=?", (change.id,))]
                    if sorted(x['questionId'] for x in linked) != sorted(x['questionId'] for x in ins['results'] if x['status']=='fail'):
                        fail('Для каждого нарушения чек-листа нужна ровно одна карточка')

                if any(c.collection=='questions' for c in batch.changes):current_checklist(con)
                con.execute('insert into mutations values(?,?,?)',(actor['id'],batch.mutationId,legacy_hash))
            except HTTPException as error:
                if error.status_code!=409:raise
                con.execute('rollback to savepoint operation_work')
                status=409;result={'detail':error.detail}
            con.execute('release savepoint operation_work')
            resources=[]
            for change in batch.changes:
                row=get_record(con,change.collection,change.id)
                if row and (change.collection in ('sites','questions') or accessible(con,actor,row['site_id'])):
                    resources.append({'collection':change.collection,'id':change.id,'siteId':row['site_id'],'version':row['version']})
            saved=store_receipt(con,actor,batch.mutationId,'changes',body_hash,status,result,resources)
            state=db.snapshot(con,actor) if not receipt_only and status==200 else None
        if receipt_only:return saved
        if status!=200:fail(result['detail'],status)
        return state
    except (sqlite3.IntegrityError,psycopg.IntegrityError) as exc:
        fail('Операция запрещена правилами базы данных' if 'forbidden' in str(exc) or isinstance(exc,psycopg.IntegrityError) else 'Нарушена целостность данных',403)


def save_record(con, actor, collection, ident, payload, expected_version):
    old = get_record(con,collection,ident)
    if old and not accessible(con,actor,old['site_id']) and collection not in ('sites','questions'):
        fail('Нет доступа к записи',403)
    if old and collection=='visits':
        previous_visit=json.loads(old['data'])
        if actor['role']!='manager' and previous_visit['assignedTo']!=actor['id']:
            fail('Можно изменять только назначенные вам выезды',403)
        if previous_visit['status']!='planned':
            fail('Завершённые и отменённые выезды неизменяемы',403)
    if (old['version'] if old else 0) != expected_version:
        fail('Запись изменена другим пользователем. Обновите данные и сравните изменения.',409)
    if old and actor['role']=='inspector' and collection not in ('visits','inspections') and old['author_id']!=actor['id']:
        fail('Можно изменять только собственные записи',403)
    if old and collection=='inspections':
        fail('Завершённый акт неизменяем',403)
    raw = {k:v for k,v in payload.items() if k not in ('id','version','createdBy','createdAt','updatedAt')}
    if collection=='visits' and actor['role']=='inspector':
        if raw.get('assignedTo',actor['id'])!=actor['id']:
            fail('Нельзя назначать выезд другому исполнителю',403)
        raw['assignedTo']=actor['id']
    if raw.get('dueDate')=='':
        raw['dueDate']=None
    try:
        data = MODELS[collection].model_validate(raw).model_dump(mode='json')
    except ValidationError:
        fail('Проверьте поля записи: формат, обязательные значения и допустимые числа')
    before = json.loads(old['data']) if old else None
    site = data.get('siteId')
    if collection=='inventoryLines':
        parent = get_record(con,'inventories',data['inventoryId'])
        if not parent:
            fail('Инвентаризация не найдена')
        site = parent['site_id']
        if old and before['inventoryId']!=data['inventoryId']:
            fail('Перенос строки запрещён',403)
        if actor['role']!='manager' and parent['author_id']!=actor['id']:
            fail('Можно изменять только собственную инвентаризацию',403)
        if json.loads(parent['data'])['stage']!='primary':
            fail('После проверки расхождений строки пересчёта неизменяемы',409)
    if site:
        if not get_record(con,'sites',site):
            fail('Объект не найден')
        if not accessible(con,actor,site):
            fail('Объект не назначен пользователю',403)
    if collection in ('sites','questions') and actor['role']!='manager':
        fail('Справочники доступны только руководителю',403)
    if collection=='sites':
        for row in con.execute("select id,data from records where collection='sites' and id!=?", (ident,)):
            if json.loads(row['data'])['name'].casefold()==data['name'].casefold():
                fail('Название объекта уже существует',409)
    if collection=='inspections':
        from .offline import current_checklist, checklist
        version=checklist(con,data['checklistVersion']) if data['checklistVersion'] else current_checklist(con)
        data['checklistVersion']=version['id']
        questions={question['id']:question for question in version['questions']}
        if len(data['results'])!=len(questions) or {r['questionId'] for r in data['results']}!=set(questions):
            fail('Нужно заполнить каждый пункт актуального чек-листа ровно один раз')
        for result in data['results']:
            question=questions[result['questionId']]
            result.update(title=question['title'],section=question['section'])
            if result['status']=='fail' and not result['comment']:
                fail('Опишите выявленное нарушение')
            validate_photo(con, actor, site, result['photo'])
        data['inspector']=actor['name']
    if collection=='findings':
        if data['questionId'] and not get_record(con,'questions',data['questionId']):
            fail('Контрольный пункт не найден')
        if before and any(data[k]!=before[k] for k in ('inspectionId','questionId')):
            fail('Связь с исходным актом неизменяема',403)
        if data['inspectionId']:
            parent=get_record(con,'inspections',data['inspectionId'])
            if not parent or parent['site_id']!=site or (not old and parent['author_id']!=actor['id']):
                fail('Нет доступа к исходному акту',403)
            answer=next((r for r in json.loads(parent['data'])['results'] if r['questionId']==data['questionId'] and r['status']=='fail'),None)
            if not answer:
                fail('Нарушение отсутствует в чек-листе')
        if data['status']=='closed' and (not data['resolution'] or not data['evidence']):
            fail('Для закрытия нужны описание результата и фото-подтверждение')
        data['closedAt']=(before.get('closedAt') if before and before['status']=='closed' else now()) if data['status']=='closed' else None
        data['repeat']=bool(before.get('repeat')) if before else False
        if not before and data['questionId']:
            data['repeat']=any(json.loads(r['data']).get('questionId')==data['questionId'] and
                               0 <= (date.today()-date.fromisoformat(r['created_at'][:10])).days <= 90
                               for r in con.execute("select * from records where collection='findings' and site_id=? and id!=?",(site,ident)))
        for field in ('photo','evidence'):
            validate_photo(con, actor, site, data[field])
    if collection=='inventories':
        start=before['stage'] if before else 'primary'
        transitions={'primary':{'confirmed','corrected'}, 'confirmed':{'investigation','corrected'},
                     'investigation':{'approved','corrected'}, 'approved':{'recovered'}, 'corrected':set(), 'recovered':set()}
        if data['stage']!=start:
            if not old or actor['role']!='manager':
                fail('Стадию расхождений подтверждает руководитель',403)
            if data['stage'] not in transitions[start] or not data['decision']:
                fail('Укажите основание и выберите следующую стадию')
        if before and start!='primary' and any(data[k]!=before[k] for k in ('siteId','date','title')):
            fail('Реквизиты проверенной инвентаризации неизменяемы',409)
    if collection=='visits':
        assignee=con.execute('select * from users where id=? and active=1', (data['assignedTo'],)).fetchone()
        if not assignee or assignee['role']=='viewer' or not accessible(con,dict(assignee),site):
            fail('Исполнителю не назначен объект')
        data['assignee']=assignee['name']
        if not old and data['status']!='planned':
            fail('Новый выезд должен быть запланирован',403)
        if before and data['status']!='planned':
            if any(data[k]!=before.get(k,'' if k=='goal' else None) for k in ('siteId','date','kind','assignedTo','goal')):
                fail('Отмена и выполнение не меняют реквизиты плана',403)
        if data['status']=='cancelled':
            if not data['cancellationReason']:
                fail('Укажите причину отмены выезда')
            data['cancelledBy']=actor['id'];data['cancelledAt']=now()
        elif data['cancellationReason'] or data['cancelledBy'] or data['cancelledAt']:
            fail('Данные отмены допустимы только при отмене выезда',403)
        if data['status']=='completed':
            ins=get_record(con,'inspections',data['inspectionId'])
            if not ins or ins['site_id']!=site or ins['author_id']!=data['assignedTo']:
                fail('Для выполнения нужен акт назначенного исполнителя на этом объекте')
            if con.execute("select 1 from records where collection='visits' and id!=? and json_extract(data,'$.inspectionId')=?",(ident,data['inspectionId'])).fetchone():
                fail('Этот акт уже связан с другим выездом',403)
        elif data['inspectionId']:
            fail('Акт можно связать только с выполненным выездом')
    stamp=now(); serialized=json.dumps(data,ensure_ascii=False,sort_keys=True)
    if old:
        con.execute('update records set data=?,site_id=?,version=version+1,updated_at=? where collection=? and id=?',
                    (serialized,site,stamp,collection,ident))
    else:
        con.execute('insert into records values(?,?,?,?,?,?,?,?)',
                    (collection,ident,serialized,actor['id'],site,1,stamp,stamp))


def validate_photo(con, actor, site, url):
    if not url:
        return
    if not url.startswith('/api/media/') or '/' in url[len('/api/media/'):]:
        fail('Фото должно быть загружено в закрытое хранилище')
    media=con.execute('select site_id from media where id=?', (url.split('/')[-1],)).fetchone()
    if not media or media[0]!=site or not accessible(con,actor,site):
        fail('Нет доступа к фотографии',403)
