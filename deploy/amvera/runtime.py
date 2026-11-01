"""One replica, one persistent /data, one Uvicorn worker; no platform API or spending."""
import fcntl
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from server.database import Database, seed_demo
from server.maintenance import validate_database
from server.storage import check_storage
from deploy.rotate_backups import rotate

UID=10001
GID=10001


def is_mount(path):
    # Unlike os.path.ismount, mountinfo also detects same-filesystem bind mounts.
    return any(line.split()[4]==str(path) for line in Path('/proc/self/mountinfo').read_text().splitlines())


def validate_origin(origin):
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('KSU_ORIGIN must be the exact public HTTPS origin, without credentials or path')


def prepare_data(path):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink() or not is_mount(path):
        raise ValueError('Persistent data mount is missing; refusing ephemeral storage')
    if os.getuid()==0:
        os.chown(path,UID,GID)
        os.chmod(path,0o700)
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    if os.getuid()!=UID:
        raise ValueError('Runtime must use the dedicated UID 10001')
    os.umask(0o077)
    lock=path/'.runtime.lock'
    if lock.is_symlink():raise ValueError('Unexpected symlink')
    descriptor=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise ValueError('Another replica is already using this data volume')
    return descriptor


def initialize(database, bootstrap):
    database=Path(database)
    if database.is_symlink():raise ValueError('Unexpected database symlink')
    if not database.exists():
        if not bootstrap:raise ValueError('Database is absent; explicit one-time demo bootstrap is required')
        credentials=database.parent/'demo-accounts.txt'
        if credentials.exists():raise ValueError('Credentials already exist but database is missing; restore instead of reinitializing')
        passwords={role:secrets.token_urlsafe(24) for role in ('manager','inspector')}
        seed_demo(Database(database),passwords['manager'],passwords['inspector'])
        descriptor=os.open(credentials,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as output:
            output.write('ТОЛЬКО ДЕМОНСТРАЦИЯ. Не добавлять в Git.\n')
            for role,password in passwords.items():output.write(f'\nЛогин: {role}\nПароль: {password}\n')
    validate_database(database)
    check_storage(database)


def main():
    validate_origin(os.environ.get('KSU_ORIGIN',''))
    root=Path('/data')
    database=Path(os.getenv('KSU_DB',str(root/'ksu.sqlite3')))
    if database.parent!=root or database.suffix!='.sqlite3':raise ValueError('Database must be directly inside /data')
    descriptor=prepare_data(root)
    initialize(database,os.getenv('KSU_BOOTSTRAP_DEMO')=='1')
    os.environ['KSU_REQUIRE_INITIALIZED']='1'
    os.environ['KSU_SECURE_COOKIES']='1'
    os.environ['KSU_TRUST_LOCAL_PROXY']='0'
    process=subprocess.Popen([sys.executable,'-m','server','run','--host','0.0.0.0','--port','8000'])
    stopped=threading.Event()
    def stop(signum,frame):
        stopped.set()
        if process.poll() is None:process.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    next_backup=0
    try:
        while process.poll() is None:
            if not stopped.is_set() and time.monotonic()>=next_backup:
                try:
                    check_storage(database,reserve=database.stat().st_size+8*1024*1024)
                    rotate(database,root/'backups',keep=int(os.getenv('KSU_BACKUP_KEEP','2')))
                    print('Verified local snapshot complete',flush=True)
                except Exception as error:
                    # Never print credential values or data records into platform logs.
                    print('Snapshot failed:',type(error).__name__,flush=True)
                next_backup=time.monotonic()+24*3600
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:pass
            if stopped.is_set():
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        if not stopped.is_set():raise SystemExit(process.returncode or 1)
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=15)
        os.close(descriptor)


if __name__=='__main__':main()
