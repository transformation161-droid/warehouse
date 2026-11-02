"""Actual HTTP upload/preview/commit of generated books under a Docker cgroup."""
import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import httpx
from server.database import Database, ROOT, seed_demo


def run(source, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False);database=output/'data'/'ksu.sqlite3'
    db=Database(database);passwords={r:secrets.token_urlsafe(24) for r in ('manager','inspector')};seed_demo(db,passwords['manager'],passwords['inspector'])
    actor=db.user('manager')
    with db.connect(actor) as con:
        con.execute('insert into records values(?,?,?,?,?,?,?,?)',('sites','demo_site',json.dumps({'name':'DEMO site','code':'DEMO','storekeeper':'DEMO','pm':'DEMO'}),'manager',None,1,'2026-11-01','2026-11-01'))
        con.execute("insert into user_sites values('inspector','demo_site')")
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin='http://127.0.0.1:'+str(port)
    env={**os.environ,'KSU_DB':str(database),'KSU_ORIGIN':origin,'KSU_SECURE_COOKIES':'0','KSU_REQUIRE_INITIALIZED':'1'}
    log=(output/'server.log').open('w')
    server=subprocess.Popen([sys.executable,'-m','server','run','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
    latencies=[];failures=[];stop=threading.Event();monitor=None
    metrics={'synthetic_only':True,'source_bytes':Path(source).stat().st_size,'phases':{}}
    try:
        with httpx.Client(base_url=origin,trust_env=False,timeout=60) as client:
            for _ in range(150):
                try:
                    if client.get('/api/health').status_code==200:break
                except httpx.TransportError:pass
                time.sleep(.1)
            else:raise AssertionError('API did not start')
            r=client.post('/api/login',headers={'Origin':origin},json={'username':'inspector','password':passwords['inspector']});assert r.status_code==200
            client.headers.update({'Origin':origin,'X-CSRF-Token':r.json()['csrf']})
            def ping():
                with httpx.Client(base_url=origin,trust_env=False,timeout=8) as health:
                    while not stop.is_set():
                        t=time.monotonic()
                        try:
                            response=health.get('/api/health')
                            if response.status_code!=200:failures.append('status')
                            latencies.append(time.monotonic()-t)
                        except httpx.TransportError:failures.append('timeout')
                        stop.wait(.2)
            monitor=threading.Thread(target=ping,daemon=True);monitor.start()
            def require(response):
                assert response.status_code==200,('HTTP',response.status_code,response.text[:100]);return response.json()
            def wait(ident):
                started=time.monotonic()
                while time.monotonic()-started<245:
                    meta=require(client.get('/api/decision-imports/'+ident))
                    if meta['status'] not in ('uploading','inspecting','checking','committing'):return meta
                    time.sleep(.3)
                raise AssertionError('Worker deadline')
            def upload():
                with open(source,'rb') as handle:
                    def chunks():
                        while chunk:=handle.read(64*1024):yield chunk
                    start=time.monotonic();meta=require(client.post('/api/decision-imports',content=chunks(),headers={'X-KSU-Filename':'DEMO.xlsx','Content-Type':'application/octet-stream'}))
                meta=wait(meta['id']);assert meta['status']=='uploaded',meta.get('message')
                metrics['phases']['upload_seconds']=round(time.monotonic()-start,3);return meta['id']
            settings={'mapping':{field:chr(65+i) for i,field in enumerate(__import__('server.decision_import',fromlist=['FIELDS']).FIELDS)},'allowCached':True}
            ident=upload();start=time.monotonic();require(client.post('/api/decision-imports/'+ident+'/preview',json=settings));preview=wait(ident)
            assert preview['status']=='ready',(preview['status'],preview.get('message'),preview.get('errors',[])[:1])
            metrics['phases']['preview_seconds']=round(time.monotonic()-start,3)
            assert require(client.get('/api/decisions'))['total']==0
            start=time.monotonic();require(client.post('/api/decision-imports/'+ident+'/commit'));done=wait(ident)
            assert done['status']=='done',(done['status'],done.get('message'))
            metrics['phases']['commit_seconds']=round(time.monotonic()-start,3)
            listing=require(client.get('/api/decisions?limit=10'));assert listing['total']==preview['accepted']
            assert len(listing['rows'])==10
            expected=json.loads(Path(str(source).replace('.xlsx','-expected.json')).read_text())
            expected_rows=expected['rows']-1
            assert listing['total']==expected_rows
            # Independently check every normalized quantity and cost, exact row identity and original random comments.
            from server.xlsx_stream import Reader
            with Reader(source,output/'verify-strings') as reader,closing(sqlite3.connect(database)) as con:
                with open(source,'rb') as handle:source_sha=hashlib.file_digest(handle,'sha256').hexdigest()
                rows=con.execute('select source_row,data from decision_records where file_sha=? order by sheet,source_row',(source_sha,))
                raw_hash=hashlib.sha256()
                for number,cells in reader.rows(first=1,allow_cached=True):
                    raw_hash.update(json.dumps([number,cells],sort_keys=True,ensure_ascii=False).encode())
                    if number==1:continue
                    actual_number,data=next(rows);data=json.loads(data)
                    assert actual_number==number
                    for field,col in [('sourceId','A'),('quantity','G'),('cost','H'),('residual','I'),('reimbursable','N'),('reimbursed','O')]:
                        assert Decimal(data[field])==Decimal(cells[col]['value'])
                    from server.decision_import import FIELDS, MONEY
                    for index,field in enumerate(FIELDS):
                        raw=cells[chr(65+index)]['value']
                        if field in MONEY or field=='quantity':assert Decimal(data[field])==Decimal(raw)
                        else:assert data[field]==raw
                    assert data['comment']==cells['Q']['value']
                assert raw_hash.hexdigest()==expected['digest']
                assert rows.fetchone() is None
                assert con.execute('pragma integrity_check').fetchone()[0]=='ok'
                assert not con.execute('pragma foreign_key_check').fetchall()
            # Same file/settings cannot create duplicates.
            second=upload();require(client.post('/api/decision-imports/'+second+'/preview',json=settings));assert wait(second)['status']=='ready'
            require(client.post('/api/decision-imports/'+second+'/commit'));again=wait(second);assert again.get('duplicate') is True
            assert require(client.get('/api/decisions'))['total']==expected_rows
            metrics.update({'accepted':preview['accepted'],'rejected':preview['rejected'],'all_values_match_source':True,
                   'original_comments_preserved':True,'duplicate_suppressed':True,'no_rows_before_commit':True,
                   'formula_warning':bool(preview['warning']),'database_bytes':database.stat().st_size})
    finally:
        stop.set()
        if monitor:monitor.join(timeout=10)
        server.terminate();server.wait(timeout=20);log.close()
    metrics.update({'health_samples':len(latencies),'health_failures':len(failures),'health_max_seconds':round(max(latencies,default=0),3),
              'memory_peak_bytes':int(Path('/sys/fs/cgroup/memory.peak').read_text()),
              'memory_limit':Path('/sys/fs/cgroup/memory.max').read_text().strip(),
              'cpu_limit':Path('/sys/fs/cgroup/cpu.max').read_text().strip()})
    assert metrics['health_failures']==0
    assert metrics['memory_limit']=='1073741824' and metrics['cpu_limit']=='50000 100000'
    assert metrics['memory_peak_bytes']<900*1024*1024
    print(json.dumps(metrics,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');a=p.parse_args();run(a.source,a.output)
