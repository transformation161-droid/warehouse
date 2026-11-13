"""Disposable real PostgreSQL; no external DSN accepted. Shared by API/browser QA only."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
from unittest.mock import patch
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from server.app import create_app
from server.database import now,password_hash
from server.pg_admin import initialize
from server.postgres_backend import Connection
from server.service import save_record
from server.checklist_template import QUESTIONS
from server.offline import current_checklist
from tests.managed_pg_environment import environment


@contextmanager
def pilot(origin='http://testserver'):
    with environment(one_cluster=True) as pg:
        initialize(pg['source']['admin'],'ksu_runtime','ksu_backup')
        accounts={role:{'id':role,'username':role,'name':'ДЕМО · '+role,'role':role,'password':secrets.token_urlsafe(24)} for role in ('manager','inspector','viewer')}
        with psycopg.connect(pg['source']['admin'],row_factory=dict_row) as raw:
            con=Connection(raw);con.execute('set local search_path=ksu,pg_catalog')
            for a in accounts.values():con.execute('insert into users values(?,?,?,?,?,1)',(a['id'],a['username'],a['name'],a['role'],password_hash(a['password'])))
            for role in ('inspector','viewer'):con.execute('insert into user_sites values(?,?)',(role,'s1'))
            manager=accounts['manager'];con.execute("select set_config('ksu.actor',?,true)",('manager',))
            for ident,name in [('s1','ДЕМО · Склад Север'),('s2','ДЕМО · Склад Юг')]:save_record(con,manager,'sites',ident,{'name':name,'code':ident},0)
            for q in QUESTIONS:save_record(con,manager,'questions',q['id'],{k:v for k,v in q.items() if k!='id'},0)
            current_checklist(con)
        values={'KSU_DATABASE_URL':pg['source']['runtime'],'KSU_WORK_DIR':str(pg['root']/'work'),'KSU_WRITEOFF_ANALYTICS_ENABLED':'on',
                'KSU_PG_TRANSPORT_PROFILE':'verify-full','KSU_OFFLINE_MODE':'off','KSU_OFFLINE_DEPLOYMENT_ID':uuid4().hex,
                'KSU_VIEWER_ACCESS':'enabled','KSU_SCHEMA_MODE':'verified','KSU_DEPLOYMENT_PROFILE':'production'}
        with patch.dict(os.environ,values):
            app=create_app('postgresql',origin,origin.startswith('https:'))
            try:yield app,accounts,pg
            finally:app.state.import_jobs.close()
