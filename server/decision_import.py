"""Bounded, disk-staged import of ready write-off decisions. No inventory fabrication."""
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import psycopg
import subprocess
import sys
import threading
import time
import zipfile
from xml.etree.ElementTree import ParseError
from defusedxml.common import DefusedXmlException
from uuid import uuid4

from openpyxl.utils.datetime import from_excel, CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
from .database import Database, now
from .service import accessible
from .storage import check_storage, StorageFull
from .xlsx_stream import Reader, Rejected, CellError, MAX_FILE

FIELDS = {
 'sourceId':'ID строки','date':'Дата акта','site':'Объект','document':'Документ',
 'decisionType':'Тип решения','toolType':'Тип инструмента','quantity':'Кол-во',
 'cost':'Стоимость, ₽','residual':'Остаточная стоимость, ₽','reason':'Причина списания',
 'caseCategory':'Категория случая','investigation':'Статус расследования','decision':'Результат / решение',
 'reimbursable':'К возмещению, ₽','reimbursed':'Фактически возмещено, ₽',
 'responsible':'Ответственное лицо / МОЛ','comment':'Комментарий'}
REQUIRED = {'date','site','document','decisionType','toolType','quantity','cost'}
MONEY = {'cost','residual','reimbursable','reimbursed'}
MAX_NORMALIZED = 64*1024*1024
TTL = 3600
ID = re.compile(r'^[0-9a-f]{32}$')


def read_meta(folder):
    return json.loads((Path(folder)/'meta.json').read_text(encoding='utf-8'))


def write_meta(folder, data):
    target=Path(folder)/'meta.json';partial=target.with_suffix('.partial')
    partial.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');partial.chmod(0o600);partial.replace(target)


def actor_for(db, ident):
    with db.connect() as con:
        row=con.execute('select * from users where id=? and active=1',(ident,)).fetchone()
    if not row:raise Rejected('Учётная запись больше не активна')
    return dict(row)


def sites_for(db, actor):
    with db.connect(actor) as con:
        return {json.loads(row['data'])['name'].casefold():row['id'] for row in
                con.execute("select id,data from records where collection='sites'")
                if accessible(con,actor,row['id'])}


def inspect(folder):
    meta=read_meta(folder)
    with Reader(Path(folder)/'source.xlsx',Path(folder)/('inspect-'+uuid4().hex)) as reader:
        meta.update(status='uploaded',sheets=[{'index':i,'name':s[0]} for i,s in enumerate(reader.sheets)],
                    limits={'fileBytes':MAX_FILE,'unpackedBytes':256_000_000,'rows':100000,'columns':128,
                            'cells':2000000,'normalizedBytes':MAX_NORMALIZED})
    write_meta(folder,meta)


