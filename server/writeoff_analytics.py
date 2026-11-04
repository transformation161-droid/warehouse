"""Provisional, actor-scoped analytics of active decision facts; Decimal only."""
from dataclasses import dataclass,field
from datetime import date
from decimal import Decimal,InvalidOperation,localcontext
from functools import cached_property
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Literal

from fastapi import HTTPException,Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel,ConfigDict,Field,ValidationError,model_validator
from openpyxl import Workbook
from openpyxl.worksheet._writer import WorksheetWriter,ALL_TEMP_FILES
from .decision_import import FIELDS
from .decision_versions import ACTIVE
from .reports import safe_cell
from .storage import check_storage

FINANCE=('quantity','cost','residual','reimbursable','reimbursed','outstanding')
NUMBER_MAX=Decimal('1000000000000')
SNAPSHOT_ENCODE=json.JSONEncoder(ensure_ascii=False,separators=(',',':')).encode
DIMENSIONS=('toolType','reason','decisionType','responsible','investigation','caseCategory')
GROUPS={'months':'Месяцы','sites':'Объекты','reasons':'Причины','tools':'Инструменты','categories':'Категории'}
PROVISIONAL='Предварительная аналитика. Не официальная отчётность: бизнес-ключ документов и исторические пересечения требуют проверки владельцем процесса.'
DEFINITIONS={
 'documents':{'label':'Документы','unit':'док.','definition':'Число различных логических документов, имеющих хотя бы одну действующую строку в выбранном срезе. Импорты объединены только явно подтверждёнными исправлениями. Не число уникальных названий или файлов; бизнес-идентичность ещё требует проверки.'},
 'decisions':{'label':'Действующие решения','unit':'строк','definition':'Число действующих строк реестра, прошедших все фильтры. Одна строка источника считается одним результатом решения, а не отдельным юридическим актом.'},
 'quantity':{'label':'Количество по источнику','unit':'ед.','definition':'Точная сумма заполненного поля «Кол-во», без округления. Единицы неоднородны и источником не нормированы. Дробные количества требуют проверки источника, но не признаются ошибкой автоматически.'},
 'cost':{'label':'Стоимость списаний','unit':'₽','definition':'Сумма заполненного поля «Стоимость». Не автоматически установленный ущерб.'},
 'residual':{'label':'Остаточная стоимость','unit':'₽','definition':'Сумма заполненного поля «Остаточная стоимость». Не прибавляется к стоимости списаний.'},
 'reimbursable':{'label':'К возмещению','unit':'₽','definition':'Сумма заполненного назначения к возмещению в источнике. Не равна стоимости списаний.'},
 'reimbursed':{'label':'Фактически возмещено','unit':'₽','definition':'Сумма заполненных фактически возмещённых сумм. Само по себе не подтверждает полноту назначения.'},
 'outstanding':{'label':'Невозмещённый остаток','unit':'₽','definition':'Сумма max(к возмещению − фактически возмещено, 0) отдельно по каждой строке, только когда заполнены оба поля. Переплата одной строки не погашает остаток другой; при неполных полях остаток неизвестен.'}}


