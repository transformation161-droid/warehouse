"""Release entrypoint: existing verified storage only, no bootstrap, provisioning or migration."""
import fcntl
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def configure():
    from server.offline_release import Policy
    if 'KSU_OFFLINE_MODE' not in os.environ:raise ValueError('Explicit release mode required')
    Policy.environment()
    origin=os.environ.get('KSU_ORIGIN','');parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Exact HTTPS origin required for release/staging')
    if os.getuid()==0:raise ValueError('Release runtime must run as a dedicated non-root user')
    root=Path('/data')
    if not any(line.split()[4]=='/data' for line in Path('/proc/self/mountinfo').read_text().splitlines()):raise ValueError('Persistent /data mount required')
    if not os.access(root,os.R_OK|os.W_OK|os.X_OK):raise ValueError('Existing /data permissions must be prepared by the owner, not repaired at startup')
    descriptor=os.open(root/'.offline-release.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    os.set_inheritable(descriptor,True)
    build=(Path('/app/RELEASE_SHA')).read_text().strip()
    if not re.fullmatch('[a-f0-9]{40}',build):raise ValueError('Pinned build identity missing')
    os.environ.update(KSU_RELEASE_BUILD_SHA=build,KSU_SCHEMA_MODE='verified',KSU_REQUIRE_INITIALIZED='1',KSU_SECURE_COOKIES='1',KSU_TRUST_LOCAL_PROXY='0')
    os.environ['KSU_WORK_DIR']=str(root/'release-work')
    if not os.environ.get('KSU_DATABASE_URL'):
        source=Path(os.environ.get('KSU_DB','/data/ksu.sqlite3'))
        if not source.resolve().is_relative_to(root) or source.is_symlink():raise ValueError('Database must be an existing regular file under /data')
        from server.release_schema import inspect_sqlite
        if inspect_sqlite(source)['version']!=3:raise ValueError('Explicit migration on a separate copy required')
    os.umask(0o077)
    return descriptor


def main():
    try:descriptor=configure()
    except Exception as error:raise SystemExit('Release preflight refused startup: '+type(error).__name__+'. Inspect private configuration; no migration or bootstrap was executed.')
    cert=os.environ.get('KSU_RELEASE_TLS_CERT');key=os.environ.get('KSU_RELEASE_TLS_KEY')
    if bool(cert)!=bool(key):raise SystemExit('TLS certificate/key must be provided together')
    from server.app import create_app
    import uvicorn
    try:uvicorn.run(create_app(),host='0.0.0.0',port=8000,ssl_certfile=cert,ssl_keyfile=key,proxy_headers=False,access_log=False)
    finally:os.close(descriptor)


if __name__=='__main__':main()