def normalize(number, cells, mapping, sites, date1904):
    def raw(field):return cells.get(mapping.get(field,''),{}).get('value')
    values={field:raw(field) for field in mapping}
    # Formatted table tails containing only row IDs/empty zero cells are not decisions.
    business={k:v for k,v in values.items() if k!='sourceId'}
    if not any(v not in (None,'','0','0.0') for v in business.values()):return None,[]
    result={};errors=[];provenance={}
    def error(field,reason,fix):
        errors.append({'row':number,'column':mapping.get(field,''),'field':FIELDS[field],
                       'reason':reason,'fix':fix})
    for field in FIELDS:
        value=raw(field)
        cell=cells.get(mapping.get(field,''),{})
        if value is None or str(value).strip()=='':
            result[field]=None
            if field in REQUIRED:error(field,'Обязательное значение отсутствует','Заполните ячейку или исправьте сопоставление колонки')
            continue
        value=str(value).strip()
        try:
            if field=='site':
                if value.casefold() not in sites:raise ValueError('Объект не найден или не назначен пользователю')
                result['siteId']=sites[value.casefold()];result[field]=value
            elif field=='date':
                if cell.get('type','n')=='n':
                    converted=from_excel(float(value),CALENDAR_MAC_1904 if date1904 else CALENDAR_WINDOWS_1900)
                    if not isinstance(converted,(date,datetime)):raise ValueError('Некорректная дата Excel')
                    result[field]=converted.date().isoformat() if isinstance(converted,datetime) else converted.isoformat()
                else:
                    try:result[field]=date.fromisoformat(value).isoformat()
                    except ValueError:result[field]=datetime.fromisoformat(value).date().isoformat()
            elif field in MONEY or field=='quantity':
                if len(value)>40 or cell.get('type')=='b':raise ValueError('Некорректное число')
                amount=Decimal(value.replace('\u00a0','').replace(' ','').replace(',','.'))
                scale=6 if field=='quantity' else 4
                if field in MONEY and cell.get('type')=='n' and amount.is_finite() and 0<=amount<=Decimal('1000000000000') and amount.as_tuple().exponent < -4:
                    rounded=amount.quantize(Decimal('.0001'))
                    if abs(amount-rounded)<=Decimal('0.00000001'):
                        amount=rounded;result.setdefault('excelRoundingFields',[]).append(field)
                if not amount.is_finite() or amount<0 or abs(amount)>Decimal('1000000000000') or amount.as_tuple().exponent < -scale:
                    raise ValueError('Число выходит за безопасные пределы точности или размера')
                result[field]=format(amount,'f')
            else:
                maximum=4000 if field in ('comment','decision') else 500
                if len(value)>maximum:raise ValueError('Слишком длинный текст')
                if field in ('sourceId','document') and cell.get('type','n')=='n':
                    code=cell.get('numberFormat','General')
                    if re.fullmatch('0{1,64}',code):
                        integer=Decimal(value)
                        if not integer.is_finite() or integer!=integer.to_integral_value() or abs(integer)>Decimal('1000000000000'):raise ValueError('Идентификатор должен быть точным целым числом или текстом')
                        value=str(int(integer)).zfill(len(code))
                    elif code!='General':raise ValueError('Сложный числовой формат идентификатора: задайте текст')
                result[field]=value
        except (ValueError,InvalidOperation,OverflowError):
            if field=='site':reason,fix='Объект не найден или недоступен','Создайте или назначьте объект и исправьте его название в источнике'
            elif field=='date':reason,fix='Некорректная дата','Используйте дату Excel или ГГГГ-ММ-ДД'
            elif field in MONEY or field=='quantity':reason,fix='Некорректное число или точность','Укажите неотрицательное число до 10^12 без валюты; количество ≥0, до 6 знаков после запятой, деньги — до 4'
            else:reason,fix='Превышена длина текста','Сократите текст до 4000 символов для решения/комментария, до 500 для остальных полей'
            error(field,reason,fix)
        if cell:
            entry={'column':mapping[field],'type':cell.get('type'),'style':cell.get('style')}
            if cell.get('formula') is not None:entry['formula']=cell['formula']
            if result.get(field)!=cell.get('value'):entry['original']=cell.get('value')
            provenance[field]=entry
    result['sourceCells']=provenance
    unused={k:v for k,v in cells.items() if k not in set(mapping.values()) and (v.get('value') is not None or v.get('formula') is not None)}
    if unused:result['unmappedCells']=unused
    return result,errors