class Query(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True,frozen=True)
    start: date|None=None
    end: date|None=None
    site: str=Field(default='',max_length=120)
    toolType: str=Field(default='',max_length=500)
    reason: str=Field(default='',max_length=500)
    decisionType: str=Field(default='',max_length=500)
    responsible: str=Field(default='',max_length=500)
    investigation: str=Field(default='',max_length=500)
    caseCategory: str=Field(default='',max_length=500)
    toolTypeMissing: bool=False
    reasonMissing: bool=False
    decisionTypeMissing: bool=False
    responsibleMissing: bool=False
    investigationMissing: bool=False
    caseCategoryMissing: bool=False
    dateMode: Literal['all','dated','missing']='all'
    month: str=Field(default='',pattern=r'^$|^[0-9]{4}-(0[1-9]|1[0-2])$')
    documentId: str=Field(default='',max_length=80)
    metricField: Literal['','quantity','cost','residual','reimbursable','reimbursed','outstanding']=''
    coverage: Literal['all','known','missing','fractional']='all'
    chartMetric: Literal['documents','decisions','quantity','cost','residual','reimbursable','reimbursed','outstanding']='cost'
    page: int=Field(default=1,ge=1,le=1000000)
    limit: int=Field(default=50,ge=1,le=100)
    expectedSnapshot: str=Field(default='',max_length=64)

    @model_validator(mode='after')
    def consistent(self):
        if self.start and self.end and self.start>self.end:raise ValueError('Период перевёрнут')
        if self.dateMode=='missing' and (self.start or self.end or self.month):raise ValueError('Без даты несовместимо с периодом')
        if self.coverage!='all' and not self.metricField:raise ValueError('Выберите показатель детализации')
        if self.coverage=='fractional' and self.metricField!='quantity':raise ValueError('Дробность относится только к количеству')
        for dimension in DIMENSIONS:
            if getattr(self,dimension) and getattr(self,dimension+'Missing'):raise ValueError('Значение и отсутствие одновременно')
        return self

    def filters(self):return self.model_dump(mode='json',exclude={'page','limit','chartMetric','expectedSnapshot'})

    @cached_property
    def dimension_filters(self):
        return tuple((f,getattr(self,f),getattr(self,f+'Missing')) for f in DIMENSIONS if getattr(self,f) or getattr(self,f+'Missing'))


def parse(request):
    params={k:v for k,v in request.query_params.items() if v!=''}
    try:return Query.model_validate(params)
    except ValidationError:raise HTTPException(422,'Проверьте период, фильтры, показатель детализации и размер страницы')


def category(value):return str(value).strip() or None if value is not None else None


def number(value,quantity=False):
    if value is None or isinstance(value,str) and not value.strip():return None,False
    if isinstance(value,bool):return None,True
    try:
        result=Decimal(str(value))
        if not result.is_finite() or result<0 or result>NUMBER_MAX or result.as_tuple().exponent<(-6 if quantity else -4):return None,True
        return result,False
    except (ValueError,InvalidOperation):return None,True


