"""Two-account acceptance against the actual HTTPS endpoint before/after a restart.
Use only the dedicated synthetic pilot; credentials are never accepted as CLI values or printed.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image


def require(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: expected {status}, got {response.status_code}')
    return response


def clients(origin, credentials, ca=None):
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Expected HTTPS origin without path, credentials or query')
    context=ssl.create_default_context(cafile=ca)
    result={}
    use_proxy=parsed.hostname not in ('localhost','127.0.0.1','::1')
    try:
        for role in ('manager','inspector'):
            client=httpx.Client(base_url=origin,verify=context,timeout=20,trust_env=use_proxy)
            result[role]=client
            response=require(client.post('/api/login',headers={'Origin':origin},json={'username':role,'password':credentials[role]}))
            for flag in ('Secure','HttpOnly','SameSite=strict'):
                if flag not in response.headers.get('set-cookie',''):raise ValueError('Unsafe session cookie')
            client.headers.update({'Origin':origin,'X-CSRF-Token':response.json()['csrf']})
        result['anonymous']=httpx.Client(base_url=origin,verify=context,timeout=20,trust_env=use_proxy)
        return result
    except Exception:
        for client in result.values():client.close()
        raise


def create_receipt(origin, accounts):
    manager,inspector=accounts['manager'],accounts['inspector']
    sites=require(inspector.get('/api/state')).json()['sites']
    if not sites or not sites[0]['name'].startswith('ДЕМО'):
        raise ValueError('Acceptance writes are permitted only to a marked synthetic site')
    site=sites[0]['id'];visit_id=uuid4().hex;finding_id=uuid4().hex
    def create(client,collection,ident,data):
        require(client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
            {'collection':collection,'id':ident,'expectedVersion':0,'data':data}]}))
    from datetime import date
    create(manager,'visits',visit_id,{'siteId':site,'date':date.today().isoformat(),'assignedTo':'inspector','assignee':'ДЕМО','goal':'ДЕМО · HTTPS приёмка'})
    if not any(v['id']==visit_id for v in require(inspector.get('/api/state')).json()['visits']):
        raise AssertionError('The assigned visit is not shared')
    output=io.BytesIO();Image.new('RGB',(16,16),'blue').save(output,format='PNG')
    photo=require(inspector.post('/api/media',json={'siteId':site,'content':base64.b64encode(output.getvalue()).decode()})).json()['url']
    create(inspector,'findings',finding_id,{'siteId':site,'title':'ДЕМО · HTTPS проверка сохранности','photo':photo})
    return {'origin':origin,'visit_id':visit_id,'finding_id':finding_id,'photo':photo,
            'photo_sha256':hashlib.sha256(require(manager.get(photo)).content).hexdigest()}


def verify_receipt(receipt, accounts):
    for role in ('manager','inspector'):
        state=require(accounts[role].get('/api/state')).json()
        if not any(x['id']==receipt['visit_id'] for x in state['visits']):raise AssertionError('Shared visit lost')
        if not any(x['id']==receipt['finding_id'] for x in state['findings']):raise AssertionError('Shared finding lost')
        photo=require(accounts[role].get(receipt['photo']))
        if hashlib.sha256(photo.content).hexdigest()!=receipt['photo_sha256']:raise AssertionError('Photo changed/lost')
    anonymous=accounts['anonymous']
    for path in ('/api/state','/api/history','/api/reports/xlsx',receipt['photo']):require(anonymous.get(path),401)
    for path in ('/.env','/var/ksu.sqlite3','/server/app.py','/backups/pilot.sqlite3','/demo-accounts.txt'):require(anonymous.get(path),404)
    response=require(anonymous.get('/'))
    if 'max-age=' not in response.headers.get('strict-transport-security',''):raise AssertionError('HSTS missing')
    if 'frame-ancestors' not in response.headers.get('content-security-policy',''):raise AssertionError('CSP missing')
    history=require(accounts['manager'].get('/api/history',params={'collection':'findings','record_id':receipt['finding_id']})).json()
    if not history:raise AssertionError('Audit history lost')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--origin',required=True)
    parser.add_argument('--phase',choices=['create','verify'],required=True)
    parser.add_argument('--receipt',default='pilot-acceptance.json')
    args=parser.parse_args()
    # These bindings are supplied by the deployment agent securely, not put into shell history.
    credentials={role:os.environ['KSU_TEST_'+role.upper()+'_PASSWORD'] for role in ('manager','inspector')}
    accounts=clients(args.origin,credentials)
    try:
        if args.phase=='create':
            receipt=create_receipt(args.origin,accounts)
            descriptor=os.open(args.receipt,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(descriptor,'w') as output:json.dump(receipt,output)
        else:
            receipt=json.loads(Path(args.receipt).read_text())
            if receipt['origin']!=args.origin:raise ValueError('Receipt belongs to another origin')
        verify_receipt(receipt,accounts)
        print('PASS:',args.phase,'TLS verified; two accounts; shared synthetic records/photos; private files and audit')
    finally:
        for client in accounts.values():client.close()


if __name__=='__main__':main()
