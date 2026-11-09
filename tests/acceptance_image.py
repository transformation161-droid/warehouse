"""Explicit synthetic helper image seeds once; production still has no seed helper."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from server.release_schema import checksum,inspect_sqlite
from tests.pg_environment import docker


def run(image):
    with tempfile.TemporaryDirectory(prefix='ksu-android-synthetic-') as directory:
        root=Path(directory)
        args=['run','--rm','--pull','never','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
              '-e','KSU_SCHEMA_MODE=explicit-copy-migration','-v',str(root)+':/data',image]
        docker(*args,stdout=__import__('subprocess').DEVNULL)
        marker=json.loads((root/'synthetic-acceptance.json').read_text());assert marker['syntheticOnly'] is True
        accounts=json.loads((root/'accounts.json').read_text());source=root/'ksu.sqlite3';before=checksum(source)
        failed=docker(*args,check=False,capture_output=True,text=True);assert failed.returncode!=0 and checksum(source)==before
        assert inspect_sqlite(source)['version']==4
        for mode in ('pilot','off'):
            with patch.dict(os.environ,{'KSU_OFFLINE_MODE':mode,'KSU_OFFLINE_DEPLOYMENT_ID':'a'*32,'KSU_OFFLINE_USER_IDS':'inspector'}):
                app=create_app(source,'http://testserver',False)
                try:
                    for role in accounts:
                        with TestClient(app,headers={'Origin':'http://testserver'}) as client:
                            login=client.post('/api/login',json={'username':'demo-'+role,'password':accounts[role]});assert login.status_code==200
                            client.headers['X-CSRF-Token']=login.json()['csrf']
                            context=client.get('/api/offline-visit/context').json()
                            assert context['enabled']==(mode=='pilot' and role=='inspector')
                            if role in ('viewer','unassigned'):
                                assert client.post('/api/offline/packages',json={}).status_code==403
                                assert len(client.get('/api/state').json()['sites'])==(1 if role=='viewer' else 0)
                finally:app.state.import_jobs.close()
        print('PASS: synthetic helper seeds once, existing input refused unchanged, v4, all four roles, pilot/off matrix')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);run(parser.parse_args().image)