def iter_facts(db,actor,q,full=False):
    remove=" - 'sourceCells' - 'unmappedCells'"+("" if full else " - 'comment'")
    data='(d.data::jsonb'+remove+')::text' if db.backend=='postgresql' else "json_remove(d.data,'$.sourceCells','$.unmappedCells'"+("" if full else ",'$.comment'")+")"
    # PostgreSQL's PK is NOT NULL: an uncorrelated NOT IN is equivalent to ACTIVE
    # and lets the executor hash visible retirement IDs once, including their RLS.
    active='d.id not in (select retired.record_id from decision_retirements retired)' if db.backend=='postgresql' else ACTIVE
    # Bound the dimension side before joining facts. With fresh import statistics the
    # previous grouped-head join rescanned 24k facts 720 times under RLS.
    # Every relation in this statement still uses the request actor's unchanged RLS.
    sql='''with version_metadata as materialized (
      select intro.id,intro.document_id,intro.number as introduced_version,
        max(intro.number) over (partition by intro.document_id) as current_version,
        doc.label as document_label
      from decision_versions intro join decision_documents doc on doc.id=intro.document_id
    ), entry_metadata as materialized (
      select e.record_id,e.document_id,meta.introduced_version,meta.current_version,meta.document_label
      from decision_entries e join version_metadata meta on meta.id=e.version_id
    ), visible_sites as materialized (
      select id,json_extract(data,'$.name') as site_name from records where collection='sites'
    )
    select d.id,d.site_id,d.sheet,d.source_row,d.file_sha,e.document_id,
      e.introduced_version,e.current_version,e.document_label,
      s.site_name,'''+data+''' as data
      from decision_records d join entry_metadata e on e.record_id=d.id
      join visible_sites s on s.id=d.site_id
      where '''+active+''' and (?='manager' or exists(select 1 from user_sites us where us.user_id=? and us.site_id=d.site_id))
      and (?='' or d.site_id=?) order by d.id'''
    decode=json.JSONDecoder(parse_float=Decimal).decode
    with db.connect(actor) as con:
        # Aggregation/exports consume the entire cursor, not the first 10% of it.
        # Local planner intent only; no persistent PostgreSQL or resource setting.
        if db.backend=='postgresql':con.execute('set local cursor_tuple_fraction=1')
        for raw in db.iterate(con,sql,(actor['role'],actor['id'],q.site,q.site)):
            row=dict(raw);row['data']=decode(row['data'])
            item=row['data'];row['dimensions']={k:category(item.get(k)) for k in DIMENSIONS}
            day=item.get('date');row['day']=None;row['invalidDate']=False
            if day not in (None,''):
                try:
                    if not isinstance(day,str) or len(day)!=10:raise ValueError()
                    row['day']=date.fromisoformat(day)
                    if row['day'].isoformat()!=day:raise ValueError()
                except ValueError:row['day']=None;row['invalidDate']=True
            row['numbers']={};row['invalid']=set()
            for field in FINANCE[:-1]:
                value,bad=number(item.get(field),field=='quantity');row['numbers'][field]=value
                if bad:row['invalid'].add(field)
            owed,paid=row['numbers']['reimbursable'],row['numbers']['reimbursed']
            row['numbers']['outstanding']=max(owed-paid,Decimal(0)) if owed is not None and paid is not None else None
            if {'reimbursable','reimbursed'} & row['invalid']:row['invalid'].add('outstanding')
            row['overpayment']=max(paid-owed,Decimal(0)) if owed is not None and paid is not None else None
            yield row


def matches(row,q):
    day=row['day']
    if q.documentId and row['document_id']!=q.documentId:return False
    if (q.start or q.end or q.month or q.dateMode=='dated') and day is None:return False
    if q.dateMode=='missing' and day is not None:return False
    if day and ((q.start and day<q.start) or (q.end and day>q.end) or (q.month and day.isoformat()[:7]!=q.month)):return False
    for field,expected,missing in q.dimension_filters:
        if expected and row['dimensions'][field]!=expected or missing and row['dimensions'][field] is not None:return False
    if q.metricField and q.coverage!='all':
        known=row['numbers'][q.metricField] is not None
        if q.coverage=='known' and not known or q.coverage=='missing' and known:return False
        if q.coverage=='fractional' and (not known or row['numbers']['quantity']==row['numbers']['quantity'].to_integral_value()):return False
    return True


@dataclass
class Bucket:
    rows: int=0
    docs: set=field(default_factory=set)
    sums: dict=field(default_factory=lambda:{f:Decimal(0) for f in FINANCE})
    known: dict=field(default_factory=lambda:{f:0 for f in FINANCE})
    invalid: dict=field(default_factory=lambda:{f:0 for f in FINANCE})

    def add(self,row):
        self.rows+=1;self.docs.add(row['document_id'])
        for f,v in row['numbers'].items():
            if v is not None:self.sums[f]+=v;self.known[f]+=1
            if f in row['invalid']:self.invalid[f]+=1

    def value(self,metric):
        if metric=='documents':return Decimal(len(self.docs))
        if metric=='decisions':return Decimal(self.rows)
        return self.sums[metric] if self.known[metric] else None

    def report(self):
        result={f:{'value':format(self.sums[f],'f') if self.known[f] else None,'knownRows':self.known[f],
                   'missingRows':self.rows-self.known[f],'invalidRows':self.invalid[f],'complete':self.known[f]==self.rows} for f in FINANCE}
        for f,v in (('documents',len(self.docs)),('decisions',self.rows)):
            result[f]={'value':str(v),'knownRows':self.rows,'missingRows':0,'invalidRows':0,'complete':True}
        return result


