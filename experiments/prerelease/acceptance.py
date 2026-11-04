"""Paired HTTP samples per revision, identical synthetic PostgreSQL and quotas."""
import cProfile
from decimal import Decimal
import importlib.util
import json
import math
import os
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

import psycopg
from openpyxl import Workbook
from server.database import Database
from server import writeoff_analytics as after
from server.decision_import import FIELDS
from server.database import now
from server.decision_versions import ACTIVE
from uuid import uuid4
from tests.analytics_fixture import insert,records,oracle
from experiments.postgres_large.acceptance import Case,require
from experiments.postgres_large.resources import Resources

BASE='17521cd3386924e1483edd0cf68913998f720a50'


def distribution(samples):
    ordered=sorted(samples)
    return {'samples_seconds':samples,'n':len(samples),'median_seconds':round(statistics.median(samples),6),
            'p95_seconds':ordered[math.ceil(len(samples)*.95)-1],'min_seconds':min(samples),'max_seconds':max(samples)}


def query_for(module,db,actor):
    queries=[]
    def capture(con,query,params):queries.append((query,params));return iter(())
    with patch.object(db,'iterate',capture):list(module.iter_facts(db,actor,module.Query(limit=25)))
    return queries[0]


def profile(module,db,actor,path):
    times={'sql_fetch':0.0,'json_parse':0.0};original=db.iterate;decode=json.JSONDecoder.decode
    def iterate(con,query,params):
        rows=iter(original(con,query,params))
        while True:
            start=time.perf_counter()
            try:row=next(rows)
            except StopIteration:times['sql_fetch']+=time.perf_counter()-start;break
            times['sql_fetch']+=time.perf_counter()-start;yield row
    def timed_decode(self,*args,**kwargs):
        start=time.perf_counter();result=decode(self,*args,**kwargs);times['json_parse']+=time.perf_counter()-start;return result
    profiler=cProfile.Profile()
    with patch.object(db,'iterate',iterate),patch.object(json.JSONDecoder,'decode',timed_decode):
        profiler.enable();start=time.perf_counter();data=module.aggregate(db,actor,module.Query(limit=25));times['aggregate_total']=time.perf_counter()-start;profiler.disable()
    start=time.perf_counter();payload=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode();times['serialization']=time.perf_counter()-start
    times['python_other_including_decimal_and_groups']=times['aggregate_total']-times['sql_fetch']-times['json_parse']
    with path.open('w') as log:pstats.Stats(profiler,stream=log).sort_stats('cumulative').print_stats(35)
    return {'diagnostic_seconds':times,'payload_bytes':len(payload),'profiling_overhead':True}


def history_fixture(db,actor):
    """Replace all active synthetic facts, preserving source data and immutable history."""
    signature=uuid4().hex*2;created=now()
    with db.connect(actor) as con:
        con.execute('begin immediate')
        old=list(db.iterate(con,'select d.*,e.document_id from decision_records d join decision_entries e on e.record_id=d.id where '+ACTIVE,()))
        heads=list(con.execute('select v.* from decision_versions v where v.number=(select max(h.number) from decision_versions h where h.document_id=v.document_id)'))
        con.execute('insert into imports values(?,?,?,?)',(signature,actor['id'],'{"synthetic":true,"purpose":"history stress"}',created))
        versions={}
        for head in heads:
            ident=head['document_id']+'-'+str(head['number']+1);versions[head['document_id']]=ident
            con.execute('insert into decision_versions values(?,?,?,?,?,?,?,?,?,?,?,?)',(ident,head['document_id'],head['number']+1,head['id'],head['site_id'],actor['id'],created,'full','ДЕМО · Синтетическая история для приёмки',signature,'{}',ident))
        for index,row in enumerate(old,2):
            version=versions[row['document_id']];ident=uuid4().hex
            con.execute('insert into decision_retirements values(?,?,?)',(row['id'],version,row['site_id']))
            con.execute('insert into decision_records values(?,?,?,?,?,?,?,?)',(ident,signature,signature,'DEMO history',index,row['site_id'],actor['id'],row['data']))
            con.execute('insert into decision_entries values(?,?,?,?,?)',(ident,row['document_id'],version,row['site_id'],row['id']))
        db.check_capacity(con,0)


