"""Explicit manager-approved corrections. Physical source records never change."""
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .database import now
from .xlsx_stream import Rejected

FINANCE=('quantity','cost','residual','reimbursable','reimbursed')
ACTIVE="not exists(select 1 from decision_retirements retired where retired.record_id=d.id)"


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def document_id(signature,site,label):return hashlib.sha256(f'{signature}/{len(site)}:{site}{len(label)}:{label}'.encode()).hexdigest()
def business_key(site,row):return canonical([site,row['document'],row.get('sourceId') or ''])
def amounts(row):return {field:Decimal(row.get(field) or '0') for field in FINANCE}
def total():return {field:Decimal(0) for field in FINANCE}
def add(target,row):
    for field,value in amounts(row).items():target[field]+=value
def encoded_total(values):return {field:format(value,'f') for field,value in values.items()}


def head(con,ident):
    row=con.execute('select v.* from decision_versions v where document_id=? order by number desc limit 1',(ident,)).fetchone()
    if not row:raise Rejected('Документ не найден или недоступен')
    return dict(row)


def initial_document(con,signature,site,row,actor,created,seen):
    ident=document_id(signature,site,row['document']);version=ident+'-1'
    if ident not in seen:
        con.execute('insert into decision_documents values(?,?,?,?,?,?)',(ident,site,row['document'],actor['id'],created,signature))
        con.execute('insert into decision_versions values(?,?,?,?,?,?,?,?,?,?,?,?)',
            (version,ident,1,None,site,actor['id'],created,'initial','Первичный импорт',signature,'{}',version))
        seen.add(ident)
    return ident,version


def check_new_collisions(db,con,stage):
    """Require an explicit correction when an existing business key is reused."""
    stage.execute('drop table if exists incoming_keys');stage.execute('create table incoming_keys(k text primary key)')
    for site,data in stage.execute('select site_id,data from rows'):
        stage.execute('insert or ignore into incoming_keys values(?)',(business_key(site,json.loads(data)),))
    for (site,) in stage.execute('select distinct site_id from rows'):
        for existing in db.iterate(con,'select d.site_id,d.data from decision_records d where d.site_id=? and '+ACTIVE,(site,)):
            row=json.loads(existing['data']);key=business_key(existing['site_id'],row)
            # Missing IDs cannot distinguish a new book from a corrected document.
            if stage.execute('select 1 from incoming_keys where k=?',(key,)).fetchone():
                raise Rejected('Совпадают объект, документ и исходный ID. Руководитель должен явно выбрать замещение и проверить сравнение версий')