def preview(folder, db):
    meta=read_meta(folder);settings=meta['settings'];actor=actor_for(db,meta['actor'])
    check_storage(db.path,reserve=192*1024*1024)
    for old in list(Path(folder).glob('inspect-*'))+list(Path(folder).glob('preview-*')):
        if old.is_dir() and not old.is_symlink():shutil.rmtree(old)
    sites=sites_for(db,actor)
    from .writeoff_release import enabled
    if enabled() and not settings.get('reportPeriod'):raise Rejected('Выберите отчётный год и квартал отдельно от даты акта')
    if enabled() and settings.get('siteAliases') and not settings.get('aliasesConfirmed'):raise Rejected('Подтвердите сопоставление названий объектов')
    normalized_aliases={}
    for source,target in settings.get('siteAliases',{}).items():
        source=source.strip().casefold()
        if not source or len(source)>120 or target not in sites.values():
            raise Rejected('Сопоставление объектов содержит неизвестный объект или недопустимое название')
        if source in normalized_aliases and normalized_aliases[source]!=target:raise Rejected('Один алиас не может указывать на два объекта')
        normalized_aliases[source]=target
        if source in sites and sites[source]!=target:
            raise Rejected('Нельзя переназначить точное название существующего объекта')
        sites[source]=target
    stage=Path(folder)/'rows.sqlite3'
    stage.unlink(missing_ok=True)
    with closing(sqlite3.connect(stage)) as con:
        con.execute('pragma cache_size=-2048')
        con.execute('create table rows(number integer primary key, site_id text not null, data text not null)')
        con.execute('create table errors(seq integer primary key, row_no integer, column_name text, field text, reason text, fix text)')
        accepted=rejected=processed=empty=0;excluded=0;size=0;samples=[];errors=[];formula_count=0;unknown_sites=set();sums={f:Decimal(0) for f in ('quantity',*sorted(MONEY))};known={f:0 for f in sums};zeroes=0;rounded_cells=0;documents=set();dates=set()
        with Reader(Path(folder)/'source.xlsx',Path(folder)/('preview-'+uuid4().hex)) as reader:
            headers=next(reader.rows(settings['sheet'],settings['headerRow'],settings['headerRow'],allow_cached=False),None)
            if headers is None:raise Rejected('Строка заголовков не найдена')
            labels={k:v['value'] or k for k,v in headers[1].items()}
            def header_key(v):return ''.join(c for c in str(v).casefold() if c.isalnum())
            mapping=settings.get('mapping') or {field:matches[0] for field,label in FIELDS.items()
                if len(matches:=[col for col,value in labels.items() if header_key(value) in (header_key(field),header_key(label))])==1}
            if len(set(mapping.values()))!=len(mapping):raise Rejected('Одна колонка не может обозначать несколько разных показателей')
            missing=sorted(REQUIRED-set(mapping))
            meta['headers']=labels;meta['mapping']=mapping;meta['styles']=reader.formats;meta['date1904']=reader.date1904
            if missing:
                meta.update(status='mapping',missing=[FIELDS[k] for k in missing],processed=0,accepted=0,rejected=0,errors=[],rows=[])
                write_meta(folder,meta);return
            if any(k not in FIELDS or col not in labels for k,col in mapping.items()):raise Rejected('Некорректное сопоставление колонок')
            # Validate formulas per cell, so diagnostics retain exact physical coordinates.
            for number,cells in reader.rows(settings['sheet'],settings['firstRow'],settings['lastRow'],allow_cached=True,allow_missing_cached=True,allow_cell_errors=True):
                processed+=1
                record,row_errors=normalize(number,cells,mapping,sites,reader.date1904)
                if any(error['field']==FIELDS['site'] for error in row_errors):
                    raw_site=cells.get(mapping.get('site',''),{}).get('value')
                    if raw_site is not None and len(unknown_sites)<50:unknown_sites.add(str(raw_site).strip())
                if record is None:empty+=1;continue
                if settings.get('sourceDocument') and record.get('document')!=settings['sourceDocument']:
                    excluded+=1;continue
                if settings.get('reportPeriod'):record['reportPeriod']=settings['reportPeriod']
                cell_errors=[]
                for field,col in mapping.items():
                    cell=cells.get(col,{})
                    issue=None
                    if cell.get('type')=='e':issue=('Ошибка Excel в ячейке','Исправьте ошибку и сохраните книгу в Excel')
                    elif cell.get('formula') is not None:
                        formula_count+=1
                        if cell.get('value') is None:issue=('Нет сохранённого результата формулы','Пересчитайте и сохраните книгу в Excel')
                        elif not settings['allowCached']:issue=('Формула в импортируемом поле','Явно разрешите сохранённые значения формул или замените формулу значением')
                    if issue:cell_errors.append({'row':number,'column':col,'field':FIELDS[field],'reason':issue[0],'fix':issue[1]})
                blocked={error['column'] for error in cell_errors}
                row_errors=cell_errors+[error for error in row_errors if error['column'] not in blocked]
                if row_errors:
                    rejected+=1
                    for error in row_errors:
                        con.execute('insert into errors(row_no,column_name,field,reason,fix) values(?,?,?,?,?)',
                                    tuple(error[k] for k in ('row','column','field','reason','fix')))
                    errors.extend([{**error,'sheet':reader.sheets[settings['sheet']][0]} for error in row_errors[:max(0,100-len(errors))]])
                else:
                    encoded=json.dumps(record,ensure_ascii=False,separators=(',',':'));size+=len(encoded.encode())
                    if size>MAX_NORMALIZED:raise Rejected('Нормализованные данные превышают 64 МиБ; уменьшите выбранный диапазон')
                    con.execute('insert into rows values(?,?,?)',(number,record['siteId'],encoded));accepted+=1
                    for key in sums:
                        if record.get(key) is not None:sums[key]+=Decimal(record[key]);known[key]+=1
                    rounded_cells+=len(record.get('excelRoundingFields',[]))
                    if Decimal(record['quantity'])==0:zeroes+=1
                    documents.add((record['siteId'],record['document']));dates.add((record['siteId'],record['date']))
                    if len(samples)<20:samples.append({'row':number,**{k:(v[:250] if isinstance(v,str) else v) for k,v in record.items() if k not in ('sourceCells','unmappedCells')}})
                if processed%500==0:
                    con.commit()
                    check_storage(db.path,reserve=32*1024*1024)
                    if stage.stat().st_size>192*1024*1024:raise Rejected('Превышен предел временного хранилища импорта')
                    if shutil.disk_usage(folder).free<256*1024*1024:raise Rejected('Недостаточно свободного места для безопасного импорта')
            con.commit()
        profile={'sha256':meta['sha256'],'sheet':settings['sheet'],'firstRow':settings['firstRow'],
                    'lastRow':settings['lastRow'],'headerRow':settings['headerRow'],'mapping':mapping,'siteAliases':settings.get('siteAliases',{}),'reportPeriod':settings.get('reportPeriod',''),'profile':'decisions-v1'}
        if settings.get('sourceDocument'):profile['sourceDocument']=settings['sourceDocument']
        signature=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
        meta.update(summary={**{f:format(v,'f') if known[f] else None for f,v in sums.items()},'known':known,'rows':accepted,'zeroQuantityRows':zeroes,'documents':len(documents),'inventories':len(dates)},excludedDocumentRows=excluded,
                    status='ready' if accepted and not rejected else 'invalid',signature=signature,missing=[],processed=processed,
                    accepted=accepted,rejected=rejected,skipped=empty,normalizedBytes=size,errors=errors,rows=samples,
                    excelRoundingCells=rounded_cells,formulaCells=formula_count,unknownSites=sorted(unknown_sites),warning='Использованы сохранённые Excel значения; их актуальность не пересчитывается' if formula_count else '')
        write_meta(folder,meta)


