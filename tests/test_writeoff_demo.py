"""Synthetic lab package, crypto and no-production guards. No live DB."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools.writeoff_demo import demo,package


class Demo(unittest.TestCase):
    def setUp(self):
        sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        self.identity=patch.object(demo,'APP_SHA',sha);self.identity.start();self.addCleanup(self.identity.stop)

    def test_package_manifest_pinned_source_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'kit';package.build(root);self.assertEqual(package.check(root)['app_sha'],demo.APP_SHA)
            self.assertFalse(any(p.name in ('secrets.json','test-accounts.txt','ca.key') for p in root.rglob('*')))
            (root/'demo.py').write_text('tamper')
            with self.assertRaises(ValueError):package.check(root)

    def test_private_certificates_passwords_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder,patch('tools.writeoff_demo.demo.os.chown',create=True):
            root=Path(folder)/'private';pki=Path(folder)/'tls';root.mkdir();pki.mkdir()
            self.assertTrue(demo.prepare(root,pki,'192.168.1.50',8046,'a'*32)['prepared'])
            accounts=json.loads((root/'secrets.json').read_text())['accounts']
            self.assertEqual({a['username'] for a in accounts.values()},{'demo-manager','demo-inspector','demo-viewer'})
            self.assertTrue(all(len(a['password'])>=24 for a in accounts.values()))
            self.assertEqual((root/'secrets.json').stat().st_mode&0o777,0o600)
            subprocess.run(['openssl','verify','-CAfile',str(pki/'ca.crt'),'-verify_ip','192.168.1.50',str(pki/'server.crt')],check=True,capture_output=True)
            bad=subprocess.run(['openssl','verify','-CAfile',str(pki/'ca.crt'),'-verify_ip','8.8.8.8',str(pki/'server.crt')],capture_output=True)
            self.assertNotEqual(bad.returncode,0)
            old=(root/'secrets.json').read_bytes()
            with self.assertRaises(ValueError):demo.prepare(root,pki,'192.168.1.50',8046,'a'*32)
            self.assertEqual((root/'secrets.json').read_bytes(),old)

    def test_forbid_public_bind_protected_ports_and_external_seed_before_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'private';pki=Path(folder)/'tls';root.mkdir();pki.mkdir()
            for host,port in [('0.0.0.0',8046),('8.8.8.8',8046),('127.0.0.1',8019)]:
                with self.assertRaises(ValueError):demo.prepare(root,pki,host,port,'a'*32)
            self.assertFalse(list(root.iterdir()))
            demo.atomic(root/'secrets.json',{'format':demo.FORMAT,'app_sha':demo.APP_SHA,'synthetic_only':True})
            original=Path.read_text
            def read(path,*a,**kw):return demo.APP_SHA if str(path)=='/app/RELEASE_SHA' else original(path,*a,**kw)
            with patch.object(Path,'read_text',read),patch.dict(os.environ,{'KSU_PR46_TEST_MODE':'synthetic-only','KSU_PG_BOOTSTRAP_DSN':'host=working.invalid port=5432 dbname=production user=postgres password=SYNTHETIC sslmode=verify-full sslrootcert=/tls/ca.crt'}),patch('psycopg.connect',side_effect=AssertionError('must not connect')):
                with self.assertRaises(ValueError):demo.seed(root)


if __name__=='__main__':unittest.main()
