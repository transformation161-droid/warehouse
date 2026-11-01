"""Push an already-tested commit to an existing, explicitly approved Amvera project.
Does not create applications, choose tariffs, scale instances or top up a balance.
"""
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit


def validate_target(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='git.amvera.ru' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise ValueError('Use the approved HTTPS Amvera Git URL without credentials')
    parts=parsed.path.strip('/').split('/')
    if len(parts)!=2 or not all(p and all(c.isalnum() or c in '._-' for c in p) for p in parts):
        raise ValueError('Expected /account/project, with no shell characters')
    return url


def main():
    if os.getenv('KSU_AMVERA_RELEASE_ENABLED')!='true':raise SystemExit('Release is disabled until account, exact price and existing project are approved')
    target=validate_target(os.environ['AMVERA_GIT_URL'])
    for key in ('AMVERA_GIT_LOGIN','AMVERA_GIT_PASSWORD'):
        if not os.getenv(key):raise SystemExit('Secure Git authorization is missing')
    branch=os.getenv('AMVERA_GIT_BRANCH','master')
    subprocess.run(['git','check-ref-format','--branch',branch],check=True,stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix='ksu-askpass-') as folder:
        askpass=Path(folder)/'askpass.py'
        askpass.write_text('''#!/usr/bin/env python3
import os,sys
if 'username' in sys.argv[1].lower(): print(os.environ['AMVERA_GIT_LOGIN'])
else: print(os.environ['AMVERA_GIT_PASSWORD'])
''')
        askpass.chmod(0o700)
        environment={**os.environ,'GIT_ASKPASS':str(askpass),'GIT_TERMINAL_PROMPT':'0'}
        subprocess.run(['git','push',target,'HEAD:refs/heads/'+branch],env=environment,check=True)
    print('Tested commit delivered to the already approved Amvera project')


if __name__=='__main__':main()