def commit(folder, db):
    from .decision_versions import assert_plan,begin_version,initial_document,check_new_collisions
    meta=read_meta(folder);actor=actor_for(db,meta['actor'])
    if not meta.get('accepted') or meta.get('rejected'):raise Rejected('Нужен успешный предпросмотр без ошибок')
    check_storage(db.path,reserve=meta['normalizedBytes']*3+64*1024*1024)
    with closing(sqlite3.connect(Path(folder)/'rows.sqlite3')) as stage, db.connect(actor) as con:
        con.execute('begin immediate')
        current=con.execute('select role,active from users where id=?',(actor['id'],)).fetchone()
        if not current or not current['active'] or current['role']!=actor['role']:raise Rejected('Учётная запись изменилась после предпросмотра')
        version_plan=meta.get('versionPlan')
        applied=assert_plan(con,meta,actor) if version_plan else False
        existing=db.import_exists(con,meta['signature'])
        # Always recheck current object rights, including before returning duplicate.
        for (site,) in stage.execute('select distinct site_id from rows'):
            if not accessible(con,actor,site) or not con.execute("select 1 from records where collection='sites' and id=?",(site,)).fetchone():
                raise Rejected('Доступ к объекту изменился после предпросмотра')
        if existing and version_plan and not applied:raise Rejected('Эта книга уже записана в другой операции; существующие версии не изменены')
        if existing:
            meta.update(status='done',duplicate=True,committed=0)
        else:
            maximum=int(os.getenv('KSU_DB_MAX_BYTES','0')) if db.backend=='sqlite' else 0
            used=con.execute('pragma page_count').fetchone()[0]*con.execute('pragma page_size').fetchone()[0] if db.backend=='sqlite' else 0
            # A GROUP BY of the source JSON can spill whole rows to /tmp. Keep only
            # short document keys on the bounded staging disk instead.
            stage.execute('drop table if exists capacity_groups');stage.execute('create table capacity_groups(k text primary key)')
            for site,encoded in stage.execute('select site_id,data from rows'):
                stage.execute('insert or ignore into capacity_groups values(?)',(json.dumps([site,json.loads(encoded)['document']],ensure_ascii=False),))
            groups=stage.execute('select count(*) from capacity_groups').fetchone()[0]
            retired=stage.execute('select count(*) from previous where selected=1').fetchone()[0] if version_plan else 0
            estimate=meta['normalizedBytes']*3+meta['accepted']*512+groups*4096+retired*256+2*1024*1024
            meta['admissionEstimateBytes']=estimate;write_meta(folder,meta)
            if db.backend=='postgresql':db.check_capacity(con,estimate)
            if maximum and used+estimate>maximum:
                raise Rejected('Недостаточно запаса в лимите SQLite для всего импорта; уменьшите диапазон или согласуйте перенос хранения')
            if not version_plan:check_new_collisions(db,con,stage)
            data={k:meta[k] for k in ('sha256','name','signature','settings','mapping','processed','accepted','rejected','skipped','formulaCells','styles','date1904')}
            data.update(summary=meta.get('summary'),excelRoundingCells=meta.get('excelRoundingCells',0))
            con.execute('insert into imports values(?,?,?,?)',(meta['signature'],actor['id'],json.dumps(data,ensure_ascii=False),now()))
            created=now();seen=set()
            target=begin_version(con,meta,actor,created,stage) if version_plan else None
            sheet=meta['sheets'][meta['settings']['sheet']]['name']
            for number,site,encoded in stage.execute('select number,site_id,data from rows order by number'):
                # Main transaction is never committed in batches: any error rolls back rows, header and audit.
                if con.execute('select 1 from decision_records where file_sha=? and sheet=? and source_row=?',(meta['sha256'],sheet,number)).fetchone():
                    raise CellError(number,'','Исходная строка уже импортирована','Выберите непересекающийся диапазон; новая запись целиком отменена')
                try:
                    record_id=uuid4().hex
                    con.execute('insert into decision_records values(?,?,?,?,?,?,?,?)',
                        (record_id,meta['signature'],meta['sha256'],sheet,number,site,actor['id'],encoded))
                    doc_id,version_id=target or initial_document(con,meta['signature'],site,json.loads(encoded),actor,created,seen)
                    previous=stage.execute('select old_id from comparison where row_no=?',(number,)).fetchone()[0] if version_plan else None
                    con.execute('insert into decision_entries values(?,?,?,?,?)',(record_id,doc_id,version_id,site,previous))
                except (sqlite3.IntegrityError,psycopg.IntegrityError):
                    if db.backend=='postgresql':raise
                    if con.execute('select 1 from decision_records where file_sha=? and sheet=? and source_row=?',(meta['sha256'],sheet,number)).fetchone():
                        raise CellError(number,'','Исходная строка уже импортирована','Выберите непересекающийся диапазон; новая запись целиком отменена')
                    raise
            meta.update(status='done',duplicate=False,committed=meta['accepted'])
    write_meta(folder,meta)
    (Path(folder)/'source.xlsx').unlink(missing_ok=True)
    for path in Path(folder).glob('*/shared-strings.sqlite3'):path.unlink(missing_ok=True)
    (Path(folder)/'rows.sqlite3').unlink(missing_ok=True)


