"""Read-only synthetic server boundary and actual TLS trust (no verification bypass)."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import build_opener,ProxyHandler,HTTPSHandler,Request
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]

@contextmanager
def server(cert=None,key=None):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    args=[sys.executable,str(ROOT/'experiments/offline_mvp/server.py'),'--port',str(port)]
    if cert:args+=['--cert',str(cert),'--key',str(key)]
    process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    context=ssl.create_default_context(cafile=str(cert)) if cert else None
    opener=build_opener(ProxyHandler({}),*([HTTPSHandler(context=context)] if cert else []))
    origin=('https' if cert else 'http')+f'://localhost:{port}'
    try:
        for _ in range(100):
            try:
                opener.open(origin+'/build.json',timeout=.5).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('Server not started')
        yield opener,origin
    finally:process.terminate();process.wait(timeout=10)


class Stage0Server(unittest.TestCase):
    def test_no_application_write_routes_or_arbitrary_files(self):
        with server() as (opener,origin):
            for path,code in (('/api/state',404),('/server/database.py',404),('/key.pem',404),('/../../README.md',404)):
                with self.subTest(path=path),self.assertRaises(HTTPError) as error:opener.open(origin+path)
                self.assertEqual(error.exception.code,code)
            with self.assertRaises(HTTPError) as error:opener.open(Request(origin+'/api/changes',data=b'{}',method='POST'))
            self.assertEqual(error.exception.code,405)
            with self.assertRaises(HTTPError) as error:opener.open(Request(origin+'/',headers={'Host':'evil.example'}))
            self.assertEqual(error.exception.code,403)
            with self.assertRaises(HTTPError) as error:opener.open(Request(origin+'/',headers={'Cookie':'ksu_session=SYNTHETIC'}))
            self.assertEqual(error.exception.code,400)
            with opener.open(origin+'/demo-package.json') as response:package=json.load(response)
            self.assertTrue(package['demo']);self.assertEqual(package['expiresAt']-package['issuedAt'],86400000)
            self.assertEqual(hashlib.sha256(package['templateJson'].encode()).hexdigest(),package['templateSha256'])
            with opener.open(origin+package['photo']['url']) as response:self.assertEqual(hashlib.sha256(response.read()).hexdigest(),package['photo']['sha256'])

    def test_real_trusted_https(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=KSU Stage0 TEST',
                '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            with server(cert,key) as (opener,origin):
                with opener.open(origin+'/') as response:self.assertEqual(response.status,200)
                # Default roots do not trust this ephemeral CA; explicit trust above is necessary.
                untrusted=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context()))
                with self.assertRaises(OSError):untrusted.open(origin+'/',timeout=2)

    def test_untrusted_lan_http_is_refused(self):
        result=subprocess.run([sys.executable,str(ROOT/'experiments/offline_mvp/server.py'),'--host','0.0.0.0'],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0);self.assertIn('requires trusted HTTPS',result.stderr)
