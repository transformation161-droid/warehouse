"""Opt-in daily verified SQLite copies on the existing volume; no network/service.

Two local copies help with logical recovery, not loss of the Amvera account/volume.
Only this namespace's verified old files may be pruned, after a new independent restore.
"""
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
from uuid import uuid4

from .backup_job import atomic_json,verify,MAXIMUM
from .maintenance import snapshot_database
from .storage import directory_bytes,StorageFull

FORMAT='ksu-local-backup-v1'
NAME=re.compile(r'local-v4-\d{8}T\d{12}Z-[a-f0-9]{32}\.sqlite3')


def utcnow():return datetime.now(timezone.utc)


class LocalBackups:
    @classmethod
    def environment(cls,db,volume,gate):
        mode=os.getenv('KSU_LOCAL_BACKUP_MODE','off')
        if mode not in ('off','daily'):raise ValueError('Invalid local backup mode')
        if mode=='off':return None
        if db.backend!='sqlite':raise ValueError('Local daily backup supports SQLite only')
        return cls(db.path,volume,Path(os.getenv('KSU_LOCAL_BACKUP_DIR',str(Path(volume)/'backup-v4'))),gate)

    def __init__(self,source,volume,directory,gate=None):
        if Path(source).is_symlink():raise ValueError('Regular backup source required')
        self.source=Path(source).resolve(strict=True);self.volume=Path(volume).resolve(strict=True)
        self.directory=Path(os.path.abspath(directory));self.gate=gate or threading.BoundedSemaphore(1)
        self.stopped=threading.Event();self.thread=None
        if not self.source.is_relative_to(self.volume) or not self.directory.resolve().is_relative_to(self.volume) or self.directory.resolve()==self.volume:
            raise ValueError('Backup source and dedicated directory must be on the existing volume')
        cursor=self.directory
        while cursor!=self.volume:
            if cursor.is_symlink():raise ValueError('Symlinks in backup paths are forbidden')
            cursor=cursor.parent
        marker=self.directory/'namespace.json'
        expected={'format':FORMAT,'source':str(self.source.relative_to(self.volume))}
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if marker.exists():
            if marker.is_symlink() or json.loads(marker.read_text())!=expected:raise ValueError('Different/untrusted local backup namespace')
        elif any(self.directory.iterdir()):raise ValueError('New empty backup directory required')
        else:atomic_json(marker,expected,exclusive=True)

    def state(self):
        path=self.directory/'status.json'
        if path.is_symlink():raise ValueError('Unexpected status symlink')
        return json.loads(path.read_text()) if path.exists() else {}

    def summary(self,now=None):
        try:
            value=self.state();last=value.get('lastSuccess') or {};age=((now or utcnow())-datetime.fromisoformat(last['verifiedAt'])).total_seconds()
            path=self.directory/last['file']
            good=bool(value.get('lastAttempt')=='success' and last.get('restored') and NAME.fullmatch(last['file']) and
                      0<=age<=26*3600 and path.is_file() and not path.is_symlink() and path.stat().st_size==last['bytes'])
            return {'enabled':True,'healthy':good,'lastAttempt':value.get('lastAttempt','never')}
        except (OSError,ValueError,KeyError,TypeError):return {'enabled':True,'healthy':False,'lastAttempt':'unknown'}

    def owned(self):
        result=[]
        for path in self.directory.iterdir():
            if not NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():continue
            proof=path.with_suffix('.verification.json')
            if proof.is_symlink() or not proof.is_file():continue
            value=json.loads(proof.read_text())
            if value.get('format')==FORMAT and value.get('file')==path.name and value.get('restored'):result.append(path)
        return sorted(result)

    def run_once(self,now=None):
        now=now or utcnow()
        if not self.gate.acquire(blocking=False):return None
        try:
            descriptor=os.open(self.directory/'.backup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            with os.fdopen(descriptor,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                previous=self.state()
                try:
                    with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True)) as con:
                        size=con.execute('pragma page_count').fetchone()[0]*con.execute('pragma page_size').fetchone()[0]
                    if size>MAXIMUM:raise StorageFull('SQLite cap reached')
                    # Allow the live DB to grow up to its cap while snapshotting:
                    # new snapshot + two independent restore files + fixed reserve.
                    reserve=3*MAXIMUM+128*1024**2
                    cap=int(os.getenv('KSU_DATA_MAX_BYTES','2147483648'))
                    if cap<=0 or directory_bytes(self.volume)+reserve>cap or shutil.disk_usage(self.volume).free<reserve:
                        raise StorageFull('Insufficient local backup reserve; no deletion or paid growth')
                    with tempfile.TemporaryDirectory(prefix='.local-work-',dir=self.directory) as work:
                        incoming=Path(work)/'snapshot.sqlite3'
                        snapshot_database(self.source,incoming,timeout=120,maximum=MAXIMUM)
                        result=verify(incoming,work)
                        filename='local-v4-'+now.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid4().hex+'.sqlite3'
                        target=self.directory/filename
                        os.link(incoming,target)  # No overwrite; snapshot/verify already fsync their files.
                        result.update(format=FORMAT,file=filename,verifiedAt=now.isoformat())
                        atomic_json(target.with_suffix('.verification.json'),result,exclusive=True)
                    # Never prune unverified/foreign files or the previous successful backup.
                    protected={filename,(previous.get('lastSuccess') or {}).get('file')}
                    owned=self.owned()
                    for old in [p for p in owned if p.name not in protected][:max(0,len(owned)-2)]:
                        old.unlink();old.with_suffix('.verification.json').unlink()
                    atomic_json(self.directory/'status.json',{'lastAttempt':'success','lastSuccess':result})
                    return result
                except Exception as error:
                    atomic_json(self.directory/'status.json',{'lastAttempt':'failed','lastSuccess':previous.get('lastSuccess'),
                        'attemptAt':now.isoformat(),'errorType':type(error).__name__})
                    raise
        finally:self.gate.release()

    def tick(self,now=None):
        now=now or utcnow();state=self.state();last=state.get('lastSuccess')
        if state.get('lastAttempt')=='failed':
            age=(now-datetime.fromisoformat(state['attemptAt'])).total_seconds()
            if 0<=age<15*60:return False
        elif last:
            age=(now-datetime.fromisoformat(last['verifiedAt'])).total_seconds()
            if 0<=age<24*3600:return False
        return self.run_once(now) is not None

    def start(self):
        def loop():
            while not self.stopped.is_set():
                try:self.tick()
                except Exception as error:print('Local backup failed: '+type(error).__name__,flush=True)
                self.stopped.wait(60)
        self.thread=threading.Thread(target=loop,name='verified-local-backup',daemon=True);self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread:self.thread.join(timeout=5)
