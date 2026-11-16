"""Reviewable source-only kit. Exports the exact PR head, never owner workbooks or data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from deploy.release_candidate.bundle import export,verify
FORMAT='instrakt-pr46-test-v1'


def build(target):
    root=Path(target)
    if root.exists():raise FileExistsError('New output directory required')
    sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();root.mkdir()
    export(sha,root/'app-source','postgresql')
    files={'tools/Start-Writeoff-Preview.ps1':'Start-Writeoff-Preview.ps1','tools/writeoff_demo/demo.py':'demo.py',
           'tools/writeoff_demo/fixture.py':'fixture.py','tools/writeoff_demo/pg-start.sh':'pg-start.sh','docs/PR46_PREVIEW.md':'README-RU.md'}
    for source,target in files.items():(root/target).write_bytes(Path(source).read_bytes().replace(b'\r\n',b'\n'))
    manifest={'format':FORMAT,'app_sha':sha,'files':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}}
    (root/'package-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');check(root)


def check(root):
    root=Path(root);data=json.loads((root/'package-manifest.json').read_text());sha=verify(root/'app-source')
    if data['format']!=FORMAT or data['app_sha']!=sha:raise ValueError('Incorrect preview identity')
    actual={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p.name!='package-manifest.json'}
    if actual!=data['files'] or any(p.is_symlink() for p in root.rglob('*')):raise ValueError('Preview package changed')
    return {'verified':True,'app_sha':sha,'files':len(actual),'corporate_data_included':False}

if __name__=='__main__':
    if sys.argv[1]=='build':build(sys.argv[2])
    print(json.dumps(check(sys.argv[2])))
