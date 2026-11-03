"""Full real PostgreSQL load acceptance. Private fixtures; only aggregate JSON is published."""
import base64
import csv
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
import zipfile

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from PIL import Image
from openpyxl import Workbook,load_workbook
from experiments.large_xlsx.generate import generate
from experiments.postgres_large.resources import Resources,size
from server.database import Database,seed_demo,password_hash
from server.decision_import import FIELDS
from server.pg_admin import initialize,migrate_sqlite,fingerprints
from server.pg_backup import checksum,restore_database
from server.xlsx_stream import Reader
from tests.test_postgres import database_dsn

CAP=268435456
FINANCE=('quantity','cost','residual','reimbursable','reimbursed')
SETTINGS={'mapping':{field:chr(65+i) for i,field in enumerate(FIELDS)},'allowCached':True}


def tiny_book(path,cost,source_id='0000000001'):
    book=Workbook();sheet=book.active;sheet.title='DEMO version';sheet.append(list(FIELDS.values()))
    sheet.append([source_id,'2026-11-01','ДЕМО · Нова','ДЕМО · Акт','ДЕМО · Списание','ДЕМО · ТМЦ','1',cost,'5','ДЕМО','ДЕМО','ДЕМО','ДЕМО','0','0','ДЕМО','ДЕМО · Исправление'])
    book.save(path)
    return path


def require(response):
    assert response.status_code==200,('HTTP',response.status_code,response.text[:180])
    return response.json()


