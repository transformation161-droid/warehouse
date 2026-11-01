"""Deployment agent: retrieve a verified off-host snapshot using an already trusted SSH host."""
import argparse
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.maintenance import validate_database


def fetch(host, identity, directory):
    if not re.fullmatch(r'[a-z][a-z0-9_-]*@[a-zA-Z0-9.-]+', host):
        raise ValueError('Expected user@hostname, without shell characters')
    folder=Path(directory);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    remote=f'/srv/ksu-data/backups/offsite-{stamp}.sqlite3'
    destination=folder/f'ksu-{stamp}.sqlite3'
    # Strict host-key checking is mandatory. Private key contents are never read or printed.
    command=['ssh','-i',str(identity),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',host]
    subprocess.run(command+['sudo','-n','-u','ksu','env','PYTHONPATH=/srv/ksu/current','/srv/ksu/current/.venv/bin/python','-m','server',
                            'backup',remote,'--db','/srv/ksu-data/ksu.sqlite3'],check=True)
    descriptor=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(descriptor,'wb') as output:
            subprocess.run(command+['sudo','-n','-u','ksu','cat',remote],stdout=output,check=True)
        validate_database(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print('Verified off-host snapshot:',destination)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',required=True)
    parser.add_argument('--identity',required=True)
    parser.add_argument('--directory',required=True)
    options=parser.parse_args();fetch(options.host,options.identity,options.directory)
