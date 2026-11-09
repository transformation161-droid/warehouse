"""Owner-side scheduled job: verified HTTPS, streaming download, independent restore.

Credentials enter via stdin only. Never send a backup to GitHub or any third party.
All backup files are append-only; a failed run preserves the last verified success.
"""
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import build_opener,HTTPSHandler,HTTPCookieProcessor,HTTPRedirectHandler,ProxyHandler,Request
from uuid import uuid4

from .maintenance import snapshot_database
from .pg_admin import fingerprints
from .release_schema import checksum,inspect_sqlite

MAXIMUM=256*1024*1024


def atomic_json(path,value,exclusive=False):
    temporary=path.with_name('.'+path.name+'.'+uuid4().hex)
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as out:json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
    try:
        if exclusive:os.link(temporary,path)
        else:os.replace(temporary,path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:temporary.unlink(missing_ok=True)


def verify(source,work):
    source=Path(source);before=checksum(source)
    with tempfile.TemporaryDirectory(prefix='.private-restore-',dir=work) as directory:
        root=Path(directory);copy=root/'input.sqlite3';shutil.copyfile(source,copy);copy.chmod(0o600)
        version=inspect_sqlite(copy)['version']
        if version!=4:raise ValueError('Verified v4 backup required; no automatic migration in backup jobs')
        with closing(sqlite3.connect(copy)) as con:
            if con.execute('select count(*) from sessions').fetchone()[0] or con.execute('select count(*) from attempts').fetchone()[0]:
                raise ValueError('Backup contains live sessions')
            expected=fingerprints(con)
        restored=root/'restored.sqlite3';snapshot_database(copy,restored,timeout=120,maximum=MAXIMUM)
        if inspect_sqlite(restored)['version']!=4:raise ValueError('Restore schema mismatch')
        with closing(sqlite3.connect(restored)) as con:
            if fingerprints(con)!=expected:raise ValueError('Independent restore business checksums differ')
    if checksum(source)!=before:raise ValueError('Input changed during verification')
    return {'sha256':before,'bytes':source.stat().st_size,'schema':version,'restored':True,'integrity_fk':'ok','business_tables_identical':True}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Backup redirects are forbidden')


def run_once(origin,directory,credentials,ca=None,budget=2*1024**3):
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Exact approved HTTPS origin required')
    root=Path(directory);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.is_symlink():raise ValueError('Private regular directory required')
    with (root/'.job.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=json.loads((root/'status.json').read_text()) if (root/'status.json').exists() else {}
        try:
            used=sum(p.stat().st_size for p in root.iterdir() if p.is_file())
            # Reserve for download + private restore copies. Never prune existing backups.
            reserve=4*MAXIMUM+128*1024**2
            if used+MAXIMUM>budget or shutil.disk_usage(root).free<reserve:raise ValueError('Backup budget/disk reserve exhausted; no deletion or paid growth')
            jar=http.cookiejar.CookieJar();context=ssl.create_default_context(cafile=ca)
            opener=build_opener(HTTPSHandler(context=context),HTTPCookieProcessor(jar),NoRedirect())
            def post(path,body=None,csrf=None):
                headers={'Origin':origin,'Content-Type':'application/json'}
                if csrf:headers['X-CSRF-Token']=csrf
                return opener.open(Request(origin+path,json.dumps(body or {}).encode(),headers,method='POST'),timeout=150)
            with post('/api/login',credentials) as response:
                if response.status!=200:raise ValueError('Login failed')
                user=json.loads(response.read(65536))
            try:
                if user.get('role')!='manager':raise ValueError('Personal manager account required for backups')
                name='backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex
                part=root/(name+'.partial');deadline=time.monotonic()+300;digest=hashlib.sha256();size=0
                with post('/api/admin/backup',csrf=user['csrf']) as response,part.open('xb') as output:
                    os.chmod(part,0o600);expected=response.headers.get('X-Backup-SHA256','')
                    if len(expected)!=64 or response.status!=200:raise ValueError('Missing authenticated export checksum')
                    length=response.headers.get('Content-Length')
                    if not length or not length.isdecimal() or int(length)>MAXIMUM:raise ValueError('Missing/oversized content length')
                    while chunk:=response.read(65536):
                        size+=len(chunk)
                        if size>MAXIMUM or used+size>budget or time.monotonic()>deadline:raise ValueError('Backup transfer cap/deadline reached')
                        output.write(chunk);digest.update(chunk)
                    output.flush();os.fsync(output.fileno())
                    if size!=int(length) or digest.hexdigest()!=expected:raise ValueError('Truncated/mismatched backup')
                result=verify(part,root);target=root/(name+'.sqlite3')
                os.link(part,target);part.unlink()  # Own new partial only; existing backups never overwritten.
                result.update(file=target.name,verifiedAt=datetime.now(timezone.utc).isoformat())
                atomic_json(root/(name+'.verification.json'),result,exclusive=True)
                atomic_json(root/'status.json',{'lastAttempt':'success','lastSuccess':result})
                return result
            finally:
                try:
                    with post('/api/logout',csrf=user['csrf']):pass
                except Exception:pass
        except Exception as error:
            # No URLs, credentials, SQL or response bodies in operational diagnostics.
            atomic_json(root/'status.json',{'lastAttempt':'failed','errorType':type(error).__name__,'lastSuccess':previous.get('lastSuccess'),
                        'attemptAt':datetime.now(timezone.utc).isoformat()})
            raise


def health(directory,max_hours=26):
    value=json.loads((Path(directory)/'status.json').read_text());success=value.get('lastSuccess')
    return bool(value.get('lastAttempt')=='success' and success and success.get('restored') and
                0<=(datetime.now(timezone.utc)-datetime.fromisoformat(success['verifiedAt'])).total_seconds()<=max_hours*3600 and
                checksum(Path(directory)/success['file'])==success['sha256'])


def main():
    import argparse,sys
    parser=argparse.ArgumentParser();parser.add_argument('--origin');parser.add_argument('--directory',required=True);parser.add_argument('--ca')
    parser.add_argument('--health',action='store_true');args=parser.parse_args()
    if args.health:
        good=health(args.directory);print(json.dumps({'healthy':good}));raise SystemExit(0 if good else 1)
    try:result=run_once(args.origin,args.directory,json.load(sys.stdin),args.ca);print(json.dumps(result))
    except Exception as error:print(json.dumps({'success':False,'errorType':type(error).__name__}));raise SystemExit(1)


if __name__=='__main__':main()
