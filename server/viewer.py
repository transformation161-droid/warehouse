"""Explicit read projection: no attachments, source facts, personal fields or exports."""
import hashlib
import json
import os
from fastapi import HTTPException
from .service import fail

FIELDS={
 'sites':('name','status','type'),
 'visits':('siteId','date','kind','status'),
 'inspections':('siteId','date','status'),
 'findings':('siteId','title','status','severity','date','dueDate'),
}
READ_PATHS={'/api/me','/api/state','/api/offline-visit/context'}


def authorize(actor,path,method):
    if actor['role']=='viewer' and os.getenv('KSU_VIEWER_ACCESS','enabled')=='off' and path!='/api/logout':
        raise HTTPException(403,'Доступ «Только просмотр» временно отключён',headers={'X-KSU-Viewer-Disabled':'1'})
    if actor['role']=='viewer' and not (path=='/api/logout' and method=='POST') and not (method in ('GET','HEAD') and path in READ_PATHS):
        fail('Только просмотр: изменения, вложения, история, выгрузки и офлайн-доступ запрещены',403)


def project(collection,data):
    return {key:data[key] for key in FIELDS.get(collection,()) if isinstance(data.get(key),str)}


def snapshot(db,con,actor):
    state={key:[] for key in ('sites','questions','visits','inspections','findings','inventories','inventoryLines')}
    if db.backend=='postgresql':rows=con.execute('select * from viewer_records()').fetchall()
    else:
        cases=[]
        for collection,fields in FIELDS.items():
            pairs=','.join("'"+key+"',case when json_type(r.data,'$."+key+"')='text' then json_extract(r.data,'$."+key+"') else null end" for key in fields)
            cases.append("when '"+collection+"' then json_object("+pairs+")")
        rows=con.execute("select r.collection,r.id,r.version,case r.collection "+' '.join(cases)+""" end as data from records r where r.collection in ('sites','visits','inspections','findings')
          and exists(select 1 from user_sites us where us.user_id=? and us.site_id=case when r.collection='sites' then r.id else r.site_id end)
          order by r.collection,r.id""",(actor['id'],)).fetchall()
    for row in rows:
        state[row['collection']].append({'id':row['id'],'version':row['version'],**project(row['collection'],json.loads(row['data']))})
    state.update(assignees=[],activities=[],version=2,meta={'mode':'work','name':'ИНСТРАКТ | Складской учет','access':'read-only'})
    state['revision']=int(hashlib.sha256(json.dumps(state,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12],16)
    return state
