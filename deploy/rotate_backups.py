"""Daily verified snapshots, bounded retention, no deletion before a successful new snapshot."""
import argparse
import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.maintenance import snapshot_database


def rotate(database, directory, keep=7):
    if keep < 2 or keep > 30:
        raise ValueError('Retention must be between 2 and 30 verified snapshots')
    folder=Path(directory);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=folder/'.backup.lock'
    descriptor=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(descriptor,'w') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target=folder/f'daily-{stamp}.sqlite3'
        snapshot_database(database,target)
        # Only this job's own regular snapshot files are eligible, never arbitrary backups/symlinks.
        snapshots=sorted(p for p in folder.glob('daily-*.sqlite3') if p.is_file() and not p.is_symlink())
        for old in snapshots[:-keep]:old.unlink()
        return target


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--keep',type=int,default=7)
    args=parser.parse_args()
    print('Verified daily snapshot:',rotate(args.db,args.directory,args.keep))