def worker_main(folder, action):
    import resource
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(100,100))
    import signal
    expected_parent=int(os.getenv('KSU_IMPORT_PARENT_PID',str(os.getppid())))
    if sys.platform.startswith('linux'):
        # PDEATHSIG tracks the spawning Linux thread, not the server process.
        # AnyIO retires idle request threads while a long import is still running.
        # Watch the process parent instead, without adding a memory-heavy thread.
        def check_parent(signum=None, frame=None):
            if os.getppid()!=expected_parent:raise SystemExit(98)
        check_parent()
        signal.signal(signal.SIGALRM,check_parent)
        signal.setitimer(signal.ITIMER_REAL,1,1)
    meta=read_meta(folder)
    try:
        db=Database(meta['database'])
        if action=='inspect':inspect(folder)
        elif action=='preview':preview(folder,db)
        elif action=='version':
            from .decision_versions import prepare
            prepare(folder,db,meta['versionSettings'])
        elif action=='commit':commit(folder,db)
        else:raise Rejected('Неизвестная операция импорта')
    except Exception as error:
        meta=read_meta(folder)
        if isinstance(error,CellError):
            meta['errors']=[{'row':error.row,'column':error.column,'reason':error.reason,'fix':error.fix}]
        if isinstance(error,(Rejected,StorageFull)):message=str(error)
        elif isinstance(error,sqlite3.OperationalError) and 'full' in str(error).lower():message='Достигнут лимит SQLite или диска; импорт полностью отменён'
        elif isinstance(error,(sqlite3.IntegrityError,psycopg.IntegrityError)):message='Часть исходных строк уже импортирована или данные не проходят правила базы; запись отменена'
        elif isinstance(error,(zipfile.BadZipFile,ParseError,DefusedXmlException,KeyError)):message='Повреждённая или небезопасная структура XLSX. Откройте и пересохраните книгу в Excel'
        elif isinstance(error,MemoryError):message='Превышен лимит памяти обработчика; уменьшите выбранный диапазон'
        else:message='Не удалось обработать книгу: '+type(error).__name__
        meta.update(status='error',message=message);write_meta(folder,meta)


