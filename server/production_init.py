"""One-time owner initialization of an empty, already provisioned PostgreSQL schema.

No demo seed or SQLite input. Private names/config enter via file; fresh passwords
are persisted owner-only before commit and are never returned or logged.
"""
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row,tuple_row
from .backup_job import atomic_json
from .database import password_hash,now
from .managed_pg import tls,preflight
from .models import Site,Question
from .pg_admin import TABLES,fingerprints
from .postgres_backend import Connection
from .service import save_record
from .offline import current_checklist

FORMAT='ksu-production-initialization-v1'
IDENT=re.compile(r'[A-Za-z0-9_-]{1,80}')
DEMO=re.compile(r'(?i)(?:\bdemo\b|\bдемо\b|^demo[-_])')


def text(value,maximum):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum or any(ord(c)<32 for c in value) or DEMO.search(value):
        raise ValueError('Explicit non-demo owner values required')
    return value.strip()


def configuration(value):
    if not isinstance(value,dict) or set(value)!={'accounts','sites','inspectorSiteIds','questions','checklistApproved'} or value['checklistApproved'] is not True:
        raise ValueError('Owner-approved configuration and checklist required')
    accounts=value['accounts']
    if not isinstance(accounts,dict) or set(accounts)!={'manager','inspector'}:raise ValueError('Exactly two initial roles required')
    normalized={}
    for role,account in accounts.items():
        if not isinstance(account,dict) or set(account)!={'username','name'}:raise ValueError('Only private username/name, never passwords or roles, may be supplied')
        normalized[role]={'username':text(account['username'],120),'name':text(account['name'],120)}
        if len(normalized[role]['username'])<2:raise ValueError('Personal login required')
    if len({a['username'].casefold() for a in normalized.values()})!=2:raise ValueError('Distinct personal logins required')
    result={**value,'accounts':normalized}
    for collection,model,limit in (('sites',Site,100),('questions',Question,200)):
        rows=value[collection]
        if not isinstance(rows,list) or not 1<=len(rows)<=limit:raise ValueError('Explicit nonempty catalog required')
        result[collection]=[];ids=set();names=set()
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not IDENT.fullmatch(row['id']) or DEMO.search(row['id']) or row['id'] in ids:raise ValueError('Unique non-demo catalog IDs required')
            fields=model.model_validate({k:v for k,v in row.items() if k!='id'}).model_dump()
            for field,value_text in fields.items():
                if isinstance(value_text,str) and value_text:text(value_text,500)
            if collection=='sites':
                name=fields['name'].casefold()
                if name in names:raise ValueError('Distinct site names required')
                names.add(name)
            if collection=='questions':
                if fields['archived']:raise ValueError('Initial checklist must contain active questions only')
                fields['position']=len(result[collection])+1
            ids.add(row['id']);result[collection].append({'id':row['id'],**fields})
    assigned=value['inspectorSiteIds'];sites={r['id'] for r in result['sites']}
    if not isinstance(assigned,list) or not assigned or any(not isinstance(s,str) for s in assigned) or len(set(assigned))!=len(assigned) or not set(assigned)<=sites:
        raise ValueError('Explicit valid inspector site assignments required')
    return result


def template():
    from .checklist_template import QUESTIONS
    return {'accounts':{'manager':{'username':'','name':''},'inspector':{'username':'','name':''}},
        'sites':[{'id':'site-1','name':'','code':'','storekeeper':'','pm':''}],
        'inspectorSiteIds':['site-1'],'questions':[dict(q) for q in QUESTIONS],'checklistApproved':False}


def output_directory(value):
    path=Path(os.path.abspath(value))
    for ancestor in (path,*path.parents):
        if ancestor.is_symlink() or (ancestor/'.git').exists():raise ValueError('Private regular output outside Git required')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):raise FileExistsError('New empty private output required')
    return path


