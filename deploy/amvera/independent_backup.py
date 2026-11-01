"""Download a manager-authorized snapshot and encrypt with the OWNER'S public certificate.
Only ciphertext may be uploaded to a free off-host artifact; no decryption key is held by CI.
"""
import argparse
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
from urllib.parse import urlsplit

import httpx

MAX_BYTES=16*1024*1024


def encrypt(source,certificate,destination):
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise FileExistsError('Refusing to overwrite an existing encrypted copy')
    result=subprocess.run(['openssl','cms','-encrypt','-aes-256-gcm','-binary','-outform','DER',
                           '-in',str(source),'-out',str(destination),str(certificate)],capture_output=True)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise RuntimeError('Backup encryption failed')
    destination.chmod(0o600)


def download(origin,password,certificate,destination):
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.username or parsed.query or parsed.fragment:
        raise ValueError('Expected public HTTPS origin')
    with tempfile.TemporaryDirectory(prefix='ksu-offsite-') as folder:
        source=Path(folder)/'backup.sqlite3'
        with httpx.Client(base_url=origin,verify=ssl.create_default_context(),timeout=60,trust_env=True) as client:
            response=client.post('/api/login',headers={'Origin':origin},json={'username':'manager','password':password})
            if response.status_code!=200:raise RuntimeError('Backup authentication failed')
            try:
                headers={'Origin':origin,'X-CSRF-Token':response.json()['csrf']}
                with client.stream('POST','/api/admin/backup',headers=headers) as response:
                    if response.status_code!=200:raise RuntimeError(f'Backup export failed: HTTP {response.status_code}')
                    descriptor=os.open(source,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    with os.fdopen(descriptor,'wb') as output:
                        size=0
                        for chunk in response.iter_bytes():
                            size+=len(chunk)
                            if size>MAX_BYTES:raise ValueError('Free off-host backup size limit exceeded')
                            output.write(chunk)
            finally:
                client.post('/api/logout',headers=headers if 'headers' in locals() else {'Origin':origin})
        # Validate before encrypting; temporary cleartext is never published as an artifact.
        from server.maintenance import validate_database
        validate_database(source)
        encrypt(source,certificate,destination)
    print('Verified encrypted off-host backup created')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--recipient',required=True)
    args=parser.parse_args()
    download(os.environ['KSU_PILOT_ORIGIN'],os.environ['KSU_PILOT_MANAGER_PASSWORD'],args.recipient,args.output)
