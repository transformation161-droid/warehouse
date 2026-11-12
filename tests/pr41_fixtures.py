"""Shared, explicitly synthetic PR41 records. No owner files or real credentials."""
from io import BytesIO
from PIL import Image,ImageDraw
import json
import time

import psycopg
from inventory_pg_smoke import ADMIN_DSN,CI_DB,MANAGER,ELMAN
from server.checklist_template import QUESTIONS
from server.database import digest
from server.service import save_record

VIEWER={'id':'u_viewer','role':'viewer','name':'ДЕМО · Только просмотр'}


def photo():
    image=Image.new('RGB',(1200,900),'#255a80');ImageDraw.Draw(image).text((80,80),'DEMO / SYNTHETIC PR41',fill='white')
    out=BytesIO();image.save(out,format='JPEG');return out.getvalue()


def seed():
    db=CI_DB();raw=photo()
    with psycopg.connect(ADMIN_DSN) as con:
        con.execute("insert into ksu.users values('u_viewer','u_viewer','ДЕМО · Только просмотр','viewer','synthetic',1)")
        con.execute("insert into ksu.user_sites values('u_viewer','site_alpha')")
    with db.connect(MANAGER) as con:
        for q in QUESTIONS:save_record(con,MANAGER,'questions',q['id'],{k:v for k,v in q.items() if k!='id'},0)
        for ident,site in [('synthetic-alpha','site_alpha'),('synthetic-beta','site_beta'),('synthetic-orphan','site_alpha')]:
            con.execute('insert into media values(?,?,?,?,?,?)',(ident,site,MANAGER['id'],raw,raw,'2026-11-12'))
    with db.connect(ELMAN) as con:
        save_record(con,ELMAN,'inspections','synthetic-act',{'siteId':'site_alpha','date':'2026-11-12','inspector':ELMAN['name'],
            'results':[{'questionId':q['id'],'status':'fail' if i==0 else 'ok','comment':'ДЕМО · нарушение' if i==0 else '',
                        'photo':'/api/media/synthetic-alpha' if i==0 else ''} for i,q in enumerate(QUESTIONS)]},0)
        for ident,status in [('synthetic-open','open'),('synthetic-progress','inprogress'),('synthetic-closed','closed')]:
            save_record(con,ELMAN,'findings',ident,{'siteId':'site_alpha','title':'ДЕМО · '+ident,'status':status,
                'photo':'/api/media/synthetic-alpha','resolution':'ДЕМО · устранено' if status=='closed' else '',
                'evidence':'/api/media/synthetic-alpha' if status=='closed' else ''},0)
    with db.connect(MANAGER) as con:
        save_record(con,MANAGER,'findings','synthetic-beta-closed',{'siteId':'site_beta','title':'ДЕМО · Закрыто на другом объекте','status':'closed',
            'photo':'/api/media/synthetic-beta','resolution':'ДЕМО · устранено','evidence':'/api/media/synthetic-beta'},0)


def session(actor):
    token='pr41-synthetic-session-'+actor['id'];csrf='pr41-synthetic-csrf-'+actor['id']
    with psycopg.connect(ADMIN_DSN) as con:
        con.execute('insert into ksu.sessions values(%s,%s,%s,%s) on conflict(token_hash) do nothing',(digest(token),actor['id'],csrf,time.time()+3600))
    return token,csrf
