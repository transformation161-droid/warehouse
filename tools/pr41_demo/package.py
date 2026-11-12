"""Source-only Windows lab pack. Pinned app unchanged, no credentials or DBs."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from deploy.release_candidate.bundle import export,verify

APP_SHA='cc0b18238fdca4cd02876b5daf0cbef4481c3eb2'


def build(target):
    root=Path(target)
    if root.exists():raise FileExistsError('New package directory required')
    root.mkdir()
    export(APP_SHA,root/'app-source','postgresql')
    files={'tools/Start-PR41-Test.ps1':'Start-PR41-Test.ps1','tools/pr41_demo/demo.py':'demo.py',
           'tools/pr41_demo/pg-start.sh':'pg-start.sh','docs/PR41_WINDOWS_TEST.md':'README-RU.md'}
    for source,destination in files.items():(root/destination).write_bytes(Path(source).read_bytes().replace(b'\r\n',b'\n'))
    manifest={'format':'instrakt-pr41-test-v1','app_sha':APP_SHA,'tools_sha':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
              'files':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}}
    (root/'package-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    check(root)


def check(root):
    root=Path(root);data=json.loads((root/'package-manifest.json').read_text())
    if data['format']!='instrakt-pr41-test-v1' or data['app_sha']!=APP_SHA:raise ValueError('Incorrect pinned lab package')
    actual={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p.name!='package-manifest.json'}
    if actual!=data['files'] or any(p.is_symlink() for p in root.rglob('*')):raise ValueError('Package differs from manifest')
    if verify(root/'app-source')!=APP_SHA:raise ValueError('Application source changed')
    return {'verified':True,'app_sha':APP_SHA,'files':len(actual),'corporate_data_included':False}


if __name__=='__main__':
    if sys.argv[1]=='build':build(sys.argv[2])
    print(json.dumps(check(sys.argv[2])))
