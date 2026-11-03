import base64
from contextlib import asynccontextmanager
import io
import json
import os
import secrets
import time
import warnings
import asyncio
import sqlite3
import psycopg
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from PIL import Image, UnidentifiedImageError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .database import Database, ROOT, digest, password_hash, password_valid, now
from .imports import parse_import, commit_import
from .models import Changes, Login, ImportFile, MediaFile, DecisionPreview
from .reports import build_report, export
from .service import accessible, apply_changes, fail
from .storage import StorageFull, check_storage

Image.MAX_IMAGE_PIXELS=20_000_000
DUMMY_PASSWORD=password_hash(secrets.token_urlsafe(24))


def create_app(path=None, origin=None, secure=None):
    public_origin=(origin or os.getenv('KSU_ORIGIN','http://localhost:8000')).rstrip('/')
    secure_cookie=secure if secure is not None else os.getenv('KSU_SECURE_COOKIES','0')=='1'
    if public_origin.startswith('https://') and not secure_cookie:
        raise ValueError('HTTPS требует KSU_SECURE_COOKIES=1')
    if not public_origin.startswith('https://') and urlparse(public_origin).hostname not in ('localhost','127.0.0.1','testserver'):
        raise ValueError('Внешний адрес требует HTTPS')
    database_path=path or ('postgresql' if os.getenv('KSU_DATABASE_URL') else None) or os.getenv('KSU_DB',str(ROOT/'var/ksu.sqlite3'))
    if os.getenv('KSU_REQUIRE_INITIALIZED','0')=='1' and str(database_path)!='postgresql' and not str(database_path).startswith(('postgresql://','postgres://')):
        # A missing data mount must never silently create a fresh, empty production database.
        from .maintenance import validate_database
        validate_database(database_path)
    db=Database(database_path)
    from .decision_import import Jobs
    jobs=Jobs(db)
    @asynccontextmanager
    async def lifespan(application):
        try:yield
        finally:await asyncio.to_thread(jobs.close)
    app=FastAPI(title='КСУ Контроль',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.db=db;app.state.import_jobs=jobs
    write_gate=asyncio.Lock()
    backup_gate=threading.BoundedSemaphore(1)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlparse(public_origin).hostname])

    @app.exception_handler(StorageFull)
    async def storage_limit(request,error):
        return JSONResponse({'detail':str(error)},status_code=507)

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(psycopg.IntegrityError)
    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_failure(request, error):
        if 'full' in str(error).lower():
            return JSONResponse({'detail':'Лимит постоянного хранилища достигнут'},status_code=507)
        return JSONResponse({'detail':'Операция с базой временно недоступна'},status_code=503)

    @app.middleware('http')
    async def security(request, call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.headers.get('origin')!=public_origin:
                return JSONResponse({'detail':'Недопустимый источник запроса'},status_code=403)
            # Large XLSX is an authenticated raw stream, never a buffered Base64 JSON body.
            if request.url.path!='/api/decision-imports':
                body=bytearray()
                async for chunk in request.stream():
                    if len(body)+len(chunk)>6_000_000:
                        return JSONResponse({'detail':'Запрос больше 6 МБ'},status_code=413)
                    body.extend(chunk)
                request._body=bytes(body)
        if request.method in ('POST','PUT','PATCH','DELETE') and os.getenv('KSU_DATA_MAX_BYTES') and request.url.path!='/api/decision-imports':
            async with write_gate:
                try:
                    if request.url.path not in ('/api/admin/backup','/api/logout'):
                        check_storage(db.path)
                except StorageFull as error:
                    return JSONResponse({'detail':str(error)},status_code=507)
                response=await call_next(request)
        else:
            response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if secure_cookie:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    def auth(request, csrf=False):
        token=request.cookies.get('ksu_session','')
        with db.connect() as con:
            row=con.execute('select u.*,s.csrf,s.expires from sessions s join users u on u.id=s.user_id where s.token_hash=? and u.active=1 and s.expires>?',
                            (digest(token),time.time())).fetchone()
        if not row:fail('Войдите в свою учётную запись',401)
        actor=dict(row)
        if csrf and not secrets.compare_digest(request.headers.get('x-csrf-token',''),actor['csrf']):
            fail('Неверный защитный токен. Войдите повторно.',403)
        return actor

    def public_user(actor):
        return {k:actor[k] for k in ('id','username','name','role','csrf')}

    @app.get('/api/health')
    def health():
        with db.connect() as con:con.execute('select 1').fetchone()
        return {'status':'ok','data':'demonstration'}

    @app.post('/api/login')
    def login(body: Login, request: Request):
        # One limit per username and one per peer. Never trust forwarded IP headers.
        keys=[digest('user:'+body.username.casefold()),digest('peer:'+(request.client.host if request.client else 'local'))]
        with db.connect() as con:
            con.execute('begin immediate')
            for key in keys:
                r=con.execute('select * from attempts where key=?',(key,)).fetchone()
                if r and r['until']>time.time() and r['count']>=10:
                    fail('Слишком много попыток. Повторите через 15 минут.',429)
                # Reserve an attempt before expensive password verification.
                con.execute('insert into attempts values(?,1,?) on conflict(key) do update set count=case when attempts.until<? then 1 else attempts.count+1 end, until=case when attempts.until<? then excluded.until else attempts.until end',
                            (key,time.time()+900,time.time(),time.time()))
        user=db.user(body.username)
        valid=password_valid(body.password,user['password'] if user else DUMMY_PASSWORD)
        if not user or not valid:fail('Неверный логин или пароль',401)
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with db.connect() as con:
            for key in keys:
                con.execute('update attempts set count=case when count>0 then count-1 else 0 end where key=?',(key,))
            con.execute('delete from sessions where expires<?',(time.time(),))
            if request.cookies.get('ksu_session'):con.execute('delete from sessions where token_hash=?',(digest(request.cookies['ksu_session']),))
            con.execute('insert into sessions values(?,?,?,?)',(digest(token),user['id'],csrf,time.time()+8*3600))
        response=JSONResponse(public_user({**user,'csrf':csrf}))
        response.set_cookie('ksu_session',token,httponly=True,secure=secure_cookie,samesite='strict',max_age=8*3600,path='/')
        return response

    @app.get('/api/me')
    def me(request:Request):return public_user(auth(request))

    @app.post('/api/logout')
    def logout(request:Request):
        auth(request,True)
        with db.connect() as con:con.execute('delete from sessions where token_hash=?',(digest(request.cookies['ksu_session']),))
        response=JSONResponse({'ok':True});response.delete_cookie('ksu_session',path='/');return response

    @app.get('/api/state')
    def state(request:Request):
        actor=auth(request)
        with db.connect(actor) as con:return db.snapshot(con,actor)

    @app.post('/api/changes')
    def changes(body:Changes,request:Request):return apply_changes(db,auth(request,True),body)

    @app.post('/api/admin/backup')
    def export_backup(request:Request):
        actor=auth(request,True)
        if actor['role']!='manager':fail('Резервные копии доступны только руководителю',403)
        if db.backend=='postgresql':
            from .pg_backup import backup_database, BackupError
            backup_dsn=os.getenv('KSU_PG_BACKUP_DSN')
            directory=os.getenv('KSU_PG_BACKUP_DIR')
            if not backup_dsn or not directory:fail('PostgreSQL backup is not configured',503)
            if not backup_gate.acquire(blocking=False):fail('Другая копия ещё формируется',429)
            temporary=None
            try:
                Path(directory).mkdir(parents=True,exist_ok=True,mode=0o700)
                temporary=tempfile.TemporaryDirectory(prefix='.private-backup-',dir=directory)
                target=Path(temporary.name)/'backup.ksubackup'
                result=backup_database(backup_dsn,target)
                def cleanup_pg():
                    temporary.cleanup();backup_gate.release()
                return FileResponse(target,media_type='application/octet-stream',filename='ksu-postgresql.ksubackup',
                    headers={'X-Backup-SHA256':result['sha256']},background=BackgroundTask(cleanup_pg))
            except Exception:
                if temporary:temporary.cleanup()
                backup_gate.release()
                fail('Не удалось сформировать PostgreSQL backup; проверьте лимиты и настройки локально',503)
        if not backup_gate.acquire(blocking=False):fail('Другая копия ещё формируется',429)
        temporary=None
        try:
            maximum=int(os.getenv('KSU_BACKUP_EXPORT_MAX_BYTES','16777216'))
            if db.path.stat().st_size>maximum:fail('Копия превышает бесплатный лимит внешнего экспорта; требуется решение администратора',413)
            from .maintenance import snapshot_database
            temporary=tempfile.TemporaryDirectory(prefix='ksu-private-export-')
            target=Path(temporary.name)/'backup.sqlite3'
            snapshot_database(db.path,target)
            if target.stat().st_size>maximum:fail('Копия превышает лимит экспорта',413)
            def cleanup():
                temporary.cleanup()
                backup_gate.release()
            return FileResponse(target,media_type='application/octet-stream',filename='ksu-backup.sqlite3',background=BackgroundTask(cleanup))
        except Exception:
            if temporary:temporary.cleanup()
            backup_gate.release()
            raise

    @app.get('/api/history')
    def history(request:Request, collection:str='', record_id:str='', before:int=2**63-1):
        actor=auth(request)
        with db.connect(actor) as con:
            records=con.execute('''select a.* from audit a join (select collection,id,author_id,site_id,data from records union all select 'decisions',id,author_id,site_id,data from decision_records) r on r.collection=a.collection and r.id=a.record_id
                where a.seq<? and (?='' or a.collection=?) and (?='' or a.record_id=?)
                and (?='manager' or ((r.author_id=? or (r.collection='visits' and json_extract(r.data,'$.assignedTo')=?))
                and exists(select 1 from user_sites us where us.user_id=? and us.site_id=r.site_id)))
                order by a.seq desc limit 100''',(before,collection,collection,record_id,record_id,actor['role'],actor['id'],actor['id'],actor['id'])).fetchall()
            names={r['id']:r['name'] for r in con.execute('select id,name from users')}
            return [{**dict(r),'actor':names.get(r['actor_id'],'Система'),
                     'before':json.loads(r['before_json']) if r['before_json'] else None,
                     'after':json.loads(r['after_json']) if r['after_json'] else None} for r in records]

    @app.post('/api/media')
    def upload(payload:MediaFile,request:Request):
        actor=auth(request,True);site=payload.siteId;content=payload.content
        with db.connect(actor) as con:
            if not accessible(con,actor,site) or not con.execute("select 1 from records where collection='sites' and id=?",(site,)).fetchone():fail('Нет доступа к объекту',403)
        try:
            raw=base64.b64decode(content,validate=True)
            if len(raw)>3_000_000:fail('Изображение больше 3 МБ')
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    if source.format not in ('JPEG','PNG','WEBP'):fail('Нужны JPEG, PNG или WEBP')
                    source.load();image=source.convert('RGB');image.thumbnail((1600,1600))
            output=io.BytesIO();image.save(output,format='JPEG',quality=80)
            image.thumbnail((240,240));thumb=io.BytesIO();image.save(thumb,format='JPEG',quality=70)
        except HTTPException:raise
        except (ValueError,UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):fail('Невозможно прочитать изображение')
        ident=secrets.token_hex(24)
        with db.connect(actor) as con:
            if db.backend=='postgresql':
                con.execute('begin immediate');db.check_capacity(con,len(output.getvalue())+len(thumb.getvalue())+65536)
            con.execute('insert into media values(?,?,?,?,?,?)',(ident,site,actor['id'],output.getvalue(),thumb.getvalue(),now()))
        return {'url':'/api/media/'+ident}

    @app.get('/api/media/{ident}')
    def media(ident:str,request:Request,thumbnail:bool=False):
        actor=auth(request)
        with db.connect(actor) as con:
            row=con.execute('select * from media where id=?',(ident,)).fetchone()
            if not row or not accessible(con,actor,row['site_id']):fail('Фото недоступно',404)
            return Response(bytes(row['thumbnail'] if thumbnail else row['content']),media_type='image/jpeg')

    from .decision_import import Rejected, FIELDS, read_meta, write_meta, MAX_FILE

    @app.exception_handler(Rejected)
    async def import_rejected(request, error):
        return JSONResponse({'detail':str(error)},status_code=422)

    def owned_job(ident, actor):
        try:return jobs.get(ident,actor)
        except Rejected:fail('Импорт не найден',404)

    def job_view(meta):
        return {k:v for k,v in meta.items() if k not in ('database','actor','created')}

    @app.post('/api/decision-imports')
    async def upload_decisions(request:Request):
        from urllib.parse import unquote
        actor=auth(request,True)
        name=unquote(request.headers.get('X-KSU-Filename',''))
        if not name.lower().endswith('.xlsx') or len(name)>200:fail('Выберите файл XLSX, не XLS/XLSM')
        length=request.headers.get('content-length')
        if length and (not length.isdigit() or int(length)>MAX_FILE):fail('Файл больше 30 МБ',413)
        folder=jobs.create(actor,name)
        try:
            size=0;digest=__import__('hashlib').sha256()
            async with asyncio.timeout(300):
                with (folder/'source.xlsx').open('xb') as output:
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>MAX_FILE:fail('Файл больше 30 МБ',413)
                        if read_meta(folder)['status']=='cancelled':fail('Загрузка отменена',409)
                        digest.update(chunk)
                        await asyncio.to_thread(output.write,chunk)
            if not size:fail('Файл пуст')
            meta=read_meta(folder);meta.update(sha256=digest.hexdigest(),bytes=size);write_meta(folder,meta)
            jobs.start(meta['id'],actor,'inspect')
            return {'id':meta['id'],'status':'inspecting'}
        except BaseException as error:
            jobs.cancel(folder.name,actor)
            if isinstance(error,TimeoutError):fail('Истекло время загрузки (5 минут)',408)
            raise

    @app.get('/api/decision-imports')
    def active_decision_import(request:Request):
        actor=auth(request)
        if jobs.active:
            try:
                _,meta=jobs.get(jobs.active,actor)
                if meta['status'] not in ('done','error','cancelled'):return job_view(meta)
            except Rejected:pass
        return None

    @app.get('/api/decision-imports/{ident}')
    def decision_import_status(ident:str,request:Request):
        _,meta=owned_job(ident,auth(request));return job_view(meta)

    @app.post('/api/decision-imports/{ident}/preview')
    def preview_decisions(ident:str,settings:DecisionPreview,request:Request):
        actor=auth(request,True);owned_job(ident,actor)
        if not settings.headerRow<settings.firstRow<=settings.lastRow:fail('Диапазон данных должен идти после заголовков')
        jobs.start(ident,actor,'preview',settings.model_dump());return {'id':ident,'status':'checking'}

    @app.post('/api/decision-imports/{ident}/commit')
    def commit_decisions(ident:str,request:Request):
        actor=auth(request,True);_,meta=owned_job(ident,actor)
        if meta['status']=='done':return job_view(meta)
        jobs.start(ident,actor,'commit');return {'id':ident,'status':'committing'}

    @app.delete('/api/decision-imports/{ident}')
    def cancel_decision_import(ident:str,request:Request):
        actor=auth(request,True);owned_job(ident,actor)
        return job_view(jobs.cancel(ident,actor))

    @app.get('/api/decision-imports/{ident}/errors.csv')
    def decision_import_errors(ident:str,request:Request):
        folder,meta=owned_job(ident,auth(request))
        if meta['status'] not in ('ready','invalid','error'):fail('Диагностика ещё формируется',409)
        def data():
            import csv
            from contextlib import closing
            from .reports import safe_cell
            output=io.StringIO();writer=csv.writer(output,delimiter=';')
            writer.writerow(['Строка Excel','Колонка','Поле','Причина','Как исправить'])
            yield ('\ufeff'+output.getvalue()).encode('utf-8');output.seek(0);output.truncate(0)
            stage=folder/'rows.sqlite3'
            count=0
            if stage.exists():
                with closing(sqlite3.connect(stage.as_uri()+'?mode=ro',uri=True)) as con:
                    for row in con.execute('select row_no,column_name,field,reason,fix from errors order by seq'):
                        count+=1
                        writer.writerow([safe_cell(x) for x in row]);yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
            if not count:
                for error in meta.get('errors',[]):
                    writer.writerow([safe_cell(error.get(k,'')) for k in ('row','column','field','reason','fix')])
                    yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
        return StreamingResponse(data(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="import-errors.csv"'})

    def decision_scope(actor,site):
        clause="(?='manager' or exists(select 1 from user_sites us where us.user_id=? and us.site_id=d.site_id)) and (?='' or d.site_id=?)"
        return clause,(actor['role'],actor['id'],site,site)

    @app.get('/api/decisions/export.csv')
    def export_decisions(request:Request,site:str=''):
        actor=auth(request);clause,params=decision_scope(actor,site)
        def data():
            import csv
            from .reports import safe_cell
            output=io.StringIO();writer=csv.writer(output,delimiter=';')
            writer.writerow(list(FIELDS.values())+['Лист-источник','Строка Excel'])
            yield ('\ufeff'+output.getvalue()).encode('utf-8');output.seek(0);output.truncate(0)
            with db.connect(actor) as con:
                for row in con.execute('select d.data,d.sheet,d.source_row from decision_records d where '+clause+' order by d.import_hash,d.sheet,d.source_row,d.id',params):
                    item=json.loads(row['data'])
                    writer.writerow([safe_cell(item.get(k) or '') for k in FIELDS]+[safe_cell(row['sheet']),row['source_row']])
                    yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
        return StreamingResponse(data(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="decisions.csv"'})

    @app.get('/api/decisions')
    def decisions(request:Request,page:int=1,limit:int=50,site:str=''):
        actor=auth(request)
        if page<1 or not 1<=limit<=100:fail('Некорректная страница или размер страницы')
        clause,params=decision_scope(actor,site)
        with db.connect(actor) as con:
            con.execute('begin')
            count=con.execute('select count(*) from decision_records d where '+clause,params).fetchone()[0]
            rows=con.execute('select d.* from decision_records d where '+clause+' order by (select created_at from imports where hash=d.import_hash) desc,d.source_row desc,d.id limit ? offset ?',params+(limit,(page-1)*limit)).fetchall()
        return {'page':page,'limit':limit,'total':count,'rows':[{'id':r['id'],'sourceRow':r['source_row'],'sheet':r['sheet'],
                 **{k:v for k,v in json.loads(r['data']).items() if k not in ('sourceCells','unmappedCells')}} for r in rows]}

    @app.get('/api/decisions/{ident}')
    def decision_detail(ident:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:
            row=con.execute('select * from decision_records where id=?',(ident,)).fetchone()
            if not row or not accessible(con,actor,row['site_id']):fail('Запись не найдена',404)
            provenance=json.loads(db.import_metadata(con,row['import_hash']))
        return {'importStyles':provenance.get('styles',[]),'id':row['id'],'sourceRow':row['source_row'],'sheet':row['sheet'],'fileSha256':row['file_sha'],'importHash':row['import_hash'],'data':json.loads(row['data'])}

    @app.post('/api/import/{action}')
    def imports(action:str,payload:ImportFile,request:Request):
        actor=auth(request,True)
        data=payload.model_dump(exclude_none=True)
        if action=='preview':return parse_import(db,actor,data)[0]
        if action=='commit':return commit_import(db,actor,data)
        fail('Метод не найден',404)

    @app.get('/api/reports/{extension}')
    def reports(extension:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:state=db.snapshot(con,actor)
        report=build_report(state,dict(request.query_params))
        if extension=='json':return report
        content,mime=export(report,extension)
        return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="ksu-demo-{report["kind"]}.{extension}"'})

    # Only explicit public assets; never serve database, backups, SQL, tests or source directories.
    def make_asset(filename):
        def asset():
            return FileResponse(ROOT/filename)
        return asset
    for route,filename in [('/','index.html'),('/index.html','index.html'),('/app.js','app.js'),('/sync.js','sync.js'),('/decisions.js','decisions.js'),('/calendar.js','calendar.js'),('/styles.css','styles.css')]:
        app.add_api_route(route,make_asset(filename),methods=['GET'],include_in_schema=False)
    return app