class Jobs:
    def __init__(self, db):
        self.db=db;self.root=db.path.resolve().parent/'import-jobs';self.root.mkdir(exist_ok=True,mode=0o700)
        self.guard=threading.Lock();self.active=None;self.process=None;self.process_id=None;self.lock_handle=None;self.monitors=[]
        self.recover_idle()

    def recover_idle(self):
        import fcntl
        lock=(self.root/'.active.lock').open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:lock.close();return
        candidate=None
        for folder in self.root.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not ID.fullmatch(folder.name):continue
            try:
                meta=read_meta(folder)
                if meta['status'] in ('uploading','inspecting','checking','committing'):
                    write_meta(folder,self.reconcile(meta))
                elif meta['status'] in ('uploaded','ready','mapping','invalid') and time.time()-meta['created']<TTL:
                    candidate=folder.name
            except (OSError,ValueError,KeyError):continue
        if candidate:self.active=candidate;self.lock_handle=lock
        else:lock.close()

    def get(self, ident, actor):
        if not ID.fullmatch(ident):raise Rejected('Импорт не найден')
        folder=self.root/ident
        if folder.is_symlink() or not (folder/'meta.json').is_file():raise Rejected('Импорт не найден')
        meta=read_meta(folder)
        if meta['actor']!=actor['id']:raise Rejected('Импорт не найден')
        return folder,meta

    def create(self, actor, name):
        import fcntl
        with self.guard:
            if self.active is not None:
                old=read_meta(self.root/self.active)
                if old['status'] not in ('done','error','cancelled') and ((self.process and self.process.poll() is None) or time.time()-old['created']<TTL):
                    raise Rejected('Другой импорт ещё активен; завершите или отмените его')
                self.release()
            self.lock_handle=(self.root/'.active.lock').open('a')
            try:fcntl.flock(self.lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                self.lock_handle.close();self.lock_handle=None
                raise Rejected('Другой импорт ещё активен')
            try:check_storage(self.db.path,reserve=512*1024*1024)
            except Exception:
                self.release();raise
            if shutil.disk_usage(self.root).free<768*1024*1024:
                self.release();raise Rejected('Для импорта требуется не менее 768 МиБ свободного диска')
            for folder in self.root.iterdir():
                if folder.is_dir() and not folder.is_symlink() and ID.fullmatch(folder.name):
                    try:
                        if time.time()-read_meta(folder)['created']>TTL:shutil.rmtree(folder)
                    except (OSError,ValueError,KeyError):pass
            retained=[]
            for existing in self.root.iterdir():
                if existing.is_dir() and not existing.is_symlink() and ID.fullmatch(existing.name):
                    try:retained.append((read_meta(existing)['created'],existing))
                    except (OSError,ValueError,KeyError):pass
            for _,expired in sorted(retained)[:-31]:shutil.rmtree(expired)
            ident=uuid4().hex;folder=self.root/ident;folder.mkdir(mode=0o700)
            write_meta(folder,{'id':ident,'database':getattr(self.db,'locator',str(self.db.path.resolve())),'actor':actor['id'],'name':name,
                               'created':time.time(),'status':'uploading'})
            self.active=ident
            return folder

    def start(self, ident, actor, action, settings=None):
        with self.guard:
            folder,meta=self.get(ident,actor)
            allowed={'inspect':{'uploading'},'preview':{'uploaded','mapping','ready','invalid','error'},'version':{'ready','error'},'commit':{'ready'}}
            if self.process is not None and self.process.poll() is None and (self.process_id!=ident or meta['status'] not in ('inspecting','checking','committing','uploading')):
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:pass
            if meta['status'] not in allowed[action] or (self.process is not None and self.process.poll() is None):
                raise Rejected('Дождитесь текущей операции импорта')
            if self.active!=ident:raise Rejected('Начата другая загрузка; загрузите файл заново')
            if settings is not None:
                if action=='version':
                    meta['versionSettings']=settings;meta['message']='';meta.pop('versionPlan',None);meta.pop('confirmedVersionToken',None)
                else:
                    meta['settings']=settings
                    meta.update(processed=0,accepted=0,rejected=0,rows=[],errors=[],missing=[],message='',warning='',formulaCells=0)
                    meta.pop('versionPlan',None);meta.pop('confirmedVersionToken',None)
                    meta.pop('versionSettings',None)
                    for key in ('summary',):meta.pop(key,None)
            meta['action']=action
            meta['status']={'inspect':'inspecting','preview':'checking','version':'checking','commit':'committing'}[action]
            write_meta(folder,meta)
            log=(folder/'worker.log').open('wb')
            self.process=subprocess.Popen([sys.executable,'-m','server.decision_import',str(folder),action],
                             stdout=log,stderr=log,cwd=Path(__file__).resolve().parent.parent,
                             env={**os.environ,'KSU_IMPORT_PARENT_PID':str(os.getpid())})
            self.process_id=ident
            log.close();process=self.process
            def monitor():
                try:process.wait(timeout=min(240,max(1,int(os.getenv('KSU_IMPORT_PHASE_TIMEOUT_SECONDS','240')))))
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
                with self.guard:
                    if self.process is not process:return
                    current=read_meta(folder)
                    if current['status'] in ('inspecting','checking','committing'):
                        current=self.reconcile(current)
                        write_meta(folder,current)
            thread=threading.Thread(target=monitor,daemon=True)
            self.monitors=[t for t in self.monitors if t.is_alive()]+[thread];thread.start()

    def cancel(self, ident, actor):
        with self.guard:
            folder,meta=self.get(ident,actor)
            if self.active==ident and self.process_id==ident and self.process and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            meta=self.reconcile(read_meta(folder),cancel=True)
            write_meta(folder,meta)
            for name in ('source.xlsx','rows.sqlite3'):(folder/name).unlink(missing_ok=True)
            for scratch in list(folder.glob('inspect-*'))+list(folder.glob('preview-*')):
                if scratch.is_dir() and not scratch.is_symlink():shutil.rmtree(scratch)
            if self.active==ident:self.release()
            return meta

    def reconcile(self, meta, cancel=False):
        if meta.get('action') not in (None,'commit'):
            meta.update(status='cancelled' if cancel else 'error',message='Проверка прервана; запись версии не подтверждалась')
            return meta
        signature=meta.get('signature')
        if signature:
            with self.db.connect(actor_for(self.db,meta['actor'])) as con:
                imported=self.db.import_exists(con,signature)
                if meta.get('versionPlan'):
                    imported=bool(con.execute('select 1 from decision_versions where operation_key=?',(meta['versionPlan']['token'],)).fetchone())
                count=con.execute('select count(*) from decision_records where import_hash=?',(signature,)).fetchone()[0]
            if imported:
                meta.update(status='done',committed=count,warning='Завершённая транзакция подтверждена по журналу импорта')
                return meta
        if meta['status']!='done':
            meta.update(status='cancelled' if cancel else 'error',message='Обработка прервана; неполные данные не записаны')
        return meta

    def release(self):
        if self.lock_handle:self.lock_handle.close();self.lock_handle=None
        self.active=None

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        for thread in self.monitors:thread.join(timeout=3)
        if self.active:
            folder=self.root/self.active
            meta=read_meta(folder)
            if meta['status'] in ('uploading','inspecting','checking','committing'):
                write_meta(folder,self.reconcile(meta))
        self.release()


if __name__=='__main__':worker_main(Path(sys.argv[1]),sys.argv[2])
