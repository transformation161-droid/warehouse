"""Restricted release activation; uses existing users and never initializes business data."""
from dataclasses import dataclass
import hashlib
import os
import re
from fastapi import Request
from fastapi.responses import FileResponse,Response
from .database import ROOT
from .service import fail

ASSETS=('index.html','app.js','transport.js','store.js','prototype.css','manifest.webmanifest','icon.svg')


@dataclass(frozen=True)
class Policy:
    mode:str
    deployment_id:str
    users:frozenset

    @classmethod
    def environment(cls):
        if os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off') not in ('off','on'):raise ValueError('Invalid write-off analytics mode')
        if os.getenv('KSU_VIEWER_ACCESS','enabled') not in ('enabled','off'):raise ValueError('Invalid viewer access mode')
        mode=os.getenv('KSU_OFFLINE_MODE','off')
        if mode not in ('off','staging','pilot'):raise ValueError('Invalid offline mode')
        identity=os.getenv('KSU_OFFLINE_DEPLOYMENT_ID','')
        if not re.fullmatch('[a-f0-9]{32}',identity):raise ValueError('Stable offline deployment ID required')
        users=frozenset(filter(None,os.getenv('KSU_OFFLINE_USER_IDS','').split(',')))
        if any(not re.fullmatch('[a-zA-Z0-9_-]{1,80}',value) for value in users):raise ValueError('Use existing user IDs, not logins or roles')
        if mode!='off' and not users:raise ValueError('Explicit inspector cohort required')
        if os.getenv('KSU_OFFLINE_VISIT_LAB_ID') or os.getenv('KSU_BOOTSTRAP_DEMO') not in (None,'','0'):
            raise ValueError('Lab/demo bootstrap is forbidden in a release')
        return cls(mode,identity,users)

    def enabled(self,actor):return self.mode!='off' and actor['role']=='inspector' and actor['id'] in self.users

    def authorize(self,actor):
        if not self.enabled(actor):fail('Новые офлайн-операции отключены для этой учётной записи. Локальная копия сохранена.',403)

    def plan_permissions(self,actor,batch):
        if actor['role']=='inspector' and any(c.collection=='visits' and (c.expectedVersion==0 or c.data.get('status')!='completed') for c in batch.changes):
            fail('Первый релиз сохраняет прежние права: инспектор завершает назначенный выезд, план изменяет руководитель.',403)

    def routes(self,path,method='GET'):
        if path.startswith(('/api/decision-imports','/api/decisions','/api/decision-documents','/api/writeoff-analytics')):
            if os.getenv('KSU_WRITEOFF_ANALYTICS_ENABLED','off')!='on':
                fail('Раздел аналитики списаний пока не включён',404)
            ident=r'[a-f0-9]{32}';document=r'[a-f0-9]{64}'
            reads=(r'/api/decision-imports(?:/'+ident+r'(?:/(?:comparison|version-inputs|errors\.csv))?)?',
                   r'/api/decisions(?:/(?:'+ident+r'|totals|export\.csv))?',
                   r'/api/decision-documents(?:/'+document+r'/versions)?',
                   r'/api/writeoff-analytics(?:/import-options|/export/(?:rows|summary)\.(?:csv|xlsx))?')
            writes=(r'/api/decision-imports(?:/'+ident+r'/(?:preview|commit|version-preview|version-confirm))?',
                    r'/api/writeoff-analytics/documents/'+document+r'/inventory-date')
            allowed=reads if method=='GET' else writes if method=='POST' else (r'/api/decision-imports/'+ident,) if method=='DELETE' else ()
            if not any(re.fullmatch(pattern,path) for pattern in allowed):fail('Метод аналитики не включён',404)


def install(app,auth,policy):
    app.state.offline_release=policy;app.state.offline_deployment_id=policy.deployment_id
    folder=ROOT/'offline'
    revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in (*ASSETS,'sw.js','ksu.css'))).hexdigest()

    @app.get('/api/offline-visit/context')
    def context(request:Request):
        actor=auth(request)
        return {'mode':policy.mode,'enabled':policy.enabled(actor),'deploymentId':policy.deployment_id,'userId':actor['id'],'role':actor['role']}

    def asset(name):
        def serve():return FileResponse(folder/name)
        return serve
    app.add_api_route('/offline-visit/',asset('index.html'),methods=['GET'])
    for name in (*ASSETS,'ksu.css'):app.add_api_route('/offline-visit/'+name,asset(name),methods=['GET'])

    @app.get('/offline-visit/sw.js')
    def worker():
        return Response((folder/'sw.js').read_text().replace('__BUILD__',policy.deployment_id+'-'+revision),media_type='text/javascript',headers={'Service-Worker-Allowed':'/offline-visit/'})
