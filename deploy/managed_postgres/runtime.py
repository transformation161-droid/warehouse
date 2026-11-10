"""Managed production entrypoint: PostgreSQL only, runtime credentials only, no SQLite /data access."""
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def configure():
    from server.managed_pg import binding,tls
    from server.offline_release import Policy
    if 'KSU_OFFLINE_MODE' not in os.environ:raise ValueError('Explicit release policy required')
    Policy.environment()
    origin=os.getenv('KSU_ORIGIN','');parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:raise ValueError('Exact HTTPS origin required')
    if os.getuid()==0:raise ValueError('Dedicated non-root runtime required')
    if any(os.getenv(key) for key in ('PGSERVICE','PGPASSWORD','PGHOST','PGUSER','PGDATABASE')):raise ValueError('Implicit libpq credentials/defaults forbidden')
    if os.getenv('KSU_DB') or os.getenv('KSU_LOCAL_BACKUP_MODE','off')!='off':raise ValueError('SQLite settings forbidden in managed production')
    for key in ('KSU_PG_ADMIN_DSN','KSU_PG_BOOTSTRAP_DSN','KSU_PG_BACKUP_DSN','KSU_PG_RUNTIME_PASSWORD','KSU_PG_BACKUP_PASSWORD'):
        if os.getenv(key) or os.getenv(key+'_FILE'):raise ValueError('Administrator/backup credentials must stay outside application')
    dsn=binding('KSU_DATABASE_URL');info=tls(dsn)
    if info['user']!='ksu_runtime':raise ValueError('Dedicated runtime role required')
    build=Path('/app/RELEASE_SHA').read_text().strip()
    if not re.fullmatch('[a-f0-9]{40}',build):raise ValueError('Pinned build identity required')
    # No fcntl/SQLite/backup on Amvera NFS. Import scratch is disposable and local.
    work=Path('/tmp/ksu-managed-work')
    mounts=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before,after=line.split(' - ',1);mount=Path(before.split()[4])
        if work.is_relative_to(mount):mounts.append((len(mount.parts),after.split()[0]))
    if not mounts or max(mounts)[1] in ('nfs','nfs4','cifs','smb3','9p','ceph'):raise ValueError('Container-local scratch required')
    if work.is_symlink():raise ValueError('Private local scratch required')
    work.mkdir(mode=0o700,exist_ok=True)
    from server.storage import check_storage
    os.environ.update(KSU_DATABASE_URL=dsn,KSU_WORK_DIR=str(work),KSU_BACKUP_VOLUME_ROOT=str(work),
        KSU_SCHEMA_MODE='verified',KSU_REQUIRE_INITIALIZED='1',KSU_SECURE_COOKIES='1',
        KSU_TRUST_LOCAL_PROXY='0',KSU_RELEASE_BUILD_SHA=build)
    os.environ.pop('KSU_DATABASE_URL_FILE',None)
    check_storage(work/'staging')
    os.umask(0o077)
    return dsn


def main():
    from server.managed_pg import preflight
    try:
        dsn=configure();preflight(dsn,'runtime')
        from server.app import create_app
        app=create_app('postgresql')
    except Exception as error:raise SystemExit('Managed PostgreSQL preflight refused: '+type(error).__name__+'. No SQLite fallback, migration or provisioning was executed.')
    cert=os.getenv('KSU_RELEASE_TLS_CERT');key=os.getenv('KSU_RELEASE_TLS_KEY')
    if bool(cert)!=bool(key):raise SystemExit('TLS test certificate/key must be provided together')
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=8000,proxy_headers=False,access_log=False,ssl_certfile=cert,ssl_keyfile=key)


if __name__=='__main__':main()
