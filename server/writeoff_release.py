"""Small release-only additions using existing ACL and append-only v4 versions."""
import hashlib
import json
import os
from uuid import uuid4
from fastapi import HTTPException,Request
from .database import now
from .decision_versions import ACTIVE,head,canonical
from .models import InventoryDateOverride


def enabled():return os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off')=='on'


def install(app,db,auth):
    def manager(request,write=False):
        actor=auth(request,write)
        if not enabled():raise HTTPException(404,'Раздел аналитики списаний пока не включён')
        if actor['role']!='manager':raise HTTPException(403,'Действие доступно только руководителю')
        return actor

    @app.get('/api/writeoff-analytics/import-options')
    def options(request:Request):
        actor=manager(request);aliases={}
        with db.connect(actor) as con:
            sites=[{'id':r['id'],'name':json.loads(r['data'])['name']} for r in con.execute("select id,data from records where collection='sites'")]
            ids={s['id'] for s in sites}
            for row in con.execute('select raw_json from imports order by created_at desc limit 100'):
                for name,ident in json.loads(row['raw_json']).get('settings',{}).get('siteAliases',{}).items():
                    if ident in ids:aliases.setdefault(name,ident)
        return {'sites':sites,'suggestedAliases':aliases,'confirmationRequired':True}

    @app.post('/api/writeoff-analytics/documents/{ident}/inventory-date')
    def event_date(ident:str,body:InventoryDateOverride,request:Request):
        actor=manager(request,True)
        if (body.mode=='date')!=(body.date is not None):raise HTTPException(422,'Дата нужна только для режима уточнения даты')
        payload=body.model_dump(mode='json');encoded=canonical(payload)
        operation=hashlib.sha256(('inventory-date/'+actor['id']+'/'+ident+'/'+body.mutationId).encode()).hexdigest()
        with db.connect(actor) as con:
            con.execute('begin immediate')
            doc=con.execute('select * from decision_documents where id=?',(ident,)).fetchone()
            if not doc:raise HTTPException(404,'Документ не найден или недоступен')
            duplicate=con.execute('select number,summary_json from decision_versions where operation_key=?',(operation,)).fetchone()
            if duplicate:
                if json.loads(duplicate['summary_json']).get('request')!=payload:raise HTTPException(409,'Идентификатор операции уже использован с другими данными')
                return {'version':duplicate['number'],'duplicate':True}
            old=head(con,ident)
            if old['number']!=body.expectedVersion:raise HTTPException(409,'Версия документа изменилась; обновите данные перед уточнением')
            row=con.execute('select d.data from decision_records d join decision_entries e on e.record_id=d.id where e.document_id=? and '+ACTIVE+' limit 1',(ident,)).fetchone()
            if not row:raise HTTPException(422,'Документ не содержит действующих строк')
            stamp=now();signature=operation
            con.execute('insert into imports values(?,?,?,?)',(signature,actor['id'],canonical({'kind':'inventory-date-override','request':payload}),stamp))
            event={'mode':body.mode,'date':body.date.isoformat() if body.date else None,'reason':body.reason}
            summary=canonical({'kind':'inventory-date-override','request':payload,'inventoryEventOverride':event})
            con.execute('insert into decision_versions values(?,?,?,?,?,?,?,?,?,?,?,?)',
                (uuid4().hex,ident,old['number']+1,old['id'],doc['site_id'],actor['id'],stamp,'partial',body.reason,signature,summary,operation))
        return {'version':old['number']+1,'duplicate':False,'inventoryEventOverride':event}
