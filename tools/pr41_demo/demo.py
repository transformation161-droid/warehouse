"""Pinned PR41 synthetic-only owner lab. Not imported by the application."""
import base64
from contextlib import closing
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
from uuid import uuid4

APP_SHA='cc0b18238fdca4cd02876b5daf0cbef4481c3eb2'
FORMAT='instrakt-pr41-test-v1'
DB='ksu_pr41_demo'


def atomic(path,value):
    path=Path(path)
    if path.exists():raise ValueError('No overwrite of test secrets/output')
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    path.chmod(0o600)


def private_host(host):
    ip=ipaddress.IPv4Address(host)
    return host=='127.0.0.1' or any(ip in ipaddress.IPv4Network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))


def prepare(root,pki,host,port,instance,uid=10001,gid=10001):
    root=Path(root);pki=Path(pki)
    if not private_host(host) or not 1024<=port<=65535 or port in (8003,8018,8019):raise ValueError('Dedicated private/loopback test endpoint required')
    if len(instance)!=32 or any(c not in '0123456789abcdef' for c in instance):raise ValueError('New test identity required')
    if any(root.iterdir()) or any(pki.iterdir()):raise ValueError('NEW EMPTY private directory/PKI volume required')
    from psycopg.conninfo import make_conninfo
    service={role:secrets.token_urlsafe(24) for role in ('postgres','ksu_admin','ksu_runtime','ksu_backup')}
    accounts={role:{'id':'demo-'+role,'username':'demo-'+role,'name':'ДЕМО · '+name,'password':secrets.token_urlsafe(18),'role':role}
              for role,name in [('manager','Руководитель'),('inspector','КСУ'),('viewer','Только просмотр')]}
    origin=f'https://{host}:{port}'
    info={'format':FORMAT,'app_sha':APP_SHA,'synthetic_only':True,'instance':instance,'origin':origin,'accounts':accounts,'service':service}
    atomic(root/'secrets.json',info)
    (root/'postgres-password').write_text(service['postgres']);(root/'postgres-password').chmod(0o600)
    (root/'server.env').write_text(f'POSTGRES_DB={DB}\nPOSTGRES_PASSWORD_FILE=/private/postgres-password\n')
    def dsn(user):return make_conninfo(host='pr41-db',port=5432,dbname=DB,user=user,password=service[user],sslmode='verify-full',sslrootcert='/tls/ca.crt')
    for name,user,key in [('bootstrap','postgres','KSU_PG_BOOTSTRAP_DSN'),('admin','ksu_admin','KSU_PG_ADMIN_DSN'),('runtime','ksu_runtime','KSU_DATABASE_URL')]:
        rows=[key+'='+dsn(user),'KSU_PG_TRANSPORT_PROFILE=verify-full','KSU_PR41_TEST_MODE=synthetic-only','KSU_INVENTORY_PLAN_ENABLED=on','KSU_WORK_DIR=/tmp/ksu-pr41-seed']
        if name=='runtime':rows+=['KSU_ORIGIN='+origin,'KSU_OFFLINE_MODE=off','KSU_OFFLINE_DEPLOYMENT_ID='+instance,'KSU_VIEWER_ACCESS=enabled',
            'KSU_LOCAL_BACKUP_MODE=off','KSU_RELEASE_TLS_CERT=/tls/server.crt','KSU_RELEASE_TLS_KEY=/tls/server.key']
        (root/(name+'.env')).write_text('\n'.join(rows)+'\n');(root/(name+'.env')).chmod(0o600)
    def openssl(*args):subprocess.run(['openssl',*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    openssl('req','-x509','-newkey','rsa:2048','-nodes','-days','14','-subj','/CN=INSTRAKT PR41 TEST CA '+instance[:8],
            '-keyout',str(pki/'ca.key'),'-out',str(pki/'ca.crt'),'-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    openssl('req','-newkey','rsa:2048','-nodes','-subj','/CN=INSTRAKT PR41 SYNTHETIC','-keyout',str(pki/'server.key'),'-out',str(pki/'server.csr'))
    ext=pki/'server.ext';ext.write_text('subjectAltName=DNS:localhost,DNS:pr41-db,IP:127.0.0.1'+('' if host=='127.0.0.1' else ',IP:'+host)+'\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n')
    openssl('x509','-req','-in',str(pki/'server.csr'),'-CA',str(pki/'ca.crt'),'-CAkey',str(pki/'ca.key'),'-CAcreateserial','-days','7','-extfile',str(ext),'-out',str(pki/'server.crt'))
    der=ssl.PEM_cert_to_DER_cert((pki/'ca.crt').read_text())
    (root/'PR41-Test-CA.cer').write_bytes(der);(root/'PR41-Test-CA.crt').write_bytes((pki/'ca.crt').read_bytes())
    for path in pki.iterdir():path.chmod(0o600 if path.name=='ca.key' else 0o644)
    (pki/'server.key').chmod(0o640);os.chown(pki/'server.key',10001,10001)
    text='ТОЛЬКО ДЕМО. Не использовать эти аккаунты в рабочей системе.\n'+origin+'\n\n'
    for a in accounts.values():text+=a['name']+'\nЛогин: '+a['username']+'\nПароль: '+a['password']+'\n\n'
    (root/'test-accounts.txt').write_text(text,encoding='utf-8-sig');(root/'test-accounts.txt').chmod(0o600)
    result={'format':FORMAT,'app_sha':APP_SHA,'instance':instance,'origin':origin,'host':host,'port':port,
            'ca_thumbprint':hashlib.sha1(der).hexdigest().upper(),'ca_sha256':hashlib.sha256(der).hexdigest(),'certificate_days':7}
    result['env_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.env')}
    atomic(root/'prepared.json',result)
    for output in root.iterdir():os.chown(output,uid,gid)
    os.chown(root,uid,gid);root.chmod(0o700)
    return {'prepared':True,'app_sha':APP_SHA,'synthetic_only':True}


def seed(root):
    sys.path.insert(0,'/app')  # helper is mounted outside the unchanged application image
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    import psycopg
    from psycopg.rows import dict_row
    from psycopg import sql
    from server.managed_pg import bootstrap,preflight
    from server.pg_admin import initialize,fingerprints
    from server.postgres_backend import Connection,PostgresDatabase
    from server.database import now,password_hash
    from server.service import save_record
    from server.offline import current_checklist
    from server.checklist_template import QUESTIONS
    from server.inventory_workflow import create_plan,count_line,finalize_inventory
    from server.models import InventoryPlanUpload,InventoryCountUpdate,InventoryFinalize
    from PIL import Image,ImageDraw
    info=json.loads((Path(root)/'secrets.json').read_text())
    if info.get('format')!=FORMAT or info.get('app_sha')!=APP_SHA or info.get('synthetic_only') is not True or os.getenv('KSU_PR41_TEST_MODE')!='synthetic-only':raise ValueError('Synthetic lab identity required')
    if Path('/app/RELEASE_SHA').read_text().strip()!=APP_SHA:raise ValueError('Only approved PR41 image allowed')
    source=conninfo_to_dict(os.environ['KSU_PG_BOOTSTRAP_DSN'])
    if source.get('host')!='pr41-db' or source.get('port')!='5432' or source.get('dbname')!=DB or source.get('user')!='postgres' or source.get('sslmode')!='verify-full' or source.get('sslrootcert')!='/tls/ca.crt':raise ValueError('Only isolated demo database accepted')
    def dsn(user):return make_conninfo(**{**source,'user':user,'password':info['service'][user]})
    with psycopg.connect(dsn('postgres')) as con:
        if con.execute("select to_regnamespace('ksu')").fetchone()[0]:raise ValueError('Destination not empty: seed NEVER retries or overwrites')
    bootstrap(dsn('postgres'),{key:info['service'][key] for key in ('ksu_admin','ksu_runtime','ksu_backup')})
    initialize(dsn('ksu_admin'),'ksu_runtime','ksu_backup')
    with psycopg.connect(dsn('ksu_admin'),row_factory=dict_row) as raw:
        # Existing PR40 rules applied only to this newly created synthetic cluster.
        raw.execute(Path('/app/server/postgres/005_inventory_count.sql').read_text())
        con=Connection(raw);con.execute('set local search_path=ksu,pg_catalog')
        for a in info['accounts'].values():
            con.execute('insert into users values(?,?,?,?,?,1)',(a['id'],a['username'],a['name'],a['role'],password_hash(a['password'])))
        for role in ('inspector','viewer'):con.execute('insert into user_sites values(?,?)',(info['accounts'][role]['id'],'demo-north'))
        manager=info['accounts']['manager'];con.execute("select set_config('ksu.actor',?,true)",(manager['id'],))
        for site,name in [('demo-north','ДЕМО · Склад Север'),('demo-south','ДЕМО · Склад Юг')]:save_record(con,manager,'sites',site,{'name':name,'code':site},0)
        for q in QUESTIONS:save_record(con,manager,'questions',q['id'],{k:v for k,v in q.items() if k!='id'},0)
        current_checklist(con)
    db=PostgresDatabase(dsn('ksu_runtime'));manager=info['accounts']['manager'];field=info['accounts']['inspector']
    picture=Image.new('RGB',(1200,900),'#315d85');ImageDraw.Draw(picture).text((100,100),'DEMO / PR41 TEST ONLY',fill='white');out=io.BytesIO();picture.save(out,format='JPEG');picture=out.getvalue()
    with db.connect(manager) as con:
        for ident,site in [('demo-photo-north','demo-north'),('demo-photo-south','demo-south')]:con.execute('insert into media values(?,?,?,?,?,?)',(ident,site,manager['id'],picture,picture,now()))
    with db.connect(field) as con:
        save_record(con,field,'inspections','demo-act',{'siteId':'demo-north','date':'2026-11-12','inspector':field['name'],
            'results':[{'questionId':q['id'],'status':'fail' if i==0 else 'ok','comment':'ДЕМО · Нарушение хранения' if i==0 else '',
                        'photo':'/api/media/demo-photo-north' if i==0 else ''} for i,q in enumerate(QUESTIONS)]},0)
        for ident,status in [('demo-open','open'),('demo-progress','inprogress'),('demo-closed','closed')]:
            data={'siteId':'demo-north','title':'ДЕМО · '+{'open':'Открытое нарушение','inprogress':'Нарушение в работе','closed':'Устранённое нарушение'}[status],
                  'description':'Только вымышленные данные для приёмки PR41','status':status,'photo':'/api/media/demo-photo-north',
                  'resolution':'ДЕМО · Устранено' if status=='closed' else '', 'evidence':'/api/media/demo-photo-north' if status=='closed' else ''}
            if status=='open':data.update(inspectionId='demo-act',questionId=QUESTIONS[0]['id'])
            save_record(con,field,'findings',ident,data,0)
    with db.connect(manager) as con:save_record(con,manager,'findings','demo-south-closed',{'siteId':'demo-south','title':'ДЕМО · Устранено на южном складе','status':'closed',
        'photo':'/api/media/demo-photo-south','evidence':'/api/media/demo-photo-south','resolution':'ДЕМО · Устранено'},0)
    def plan(title,day,rows):
        import csv
        data=io.StringIO();w=csv.writer(data,delimiter=';');w.writerow(['ID','Наименование','№','Количество','Стоимость','Бренд'])
        for i,(name,qty,price,brand) in enumerate(rows,1):w.writerow([str(i),name,'DEMO-'+str(i),qty,price,brand])
        return create_plan(db,manager,InventoryPlanUpload(name='demo.csv',content=base64.b64encode(data.getvalue().encode('utf-8-sig')).decode(),siteId='demo-north',date=day,title=title))
    rows=[('ДЕМО · Наличие',1,100,'ДЕМО · Бренд А'),('ДЕМО · Недостача',2,100,'ДЕМО · Бренд Б'),('ДЕМО · Излишек, нулевой учёт',0,100,'ДЕМО · Бренд В'),('ДЕМО · Дробное количество',.001,100,'ДЕМО · Бренд Г'),('ДЕМО · '+('Длинное наименование '*16),3,1500,'ДЕМО · '+('Длинный бренд '*7))]
    first=plan('ДЕМО · 1. Быстрая инвентаризация','2026-11-12',rows)
    second=plan('ДЕМО · 2. Пример четырёх статусов','2026-11-13',rows[:4])
    sample=sorted([x for x in second['state']['inventoryLines'] if x['inventoryId']==second['inventoryId']],key=lambda x:x['sourceRow'])
    for i,actual in enumerate([1,0,1]):count_line(db,field,sample[i]['id'],InventoryCountUpdate(expectedVersion=sample[i]['version'],actual=actual,reason='ДЕМО · Проверено на месте',photo='/api/media/demo-photo-north'))
    last=plan('ДЕМО · 3. Закрытый пересчёт','2026-11-14',rows[:2])
    for x in [x for x in last['state']['inventoryLines'] if x['inventoryId']==last['inventoryId']]:count_line(db,field,x['id'],InventoryCountUpdate(expectedVersion=x['version'],actual=x['expected']))
    finalize_inventory(db,manager,last['inventoryId'],InventoryFinalize(expectedVersion=1))
    plan('ДЕМО · 4. Длинный список 121','2026-11-15',[(f'ДЕМО · Позиция {i:03}',1,100,'ДЕМО · Бренд') for i in range(1,122)])
    for role in ('admin','runtime','backup'):preflight(dsn('ksu_'+role),role)
    result={'format':FORMAT,'app_sha':APP_SHA,'instance':info['instance'],'synthetic_only':True,'seeded':True,'accounts':3,'questions':22,'inventories':4,'primary_inventory':first['inventoryId']}
    atomic(Path(root)/'seed-complete.json',result)
    return {'seeded':True,'accounts':3,'inventories':4,'questions':22,'synthetic_only':True}


if __name__=='__main__':
    try:
        if sys.argv[1]=='prepare':result=prepare(sys.argv[2],sys.argv[3],sys.argv[4],int(sys.argv[5]),sys.argv[6],*(map(int,sys.argv[7:9])))
        elif sys.argv[1]=='seed':result=seed(sys.argv[2])
        else:raise ValueError('Unknown lab operation')
        print(json.dumps(result))
    except Exception as e:raise SystemExit('PR41 synthetic lab refused: '+type(e).__name__+'. Keep private files; do not reset or overwrite.')
