"""Export only committed release source; no checkout data, credentials or backups.

The bundle's Dockerfile stamps the source commit without Amvera build-arg support.
It is a delivery artifact to review; this command never pushes or deploys anything.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ACCEPTED='b7b0c626b31c8152890ffa040e98809d8f54db74'
CONFIG='''meta:
  environment: docker
  toolchain:
    name: docker
    version: latest
build:
  dockerfile: Dockerfile.offline-release
run:
  persistenceMount: /data
  containerPort: 8000
'''


def allowed(path):
    return bool(path in ('.dockerignore','requirements.txt','constraints.txt','Dockerfile.offline-release','assets/instrakt-logo.svg') or
        re.fullmatch(r'server/[^/]+\.py',path) or re.fullmatch(r'server/postgres/[^/]+\.sql',path) or
        re.fullmatch(r'deploy/offline_release/[^/]+\.py',path) or
        re.fullmatch(r'(offline|release_online)/[^/]+\.(js|css|html|svg|webmanifest|txt)',path))


def git(*args):return subprocess.run(['git',*args],check=True,capture_output=True).stdout


def export(ref,output):
    if not re.fullmatch('[a-f0-9]{40}',ref):raise ValueError('Full source SHA required')
    git('merge-base','--is-ancestor',ACCEPTED,ref)
    root=Path(output).absolute()
    if root.exists() or root.is_symlink():raise FileExistsError('New output directory required')
    entries=[]
    for entry in git('ls-tree','-r','-z',ref).split(b'\0'):
        if not entry:continue
        metadata,path=entry.split(b'\t',1);path=path.decode('utf-8');mode,kind,blob=metadata.decode().split()
        if not allowed(path):continue
        if mode not in ('100644','100755') or kind!='blob':raise ValueError('Regular committed source files only')
        entries.append((path,blob))
    if not any(path=='Dockerfile.offline-release' for path,_ in entries):raise ValueError('Missing release Dockerfile')
    root.mkdir(parents=True,mode=0o700)
    for path,blob in entries:
        value=git('cat-file','blob',blob)
        if path=='Dockerfile.offline-release':
            if value.count(b'ARG KSU_BUILD_SHA\n')!=1:raise ValueError('Unexpected build identity instruction')
            value=value.replace(b'ARG KSU_BUILD_SHA\n',('ARG KSU_BUILD_SHA='+ref+'\n').encode())
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as out:out.write(value)
    (root/'amvera.yml').write_text(CONFIG)
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}
    manifest={'format':'ksu-source-delivery-v1','source_sha':ref,'accepted_ancestor':ACCEPTED,
        'transformations':['Dockerfile.offline-release: default KSU_BUILD_SHA','amvera.yml: explicit release build, existing /data'],
        'files':files}
    (root/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    verify(root)
    return {'source_sha':ref,'source_files':len(files),'corporate_data_included':False,'deployed':False}


def verify(root):
    root=Path(root)
    if (root/'release-manifest.json').is_symlink():raise ValueError('Delivery manifest symlink forbidden')
    manifest=json.loads((root/'release-manifest.json').read_text())
    if manifest.get('format')!='ksu-source-delivery-v1' or not re.fullmatch('[a-f0-9]{40}',manifest['source_sha']):raise ValueError('Invalid source manifest')
    actual={}
    for path in root.rglob('*'):
        if path.is_symlink():raise ValueError('Delivery symlinks forbidden')
        if not path.is_file() or path==root/'release-manifest.json':continue
        name=path.relative_to(root).as_posix()
        if not allowed(name) and name!='amvera.yml':raise ValueError('Non-source delivery file')
        actual[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=manifest['files']:raise ValueError('Source delivery hashes differ')
    if (root/'amvera.yml').read_text()!=CONFIG:raise ValueError('Unreviewed delivery configuration')
    return manifest['source_sha']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref');parser.add_argument('--output');parser.add_argument('--verify')
    args=parser.parse_args()
    if args.verify:print(json.dumps({'verified_source_sha':verify(args.verify)}))
    elif args.ref and args.output:print(json.dumps(export(args.ref,args.output)))
    else:parser.error('Use --ref FULL_SHA --output NEW_DIRECTORY, or --verify DIRECTORY')


if __name__=='__main__':main()
