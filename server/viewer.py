"""Read-only shared UI model. Explicit business fields; never users, sources or audit."""
import hashlib
import json
import math
import os
import re
from fastapi import HTTPException
from .service import fail

# Operational text/photos are intentionally readable within assigned sites. Structured
# personal fields (owner/inspector/assignee/storekeeper/pm/authors) never cross this boundary.
FIELDS={
 'sites':dict.fromkeys(('name','code'),str),
 'questions':dict.fromkeys(('section','title'),str),
 'visits':dict.fromkeys(('siteId','date','kind','status','goal','inspectionId','cancellationReason'),str),
 'inspections':{'siteId':str,'date':str,'results':list},
 'findings':{**dict.fromkeys(('siteId','inspectionId','questionId','section','title','description','severity','dueDate','status','resolution','photo','evidence','closedAt'),str),'repeat':bool},
 'inventories':{**dict.fromkeys(('siteId','date','title','status','stage','decision'),str),'plannedPositions':float},
 'inventoryLines':{**dict.fromkeys(('inventoryId','item','sku','category','unit','reason'),str),**dict.fromkeys(('expected','actual','price'),float)},
}
RESULT_FIELDS=dict.fromkeys(('questionId','title','section','status','comment','photo'),str)
READ_PATHS={'/api/me','/api/state','/api/offline-visit/context'}
MEDIA_ID=r'[a-zA-Z0-9_-]{1,100}'


def authorize(actor,path,method):
    if actor['role']!='viewer':return
    if os.getenv('KSU_VIEWER_ACCESS','enabled')=='off' and path!='/api/logout':
        raise HTTPException(403,'Доступ «Только просмотр» временно отключён',headers={'X-KSU-Viewer-Disabled':'1'})
    allowed=path in READ_PATHS or re.fullmatch('/api/media/'+MEDIA_ID,path) or re.fullmatch(r'/api/reports/(json|csv|xlsx|pdf)',path)
    if not (path=='/api/logout' and method=='POST') and not (method in ('GET','HEAD') and allowed):
        fail('Только просмотр: изменения, импорт, история, резервные копии и офлайн-доступ запрещены',403)


def typed(data,fields):
    result={}
    for key,kind in fields.items():
        value=data.get(key)
        if kind is str and isinstance(value,str):result[key]=value
        elif kind is bool and isinstance(value,bool):result[key]=value
        elif kind is float and isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):result[key]=value
        elif kind is list and isinstance(value,list):result[key]=[typed(row,RESULT_FIELDS) for row in value if isinstance(row,dict)]
    return result


def project(collection,data):
    return typed(data,FIELDS.get(collection,{}))


def media_links_sql(postgres=False):
    # A site ACL alone must not expose orphaned media or a foreign-site URL injected
    # into a permitted record. The attachment's own site and a visible parent must match.
    if postgres:
        photo="r.data::jsonb->>'photo'";evidence="r.data::jsonb->>'evidence'"
        photos="select 1 from jsonb_array_elements(case when jsonb_typeof(r.data::jsonb->'results')='array' then r.data::jsonb->'results' else '[]'::jsonb end) x where x->>'photo'='/api/media/'||m.id"
        actor='actor_id()'
    else:
        photo="json_extract(r.data,'$.photo')";evidence="json_extract(r.data,'$.evidence')"
        photos="select 1 from json_each(case when json_type(r.data,'$.results')='array' then json_extract(r.data,'$.results') else '[]' end) x where case when x.type='object' then json_extract(x.value,'$.photo') else null end='/api/media/'||m.id"
        actor='?'
    parent_site="r.data::jsonb->>'siteId'" if postgres else "json_extract(r.data,'$.siteId')"
    return f"""select m.id from media m where exists(select 1 from user_sites u where u.user_id={actor} and u.site_id=m.site_id)
      and exists(select 1 from records r where r.site_id=m.site_id and {parent_site}=m.site_id and
       ((r.collection='findings' and ({photo}='/api/media/'||m.id or {evidence}='/api/media/'||m.id))
        or (r.collection='inspections' and exists({photos}))))"""


def media_ids(db,con,actor):
    query='select id from viewer_media_links()' if db.backend=='postgresql' else media_links_sql()
    return {row['id'] for row in con.execute(query,() if db.backend=='postgresql' else (actor['id'],))}


def read_media(db,con,actor,ident,thumbnail=False):
    if db.backend=='postgresql':
        row=con.execute('select content from viewer_media(?,?)',(ident,thumbnail)).fetchone()
    else:
        row=con.execute('select '+('thumbnail' if thumbnail else 'content')+' as content from media where id=? and id in ('+media_links_sql()+')',(ident,actor['id'])).fetchone()
    if not row:fail('Фото недоступно',404)
    return bytes(row['content'])


def snapshot(db,con,actor):
    state={key:[] for key in FIELDS}
    if db.backend=='postgresql':rows=con.execute('select * from viewer_records()').fetchall()
    else:
        rows=con.execute("""select r.collection,r.id,r.version,r.data,r.created_at from records r where
          (r.collection='questions' and exists(select 1 from user_sites u join records s on s.collection='sites' and s.id=u.site_id where u.user_id=?))
          or (r.collection<>'questions' and exists(select 1 from user_sites u where u.user_id=? and u.site_id=case when r.collection='sites' then r.id else r.site_id end))
          order by r.collection,r.id""",(actor['id'],actor['id'])).fetchall()
    allowed_media=media_ids(db,con,actor)
    for row in rows:
        collection=row['collection']
        if collection not in FIELDS:continue
        raw=json.loads(row['data']);data=project(collection,raw)
        created=row.get('created_at') if isinstance(row,dict) else row['created_at']
        data.update(id=row['id'],version=row['version'],createdAt=created if created is not None else raw.get('createdAt',''))
        for target in [data,*data.get('results',[])]:
            for key in ('photo','evidence'):
                url=target.get(key,'');match=re.fullmatch('/api/media/('+MEDIA_ID+')',url)
                if key in target and (not match or match[1] not in allowed_media):target[key]=''
        if collection=='inspections':data.setdefault('results',[]);data['inspector']=''
        state[collection].append(data)
    # Reject dangling/cross-site parent links as well as a forged siteId in payload JSON.
    sites={r['id'] for r in state['sites']}
    for key in ('visits','inspections','findings','inventories'):
        state[key]=[r for r in state[key] if r.get('siteId') in sites]
    parents={r['id'] for r in state['inventories']}
    state['inventoryLines']=[r for r in state['inventoryLines'] if r.get('inventoryId') in parents]
    for collection,prefix in (('sites','s'),('questions','q')):
        state[collection].sort(key=lambda r:(0,int(r['id'][1:])) if r['id'].startswith(prefix) and r['id'][1:].isdigit() else (1,r['id']))
    state.update(assignees=[],activities=[],version=2,meta={'mode':'work','name':'ИНСТРАКТ | Складской учет','access':'read-only'})
    state['revision']=int(hashlib.sha256(json.dumps(state,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12],16)
    return state
