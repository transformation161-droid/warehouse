"""Owner-local PERSONAL viewer creation on a closed staging/candidate copy only. No HTTP route."""
import json
from pathlib import Path
from uuid import uuid4
from .database import Database,password_hash,now
from .release_schema import inspect_sqlite


def add_to_copy(path,values,closed=False):
    path=Path(path)
    if not closed or any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):raise ValueError('Closed staging COPY required')
    if inspect_sqlite(path)['version']!=4:raise ValueError('Explicit migration to v4 required')
    if set(values)!={'username','name','password','siteIds'} or not isinstance(values['siteIds'],list):raise ValueError('Explicit personal account fields and siteIds required')
    if not 2<=len(values['username'])<=120 or not 1<=len(values['name'])<=120 or len(values['password'])<14 or values['username'].lower() in ('guest','viewer'):
        raise ValueError('Use a personal login/name and a strong password, never a shared guest')
    sites=sorted(set(values['siteIds']));ident=uuid4().hex;db=Database(path)
    with db.connect() as con:
        for site in sites:
            if not con.execute("select 1 from records where collection='sites' and id=?",(site,)).fetchone():raise ValueError('Unknown ACL site')
        con.execute('insert into users values(?,?,?,?,?,1)',(ident,values['username'],values['name'],'viewer',password_hash(values['password'])))
        for site in sites:con.execute('insert into user_sites values(?,?)',(ident,site))
        con.execute('insert into audit(at,actor_id,collection,record_id,operation,after_json) values(?,?,?,?,?,?)',
                    (now(),'owner-local','users',ident,'viewer-create',json.dumps({'id':ident,'role':'viewer','siteIds':sites})))
    return {'id':ident,'role':'viewer','siteIds':sites,'auditRecorded':True}


if __name__=='__main__':
    import argparse,sys
    parser=argparse.ArgumentParser();parser.add_argument('--database',required=True);parser.add_argument('--closed-copy',action='store_true',required=True);args=parser.parse_args()
    print(json.dumps(add_to_copy(args.database,json.load(sys.stdin),args.closed_copy)))