def run(output):
    out=Path(output).parent;root=Path(tempfile.mkdtemp(prefix='prerelease-private-',dir=out));root.chmod(0o770)
    os.environ['KSU_WORK_DIR']=str(root/'fixture-work')
    spec=importlib.util.spec_from_file_location('server.prerelease_baseline','/baseline/server/writeoff_analytics.py')
    before=importlib.util.module_from_spec(spec);sys.modules[spec.name]=before;spec.loader.exec_module(before)
    restart_each=os.getenv('KSU_PRERELEASE_BLOCK_RESTARTS')!='1'
    sample_count=int(os.getenv('KSU_PRERELEASE_SAMPLES','30'));assert sample_count in (20,30)
    modules={'before':before,'after':after};resources=Resources(root);case=None;result={'base_commit':BASE,'synthetic_only':True,'resources_unchanged':True,'rows':24000,'samples_per_cohort':sample_count,'restart_each_request':restart_each,
      'protocol':'Same physical PostgreSQL facts, roles and limits. Fresh-import statistics stay fixed throughout the first cohort. API restart: '+('each first-open request' if restart_each else 'each five-request block for both revisions; no result cache exists')+'. Warm samples use ANALYZE once. No OS/page-cache flush is claimed. Autovacuum is disabled only on private fixture tables to keep statistics comparable; production configuration is unchanged.',
      'before':{},'after':{}}
    try:
        case=Case(root,'source',resources);db=Database(case.runtime);actor=db.user('manager')
        with psycopg.connect(case.admin) as con:
            for table in ('decision_records','decision_entries','decision_documents','decision_versions','decision_retirements','audit'):
                con.execute('alter table ksu.'+table+' set (autovacuum_enabled=false)')
        case.mark('fixture');rows=insert(db,records(24000))
        target=rows[1];updated={**target,'cost':'12','site':'ДЕМО · Дом Горизонтов','comment':'ДЕМО · Исправление'}
        book=Workbook();sheet=book.active;sheet.title='DEMO optimization';sheet.append(list(FIELDS.values()));sheet.append([updated.get(f) for f in FIELDS]);source=root/'DEMO-correction.xlsx';book.save(source)
        ident=case.upload(source,'manager');assert case.preview(ident,'manager')['status']=='ready'
        settings={'documentId':target['_doc'],'expectedVersion':1,'mode':'partial','reason':'ДЕМО · Приёмка оптимизации','targetIds':[target['_id']],'matches':{}}
        require(case.clients['manager'].post('/api/decision-imports/'+ident+'/version-preview',json=settings));meta=case.wait(ident,'manager');assert meta['versionPlan']['ready']
        require(case.clients['manager'].post('/api/decision-imports/'+ident+'/version-confirm',json={'token':meta['versionPlan']['token']}));assert case.wait(ident,'manager')['status']=='done';target['cost']='12'
        expected,coverage=oracle(rows);result['independent_totals']=expected;snapshots=set()
        def verify(data,limited=False):
            reference,known=(oracle([r for r in rows if r['_site']=='s1']) if limited else (expected,coverage))
            for field,value in reference.items():
                metric=data['metrics'][field];assert (Decimal(metric['value']) if metric['value'] is not None else None)==(Decimal(value) if value is not None else None),field
                if field in known:assert (metric['knownRows'],metric['missingRows'])==known[field]
            assert len(data['rows'])<=25
            if not limited:snapshots.add(data['snapshot'])
        def switch(variant):
            case.source_root='/baseline' if variant=='before' else '/source'
            os.environ['PYTHONPATH']=('/baseline:/source' if variant=='before' else '/source');case.restart()
        for phase in ('first_open','warm'):
            if phase=='warm':
                with psycopg.connect(case.admin,autocommit=True) as con:con.execute('ANALYZE')
            samples={variant:[] for variant in modules};server_elapsed={variant:[] for variant in modules}
            # Interleave five-sample blocks, so neither revision owns the whole early/late run.
            for block in range(sample_count//5):
                for variant in (('before','after') if block%2==0 else ('after','before')):
                    case.mark(phase+':'+variant);switch(variant)
                    for index in range(5):
                        if phase=='first_open' and index and restart_each:switch(variant)
                        start=time.perf_counter();response=case.clients['manager'].get('/api/writeoff-analytics',params={'limit':25},timeout=60 if variant=='before' else 30);data=require(response)
                        assert data['definitions']['quantity']['label']==('Единицы по источнику' if variant=='before' else 'Количество по источнику'),'Wrong source revision served'
                        elapsed=round(time.perf_counter()-start,6);samples[variant].append(elapsed);server_elapsed[variant].append(data['elapsedSeconds']);verify(data)
                        assert len(response.content)<250000
                    print(phase,variant,len(samples[variant]),'last_s',elapsed,flush=True)
            for variant,module in modules.items():
                assert len(samples[variant])==sample_count
                result[variant][phase]=distribution(samples[variant]);result[variant][phase]['server_elapsed_seconds']=server_elapsed[variant]
                query,params=query_for(module,db,actor)
                with db.connect(actor) as con:
                    if variant=='after':con.execute('set local cursor_tuple_fraction=1')
                    plan=con.execute('explain (analyze,buffers,settings,format json) '+query,params).fetchone()[0]
                    cursor=con.execute('explain (settings,format json) declare diagnostic no scroll cursor for '+query,params).fetchone()[0]
                (out/('prerelease-plan-'+variant+'-'+phase+'.json')).write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
                (out/('prerelease-cursor-'+variant+'-'+phase+'.json')).write_text(json.dumps(cursor,ensure_ascii=False,indent=2)+'\n')
                result[variant][phase]['sql_plan_execution_ms']=plan[0]['Execution Time']
                Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        for variant,module in modules.items():
            case.mark('profile:'+variant);result[variant]['profile']=profile(module,db,actor,out/('prerelease-profile-'+variant+'.txt'))
            switch(variant);verify(require(case.clients['limited'].get('/api/writeoff-analytics',params={'limit':25})),True)
            assert require(case.clients['limited'].get('/api/writeoff-analytics?site=s2'))['totalRows']==0
        assert len(snapshots)==1
        # Issue #25 makes 5s a target, not a hardware-independent SLA. Compare
        # paired revisions on the same host; keep absolute timings in the report.
        assert result['after']['first_open']['p95_seconds']<result['before']['first_open']['p95_seconds']*.6
        assert result['after']['warm']['p95_seconds']<max(3.5,result['before']['warm']['p95_seconds']*1.15)
        result['same_snapshot_comparison_cohorts']=True;result['database']=case.stats()
        original_snapshot=next(iter(snapshots));case.mark('history-stress');history_fixture(db,actor)
        start=time.perf_counter();history=require(case.clients['manager'].get('/api/writeoff-analytics',params={'limit':25},timeout=30));history_seconds=time.perf_counter()-start;verify(history)
        assert history['snapshot']!=original_snapshot
        result['history_stress']={'seconds':history_seconds,'database':case.stats(),'same_totals':True,'changed_snapshot':True}
        backup,backup_info=case.backup_http('history')
        restored=Case(root,'restored',resources,(backup,case))
        try:
            restored.mark('restored-history');data=require(restored.clients['manager'].get('/api/writeoff-analytics',params={'limit':25}));verify(data);assert data['snapshot']==history['snapshot']
            verify(require(restored.clients['limited'].get('/api/writeoff-analytics',params={'limit':25})),True)
            result['independent_restore']={'verified':True,'backup':backup_info,'seconds':restored.restore_seconds}
        finally:restored.close()
        result['health']=case.health
        assert all(item['failures']==0 for item in case.health.values());result['verified']=True
    finally:
        if case:case.close()
        result['resources']=resources.finish();Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    assert result['resources']['oom_kills']==0
    print('PASS:',4*sample_count,'paired real HTTP samples, same 24k PostgreSQL facts, exact metrics/snapshot/roles, plans and stage traces; quotas unchanged',flush=True)


if __name__=='__main__':run(sys.argv[1])
