import base64
import hashlib
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
from .database import Database, ROOT, digest, password_hash, password_valid, now, is_postgres
from .imports import parse_import, commit_import
from .models import Changes, Login, ImportFile, MediaFile, DecisionPreview, DecisionVersionPreview, DecisionVersionConfirm, InventoryPlanUpload, InventoryCountUpdate, InventoryFinalize
from .reports import build_report, export
from .service import accessible, apply_changes, fail
from .storage import StorageFull, check_storage

Image.MAX_IMAGE_PIXELS=20_000_000
DUMMY_PASSWORD=password_hash(secrets.token_urlsafe(24))


def create_app(path=None, origin=None, secure=None):
    release_policy=None
    if os.getenv('KSU_DEPLOYMENT_PROFILE')=='production' and os.getenv('KSU_OFFLINE_MODE') is None:
        raise ValueError('Explicit production release policy required')
    if os.getenv('KSU_OFFLINE_MODE') is not None:
        from .offline_release import Policy
        release_policy=Policy.environment()
    public_origin=(origin or os.getenv('KSU_ORIGIN','http://localhost:8000')).rstrip('/')
    secure_cookie=secure if secure is not None else os.getenv('KSU_SECURE_COOKIES','0')=='1'
    if public_origin.startswith('https://') and not secure_cookie:
        raise ValueError('HTTPS требует KSU_SECURE_COOKIES=1')
    if not public_origin.startswith('https://') and urlparse(public_origin).hostname not in ('localhost','127.0.0.1','testserver'):
        raise ValueError('Внешний адрес требует HTTPS')
    database_path=path or ('postgresql' if os.getenv('KSU_DATABASE_URL') else None) or os.getenv('KSU_DB',str(ROOT/'var/ksu.sqlite3'))
    if os.getenv('KSU_REQUIRE_INITIALIZED','0')=='1' and not is_postgres(database_path):
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
    app.state.backup_gate=backup_gate
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
        if request.url.path.startswith('/api/') and request.url.path not in ('/api/login','/api/logout') and request.cookies.get('ksu_session'):
            with db.connect() as con:
                reader=con.execute('select u.id,u.role from sessions s join users u on u.id=s.user_id where s.token_hash=? and s.expires>? and u.active=1',(digest(request.cookies['ksu_session']),time.time())).fetchone()
            expected=[request.headers.get('X-KSU-Expected-User')]
            if request.url.path.startswith('/api/media/'):expected.append(request.query_params.get('expectedUser'))
            if reader and any(value and value!=reader[0] for value in expected):
                return JSONResponse({'detail':'Сессия изменилась. Локальные очереди сохранены.'},status_code=403,headers={'Cache-Control':'no-store','X-KSU-Identity-Mismatch':'1'})
            if reader and reader[1]=='viewer':
                from .viewer import authorize
                try:authorize({'role':'viewer'},request.url.path,request.method)
                except HTTPException as error:return JSONResponse({'detail':error.detail},status_code=error.status_code,headers={'Cache-Control':'no-store',**(error.headers or {})})
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
        from .viewer import authorize
        authorize(actor,request.url.path,request.method)
        if actor['role']=='viewer' and not getattr(db,'viewer_read_capable',True):
            fail('Для просмотра требуется явное обновление функций чтения на новой PostgreSQL-копии',403)
        if release_policy:release_policy.routes(request.url.path,request.method)
        if os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off')=='on' and request.url.path.startswith('/api/decision-imports') and actor['role']!='manager':
            fail('Импорт списаний доступен только руководителю',403)
        expected=[request.headers.get('X-KSU-Expected-User')]
        if request.url.path.startswith('/api/media/'):expected.append(request.query_params.get('expectedUser'))
        if any(value and value!=actor['id'] for value in expected):
            raise HTTPException(403,'Сессия принадлежит другому пользователю. Очередь сохранена.',headers={'X-KSU-Identity-Mismatch':'1'})
        if request.headers.get('X-KSU-Offline-Deployment') and request.headers['X-KSU-Offline-Deployment']!=getattr(app.state,'offline_deployment_id',getattr(app.state,'offline_lab_id',None)):
            fail('Другая установка приложения. Очередь сохранена.',409)
        if csrf and not secrets.compare_digest(request.headers.get('x-csrf-token',''),actor['csrf']):
            fail('Неверный защитный токен. Войдите повторно.',403)
        if csrf and request.url.path in ('/api/offline/packages','/api/offline/changes') and hasattr(app.state,'offline_release'):
            app.state.offline_release.authorize(actor)
        return actor

    def public_user(actor):
        return {k:actor[k] for k in ('id','username','name','role','csrf')}

    @app.get('/api/health')
    def health():
        with db.connect() as con:con.execute('select 1').fetchone()
        if release_policy:
            result={'status':'ok','data':'application','build':os.getenv('KSU_RELEASE_BUILD_SHA','unknown'),'offline':release_policy.mode}
            result['backend']=db.backend
            local=getattr(app.state,'local_backups',None)
            if local:result['localBackup']=local.summary()
            return result
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
    def changes(body:Changes,request:Request):
        actor=auth(request,True)
        if release_policy:release_policy.plan_permissions(actor,body)
        return apply_changes(db,actor,body)

    from .models import OfflinePackage, OfflineChanges
    from . import offline

    @app.get('/api/checklists/current')
    def current_checklist(request:Request):
        actor=auth(request)
        with db.connect(actor) as con:
            con.execute('begin immediate')
            return offline.current_checklist(con)

    @app.get('/api/checklists/{ident}')
    def historical_checklist(ident:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:return offline.checklist(con,ident)

    @app.get('/api/receipts/{ident:path}')
    def operation_receipt(ident:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:
            value=offline.receipt(con,actor,ident)
            if value is None:fail('Подтверждение не найдено',404)
            return value

    @app.post('/api/offline/packages')
    def offline_package(body:OfflinePackage,request:Request):
        actor=auth(request,True)
        if len(set(body.siteIds))!=len(body.siteIds):fail('Объект повторяется')
        with db.connect(actor) as con:
            con.execute('begin immediate')
            return offline.package(con,actor,body,db)

    @app.post('/api/offline/changes')
    def offline_changes(body:OfflineChanges,request:Request):
        actor=auth(request,True)
        if release_policy:release_policy.plan_permissions(actor,body)
        value=apply_changes(db,actor,body,receipt_only=True,package_id=body.packageId)
        return JSONResponse(value,status_code=value['status'])

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
            export_dir=os.getenv('KSU_BACKUP_EXPORT_DIR')
            if export_dir:
                from .backup_export import directory,MAXIMUM
                folder=directory(db.path);maximum=MAXIMUM
            else:folder=None;maximum=int(os.getenv('KSU_BACKUP_EXPORT_MAX_BYTES','16777216'))
            if db.path.stat().st_size>maximum:fail('Копия превышает бесплатный лимит внешнего экспорта; требуется решение администратора',413)
            from .maintenance import snapshot_database
            temporary=tempfile.TemporaryDirectory(prefix='.ksu-private-export-',dir=folder)
            target=Path(temporary.name)/'backup.sqlite3'
            snapshot_database(db.path,target,timeout=120 if export_dir else None,maximum=maximum)
            if target.stat().st_size>maximum:fail('Копия превышает лимит экспорта',413)
            def cleanup():
                temporary.cleanup()
                backup_gate.release()
            from .release_schema import checksum
            return FileResponse(target,media_type='application/octet-stream',filename='ksu-backup.sqlite3',headers={'X-Backup-SHA256':checksum(target)},background=BackgroundTask(cleanup))
        except Exception:
            if temporary:temporary.cleanup()
            backup_gate.release()
            raise

    @app.get('/api/history')
    def history(request:Request, collection:str='', record_id:str='', before:int=2**63-1):
        actor=auth(request)
        with db.connect(actor) as con:
            if db.backend=='postgresql':
                # Audit RLS already enforces author/assignment + site access. Keep the
                # keyset LIMIT above any register join. A small work_mem avoids a
                # full-register hashed RLS subplan for each 100-row page.
                con.execute("set local work_mem='64kB'")
                records=con.execute("select * from audit where seq<? and (?='' or collection=?) and (?='' or record_id=?) order by seq desc limit 100",
                                    (before,collection,collection,record_id,record_id)).fetchall()
            else:
                records=con.execute('''select a.* from audit a join (select collection,id,author_id,site_id,data from records union all select 'decisions',id,author_id,site_id,data from decision_records union all select 'decisionVersions',id,author_id,site_id,'{}' from decision_documents) r on r.collection=a.collection and r.id=a.record_id
                where a.seq<? and (?='' or a.collection=?) and (?='' or a.record_id=?)
                and (?='manager' or ((r.collection='decisionVersions' or r.author_id=? or (r.collection='visits' and json_extract(r.data,'$.assignedTo')=?))
                and exists(select 1 from user_sites us where us.user_id=? and us.site_id=r.site_id)))
                order by a.seq desc limit 100''',(before,collection,collection,record_id,record_id,actor['role'],actor['id'],actor['id'],actor['id'])).fetchall()
            names={r['id']:r['name'] for r in con.execute('select id,name from users')}
            return [{**dict(r),'actor':names.get(r['actor_id'],'Система'),
                     'before':json.loads(r['before_json']) if r['before_json'] else None,
                     'after':json.loads(r['after_json']) if r['after_json'] else None} for r in records]

    @app.post('/api/media')
    def upload(payload:MediaFile,request:Request):
        actor=auth(request,True);site=payload.siteId;content=payload.content
        if payload.mutationId and hasattr(app.state,'offline_release'):app.state.offline_release.authorize(actor)
        with db.connect(actor) as con:
            if not accessible(con,actor,site) or not con.execute("select 1 from records where collection='sites' and id=?",(site,)).fetchone():fail('Нет доступа к объекту',403)
        try:
            raw=base64.b64decode(content,validate=True)
            if len(raw)>3_000_000:fail('Изображение больше 3 МБ')
            raw_hash=hashlib.sha256(raw).hexdigest()
            if bool(payload.mutationId)!=bool(payload.sha256):fail('Для идемпотентного фото нужны mutationId и sha256')
            if payload.sha256 and not secrets.compare_digest(raw_hash,payload.sha256):fail('Контрольная сумма фото не совпадает',409)
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
            con.execute('begin immediate')
            if not accessible(con,actor,site):fail('Нет доступа к объекту',403)
            body_hash=digest(offline.canonical({'siteId':site,'sha256':raw_hash}))
            if payload.mutationId:
                saved=offline.receipt(con,actor,payload.mutationId,'media',body_hash)
                if saved:return saved
                if con.execute('select 1 from mutations where user_id=? and id=?',(actor['id'],payload.mutationId)).fetchone():
                    fail('Идентификатор операции уже использован',409)
                existing=con.execute('select media_id from media_keys where user_id=? and site_id=? and content_hash=?',
                                     (actor['id'],site,raw_hash)).fetchone()
                if existing:ident=existing[0]
            else:existing=None
            if db.backend=='postgresql':
                db.check_capacity(con,(0 if existing else len(output.getvalue())+len(thumb.getvalue()))+65536)
            if not existing:
                con.execute('insert into media values(?,?,?,?,?,?)',(ident,site,actor['id'],output.getvalue(),thumb.getvalue(),now()))
                if payload.mutationId:con.execute('insert into media_keys values(?,?,?,?)',(actor['id'],site,raw_hash,ident))
            if payload.mutationId:
                return offline.store_receipt(con,actor,payload.mutationId,'media',body_hash,200,
                    {'url':'/api/media/'+ident,'sha256':raw_hash},[{'siteId':site,'mediaId':ident}])
        return {'url':'/api/media/'+ident}

    @app.get('/api/media/{ident}')
    def media(ident:str,request:Request,thumbnail:bool=False):
        actor=auth(request)
        with db.connect(actor) as con:
            if actor['role']=='viewer':
                from .viewer import read_media
                return Response(read_media(db,con,actor,ident,thumbnail),media_type='image/jpeg')
            row=con.execute('select * from media where id=?',(ident,)).fetchone()
            if not row or not accessible(con,actor,row['site_id']):fail('Фото недоступно',404)
            return Response(bytes(row['thumbnail'] if thumbnail else row['content']),media_type='image/jpeg')

    from .decision_import import Rejected, FIELDS, read_meta, write_meta, MAX_FILE
    from .decision_versions import ACTIVE,head,comparison,amounts,total,add,encoded_total

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
        if meta.get('versionPlan'):fail('Подтвердите сравнение через операцию замещения',422)
        if meta['status']=='done':return job_view(meta)
        jobs.start(ident,actor,'commit');return {'id':ident,'status':'committing'}

    @app.post('/api/decision-imports/{ident}/version-preview')
    def preview_version(ident:str,settings:DecisionVersionPreview,request:Request):
        actor=auth(request,True)
        if actor['role']!='manager':fail('Замещение подтверждает только руководитель',403)
        owned_job(ident,actor);jobs.start(ident,actor,'version',settings.model_dump());return {'id':ident,'status':'checking'}

    @app.get('/api/decision-imports/{ident}/comparison')
    def version_comparison(ident:str,request:Request,page:int=1,limit:int=50):
        actor=auth(request)
        if actor['role']!='manager':fail('Сравнение замещения доступно руководителю',403)
        if page<1 or not 1<=limit<=100:fail('Некорректная страница')
        folder,meta=owned_job(ident,actor)
        if meta['status']!='ready':fail('Дождитесь сравнения',409)
        return comparison(folder,page,limit)

    @app.get('/api/decision-imports/{ident}/version-inputs')
    def version_inputs(ident:str,request:Request,page:int=1,limit:int=20):
        actor=auth(request)
        if actor['role']!='manager':fail('Только руководитель',403)
        if page<1 or not 1<=limit<=100:fail('Некорректная страница')
        folder,meta=owned_job(ident,actor)
        if meta['status']!='ready':fail('Сначала проверьте книгу',409)
        from contextlib import closing
        with closing(sqlite3.connect(folder/'rows.sqlite3')) as stage:
            return {'total':meta['accepted'],'rows':[{'row':n,**{k:v for k,v in json.loads(data).items() if k in FIELDS}}
                for n,data in stage.execute('select number,data from rows order by number limit ? offset ?',(limit,(page-1)*limit))]}

    @app.post('/api/decision-imports/{ident}/version-confirm')
    def confirm_version(ident:str,body:DecisionVersionConfirm,request:Request):
        actor=auth(request,True)
        if actor['role']!='manager':fail('Замещение подтверждает только руководитель',403)
        with jobs.guard:
            folder,meta=owned_job(ident,actor);plan=meta.get('versionPlan')
            if not plan or not plan['ready'] or body.token!=plan['token']:fail('Нужно актуальное сравнение без неоднозначностей',409)
            if meta['status']=='done':return job_view(meta)
            if meta['status']!='ready':fail('Дождитесь текущей операции',409)
            meta['confirmedVersionToken']=body.token;write_meta(folder,meta)
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
            writer.writerow(['Строка Excel','Колонка','Поле','Причина','Как исправить','Лист'])
            yield ('\ufeff'+output.getvalue()).encode('utf-8');output.seek(0);output.truncate(0)
            stage=folder/'rows.sqlite3'
            count=0
            if stage.exists():
                with closing(sqlite3.connect(stage.as_uri()+'?mode=ro',uri=True)) as con:
                    sheet=meta.get('sheets',[{}])[meta.get('settings',{}).get('sheet',0)].get('name','')
                    for row in con.execute('select row_no,column_name,field,reason,fix from errors order by seq'):
                        count+=1
                        writer.writerow([safe_cell(x) for x in (*row,sheet)]);yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
            if not count:
                for error in meta.get('errors',[]):
                    writer.writerow([safe_cell(error.get(k,'')) for k in ('row','column','field','reason','fix','sheet')])
                    yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
        return StreamingResponse(data(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="import-errors.csv"'})

    def decision_scope(actor,site,document='',history=False,version=0,source_id=''):
        clause="(?='manager' or exists(select 1 from user_sites us where us.user_id=? and us.site_id=d.site_id)) and (?='' or d.site_id=?)"
        params=(actor['role'],actor['id'],site,site)
        if document:
            clause+=' and exists(select 1 from decision_entries entry where entry.record_id=d.id and entry.document_id=?)';params+=(document,)
        if source_id:
            clause+=" and json_extract(d.data,'$.sourceId')=?";params+=(source_id,)
        if version:
            if not document or version<1:fail('Для исторической версии выберите документ',422)
            clause+=' and exists(select 1 from decision_entries entry join decision_versions intro on intro.id=entry.version_id where entry.record_id=d.id and intro.number<=?) and not exists(select 1 from decision_retirements retired join decision_versions ended on ended.id=retired.version_id where retired.record_id=d.id and ended.number<=?)';params+=(version,version)
        elif not history:clause+=' and '+ACTIVE
        return clause,params

    @app.get('/api/decisions/totals')
    def decision_totals(request:Request,site:str='',documentId:str='',version:int=0):
        actor=auth(request);clause,params=decision_scope(actor,site,documentId,version=version);values=total();count=0
        with db.connect(actor) as con:
            for row in db.iterate(con,'select d.data from decision_records d where '+clause,params):add(values,json.loads(row['data']));count+=1
        return {'rows':count,'totals':encoded_total(values),'scope':'historical' if version else 'active'}

    @app.get('/api/decision-documents')
    def decision_documents(request:Request,site:str='',q:str='',page:int=1,limit:int=50):
        actor=auth(request)
        if page<1 or not 1<=limit<=100 or len(q)>200:fail('Некорректная страница или поиск')
        with db.connect(actor) as con:
            clause="(?='manager' or exists(select 1 from user_sites us where us.user_id=? and us.site_id=doc.site_id)) and (?='' or doc.site_id=?) and (?='' or doc.label like ?)"
            params=(actor['role'],actor['id'],site,site,q,'%'+q+'%')
            count=con.execute('select count(*) from decision_documents doc where '+clause,params).fetchone()[0]
            rows=con.execute('''select doc.*,(select max(number) from decision_versions where document_id=doc.id) as version,
              (select count(*) from decision_entries entry join decision_records d on d.id=entry.record_id where entry.document_id=doc.id and '''+ACTIVE+''') as active_rows
              from decision_documents doc where '''+clause+' order by doc.created_at desc,doc.id limit ? offset ?',params+(limit,(page-1)*limit)).fetchall()
        return {'total':count,'page':page,'rows':[dict(row) for row in rows]}

    @app.get('/api/decision-documents/{ident}/versions')
    def document_versions(ident:str,request:Request,before:int=2**31-1):
        actor=auth(request)
        with db.connect(actor) as con:
            document=con.execute('select * from decision_documents where id=?',(ident,)).fetchone()
            if not document or not accessible(con,actor,document['site_id']):fail('Документ не найден',404)
            rows=con.execute('select v.*,u.name as actor from decision_versions v join users u on u.id=v.author_id where document_id=? and number<? order by number desc limit 100',(ident,before)).fetchall()
        return [{k:v for k,v in dict(row).items() if k!='summary_json'}|{'summary':json.loads(row['summary_json'])} for row in rows]

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
                for row in db.iterate(con,'select d.data,d.sheet,d.source_row from decision_records d where '+clause+' order by '+db.decision_order+',d.sheet,d.source_row,d.id',params):
                    item=json.loads(row['data'])
                    writer.writerow([safe_cell(item.get(k) or '') for k in FIELDS]+[safe_cell(row['sheet']),row['source_row']])
                    yield output.getvalue().encode('utf-8');output.seek(0);output.truncate(0)
        return StreamingResponse(data(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="decisions.csv"'})

    @app.get('/api/decisions')
    def decisions(request:Request,page:int=1,limit:int=50,site:str='',documentId:str='',history:bool=False,version:int=0,sourceId:str=''):
        actor=auth(request)
        if page<1 or not 1<=limit<=100:fail('Некорректная страница или размер страницы')
        clause,params=decision_scope(actor,site,documentId,history,version,sourceId)
        with db.connect(actor) as con:
            con.execute('begin')
            count=con.execute('select count(*) from decision_records d where '+clause,params).fetchone()[0]
            rows=con.execute('select d.*,entry.document_id,intro.number as version,case when '+ACTIVE+' then 1 else 0 end as active from decision_records d left join decision_entries entry on entry.record_id=d.id left join decision_versions intro on intro.id=entry.version_id where '+clause+' order by '+db.decision_order+' desc,d.source_row desc,d.id limit ? offset ?',params+(limit,(page-1)*limit)).fetchall()
        return {'page':page,'limit':limit,'total':count,'scope':'historical' if history or version else 'active','rows':[{'id':r['id'],'sourceRow':r['source_row'],'sheet':r['sheet'],'documentId':r['document_id'],'version':r['version'],'active':bool(r['active']),
                 **{k:v for k,v in json.loads(r['data']).items() if k not in ('sourceCells','unmappedCells')}} for r in rows]}

    @app.get('/api/decisions/{ident}')
    def decision_detail(ident:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:
            row=con.execute('select * from decision_records where id=?',(ident,)).fetchone()
            if not row or not accessible(con,actor,row['site_id']):fail('Запись не найдена',404)
            provenance=json.loads(db.import_metadata(con,row['import_hash']))
            entry=con.execute('select e.document_id,v.number as version from decision_entries e join decision_versions v on v.id=e.version_id where record_id=?',(ident,)).fetchone()
            retired=con.execute('select version_id from decision_retirements where record_id=?',(ident,)).fetchone()
        return {'importStyles':provenance.get('styles',[]),'id':row['id'],'sourceRow':row['source_row'],'sheet':row['sheet'],'fileSha256':row['file_sha'],'importHash':row['import_hash'],'data':json.loads(row['data']),
                'documentId':entry['document_id'] if entry else None,'version':entry['version'] if entry else None,'active':not retired,'retiredByVersion':retired['version_id'] if retired else None}

    @app.post('/api/import/{action}')
    def imports(action:str,payload:ImportFile,request:Request):
        actor=auth(request,True)
        data=payload.model_dump(exclude_none=True)
        if action=='preview':return parse_import(db,actor,data)[0]
        if action=='commit':return commit_import(db,actor,data)
        fail('Метод не найден',404)

    @app.post('/api/inventory-plan/preview')
    def preview_inventory_plan(payload:ImportFile,request:Request):
        """Preview a source workbook without changing production records."""
        actor=auth(request,True)
        if actor['role']=='viewer':fail('Только просмотр',403)
        from .inventory_plan import parse_plan,PlanError
        import base64,binascii
        try:raw=base64.b64decode(payload.content,validate=True)
        except (ValueError,binascii.Error):fail('Неверные данные файла')
        try:return parse_plan(raw,payload.name,mapping=payload.mapping)
        except PlanError as error:fail(str(error))

    @app.post('/api/inventory-plan/commit')
    def import_inventory_plan(payload:InventoryPlanUpload,request:Request):
        actor=auth(request,True)
        from .inventory_workflow import create_plan
        return create_plan(db,actor,payload)

    @app.patch('/api/inventory-lines/{ident}/count')
    def save_inventory_fact(ident:str,payload:InventoryCountUpdate,request:Request):
        actor=auth(request,True)
        from .inventory_workflow import count_line
        return count_line(db,actor,ident,payload)

    @app.post('/api/inventories/{ident}/finalize')
    def finalize_inventory_plan(ident:str,payload:InventoryFinalize,request:Request):
        actor=auth(request,True)
        from .inventory_workflow import finalize_inventory
        return finalize_inventory(db,actor,ident,payload)

    @app.get('/api/inventories/{ident}/statement.{extension}')
    def inventory_statement(ident:str,extension:str,request:Request):
        """Per-inventory PDF/XLSX, server-filtered by the authenticated user's site RLS."""
        if extension not in ('pdf','xlsx'):fail('Формат не поддерживается',404)
        actor=auth(request)
        with db.connect(actor) as con:state=db.snapshot(con,actor)
        inventory=next((item for item in state['inventories'] if item['id']==ident),None)
        if inventory is None:fail('Инвентаризация недоступна',404)
        site=next((s for s in state['sites'] if s['id']==inventory['siteId']),None)
        if site is None:fail('Объект недоступен',404)
        lines=sorted((line for line in state['inventoryLines'] if line['inventoryId']==ident),
                     key=lambda line:(line.get('sourceRow',0),line['id']))
        from .inventory_statement import summarize_inventory,export_inventory_xlsx,export_inventory_pdf
        statement=summarize_inventory(lines)
        source={'site':site['name'],'title':inventory['title'],'date':inventory['date'],
                'status':inventory['status']}
        output=(export_inventory_xlsx(source,statement) if extension=='xlsx'
                else export_inventory_pdf(source,statement))
        mime=('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
              if extension=='xlsx' else 'application/pdf')
        return Response(output,media_type=mime,headers={'Cache-Control':'no-store',
             'Content-Disposition':f'attachment; filename="ksu-inventory-statement.{extension}"'})

    @app.get('/api/reports/{extension}')
    def reports(extension:str,request:Request):
        actor=auth(request)
        with db.connect(actor) as con:state=db.snapshot(con,actor)
        if actor['role']=='viewer':
            site=request.query_params.get('site','all')
            if site!='all' and site not in {row['id'] for row in state['sites']}:fail('Нет доступа к объекту отчёта',404)
            if request.query_params.get('kind')=='inventory-detail':
                ident=request.query_params.get('inventoryId','')
                inventory=next((row for row in state['inventories'] if row['id']==ident),None)
                if not inventory:fail('Инвентаризация недоступна',404)
                report={'headers':['ТМЦ','Артикул','Категория','Единица','Учёт','Факт','Цена'],
                        'rows':[[row.get(key,'') for key in ('item','sku','category','unit','expected','actual','price')] for row in state['inventoryLines'] if row['inventoryId']==ident],
                        'period':inventory['date'],'kind':'inventory-detail'}
                if extension=='json':return report
                content,mime=export(report,extension)
                return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="inventory.{extension}"'})
        report=build_report(state,dict(request.query_params))
        if extension=='json':return report
        content,mime=export(report,extension)
        prefix='instrakt' if release_policy else 'ksu-demo'
        return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="{prefix}-{report["kind"]}.{extension}"'})

    # Only explicit public assets; never serve database, backups, SQL, tests or source directories.
    from .writeoff_analytics import install as install_writeoff_analytics
    install_writeoff_analytics(app,db,auth)
    from .writeoff_release import install as install_writeoff_release
    install_writeoff_release(app,db,auth)
    def make_asset(filename):
        def asset():
            if release_policy and filename in ('decisions.js','calendar.js'):return Response(status_code=404)
            if release_policy and filename=='writeoff_analytics.js' and os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off')!='on':return Response(status_code=404)
            if release_policy and filename=='index.html' and os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off')!='on':
                return Response((ROOT/'release_online/index.html').read_text().replace('<script src="writeoff_analytics.js" defer></script>',''),media_type='text/html')
            return FileResponse(ROOT/'release_online'/filename if release_policy and (ROOT/'release_online'/filename).is_file() else ROOT/filename)
        return asset
    for route,filename in [('/checklists.js','release_online/checklists.js'),('/viewer.js','release_online/viewer.js'),('/assets/instrakt-logo.svg','assets/instrakt-logo.svg'),('/','index.html'),('/index.html','index.html'),('/app.js','app.js'),('/sync.js','sync.js'),('/decisions.js','decisions.js'),('/calendar.js','calendar.js'),('/writeoff_analytics.js','writeoff_analytics.js'),('/inventory-count.js','inventory-count.js'),('/styles.css','styles.css')]:
        app.add_api_route(route,make_asset(filename),methods=['GET'],include_in_schema=False)
    if release_policy:
        from .offline_release import install as install_release
        install_release(app,auth,release_policy)
    elif os.getenv('KSU_OFFLINE_VISIT_LAB_ID'):
        from .offline_visit import install as install_offline_visit
        install_offline_visit(app,auth,os.environ['KSU_OFFLINE_VISIT_LAB_ID'])
    return app
