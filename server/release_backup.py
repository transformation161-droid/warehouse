"""Owner-local snapshot from a FULL downloaded copy of stopped /data, including WAL.

Never stops/downloads/opens the live Amvera database. Closed-copy confirmation is required.
Original files and existing backups are never modified or overwritten.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .release_schema import checksum,inspect_sqlite
from .maintenance import snapshot_database


def snapshot_frozen(directory,destination,confirmed=False):
    if not confirmed:raise ValueError('Owner must confirm a full frozen /data copy from a stopped application')
    root=Path(directory).resolve(strict=True);source=root/'ksu.sqlite3';target=Path(destination).absolute()
    paths=[root/('ksu.sqlite3'+suffix) for suffix in ('','-wal','-shm','-journal') if (root/('ksu.sqlite3'+suffix)).exists()]
    if not source.is_file() or any(p.is_symlink() or not p.is_file() for p in paths):raise ValueError('Regular copied database and journal files required')
    if target.exists() or target.is_symlink() or Path(str(target)+'.verification.json').exists() or target.resolve().is_relative_to(root):raise ValueError('Independent new backup path required')
    before={path.name:checksum(path) for path in paths};size=sum(path.stat().st_size for path in paths)
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if shutil.disk_usage(target.parent).free<size*3+128*1024*1024:raise ValueError('Insufficient space for private WAL recovery and independent backup')
    with tempfile.TemporaryDirectory(prefix='.frozen-recovery-',dir=target.parent) as private:
        copy=Path(private)/'ksu.sqlite3'
        for path in paths:
            if path.name.endswith('-shm'):continue  # Rebuild platform-specific WAL index on the private copy.
            output=Path(private)/path.name
            with path.open('rb') as incoming,output.open('xb') as handle:shutil.copyfileobj(incoming,handle,1024*1024)
            output.chmod(0o600)
        with closing(sqlite3.connect(copy)) as con:con.execute('select count(*) from sqlite_master').fetchone()
        inventory=inspect_sqlite(copy)
        snapshot_database(copy,target)
        after={path.name:checksum(path) for path in paths}
        current={p.name for p in root.iterdir() if p.name in ('ksu.sqlite3','ksu.sqlite3-wal','ksu.sqlite3-shm','ksu.sqlite3-journal')}
        if before!=after or current!=set(before):raise ValueError('Frozen copy changed; do not use the output or start staging')
        report={'schema_version':inventory['version'],'original_files_unchanged':True,'backup_sha256':checksum(target),
                'backup_bytes':target.stat().st_size,'sessions_revoked':True,'copied_wal':'ksu.sqlite3-wal' in before}
        descriptor=os.open(str(target)+'.verification.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'w') as handle:json.dump(report,handle,indent=2)
        return report


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--frozen-directory',required=True)
    parser.add_argument('--destination',required=True);parser.add_argument('--confirmed-stopped-copy',action='store_true',required=True)
    args=parser.parse_args()
    report=snapshot_frozen(args.frozen_directory,args.destination,args.confirmed_stopped_copy)
    print(json.dumps(report))


if __name__=='__main__':main()
