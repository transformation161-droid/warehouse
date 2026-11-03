"""24k synthetic PostgreSQL facts, bounded HTTP dashboard and independent integer oracle."""
from concurrent.futures import ThreadPoolExecutor
import csv
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from openpyxl import Workbook,load_workbook
import psycopg
from server.database import Database
from server.decision_import import FIELDS
from tests.analytics_fixture import insert,records,oracle
from experiments.postgres_large.acceptance import Case,require
from experiments.postgres_large.resources import Resources


def verify(data,rows):
    expected,coverage=oracle(rows)
    assert data['totalRows']==len(rows) and len(data['rows'])<=25
    for field,value in expected.items():
        actual=data['metrics'][field]
        assert (Decimal(actual['value']) if actual['value'] is not None else None)==(Decimal(value) if value is not None else None),field
        if field in coverage:assert (actual['knownRows'],actual['missingRows'])==coverage[field],field
    return expected


def run(output):
    root=Path(tempfile.mkdtemp(prefix='analytics-private-',dir=Path(output).parent));root.chmod(0o770)
    os.environ['KSU_WORK_DIR']=str(root/'fixture-work');resources=Resources(root);cases=[];result={'synthetic_only':True,'official_reporting':False,'rows':24000,'database_cap_bytes':268435456}
    try:
        case=Case(root,'source',resources);cases.append(case);case.mark('fixture')
        start=time.monotonic();rows=insert(Database(case.runtime),records(24000));result['fixture_seconds']=round(time.monotonic()-start,3)
        assert case.counts()['decision_records']==24000
        # Actual uploaded/previewed/manager-confirmed partial correction in the large registry.
        target=next(r for r in rows if r['sourceId']=='DEMO-000001');book=Workbook();sheet=book.active;sheet.title='DEMO analytics correction';sheet.append(list(FIELDS.values()))
        updated={**target,'cost':'12','site':'ДЕМО · Дом Горизонтов','comment':'ДЕМО · Исправление'};sheet.append([updated.get(f) for f in FIELDS]);path=root/'DEMO-correction.xlsx';book.save(path)
        case.mark('correction');ident=case.upload(path,'manager');assert case.preview(ident,'manager')['status']=='ready'
        settings={'documentId':target['_doc'],'expectedVersion':1,'mode':'partial','reason':'ДЕМО · Нагрузочная проверка действующей версии','targetIds':[target['_id']],'matches':{}}
        require(case.clients['manager'].post('/api/decision-imports/'+ident+'/version-preview',json=settings));meta=case.wait(ident,'manager');assert meta['versionPlan']['ready']
        require(case.clients['manager'].post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']}));assert case.wait(ident,'manager')['status']=='done'
        target['cost']='12';case.mark('dashboard');times=[];payloads=[]
        for _ in range(3):
            start=time.monotonic();response=case.clients['manager'].get('/api/writeoff-analytics',params={'limit':25});data=require(response)
            times.append(round(time.monotonic()-start,3));payloads.append(len(response.content));result['independent_totals']=verify(data,rows)
            assert len(response.content)<250000
        result['dashboard_seconds']=times;result['dashboard_payload_bytes']=payloads
        for params,selected in [({'site':'s2'},[r for r in rows if r['_site']=='s2']),
                                ({'dateMode':'missing'},[r for r in rows if not r['date']]),
                                ({'start':'2026-02-01','end':'2026-04-30','reason':'ДЕМО · Причина 1'},[r for r in rows if r['date'] and '2026-02-01'<=r['date']<='2026-04-30' and r['reason']=='ДЕМО · Причина 1'])]:
            verify(require(case.clients['manager'].get('/api/writeoff-analytics',params={**params,'limit':25})),selected)
        verify(require(case.clients['limited'].get('/api/writeoff-analytics',params={'limit':25})),[r for r in rows if r['_site']=='s1'])
        assert require(case.clients['limited'].get('/api/writeoff-analytics?site=s2'))['totalRows']==0
        case.mark('concurrent')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda role:require(case.clients[role].get('/api/writeoff-analytics',params={'limit':25})),('manager','limited')))
        verify(responses[0],rows);verify(responses[1],[r for r in rows if r['_site']=='s1'])
        case.mark('exports');result['exports']={}
        for kind in ('rows','summary'):
            for ext in ('csv','xlsx'):
                start=time.monotonic();dest=root/(kind+'.'+ext)
                with case.clients['manager'].stream('GET','/api/writeoff-analytics/export/'+kind+'.'+ext,params={'expectedSnapshot':data['snapshot']}) as response:
                    assert response.status_code==200,response.status_code
                    with dest.open('wb') as handle:
                        for chunk in response.iter_bytes(65536):handle.write(chunk)
                result['exports'][kind+'.'+ext]={'seconds':round(time.monotonic()-start,3),'bytes':dest.stat().st_size}
                if ext=='csv':
                    with dest.open(encoding='utf-8-sig',newline='') as handle:table=list(csv.DictReader(handle,delimiter=';'))
                else:
                    book=load_workbook(dest,read_only=True,data_only=False);sheet=book.worksheets[0];iterator=sheet.values;headers=next(iterator);table=[dict(zip(headers,r)) for r in iterator];book.close()
                if kind=='rows':
                    assert len(table)==24000
                    converted=[{'_doc':r['ID документа'],**{k:r[label] for k,label in FIELDS.items()}} for r in table]
                    assert oracle(converted)==oracle(rows)
                else:
                    assert Decimal(table[0]['Стоимость списаний (₽)'])==Decimal(result['independent_totals']['cost'])
                dest.unlink()
        result['warm_dashboard_seconds']=[];case.mark('warm-dashboard')
        for _ in range(3):
            start=time.monotonic();warm=require(case.clients['manager'].get('/api/writeoff-analytics',params={'limit':25}))
            result['warm_dashboard_seconds'].append(round(time.monotonic()-start,3));verify(warm,rows)
        result['database']=case.stats()
        backup,backup_metrics=case.backup_http('analytics');restored=Case(root,'restored',resources,(backup,case));cases.append(restored)
        restored.mark('restored-analytics');verified=require(restored.clients['manager'].get('/api/writeoff-analytics',params={'limit':25}));verify(verified,rows)
        assert verified['snapshot']==data['snapshot']
        verify(require(restored.clients['limited'].get('/api/writeoff-analytics',params={'limit':25})),[r for r in rows if r['_site']=='s1'])
        result['independent_restore']={'verified':True,'restore_seconds':restored.restore_seconds,'backup':backup_metrics,'same_analytics_snapshot':True}
        result['health']={c.name:c.health for c in cases};assert all(s['failures']==0 for c in cases for s in c.health.values())
        result['verified']=True
    finally:
        for case in reversed(cases):case.close()
        result['resources']=resources.finish();Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    assert result['resources']['oom_kills']==0
    print('PASS: 24000 active/24001 physical PostgreSQL facts, independent integer metrics, scope, exact four exports, independent restored analytics')


if __name__=='__main__':run(sys.argv[1])