def snapshot_update(digest,row):
    digest.update(SNAPSHOT_ENCODE([row['id'],row['document_id'],row['current_version'],row['site_name']]).encode()+b'\n')


def public_row(row):
    result={k:format(v,'f') if isinstance(v,Decimal) else v for k,v in row['data'].items() if k in FIELDS and k!='comment'}
    result.update({k:format(v,'f') if v is not None else None for k,v in row['numbers'].items()})
    return {**result,'id':row['id'],'siteId':row['site_id'],'siteName':row['site_name'],'documentId':row['document_id'],
            'currentVersion':row['current_version'],'introducedVersion':row['introduced_version'],'sheet':row['sheet'],'sourceRow':row['source_row'],
            'outstanding':format(row['numbers']['outstanding'],'f') if row['numbers']['outstanding'] is not None else None,
            'missingFields':[f for f,v in row['numbers'].items() if v is None],'invalidFields':sorted(row['invalid'])}


def aggregate(db,actor,q,all_groups=False):
    start=time.monotonic();overall=Bucket();groups={g:{} for g in GROUPS};labels={g:{} for g in GROUPS};facets={f:set() for f in DIMENSIONS};facet_more=set()
    page=[];quality={'missingDateRows':0,'invalidDateRows':0,'overpaymentRows':0,'overpaymentValue':Decimal(0),'fractionalQuantityRows':0};digest=hashlib.sha256()
    digest.update(json.dumps(q.filters(),sort_keys=True,ensure_ascii=False).encode())
    with localcontext() as context:
        context.prec=50
        for scanned,row in enumerate(iter_facts(db,actor,q),1):
            if scanned%256==0 and time.monotonic()-start>120:raise HTTPException(503,'Превышено время аналитики; сузьте фильтры')
            for f,v in row['dimensions'].items():
                if v not in facets[f]:
                    if len(facets[f])<100:facets[f].add(v)
                    else:facet_more.add(f)
            if not matches(row,q):continue
            snapshot_update(digest,row);overall.add(row)
            quantity=row['numbers']['quantity']
            if quantity is not None and quantity!=quantity.to_integral_value():quality['fractionalQuantityRows']+=1
            if row['day'] is None:quality['missingDateRows']+=1
            if row['invalidDate']:quality['invalidDateRows']+=1
            if row['overpayment'] is not None and row['overpayment']>0:quality['overpaymentRows']+=1;quality['overpaymentValue']+=row['overpayment']
            if (q.page-1)*q.limit<=overall.rows-1<q.page*q.limit:page.append(public_row(row))
            keys={'months':row['day'].isoformat()[:7] if row['day'] else None,'sites':row['site_id'],
                  'reasons':row['dimensions']['reason'],'tools':row['dimensions']['toolType'],'categories':row['dimensions']['caseCategory']}
            for g,key in keys.items():
                if key not in groups[g]:groups[g][key]=Bucket()
                groups[g][key].add(row)
                labels[g][key]=row['site_name'] if g=='sites' else key or ('Без даты' if g=='months' else 'Не указано')
            if overall.rows%256==0 and time.monotonic()-start>120:raise HTTPException(503,'Превышено время аналитики; сузьте фильтры')
        charts={}
        for g,values in groups.items():
            keys=sorted(values,key=lambda k:(k is None,k or '')) if g=='months' else sorted(values,key=lambda k:(values[k].value(q.chartMetric) is None,-(values[k].value(q.chartMetric) or Decimal(0)),labels[g][k]))
            count=len(keys)
            if not all_groups:
                if g=='months' and count>24:keys=[k for k in keys if k is not None][-(23 if None in values else 24):]+([None] if None in values else [])
                elif g!='months':keys=keys[:12]
            maximum=max((values[k].value(q.chartMetric) or Decimal(0) for k in keys),default=Decimal(0))
            recovery_max=max((values[k].sums[f] for k in keys for f in ('reimbursable','reimbursed')),default=Decimal(0))
            points=[]
            for k in keys:
                v=values[k];value=v.value(q.chartMetric)
                width=lambda x,scale:format((x/scale*100).quantize(Decimal('.01')),'f') if x is not None and scale else '0'
                points.append({'key':k,'label':labels[g][k],'metrics':v.report(),'width':width(value,maximum),
                               'reimbursableWidth':width(v.value('reimbursable'),recovery_max),'reimbursedWidth':width(v.value('reimbursed'),recovery_max)})
            charts[g]={'items':points,'totalGroups':count,'truncated':len(points)<count}
    quality['overpaymentValue']=format(quality['overpaymentValue'],'f')
    return {'provisional':True,'notice':PROVISIONAL,'scope':'active','filters':q.filters(),'snapshot':digest.hexdigest(),
            'definitions':DEFINITIONS,'metrics':overall.report(),'charts':charts,'quality':quality,
            'facets':{f:{'values':sorted(v,key=lambda x:(x is None,x or '')),'truncated':f in facet_more} for f,v in facets.items()},
            'rows':page,'page':q.page,'limit':q.limit,'totalRows':overall.rows,'elapsedSeconds':round(time.monotonic()-start,3)}