class Case:
    def __init__(self,root,name,resources,restoring=None):
        self.root=Path(root)/name;self.root.mkdir();self.resources=resources;self.name=name
        prefix='KSU_TEST_PG_RESTORE_' if restoring else 'KSU_TEST_PG_'
        self.root_admin=os.environ[prefix+'ADMIN_DSN']
        self.admin=database_dsn(self.root_admin,'ksu_load_'+uuid4().hex)
        self.dbname=conninfo_to_dict(self.admin)['dbname']
        self.runtime=database_dsn(os.environ[prefix+'RUNTIME_DSN'],self.dbname)
        self.backup=database_dsn(os.environ['KSU_TEST_PG_BACKUP_DSN'],self.dbname)
        if restoring:
            info=conninfo_to_dict(self.backup);info.update({k:v for k,v in conninfo_to_dict(self.admin).items() if k not in ('user','password')})
            self.backup=make_conninfo(**info)
        self.passwords=restoring[1].passwords if restoring else {r:secrets.token_urlsafe(24) for r in ('manager','inspector','limited')}
        self.clients={};self.server=None;self.log=None;self.health_stop=threading.Event();self.health_pause=threading.Event();self.health={};self.phase='setup'
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.dbname)))
        if restoring:
            start=time.monotonic();self.restored=restore_database(restoring[0],self.admin,'ksu_runtime','ksu_backup');self.restore_seconds=round(time.monotonic()-start,3)
        else:
            source=self.root/'seed.sqlite3';db=Database(source);seed_demo(db,self.passwords['manager'],self.passwords['inspector'])
            with db.connect() as con:
                con.execute('insert into users values(?,?,?,?,?,?)',('limited','limited','ДЕМО · Ограниченный инспектор','inspector',password_hash(self.passwords['limited']),1))
                con.execute("insert into user_sites values('limited','s1')")
            initialize(self.admin,'ksu_runtime','ksu_backup');migrate_sqlite(source,self.admin)
        self.start()
        self.health_thread=threading.Thread(target=self.monitor_health,daemon=True);self.health_thread.start()
    def mark(self,phase):
        self.phase=phase;self.resources.phase=self.name+':'+phase
        print('PHASE:',self.resources.phase,flush=True)
    def start(self,timeout=240):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.origin=f'http://127.0.0.1:{port}'
        env={**os.environ,'KSU_DATABASE_URL':self.runtime,'KSU_WORK_DIR':str(self.root/'work'),
             'KSU_PG_BACKUP_DSN':self.backup,'KSU_PG_BACKUP_DIR':str(self.root/'exports'),'KSU_ORIGIN':self.origin,
             'KSU_SECURE_COOKIES':'0','KSU_REQUIRE_INITIALIZED':'1','KSU_IMPORT_PHASE_TIMEOUT_SECONDS':str(timeout)}
        self.log=(self.root/'server.log').open('a')
        self.server=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],env=env,stdout=self.log,stderr=self.log)
        with httpx.Client(trust_env=False) as client:
            for _ in range(200):
                try:
                    if client.get(self.origin+'/api/health',timeout=.3).status_code==200:break
                except httpx.TransportError:pass
                time.sleep(.1)
            else:raise AssertionError('Local API failed to start')
        for role in self.passwords:
            client=httpx.Client(base_url=self.origin,headers={'Origin':self.origin},trust_env=False,timeout=600)
            meta=require(client.post('/api/login',json={'username':role,'password':self.passwords[role]}));client.headers['X-CSRF-Token']=meta['csrf'];self.clients[role]=client
    def stop_server(self):
        self.health_pause.set()
        for client in self.clients.values():client.close()
        self.clients={}
        if self.server:
            self.server.terminate()
            try:self.server.wait(timeout=20)
            except subprocess.TimeoutExpired:self.server.kill();self.server.wait(timeout=10)
            self.server=None
        if self.log:self.log.close();self.log=None
    def restart(self,timeout=240):self.stop_server();self.start(timeout);self.health_pause.clear()
    def close(self):
        self.health_stop.set();self.health_thread.join(timeout=10);self.stop_server()
        with psycopg.connect(self.root_admin,autocommit=True) as con:con.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.dbname)))
    def monitor_health(self):
        with httpx.Client(trust_env=False,timeout=5) as client:
            while not self.health_stop.wait(.5):
                if self.health_pause.is_set():continue
                sample=self.health.setdefault(self.phase,{'samples':0,'failures':0,'max_seconds':0})
                t=time.monotonic()
                try:response=client.get(self.origin+'/api/health');assert response.status_code==200
                except (httpx.TransportError,AssertionError):
                    if self.health_pause.is_set():continue
                    sample['failures']+=1
                sample['samples']+=1;sample['max_seconds']=max(sample['max_seconds'],round(time.monotonic()-t,3))
    def wait(self,ident,role='inspector'):
        start=time.monotonic()
        while time.monotonic()-start<270:
            meta=require(self.clients[role].get('/api/decision-imports/'+ident))
            if meta['status'] not in ('uploading','inspecting','checking','committing'):return meta
            time.sleep(.35)
        raise AssertionError('Worker/HTTP status deadline exceeded')
    def upload(self,book,role='inspector'):
        with open(book,'rb') as handle:
            def chunks():
                while chunk:=handle.read(65536):yield chunk
            meta=require(self.clients[role].post('/api/decision-imports',content=chunks(),headers={'X-KSU-Filename':'DEMO.xlsx','Content-Type':'application/octet-stream'}))
        meta=self.wait(meta['id'],role);assert meta['status']=='uploaded',meta.get('message');return meta['id']
    def preview(self,ident,role='inspector'):
        require(self.clients[role].post('/api/decision-imports/'+ident+'/preview',json=SETTINGS));return self.wait(ident,role)
    def commit(self,ident,role='inspector'):
        require(self.clients[role].post('/api/decision-imports/'+ident+'/commit'));return self.wait(ident,role)
    def counts(self):
        with psycopg.connect(self.admin) as con:return {t:con.execute('select count(*) from ksu.'+t).fetchone()[0] for t in ('decision_records','imports','audit')}
    def stats(self):
        with psycopg.connect(self.admin) as con:
            result={'database_bytes':con.execute('select pg_database_size(current_database())').fetchone()[0],
                    'wal_disk_bytes':int(con.execute('select coalesce(sum(size),0) from pg_ls_waldir()').fetchone()[0]),
                    'wal_lsn':str(con.execute('select pg_current_wal_lsn()').fetchone()[0]),'tables':{}}
            for table in ('decision_records','audit','imports'):
                total,heap,indexes=con.execute('select pg_total_relation_size(%s),pg_relation_size(%s),pg_indexes_size(%s)',('ksu.'+table,)*3).fetchone()
                result['tables'][table]={'total_bytes':total,'heap_bytes':heap,'index_bytes':indexes,'toast_and_aux_bytes':total-heap-indexes}
            result['counts']=self.counts()
            return result
    def import_book(self,book,expected,repeat=True,allow_cap=False):
        metrics={'file_bytes':Path(book).stat().st_size,'expected_rows':expected['rows']-1};before=self.counts()
        self.mark('upload');start=time.monotonic();ident=self.upload(book);metrics['upload_seconds']=round(time.monotonic()-start,3)
        assert self.clients['manager'].get('/api/decision-imports/'+ident).status_code==404
        self.mark('preview');start=time.monotonic();meta=self.preview(ident);metrics['preview_seconds']=round(time.monotonic()-start,3)
        assert meta['status']=='ready',(meta['status'],meta.get('message'),meta.get('errors',[])[:1]);assert meta['accepted']==expected['rows']-1
        assert self.counts()==before
        metrics['normalized_bytes']=meta['normalizedBytes'];metrics['admission_estimate_bytes']=meta['normalizedBytes']*3+meta['accepted']*192+2*1024*1024
        metrics['before']=self.stats();self.mark('commit');start=time.monotonic();done=self.commit(ident);metrics['commit_seconds']=round(time.monotonic()-start,3)
        if done['status']!='done':
            assert allow_cap and done['status']=='error' and 'cap reached' in done.get('message',''),done.get('message')
            assert self.counts()==before;metrics.update(outcome='cap_rejected',message=done['message'],rollback=True,after=self.stats());return metrics
        metrics.update(outcome='committed',after=self.stats());assert done['committed']==expected['rows']-1
        assert metrics['after']['counts']['decision_records']-before['decision_records']==expected['rows']-1
        self.mark('verify-all');metrics['verification']=self.verify(book,expected)
        if repeat:
            counts=self.counts();self.mark('repeat');repeat_id=self.upload(book);assert self.preview(repeat_id)['status']=='ready';again=self.commit(repeat_id)
            assert again['status']=='done' and again['duplicate'];assert self.counts()==counts;metrics['repeat_no_duplicate']=True
        print('PASS:',self.name,metrics['outcome'],metrics['expected_rows'],'rows',flush=True)
        return metrics
    def verify(self,book,expected):
        file_sha=checksum(book);digest=hashlib.sha256();reader_hash=hashlib.sha256();totals={field:Decimal(0) for field in FINANCE};checked=0
        with psycopg.connect(self.admin) as con,Reader(book,self.root/('verify-'+uuid4().hex)) as reader:
            with con.cursor(name='verify_source') as cursor:
                cursor.itersize=64;cursor.execute('select source_row,data from ksu.decision_records where file_sha=%s order by source_row',(file_sha,))
                rows=iter(cursor)
                for number,cells in reader.rows(allow_cached=True):
                    reader_hash.update(json.dumps([number,cells],sort_keys=True,ensure_ascii=False).encode())
                    if number==1:continue
                    actual_number,encoded=next(rows);record=json.loads(encoded);assert actual_number==number
                    values=[record[field] for field in FIELDS]
                    digest.update(json.dumps([number,values],ensure_ascii=False,separators=(',',':')).encode()+b'\n')
                    assert record['sourceId']==cells['A']['value'].zfill(10)
                    assert record['sourceCells']['sourceId']['original']==cells['A']['value']
                    for field in FINANCE:totals[field]+=Decimal(record[field])
                    checked+=1
                assert next(rows,None) is None
            assert con.execute("select count(*) from ksu.audit a join ksu.decision_records d on a.record_id=d.id and a.collection='decisions' where d.file_sha=%s and a.actor_id='inspector' and a.operation='import' and a.after_json=d.data and a.before_json is null",(file_sha,)).fetchone()[0]==checked
        assert reader_hash.hexdigest()==expected['digest']
        assert digest.hexdigest()==expected['projection_digest']
        assert {k:Decimal(v) for k,v in expected['totals'].items()}==totals
        return {'rows':checked,'all_fields_and_comments_match':True,'leading_zero_ids_match':True,'every_audit_present':True,
                'digest':digest.hexdigest(),'totals':{k:format(v,'f') for k,v in totals.items()}}

    def change(self,role,collection,data,ident,version=0):
        return self.clients[role].post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
            {'collection':collection,'id':ident,'expectedVersion':version,'data':data}]})

    def operational(self):
        self.mark('operational')
        image=io.BytesIO();Image.new('RGB',(32,32),'blue').save(image,format='PNG')
        photo=require(self.clients['inspector'].post('/api/media',json={'siteId':'s2','content':base64.b64encode(image.getvalue()).decode()}))['url']
        require(self.change('inspector','findings',{'siteId':'s2','title':'ДЕМО · Фото после восстановления','photo':photo},'DEMO-photo'))
        visit={'siteId':'s1','date':'2026-12-01','goal':'ДЕМО · Приёмка'}
        require(self.change('inspector','visits',visit,'DEMO-visit'));visit['date']='2026-12-02'
        require(self.change('inspector','visits',visit,'DEMO-visit',1))
        assert self.change('manager','visits',visit,'DEMO-visit',1).status_code==409
        require(self.change('inspector','inventories',{'siteId':'s1','date':'2026-11-01','title':'ДЕМО · Финансы'},'DEMO-inventory'))
        require(self.change('inspector','inventoryLines',{'inventoryId':'DEMO-inventory','item':'ДЕМО · ТМЦ','expected':7,'actual':3,'price':10.125},'DEMO-line'))
        return {'photo':photo,'photo_sha256':hashlib.sha256(self.clients['manager'].get(photo).content).hexdigest(),
                'reports':self.reports(),'version':2}

    def reports(self):
        result={}
        for role in ('manager','inspector'):
            client=self.clients[role];report=require(client.get('/api/reports/json?kind=inventory&site=s1'))
            assert report['rows'][0][2]==40.5
            for extension in ('csv','xlsx','pdf'):
                response=client.get('/api/reports/'+extension+'?kind=inventory&site=s1');assert response.status_code==200
                if extension=='csv':assert '40.5' in response.text
                elif extension=='xlsx':
                    with io.BytesIO(response.content) as stream:
                        book=load_workbook(stream,read_only=True,data_only=True);assert book.active['C2'].value==40.5;book.close()
                else:assert response.content.startswith(b'%PDF')
            result[role]=report
        return result

    def reads(self):
        self.mark('register-read');result={};total=self.counts()['decision_records'];expected_sites={}
        with psycopg.connect(self.admin) as con:
            expected_sites=dict(con.execute('select site_id,count(*) from ksu.decision_records group by site_id').fetchall())
        for role in ('manager','inspector','limited'):
            client=self.clients[role];count=expected_sites.get('s1',0) if role=='limited' else total
            timings=[]
            for page in sorted({1,max(1,(count//2)//100+1),max(1,(count+99)//100)}):
                start=time.monotonic();data=require(client.get('/api/decisions',params={'page':page,'limit':100}));timings.append(time.monotonic()-start)
                assert data['total']==count and len(data['rows'])==min(100,count-(page-1)*100)
                assert all(row['siteId']=='s1' for row in data['rows']) if role=='limited' else True
            for site in ('s1','s2'):
                data=require(client.get('/api/decisions',params={'site':site,'limit':100}))
                assert data['total']==(0 if role=='limited' and site=='s2' else expected_sites.get(site,0))
            response=client.get('/api/state');state=require(response);assert 'decisions' not in state and len(response.content)<100000
            result[role]={'rows':count,'page_max_seconds':round(max(timings),3),'state_bytes':len(response.content)}
        foreign=require(self.clients['manager'].get('/api/decisions?site=s2&limit=1'))['rows']
        if foreign:assert self.clients['limited'].get('/api/decisions/'+foreign[0]['id']).status_code==404
        assert self.clients['inspector'].post('/api/admin/backup').status_code==403
        assert self.clients['manager'].post('/api/admin/backup',headers={'X-CSRF-Token':'invalid'}).status_code==403
        with httpx.Client(base_url=self.origin,trust_env=False) as client:
            for path in ('/api/decisions','/api/history','/api/decisions/export.csv'):assert client.get(path).status_code==401
        with psycopg.connect(self.runtime) as con:
            con.execute("select set_config('ksu.actor','limited',true)")
            assert con.execute('select count(*) from ksu.decision_records').fetchone()[0]==expected_sites.get('s1',0)
            assert con.execute("select count(*) from ksu.audit where collection='decisions'").fetchone()[0]==0
        return result

    def scope_import(self,book):
        self.mark('import-scope');before=self.counts();ident=self.upload(book,role='limited');meta=self.preview(ident,role='limited')
        assert meta['status']=='invalid' and meta['accepted']==0 and meta['rejected']>10000
        assert self.counts()==before
        return {'unauthorized_site_all_rows_rejected':meta['rejected'],'no_writes':True}

    def history(self,role='manager'):
        self.mark('full-history');start=time.monotonic();before=2**63-1;count=0;digest=hashlib.sha256();pages=0
        with psycopg.connect(self.admin) as con:
            # All large books are imported by inspector; the limited user owns none.
            expected=con.execute("select count(*) from ksu.audit where collection='decisions'").fetchone()[0] if role!='limited' else 0
        while True:
            rows=require(self.clients[role].get('/api/history',params={'collection':'decisions','before':before}))
            assert len(rows)<=100
            if not rows:break
            for row in rows:
                assert row['seq']<before and row['operation']=='import' and row['actor_id']=='inspector'
                assert json.loads(row['after_json'])==row['after'];before=row['seq']
                digest.update(json.dumps([row['seq'],row['record_id'],row['after_json']],ensure_ascii=False,separators=(',',':')).encode()+b'\n')
                count+=1
            pages+=1
        assert count==expected
        return {'rows':count,'pages':pages,'seconds':round(time.monotonic()-start,3),'digest':digest.hexdigest()}

    def export(self,expected,role='manager',site=''):
        self.mark('export');start=time.monotonic();count=0;totals={field:Decimal(0) for field in FINANCE};digest=hashlib.sha256()
        with self.clients[role].stream('GET','/api/decisions/export.csv',params={'site':site}) as response:
            assert response.status_code==200
            lines=iter(response.iter_lines());headers=next(lines).lstrip('\ufeff').split(';');assert headers[:len(FIELDS)]==list(FIELDS.values())
            for row in csv.reader(lines,delimiter=';'):
                assert len(row)==len(FIELDS)+2
                for field in FINANCE:totals[field]+=Decimal(row[list(FIELDS).index(field)])
                assert len(row[0])==10 and row[0].isdigit()
                digest.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
        assert count==sum(e['rows']-1 for e in expected)
        assert totals=={f:sum((Decimal(e['totals'][f]) for e in expected),Decimal(0)) for f in FINANCE}
        return {'rows':count,'seconds':round(time.monotonic()-start,3),'digest':digest.hexdigest(),'totals':{k:format(v,'f') for k,v in totals.items()}}

    def backup_http(self,label):
        self.mark('backup');target=self.root/(label+'.ksubackup');start=time.monotonic()
        with self.clients['manager'].stream('POST','/api/admin/backup') as response:
            assert response.status_code==200
            with target.open('xb') as handle:
                for chunk in response.iter_bytes(1024*1024):handle.write(chunk)
            assert checksum(target)==response.headers['X-Backup-SHA256']
        with zipfile.ZipFile(target) as package:manifest=json.loads(package.read('manifest.json'))
        return target,{'seconds':round(time.monotonic()-start,3),'bytes':target.stat().st_size,'sha256':checksum(target),'tables':manifest['tables']}

    def failures(self,book):
        self.mark('fault-injection');before=self.counts();ident=self.upload(book);assert self.preview(ident)['status']=='ready'
        with psycopg.connect(self.admin) as con:
            con.execute("create function ksu.synthetic_failure() returns trigger language plpgsql as $$ begin if NEW.source_row=500 then raise exception 'synthetic failure' using errcode='23514'; end if; return NEW; end $$")
            con.execute('create trigger synthetic_failure before insert on ksu.decision_records for each row execute function ksu.synthetic_failure()')
        assert self.commit(ident)['status']=='error';assert self.counts()==before;rollback_stats=self.stats()
        with psycopg.connect(self.admin) as con:
            con.execute('drop trigger synthetic_failure on ksu.decision_records');con.execute('drop function ksu.synthetic_failure()')
        # Preview again recovers the same job without a partial header or audit.
        assert self.preview(ident)['status']=='ready'
        assert self.commit(ident)['status']=='done'
        committed=self.counts();assert committed['decision_records']==11095
        ident=self.upload(book)
        require(self.clients['inspector'].post('/api/decision-imports/'+ident+'/preview',json=SETTINGS))
        killed=False
        for _ in range(100):
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit():continue
                try:
                    cmd=(proc/'cmdline').read_bytes();stat=(proc/'stat').read_text().split()
                    if int(stat[3])==self.server.pid and b'server.decision_import' in cmd:
                        os.kill(int(proc.name),signal.SIGKILL);killed=True;break
                except (FileNotFoundError,ProcessLookupError):continue
            if killed:break
            time.sleep(.01)
        assert killed and self.wait(ident)['status']=='error';assert self.counts()==committed
        ident=self.upload(book);self.restart(timeout=1)
        timed=self.preview(ident);assert timed['status']=='error';assert self.counts()==committed
        self.restart();ident=self.upload(book);assert self.preview(ident)['status']=='ready';assert self.commit(ident)['duplicate']
        return {'mid_commit_rollback_at_row':500,'recovered_rows':11095,'worker_sigkill_recovered':True,
                'lowered_one_second_timeout_recovered':True,'actual_oom_injected':False,'after_rollback':rollback_stats,'counts_after_failures':committed}

    def corrected_version(self):
        self.mark('corrected-version');original=None
        for cost in ('10.0000','12.0000'):
            path=tiny_book(self.root/('DEMO-'+cost+'.xlsx'),cost)
            ident=self.upload(path);assert self.preview(ident)['status']=='ready';assert self.commit(ident)['committed']==1
            if original is None:original=checksum(path)
            else:assert original!=checksum(path)
        rows=require(self.clients['manager'].get('/api/decisions'))
        assert rows['total']==2 and len({row['sourceId'] for row in rows['rows']})==1
        assert sum(Decimal(row['cost']) for row in rows['rows'])==Decimal(22)
        return {'new_sha_same_source_id_keeps_both_versions':True,'rows':2,'naive_sum':'22.0000',
                'intended_latest_cost':'12.0000','kpi_release_blocked_pending_version_policy':True}


def run(output):
    metrics={'synthetic_only':True,'database_cap_bytes':CAP,'cap_raised':False};cases=[]
    with tempfile.TemporaryDirectory(prefix='pg-large-',dir=os.environ['KSU_TEST_DATA_DIR']) as directory:
        root=Path(directory);resources=Resources(root)
        try:
            resources.phase='generate';books={};expected={}
            for label,target,variant,site,shared in [('18',18000000,1,'ДЕМО · Нова',False),('30',30000000,2,'ДЕМО · Дом Горизонтов',True),('18b',18000000,3,'ДЕМО · Дом Горизонтов',False)]:
                books[label]=root/('DEMO-'+label+'.xlsx');expected[label]=generate(books[label],target,shared,variant=variant,site=site,rich=True,scratch=root)
            metrics['fixtures']={k:{f:v for f,v in e.items() if f!='site'} for k,e in expected.items()}
            a=Case(root,'standalone18',resources);cases.append(a);metrics['18']=a.import_book(books['18'],expected['18'])
            metrics['18_then_30']=a.import_book(books['30'],expected['30'],allow_cap=True)
            metrics['18_then_18b']=a.import_book(books['18b'],expected['18b'])
            metrics['18_18b_then_30']=a.import_book(books['30'],expected['30'],allow_cap=True)
            b=Case(root,'standalone30',resources);cases.append(b);metrics['30']=b.import_book(books['30'],expected['30'])
            metrics['30_then_18']=b.import_book(books['18'],expected['18'],allow_cap=True)
            b.stop_server()
            metrics['operational']=a.operational()
            metrics['import_scope']=a.scope_import(books['18b'])
            metrics['reads']=a.reads();metrics['history']={role:a.history(role) for role in ('manager','inspector','limited')}
            metrics['export']=a.export([expected['18'],expected['18b']])
            metrics['scoped_export']=a.export([expected['18']],role='limited',site='s1')
            a.mark('fingerprints')
            with psycopg.connect(a.admin) as con:original=fingerprints(con,True)
            first,metrics['backup_first']=a.backup_http('first');second,metrics['backup_repeat']=a.backup_http('second')
            assert metrics['backup_first']['tables']==metrics['backup_repeat']['tables']==original
            old_cookie=a.clients['manager'].build_request('GET','/api/me').headers['cookie']
            a.stop_server()
            resources.phase='independent-restore';restored=Case(root,'independent-restored',resources,restoring=(second,a));cases.append(restored)
            assert restored.restored['tables']==original
            metrics['restore']={'seconds':restored.restore_seconds,'tables':restored.restored['tables'],'sessions_revoked':restored.restored['sessions_revoked'],'stats':restored.stats()}
            with httpx.Client(trust_env=False) as client:assert client.get(restored.origin+'/api/me',headers={'Cookie':old_cookie}).status_code==401
            assert restored.clients['manager'].get(metrics['operational']['photo']).status_code==200
            assert hashlib.sha256(restored.clients['manager'].get(metrics['operational']['photo']).content).hexdigest()==metrics['operational']['photo_sha256']
            assert restored.clients['limited'].get(metrics['operational']['photo']).status_code==404
            assert restored.reports()==metrics['operational']['reports']
            metrics['restored_reads']=restored.reads()
            metrics['restored_history']={role:restored.history(role) for role in ('manager','inspector','limited')}
            assert {role:data['digest'] for role,data in metrics['history'].items()}=={role:data['digest'] for role,data in metrics['restored_history'].items()}
            exported=restored.export([expected['18'],expected['18b']]);assert exported['digest']==metrics['export']['digest'];metrics['restored_export']=exported
            metrics['restored_rows']={label:restored.verify(books[label],expected[label]) for label in ('18','18b')}
            for label in ('18','18b'):
                before=restored.counts();ident=restored.upload(books[label]);assert restored.preview(ident)['status']=='ready';assert restored.commit(ident)['duplicate'];assert restored.counts()==before
            visit=next(v for v in require(restored.clients['manager'].get('/api/state'))['visits'] if v['id']=='DEMO-visit');assert visit['version']==2
            visit['date']='2026-12-03';require(restored.change('inspector','visits',visit,'DEMO-visit',2))
            assert restored.change('manager','visits',visit,'DEMO-visit',2).status_code==409
            small=tiny_book(restored.root/'DEMO-new.xlsx','14.1250','0000999999');before=restored.counts()
            ident=restored.upload(small);assert restored.preview(ident)['status']=='ready';assert restored.commit(ident)['committed']==1
            assert restored.counts()['decision_records']==before['decision_records']+1
            metrics['continuation']={'repeat_no_duplicates':True,'visit_version_3':True,'new_import_rows':1,'old_session_401':True,'stale_version_409':True,'first_backup_preserved':checksum(first)==metrics['backup_first']['sha256']}
            _,metrics['backup_after_restore']=restored.backup_http('after-restore')
            with psycopg.connect(restored.admin) as con:assert metrics['backup_after_restore']['tables']==fingerprints(con,True)
            restored.stop_server()
            fault=Case(root,'faults',resources);cases.append(fault);metrics['faults']=fault.failures(books['18']);fault.stop_server()
            version=Case(root,'version-probe',resources);cases.append(version);metrics['corrected_version']=version.corrected_version()
            for case in cases:assert not any(p['failures'] for p in case.health.values()),case.health
            metrics['health']={case.name:case.health for case in cases}
        except Exception as error:
            metrics['failure']={'class':type(error).__name__,'message':str(error)[:500]}
            raise
        finally:
            cleanup=[]
            for case in reversed(cases):
                try:case.close()
                except Exception as error:cleanup.append(type(error).__name__)
            metrics['cleanup_errors']=cleanup
            metrics['health']={case.name:case.health for case in cases}
            metrics['app_resources']=resources.finish()
            Path(output).write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
        assert not cleanup,cleanup
        assert metrics['app_resources']['oom_kills']==0
        Path(output).write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
        print('PASS: PostgreSQL large import, accumulation, full read/export, repeated independent backup/restore and fault recovery',flush=True)


if __name__=='__main__':run(sys.argv[1])
