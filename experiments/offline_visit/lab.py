"""Standalone synthetic SQLite lab. Never connects to an inherited PostgreSQL DSN."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
from datetime import date
from uuid import uuid4

from server.database import Database,seed_demo
from server.models import Changes
from server.service import apply_changes

FORMAT='ksu-offline-visit-lab-v1'


def initialize(directory):
    root=Path(directory).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker=root/'stage2-lab.json';database=root/'ksu-stage2.sqlite3';accounts=root/'demo-accounts.txt'
    if marker.exists():
        value=json.loads(marker.read_text())
        if value.get('format')!=FORMAT or value.get('backend')!='sqlite' or not re.fullmatch('[a-f0-9]{32}',value.get('labId','')) or any(p.is_symlink() for p in (marker,database,accounts)) or not database.is_file() or not accounts.is_file():
            raise ValueError('Invalid lab marker/data; refusing any automatic repair or reseed')
        return database,value['labId']
    if any(root.iterdir()):raise ValueError('New lab directory must be empty. No existing data may be adopted')
    manager=secrets.token_urlsafe(24);inspector=secrets.token_urlsafe(24)
    db=Database(database);seed_demo(db,manager,inspector)
    apply_changes(db,db.user('manager'),Changes.model_validate({'mutationId':uuid4().hex,'changes':[{
        'collection':'visits','id':'DEMO-stage2-visit','expectedVersion':0,
        'data':{'siteId':'s1','date':date.today().isoformat(),'assignedTo':'inspector','goal':'ДЕМО · Полный офлайн-выезд КСУ'}}]}))
    descriptor=os.open(accounts,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
        handle.write(f'ДЕМО · Только эта изолированная установка\nmanager: {manager}\ninspector: {inspector}\n')
    lab_id=secrets.token_hex(16)
    descriptor=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w') as handle:json.dump({'format':FORMAT,'backend':'sqlite','labId':lab_id},handle)
    return database,lab_id


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True);parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8012);parser.add_argument('--origin',default='http://127.0.0.1:8012')
    parser.add_argument('--cert');parser.add_argument('--key');args=parser.parse_args()
    if args.port==8003:parser.error('Port 8003 is reserved for the existing installation')
    if bool(args.cert)!=bool(args.key):parser.error('Certificate and key are required together')
    if not args.cert and args.origin!=f'http://127.0.0.1:{args.port}':parser.error('LAN access requires trusted HTTPS')
    if args.cert and not args.origin.startswith('https://'):parser.error('Certificate requires HTTPS origin')
    if os.getenv('KSU_DATABASE_URL') or os.getenv('KSU_PG_ADMIN_DSN'):parser.error('Inherited database DSN is forbidden in this standalone lab')
    database,lab_id=initialize(args.directory)
    os.environ['KSU_OFFLINE_VISIT_LAB_ID']=lab_id
    os.environ['KSU_DB_MAX_BYTES']='268435456';os.environ['KSU_DATA_MAX_BYTES']='2147483648'
    from server.app import create_app
    import uvicorn
    app=create_app(database,args.origin,bool(args.cert))
    print('SYNTHETIC STAGE 2. Accounts are in the private lab/demo-accounts.txt; no existing database was mounted.',flush=True)
    uvicorn.run(app,host=args.host,port=args.port,ssl_certfile=args.cert,ssl_keyfile=args.key,access_log=False)


if __name__=='__main__':main()
