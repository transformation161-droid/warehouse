"""Actual Caddy TLS + persistent SQLite acceptance. No insecure TLS flags, synthetic data only."""
import base64
import io
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image
from server.database import Database, ROOT, seed_demo
from server.maintenance import snapshot_database


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.getenv('CADDY_BIN'), 'Set CADDY_BIN to run the real TLS acceptance test')
class HTTPSPilot(unittest.TestCase):
    def test_two_accounts_tls_restart_and_restore(self):
        with tempfile.TemporaryDirectory(prefix='ksu-tls-') as directory, ExitStack() as stack:
            folder = Path(directory)
            db_path = folder/'data'/'ksu.sqlite3'
            manager_password, inspector_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            seed_demo(Database(db_path), manager_password, inspector_password)
            api_port, tls_port = unused_port(), unused_port()
            origin = f'https://localhost:{tls_port}'
            environment = {**os.environ, 'KSU_DB': str(db_path), 'KSU_ORIGIN': origin,
                           'KSU_SECURE_COOKIES': '1', 'KSU_REQUIRE_INITIALIZED': '1',
                           'KSU_TRUST_LOCAL_PROXY': '1'}
            api_log = stack.enter_context((folder/'api.log').open('w'))
            caddy_log = stack.enter_context((folder/'caddy.log').open('w'))
            processes = []

            def start_api(database):
                process = subprocess.Popen([sys.executable, '-m', 'server', 'run', '--port', str(api_port)],
                                           cwd=ROOT, env={**environment, 'KSU_DB': str(database)},
                                           stdout=api_log, stderr=api_log)
                processes.append(process)
                return process

            def stop(process):
                process.terminate()
                process.wait(timeout=15)

            server = start_api(db_path)
            caddyfile = folder/'Caddyfile'
            caddyfile.write_text('''{
                admin off
                auto_https disable_redirects
                skip_install_trust
                storage file_system {
                    root %s
                }
            }
            https://localhost:%d {
                bind 127.0.0.1
                tls internal
                request_body {
                    max_size 6000000
                }
                reverse_proxy 127.0.0.1:%d
            }
            ''' % (folder/'caddy-state', tls_port, api_port))
            proxy = subprocess.Popen([os.environ['CADDY_BIN'], 'run', '--config', str(caddyfile), '--adapter', 'caddyfile'],
                                     stdout=caddy_log, stderr=caddy_log,
                                     env={**os.environ, 'XDG_DATA_HOME': str(folder/'xdg'), 'XDG_CONFIG_HOME': str(folder/'config')})
            processes.append(proxy)
            try:
                ca = folder/'caddy-state/pki/authorities/local/root.crt'
                for _ in range(150):
                    if proxy.poll() is not None: raise AssertionError('Caddy exited: '+(folder/'caddy.log').read_text()[-2000:])
                    if ca.exists(): break
                    time.sleep(.1)
                self.assertTrue(ca.exists(), 'Caddy root CA was not generated')
                context = ssl.create_default_context(cafile=str(ca))
                # A locally trusted CA, with certificate and hostname checks fully enabled.
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                manager = stack.enter_context(httpx.Client(base_url=origin, verify=context, trust_env=False))
                inspector = stack.enter_context(httpx.Client(base_url=origin, verify=context, trust_env=False))
                anonymous = stack.enter_context(httpx.Client(base_url=origin, verify=context, trust_env=False))

                def ready():
                    for _ in range(150):
                        try:
                            r = anonymous.get('/api/health', timeout=1)
                            if r.status_code == 200: return
                        except httpx.TransportError: pass
                        time.sleep(.1)
                    raise AssertionError('TLS health check failed')

                ready()
                for client, username, password in [(manager,'manager',manager_password),(inspector,'inspector',inspector_password)]:
                    response = client.post('/api/login', headers={'Origin':origin}, json={'username':username,'password':password})
                    self.assertEqual(response.status_code, 200)
                    for flag in ('Secure','HttpOnly','SameSite=strict'):self.assertIn(flag,response.headers['set-cookie'])
                    client.headers.update({'Origin':origin,'X-CSRF-Token':response.json()['csrf']})
                for path in ['/api/state','/api/history','/api/reports/xlsx','/api/media/private']:
                    self.assertEqual(anonymous.get(path).status_code,401)
                for path in ['/.env','/var/ksu.sqlite3','/backups/a.sqlite3','/server/app.py','/demo-accounts.txt']:
                    self.assertEqual(anonymous.get(path).status_code,404)
                self.assertIn('max-age=',manager.get('/').headers['strict-transport-security'])
                self.assertEqual(anonymous.post('/api/logout',headers={'Origin':'https://evil.example'}).status_code,403)
                def change(client, collection, ident, data):
                    response = client.post('/api/changes',json={'mutationId':uuid4().hex,'changes':[
                        {'collection':collection,'id':ident,'expectedVersion':0,'data':data}]})
                    self.assertEqual(response.status_code,200,response.text)
                change(manager,'visits','pilotvisit',{'siteId':'s1','date':'2026-11-01','assignee':'ДЕМО','assignedTo':'inspector','goal':'ДЕМО · Интернет-пилот'})
                self.assertEqual(inspector.get('/api/state').json()['visits'][0]['id'],'pilotvisit')
                image = Image.new('RGB',(16,16),'blue');output=io.BytesIO();image.save(output,format='PNG')
                response = inspector.post('/api/media',json={'siteId':'s1','content':base64.b64encode(output.getvalue()).decode()})
                self.assertEqual(response.status_code,200)
                photo_url = response.json()['url']
                change(inspector,'findings','pilotfinding',{'siteId':'s1','title':'ДЕМО · Проверка сохранности','photo':photo_url})
                photo_before = manager.get(photo_url).content
                self.assertEqual(anonymous.get(photo_url).status_code,401)
                stop(server);server=start_api(db_path);ready()
                self.assertEqual(manager.get('/api/state').json()['findings'][0]['id'],'pilotfinding')
                self.assertEqual(manager.get(photo_url).content,photo_before)
                # Simulate a clean release/process replacement, preserving only the mounted data directory.
                stop(server);server=start_api(db_path);ready()
                self.assertEqual(inspector.get('/api/state').json()['visits'][0]['id'],'pilotvisit')
                backup = folder/'off-host-copy'/'backup.sqlite3'
                snapshot_database(db_path,backup)
                restored = folder/'restored-volume'/'restored.sqlite3'
                snapshot_database(backup,restored)
                stop(server);server=start_api(restored);ready()
                self.assertEqual(manager.get('/api/state').status_code,401)
                response=manager.post('/api/login',json={'username':'manager','password':manager_password})
                self.assertEqual(response.status_code,200)
                self.assertEqual(manager.get(photo_url).content,photo_before)
                self.assertEqual(manager.get('/api/state').json()['findings'][0]['id'],'pilotfinding')
                self.assertTrue(manager.get('/api/history?collection=findings').json())
                # Exercise the same probes that will be used against the real public endpoint.
                from deploy.accept_live import clients, create_receipt, verify_receipt
                probes=clients(origin,{'manager':manager_password,'inspector':inspector_password},str(ca))
                try:
                    receipt=create_receipt(origin,probes)
                    verify_receipt(receipt,probes)
                    stop(server);server=start_api(restored);ready()
                    verify_receipt(receipt,probes)
                finally:
                    for client in probes.values():client.close()
                print('PASS: verified TLS, two sessions, private files, two restarts, records/photos/audit, restored backup, revoked sessions')
            finally:
                for process in processes:
                    if process.poll() is None:stop(process)
