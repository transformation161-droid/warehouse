"""Explicit synthetic-only helper, absent from the production image. Never accepts a source DB."""
import base64
import io
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from PIL import Image
from server.database import Database,password_hash
from server.service import save_record


def initialize(root):
    root=Path(root)
    if any(root.iterdir()):raise ValueError('NEW EMPTY SYNTHETIC DIRECTORY REQUIRED')
    if os.getenv('KSU_OFFLINE_MODE') is not None:raise ValueError('Seed helper cannot run in release runtime configuration')
    accounts={role:secrets.token_urlsafe(24) for role in ('manager','inspector','viewer','unassigned')}
    db=Database(root/'ksu.sqlite3')
    with db.connect() as con:
        for role,password in accounts.items():
            con.execute('insert into users values(?,?,?,?,?,1)',(role,'demo-'+role,'ДЕМО · '+role,'viewer' if role=='unassigned' else role,password_hash(password)))
        for ident,site in [('inspector','s1'),('viewer','s1'),('manager','s1')]:con.execute('insert into user_sites values(?,?)',(ident,site))
    with db.connect({'id':'manager','role':'manager'}) as con:
        for index in range(22):
            con.execute('insert into records values(?,?,?,?,?,?,?,?)',('questions','q'+str(index+1),json.dumps({'section':'ДЕМО · Контроль','title':'ДЕМО · Вопрос '+str(index+1)},ensure_ascii=False),'manager',None,1,'2026-11-09','2026-11-09'))
    actor={'id':'manager','role':'manager','name':'ДЕМО · manager'}
    def change(collection,ident,data):
        with db.connect(actor) as con:save_record(con,actor,collection,ident,data,0)
    for ident in ('s1','s2'):change('sites',ident,{'name':'ДЕМО · '+ident,'storekeeper':'PRIVATE-SYNTHETIC-PERSON','code':'DEMO-'+ident})
    for site in ('s1','s2'):
        change('visits','demo-visit-'+site,{'siteId':site,'date':'2026-11-09','assignedTo':'inspector' if site=='s1' else 'manager','goal':'PRIVATE-SYNTHETIC-PLAN'})
        change('findings','demo-finding-'+site,{'siteId':site,'title':'ДЕМО · Открытое нарушение '+site,'description':'PRIVATE-SYNTHETIC-NOTE','owner':'PRIVATE-SYNTHETIC-PERSON'})
    (root/'synthetic-acceptance.json').write_text(json.dumps({'format':'ksu-release-acceptance-v1','syntheticOnly':True,'schema':4}))
    (root/'accounts.json').write_text(json.dumps(accounts));(root/'accounts.json').chmod(0o600)
    return accounts


if __name__=='__main__':
    import sys
    initialize(sys.argv[1]);print('Synthetic acceptance created; private credentials are in accounts.json, never in stdout.')
