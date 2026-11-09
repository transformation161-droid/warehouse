"""Explicitly enabled synthetic stage-2 shell; no change to the existing UI or API ACL."""
import hashlib
import re
from fastapi import Request
from fastapi.responses import FileResponse,Response
from .database import ROOT

ASSETS=('index.html','app.js','transport.js','store.js','prototype.css','manifest.webmanifest','icon.svg')


def install(app,auth,lab_id):
    if not re.fullmatch('[a-f0-9]{32}',lab_id):raise ValueError('Synthetic lab identity required')
    app.state.offline_lab_id=lab_id
    folder=ROOT/'experiments/offline_visit'
    revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in (*ASSETS,'sw.js'))+(ROOT/'styles.css').read_bytes()).hexdigest()

    @app.get('/api/offline-visit/context')
    def context(request:Request):
        user=auth(request)
        return {'synthetic':True,'deploymentId':lab_id,'stage':2,'userId':user['id'],'role':user['role']}

    def asset(name):
        def serve():return FileResponse(ROOT/'styles.css' if name=='ksu.css' else folder/name)
        return serve
    app.add_api_route('/offline-visit/',asset('index.html'),methods=['GET'])
    for name in (*ASSETS,'ksu.css'):app.add_api_route('/offline-visit/'+name,asset(name),methods=['GET'])

    @app.get('/offline-visit/sw.js')
    def worker():
        return Response((folder/'sw.js').read_text().replace('__BUILD__',revision),media_type='text/javascript',
                        headers={'Service-Worker-Allowed':'/offline-visit/'})
