"""Server-only offline contracts. No device queue, service worker or UI integration."""
from datetime import datetime, timezone
import json
import secrets
import time

from .database import digest, now


def fail(message,code=422):
    from .service import fail as error
    error(message,code)


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def epoch():return time.time()


def current_checklist(con):
    questions=[{'id':r['id'],'version':r['version'],**json.loads(r['data'])}
               for r in con.execute("select id,version,data from records where collection='questions' order by id")]
    if not questions or len(questions)>200:fail('Чек-лист должен содержать от 1 до 200 пунктов')
    data=canonical(questions);ident=digest(data)
    con.execute('insert into checklist_versions values(?,?,?) on conflict(id) do nothing',(ident,data,now()))
    return {'id':ident,'questions':questions}


def checklist(con,ident):
    row=con.execute('select data from checklist_versions where id=?',(ident,)).fetchone()
    if not row:fail('Версия чек-листа не найдена',404)
    if digest(row[0])!=ident:fail('Нарушена целостность версии чек-листа',503)
    return {'id':ident,'questions':json.loads(row[0])}


def receipt(con,actor,ident,kind=None,body_hash=None):
    from .service import accessible
    row=con.execute('select * from operation_receipts where user_id=? and id=?',(actor['id'],ident)).fetchone()
    if not row:return None
    if kind is not None and (row['kind']!=kind or row['body_hash']!=body_hash):
        fail('Идентификатор операции уже использован с другим содержимым',409)
    # Even a saved success is not permission to disclose a revoked object's receipt.
    for resource in json.loads(row['resources']):
        if resource.get('siteId') and not accessible(con,actor,resource['siteId']):fail('Подтверждение недоступно',404)
        if resource.get('collection')=='visits' and actor['role']!='manager':
            visit=con.execute("select data from records where collection='visits' and id=?",(resource['id'],)).fetchone()
            if not visit or json.loads(visit[0])['assignedTo']!=actor['id']:fail('Подтверждение недоступно',404)
    return json.loads(row['result'])


def store_receipt(con,actor,ident,kind,body_hash,status,result,resources):
    value={'mutationId':ident,'kind':kind,'status':status,'committed':status==200,'recordedAt':now(),'records':resources,**result}
    con.execute('insert into operation_receipts values(?,?,?,?,?,?,?,?)',
                (actor['id'],ident,kind,body_hash,status,canonical(value),canonical(resources),value['recordedAt']))
    return value


def package(con,actor,body,db):
    from .service import accessible, get_record
    for site in body.siteIds:
        if not accessible(con,actor,site) or not get_record(con,'sites',site):fail('Объект не назначен пользователю',403)
    if con.execute('select count(*) from offline_packages where user_id=? and expires_at>?',
                   (actor['id'],epoch())).fetchone()[0]>=20:fail('Слишком много действующих пакетов',429)
    version=current_checklist(con);sites=[];visits=[];findings=[]
    # Query bounded collections only, not snapshot() or the decisions ledger.
    for site in body.siteIds:
        record=get_record(con,'sites',site)
        sites.append({'id':site,'version':record['version'],**json.loads(record['data'])})
        for collection,target,limit in [('visits',visits,50),('findings',findings,50)]:
            condition="json_extract(data,'$.status')='planned' and json_extract(data,'$.assignedTo')=?" if collection=='visits' else "author_id=? and json_extract(data,'$.status')!='closed'"
            for row in con.execute('select * from records where collection=? and site_id=? and '+condition+' order by id limit 51',(collection,site,actor['id'])):
                data=json.loads(row['data'])
                if collection=='visits' and (data['status']!='planned' or data['assignedTo']!=actor['id']):continue
                if collection=='findings' and (row['author_id']!=actor['id'] or data['status']=='closed'):continue
                target.append({'id':row['id'],'version':row['version'],**data})
                if len(target)>limit:fail('Слишком много записей для одного офлайн-пакета',413)
    expires=epoch()+body.ttlHours*3600
    value={'format':'ksu-offline-package-v1','id':secrets.token_hex(24),'userId':actor['id'],
        'createdAt':now(),'expiresAt':datetime.fromtimestamp(expires,timezone.utc).isoformat(),
        'checklist':version,'sites':sites,'visits':visits,'findings':findings,'mediaIncluded':False}
    data=canonical(value)
    if len(data.encode())>2*1024*1024:fail('Офлайн-пакет превышает 2 МиБ',413)
    if db.backend=='postgresql':db.check_capacity(con,len(data.encode())*2+65536)
    con.execute('insert into offline_packages values(?,?,?,?,?)',(value['id'],actor['id'],expires,data,value['createdAt']))
    return value


def validate_package(con,actor,ident,batch):
    from .service import accessible
    row=con.execute('select * from offline_packages where id=? and user_id=?',(ident,actor['id'])).fetchone()
    if not row:fail('Офлайн-пакет не найден',404)
    if row['expires_at']<=epoch():fail('Офлайн-пакет истёк; подготовьте новый пакет',409)
    value=json.loads(row['data']);sites={site['id'] for site in value['sites']}
    allowed={'visits','inspections','findings'}
    for change in batch.changes:
        if change.collection not in allowed or change.data.get('siteId') not in sites:fail('Запись вне офлайн-пакета',403)
        if not accessible(con,actor,change.data['siteId']):fail('Объект больше не назначен пользователю',403)
        if change.expectedVersion:
            pinned=next((r for r in value[change.collection] if r['id']==change.id),None) if change.collection in ('visits','findings') else None
            if not pinned or pinned['version']!=change.expectedVersion:fail('Версия записи отсутствует в пакете',409)
        if change.collection=='inspections' and change.data.get('checklistVersion')!=value['checklist']['id']:
            fail('Акт должен использовать версию чек-листа из пакета',409)
