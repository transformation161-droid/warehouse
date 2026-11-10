import base64
import csv
import io
import json
import os
import zipfile
from datetime import datetime
from uuid import uuid4
from openpyxl import load_workbook
from .database import digest, now
from .models import InventoryLine
from .service import fail, save_record

REQUIRED=['object','date','item','expected','actual','price']
OPTIONAL=['title','sku','category','unit','owner','reason']


def parse_import(db, actor, payload):
    try:raw=base64.b64decode(payload['content'],validate=True)
    except (ValueError,KeyError):fail('Не удалось прочитать файл')
    if len(raw)>3_000_000:fail('Файл больше 3 МБ')
    if payload.get('name','').lower().endswith('.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist())>20_000_000:fail('Слишком большая распакованная книга')
            book=load_workbook(io.BytesIO(raw),read_only=True,data_only=False,keep_links=False)
            sheet=book.active; rows=[]
            for row in sheet.iter_rows():
                if len(rows)>2000:fail('Не больше 2000 строк за один импорт')
                if any(c.data_type=='f' for c in row):fail('Замените формулы значениями перед импортом')
                rows.append([c.value.isoformat()[:10] if isinstance(c.value,datetime) else str(c.value or '') if c.value!=0 else '0' for c in row])
            book.close()
        except HTTPException:raise
        except Exception:fail('Повреждённая или неподдерживаемая книга XLSX')
    else:
        try:
            text=raw.decode('utf-8-sig'); dialect=csv.Sniffer().sniff(text[:4096],delimiters=';,\t')
            rows=list(csv.reader(io.StringIO(text),dialect=dialect,strict=True))
        except (UnicodeError,csv.Error):fail('CSV должен быть в UTF-8, с разделителем ; , или табуляцией')
    rows=[r for r in rows if any(str(v).strip() for v in r)]
    if not rows or len(rows)>2001:fail('В файле должно быть от 1 до 2000 строк данных')
    headers=[str(v).strip() for v in rows.pop(0)]
    if len(headers)>40 or len(set(headers))!=len(headers):fail('Нужны уникальные заголовки, не более 40 колонок')
    mapping=payload.get('mapping') or {k:k for k in REQUIRED+OPTIONAL if k in headers}
    missing=[k for k in REQUIRED if mapping.get(k) not in headers]
    if missing:return {'headers':headers,'missing':missing,'rows':[],'errors':[],'hash':digest(raw.hex()),'count':len(rows)},[]
    with db.connect(actor) as con:state=db.snapshot(con,actor)
    sites={s['name'].casefold():s['id'] for s in state['sites']}
    valid=[];errors=[];preview=[]
    for index,row in enumerate(rows,2):
        try:
            obj={k:row[headers.index(v)].strip() for k,v in mapping.items() if k in REQUIRED+OPTIONAL and v in headers}
            site=sites.get(obj['object'].casefold())
            if not site:raise ValueError('Объект не найден или недоступен')
            record={k:obj[k] for k in OPTIONAL+['item'] if k in obj and k!='title'}
            record['inventoryId']='preview'
            for field in ('expected','actual','price'):record[field]=float(obj[field].replace(' ','').replace(',','.'))
            line=InventoryLine.model_validate(record).model_dump()
            from datetime import date
            day=date.fromisoformat(obj['date']).isoformat()
            valid.append({'siteId':site,'date':day,'title':obj.get('title') or ('Импорт инвентаризации' if os.getenv('KSU_OFFLINE_MODE') is not None else 'ДЕМО · Импорт инвентаризации'),'line':line})
            preview.append({'row':index,'object':obj['object'],'date':day,'item':line['item'],'expected':line['expected'],'actual':line['actual'],'price':line['price']})
        except Exception as exc:
            errors.append({'row':index,'message':str(exc)[:180]})
    return {'headers':headers,'missing':[],'rows':preview[:20],'errors':errors[:100],'hash':digest(raw.hex()),'count':len(valid)},valid


def commit_import(db, actor, payload):
    preview, rows=parse_import(db,actor,payload)
    if preview['missing'] or preview['errors'] or not rows:fail('Исправьте все ошибки и сопоставление колонок до импорта')
    with db.connect(actor) as con:
        con.execute('begin immediate')
        if db.import_exists(con,preview['hash']):
            return {'duplicate':True,'state':db.snapshot(con,actor)}
        if db.backend=='postgresql':db.check_capacity(con,len(payload['content'])*5+len(rows)*16384+1024*1024)
        groups={}
        for item in rows:
            key=(item['siteId'],item['date'],item['title'])
            if key not in groups:
                ident=uuid4().hex;groups[key]=ident
                save_record(con,actor,'inventories',ident,{'siteId':key[0],'date':key[1],'title':key[2],'plannedPositions':sum((r['siteId'],r['date'],r['title'])==key for r in rows)},0)
            line=item['line'];line['inventoryId']=groups[key]
            save_record(con,actor,'inventoryLines',uuid4().hex,line,0)
        con.execute('insert into imports values(?,?,?,?)',(preview['hash'],actor['id'],json.dumps({'file':payload['content'],'name':payload['name'],'mapping':payload.get('mapping'),'normalized':rows},ensure_ascii=False),now()))
        return {'duplicate':False,'state':db.snapshot(con,actor)}


from fastapi import HTTPException