def cell(value):return safe_cell(format(value,'f') if isinstance(value,Decimal) else value if value is not None else '')


def summary_rows(data):
    headers=['Срез','Категория']
    for metric,definition in DEFINITIONS.items():headers.extend([definition['label']+' ('+definition['unit']+')','Заполнено: '+metric,'Не указано: '+metric,'Некорректно: '+metric])
    headers+=['Статус','Фильтры'];yield headers
    def line(group,label,metrics):
        result=[group,label]
        for key in DEFINITIONS:
            v=metrics[key];result.extend([v['value'],v['knownRows'],v['missingRows'],v['invalidRows']])
        return result+[PROVISIONAL,json.dumps(data['filters'],ensure_ascii=False,sort_keys=True)]
    yield line('Итог','Все выбранные строки',data['metrics'])
    for group in GROUPS:
        for row in data['charts'][group]['items']:yield line(GROUPS[group],row['label'],row['metrics'])


ROW_HEADERS=list(FIELDS.values())+['ID записи','ID документа','Действующая версия документа','Версия введения строки','Лист','Строка Excel','SHA-256 файла','Невозмещённый остаток, ₽','Некорректные поля источника','Статус','Фильтры']


def export_file(db,actor,q,kind,extension,folder):
    target=Path(folder)/('analytics-'+kind+'.'+extension);start=time.monotonic();maximum=128*1024*1024
    if shutil.disk_usage(folder).free<256*1024*1024:raise HTTPException(507,'Недостаточно места для выгрузки')
    check_storage(db.path,reserve=maximum)
    def source():
        if kind=='summary':
            data=aggregate(db,actor,q,True)
            if q.expectedSnapshot and data['snapshot']!=q.expectedSnapshot:raise HTTPException(409,'Данные изменились; обновите панель перед выгрузкой')
            yield from summary_rows(data)
        else:
            digest=hashlib.sha256();digest.update(json.dumps(q.filters(),sort_keys=True,ensure_ascii=False).encode());yield ROW_HEADERS
            with localcontext() as context:
                context.prec=50
                for scanned,row in enumerate(iter_facts(db,actor,q,True),1):
                    if scanned%256==0 and time.monotonic()-start>120:raise HTTPException(503,'Время выгрузки истекло; сузьте фильтры')
                    if not matches(row,q):continue
                    snapshot_update(digest,row)
                    yield [row['data'].get(f) for f in FIELDS]+[row['id'],row['document_id'],row['current_version'],row['introduced_version'],row['sheet'],row['source_row'],row['file_sha'],row['numbers']['outstanding'],', '.join(sorted(row['invalid'])),PROVISIONAL,json.dumps(q.filters(),ensure_ascii=False,sort_keys=True)]
            if q.expectedSnapshot and digest.hexdigest()!=q.expectedSnapshot:raise HTTPException(409,'Данные изменились; обновите панель перед выгрузкой')
    if extension=='csv':
        with target.open('x',encoding='utf-8-sig',newline='') as handle:
            writer=csv.writer(handle,delimiter=';')
            for index,row in enumerate(source()):
                writer.writerow([cell(v) for v in row])
                if index%256==0:
                    if handle.tell()>maximum or time.monotonic()-start>120:raise HTTPException(503,'Выгрузка превышает лимит; сузьте фильтры')
                    check_storage(db.path,reserve=1024*1024)
    else:
        book=Workbook(write_only=True)
        def sheet_on_disk(title):
            worksheet=book.create_sheet(title);path=str(Path(folder)/('sheet-'+str(len(book.worksheets))+'.xml'))
            worksheet._writer=WorksheetWriter(worksheet,out=path);ALL_TEMP_FILES.append(path);worksheet._writer.write_top()
            return worksheet
        sheet=sheet_on_disk('Сводки' if kind=='summary' else 'Действующие строки')
        try:
            for index,row in enumerate(source()):
                sheet.append([cell(v) for v in row])
                if index%256==0:
                    if time.monotonic()-start>120:raise HTTPException(503,'Время выгрузки истекло; сузьте фильтры')
                    check_storage(db.path,reserve=1024*1024)
            meta=sheet_on_disk('Определения');meta.append(['Статус',PROVISIONAL]);meta.append(['Числа','Десятичные показатели записаны текстом точно, без float; пустая ячейка — неизвестно, не ноль.'])
            for definition in DEFINITIONS.values():meta.append([definition['label'],definition['definition']])
            meta.append(['Фильтры',json.dumps(q.filters(),ensure_ascii=False,sort_keys=True)]);book.save(target)
        finally:
            for worksheet in book.worksheets:
                writer=getattr(worksheet,'_writer',None)
                if writer:
                    try:
                        if not worksheet.closed:worksheet.close()
                        writer.cleanup()
                    except (OSError,ValueError):pass
            book.close()
    if target.stat().st_size>maximum:raise HTTPException(503,'Файл превышает лимит; сузьте фильтры')
    target.chmod(0o600);return target


