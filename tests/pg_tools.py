"""Use the pinned image's real PG17 clients on the Linux test host, without apt/network."""
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4
from tests.pg_environment import docker


def prepare(image,root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    dependencies=docker('run','--rm','--entrypoint','/usr/bin/ldd',image,
        '/usr/lib/postgresql/17/bin/pg_dump','/usr/lib/postgresql/17/bin/pg_restore',capture_output=True,text=True).stdout
    paths=set()
    for line in dependencies.splitlines():
        for word in line.split():
            if word.startswith('/') and '.so' in word:paths.add(word)
    loader=next(path for path in paths if 'ld-linux' in path)
    name='ksu-client-tools-'+uuid4().hex[:12]
    docker('create','--name',name,image,'true',stdout=subprocess.DEVNULL)
    try:
        for source in sorted(paths):
            docker('cp','-L',name+':'+source,str(root/Path(source).name),stdout=subprocess.DEVNULL)
        env={}
        for tool in ('pg_dump','pg_restore'):
            docker('cp','-L',name+':/usr/lib/postgresql/17/bin/'+tool,str(root/tool),stdout=subprocess.DEVNULL)
            wrapper=root/(tool+'-host')
            wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(str(root/Path(loader).name))+' --library-path '+shlex.quote(str(root))+' '+shlex.quote(str(root/tool))+' "$@"\n')
            wrapper.chmod(0o755);env['KSU_'+tool.upper()+'_BIN']=str(wrapper)
        return env
    finally:docker('rm','-f',name,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