def prepare(folder,db,settings):
    from .decision_import import read_meta,write_meta,actor_for,FIELDS
    folder=Path(folder);meta=read_meta(folder);actor=actor_for(db,meta['actor'])
    if actor['role']!='manager':raise Rejected('Замещение подтверждает только руководитель')
    if meta.get('rejected') or not meta.get('accepted'):raise Rejected('Сначала нужен успешный предпросмотр книги')
    ident=settings['documentId'];mode=settings['mode'];selected=set(settings['targetIds']);matches=settings['matches']
    with closing(sqlite3.connect(folder/'rows.sqlite3')) as stage,db.connect(actor) as con:
        old_head=head(con,ident)
        if old_head['number']!=settings['expectedVersion']:raise Rejected('Версия документа изменилась; повторите сравнение')
        site=old_head['site_id']
        if any(s!=site for (s,) in stage.execute('select distinct site_id from rows')):raise Rejected('Одна коррекция относится к одному выбранному объекту и документу')
        stage.executescript('drop table if exists previous; drop table if exists comparison; drop table if exists incoming_keys; '
            'create table previous(id text primary key,k text not null,data text not null,selected integer not null); '
            'create index previous_key on previous(k); '
            'create table comparison(seq integer primary key,row_no integer unique,old_id text unique,kind text not null,fields text not null); '
            'create table incoming_keys(k text not null,row_no integer primary key); create index incoming_key on incoming_keys(k);')
        before=total();scope=total();incoming=total();before_count=0
        for r in db.iterate(con,'select d.id,d.data from decision_records d join decision_entries e on e.record_id=d.id where e.document_id=? and '+ACTIVE,(ident,)):
            data=json.loads(r['data']);include=mode=='full' or r['id'] in selected
            stage.execute('insert into previous values(?,?,?,?)',(r['id'],business_key(site,data),r['data'],int(include)))
            add(before,data);before_count+=1
            if include:add(scope,data)
        if mode=='partial' and stage.execute('select count(*) from previous where selected=1').fetchone()[0]!=len(selected):raise Rejected('Выбраны устаревшие строки или строки другого документа')
        source_document=None
        for number,s,data in stage.execute('select number,site_id,data from rows order by number'):
            row=json.loads(data)
            if source_document is None:source_document=row['document']
            elif source_document!=row['document']:raise Rejected('Выберите в книге один документ и его диапазон строк; разные документы исправляются отдельными операциями')
            stage.execute('insert into incoming_keys values(?,?)',(business_key(s,row),number));add(incoming,row)
        for other in db.iterate(con,'select d.data from decision_records d join decision_entries e on e.record_id=d.id where d.site_id=? and e.document_id<>? and '+ACTIVE,(site,ident)):
            if stage.execute('select 1 from incoming_keys where k=? limit 1',(business_key(site,json.loads(other['data'])),)).fetchone():
                raise Rejected('Ключ строки уже действует в другом документе; выберите соответствующий документ или ограничьте диапазон книги')
        counts={'added':0,'changed':0,'removed':0,'unchanged':0};conflicts=[]
        used=set()
        for number,s,encoded in stage.execute('select number,site_id,data from rows order by number'):
            row=json.loads(encoded);key=business_key(s,row);explicit=str(number) in matches
            if stage.execute('select 1 from previous where k=? and selected=0',(key,)).fetchone():
                raise Rejected('В частичной коррекции ID совпал с невыбранной действующей строкой; включите её в набор замещения')
            old=None
            if explicit:
                oldid=matches[str(number)]
                if oldid:
                    old=stage.execute('select id,data from previous where id=? and selected=1',(oldid,)).fetchone()
                    if not old:raise Rejected('Ручное сопоставление ссылается на строку вне выбранного набора')
            else:
                candidates=stage.execute('select id,data from previous where k=? and selected=1 limit 2',(key,)).fetchall()
                duplicate=stage.execute('select count(*) from incoming_keys where k=?',(key,)).fetchone()[0]>1
                if not row.get('sourceId') or duplicate or len(candidates)>1:
                    if len(conflicts)<100:conflicts.append({'row':number,'reason':'Пустой или неоднозначный исходный ID: сопоставьте вручную либо явно отметьте добавление'})
                    continue
                if candidates:old=candidates[0]
            if old and old[0] in used:raise Rejected('Одну прежнюю строку нельзя сопоставить с двумя новыми')
            fields=[]
            if old:
                used.add(old[0]);prior=json.loads(old[1])
                fields=[f for f in (*FIELDS,'reportPeriod') if (Decimal(row[f])!=Decimal(prior[f]) if f in FINANCE and row.get(f) not in ('',None) and prior.get(f) not in ('',None) else row.get(f)!=prior.get(f))]
            kind='added' if old is None else 'changed' if fields else 'unchanged';counts[kind]+=1
            stage.execute('insert into comparison(row_no,old_id,kind,fields) values(?,?,?,?)',(number,old[0] if old else None,kind,canonical(fields)))
        for oldid, in stage.execute('select id from previous where selected=1 and id not in (select old_id from comparison where old_id is not null)'):
            stage.execute("insert into comparison(old_id,kind,fields) values(?,'removed','[]')",(oldid,));counts['removed']+=1
        known_rows={str(n) for n, in stage.execute('select number from rows')}
        if not set(matches)<=known_rows:raise Rejected('Ручное сопоставление относится к строкам вне предпросмотра')
        if head(con,ident)['id']!=old_head['id']:raise Rejected('Версия документа изменилась во время сравнения; повторите его')
        after={field:before[field]-scope[field]+incoming[field] for field in FINANCE}
        summary={'counts':counts,'before':encoded_total(before),'after':encoded_total(after),'selectedBefore':encoded_total(scope),
                 'incoming':encoded_total(incoming),'beforeRows':before_count,'afterRows':before_count-stage.execute('select count(*) from previous where selected=1').fetchone()[0]+meta['accepted'],
                 'reason':settings['reason'],'mode':mode,'documentId':ident,'version':old_head['number']+1}
        plan={'settings':settings,'parentId':old_head['id'],'siteId':site,'summary':summary,'conflicts':conflicts,'ready':not conflicts}
        plan['token']=digest({'settings':settings,'parent':old_head['id'],'signature':meta['signature'],'summary':summary})
        stage.commit();meta.update(versionPlan=plan,status='ready',confirmedVersionToken=None);write_meta(folder,meta)


def comparison(folder,page=1,limit=50):
    from .decision_import import read_meta,FIELDS
    meta=read_meta(folder)
    if not meta.get('versionPlan'):raise Rejected('Сначала подготовьте сравнение')
    with closing(sqlite3.connect(Path(folder)/'rows.sqlite3')) as con:
        rows=con.execute('select c.row_no,c.old_id,c.kind,c.fields,p.data,r.data from comparison c left join previous p on p.id=c.old_id left join rows r on r.number=c.row_no order by c.seq limit ? offset ?',(limit,(page-1)*limit)).fetchall()
        result=[]
        for n,oldid,kind,fields,before,after in rows:
            result.append({'row':n,'oldId':oldid,'kind':kind,'fields':json.loads(fields),
                'before':{f:json.loads(before).get(f) for f in FIELDS} if before else None,
                'after':{f:json.loads(after).get(f) for f in FIELDS} if after else None})
        return {'page':page,'total':con.execute('select count(*) from comparison').fetchone()[0],'rows':result}


def assert_plan(con,meta,actor):
    plan=meta['versionPlan'];settings=plan['settings']
    if actor['role']!='manager' or not plan['ready'] or meta.get('confirmedVersionToken')!=plan['token']:raise Rejected('Нужно явное подтверждение руководителем актуального сравнения')
    existing=con.execute('select id from decision_versions where operation_key=?',(plan['token'],)).fetchone()
    if existing:return True
    if head(con,settings['documentId'])['id']!=plan['parentId']:raise Rejected('Версия документа изменилась после предпросмотра; повторите сравнение')
    return False


def begin_version(con,meta,actor,created,stage):
    plan=meta['versionPlan'];settings=plan['settings'];ident=plan['token']
    con.execute('insert into decision_versions values(?,?,?,?,?,?,?,?,?,?,?,?)',
        (ident,settings['documentId'],settings['expectedVersion']+1,plan['parentId'],plan['siteId'],actor['id'],created,
         settings['mode'],settings['reason'],meta['signature'],canonical(plan['summary']),plan['token']))
    for oldid, in stage.execute('select id from previous where selected=1'):
        con.execute('insert into decision_retirements values(?,?,?)',(oldid,ident,plan['siteId']))
    return settings['documentId'],ident