def initialize(dsn,values,output):
    config=configuration(values);tls(dsn);preflight(dsn,'admin');target=output_directory(output)
    credentials={role:{'id':uuid4().hex,**account,'password':secrets.token_urlsafe(24)} for role,account in config['accounts'].items()}
    with psycopg.connect(dsn,connect_timeout=10) as raw:
        raw.execute('select pg_advisory_xact_lock(170009)')
        tables=TABLES+('sessions','attempts')
        raw.execute(sql.SQL('lock table {} in exclusive mode').format(sql.SQL(',').join(sql.Identifier('ksu',t) for t in tables)))
        for table in tables:
            if raw.execute(sql.SQL('select exists(select 1 from ksu.{})').format(sql.Identifier(table))).fetchone()[0]:
                raise ValueError('Production initialization requires every business/authentication table empty; no retry/upsert/overwrite')
        target.mkdir(mode=0o700,parents=True,exist_ok=True)
        # Keep credentials on any failure, including ambiguous COMMIT/network errors.
        # Absence of the committed report means the owner must inspect, never rerun.
        atomic_json(target/'credentials.json',{'format':FORMAT,'accounts':credentials},exclusive=True)
        atomic_json(target/'status.json',{'format':FORMAT,'phase':'prepared'},exclusive=True)
        raw.row_factory=dict_row
        con=Connection(raw);con.execute('set local search_path=ksu,pg_catalog')
        stamp=now();actor={'id':credentials['manager']['id'],'role':'manager','name':credentials['manager']['name']}
        for role,account in credentials.items():
            con.execute('insert into users values(?,?,?,?,?,1)',(account['id'],account['username'],account['name'],role,password_hash(account['password'])))
            audit={k:account[k] for k in ('id','username','name')};audit.update(role=role,active=1)
            con.execute('insert into audit(at,actor_id,collection,record_id,operation,after_json) values(?,?,?,?,?,?)',
                (stamp,'owner-local:ksu_admin','users',account['id'],'create',json.dumps(audit,ensure_ascii=False)))
        con.execute("select set_config('ksu.actor',?,true)",(actor['id'],))
        for collection in ('sites','questions'):
            for row in config[collection]:save_record(con,actor,collection,row['id'],{k:v for k,v in row.items() if k!='id'},0)
        inspector=credentials['inspector']['id']
        for site in config['inspectorSiteIds']:
            con.execute('insert into user_sites values(?,?)',(inspector,site))
            con.execute('insert into audit(at,actor_id,collection,record_id,operation,after_json) values(?,?,?,?,?,?)',
                (stamp,'owner-local:ksu_admin','user_sites',inspector,'assign',json.dumps({'userId':inspector,'siteId':site})))
        version=current_checklist(con)
        con.execute('insert into audit(at,actor_id,collection,record_id,operation,after_json) values(?,?,?,?,?,?)',
            (stamp,'owner-local:ksu_admin','setup','production','initialize',json.dumps({'accounts':2,'sites':len(config['sites']),'questions':len(config['questions']),'checklistVersion':version['id']})))
        raw.row_factory=tuple_row
        before=fingerprints(raw,True)
        if raw.execute('select pg_database_size(current_database())').fetchone()[0]>int(os.getenv('KSU_PG_DATABASE_MAX_BYTES',os.getenv('KSU_DB_MAX_BYTES','268435456'))):raise ValueError('Database cap exceeded')
    report={'format':FORMAT,'initialized':True,'schema':4,'sessions_revoked':True,'tables':before,
        'accountIds':{role:a['id'] for role,a in credentials.items()},'checklistVersion':version['id'],'operational_records_created':False}
    atomic_json(target/'setup-report.json',report,exclusive=True)
    atomic_json(target/'status.json',{'format':FORMAT,'phase':'committed'})
    return {'initialized':True,'accounts':2,'sites':len(config['sites']),'questions':len(config['questions']),
        'passwords_in_private_file':True,'operational_records_created':False}