def install(app,db,auth):
    gate=threading.BoundedSemaphore(1)
    @app.get('/api/writeoff-analytics')
    def dashboard(request:Request):return aggregate(db,auth(request),parse(request))

    @app.get('/api/writeoff-analytics/export/{kind}.{extension}')
    def download(kind:str,extension:str,request:Request):
        actor=auth(request);q=parse(request)
        if kind not in ('summary','rows') or extension not in ('csv','xlsx'):raise HTTPException(422,'Неизвестный формат выгрузки')
        if not gate.acquire(blocking=False):raise HTTPException(429,'Другая аналитическая выгрузка ещё формируется')
        folder=None
        try:
            root=db.path.parent/'analytics-exports';root.mkdir(mode=0o700,exist_ok=True)
            folder=tempfile.TemporaryDirectory(prefix='.private-',dir=root)
            # Keep write-only XLSX XML scratch on the private disk, not on a small /tmp tmpfs.
            # A dedicated writer tempfile binding avoids changing process-global tempfile settings.
            target=export_file(db,actor,q,kind,extension,folder.name)
            def cleanup():folder.cleanup();gate.release()
            return FileResponse(target,filename='ksu-provisional-'+kind+'.'+extension,
                media_type='text/csv; charset=utf-8' if extension=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'X-KSU-Report-Status':'provisional'},background=BackgroundTask(cleanup))
        except BaseException:
            if folder:folder.cleanup()
            gate.release();raise
