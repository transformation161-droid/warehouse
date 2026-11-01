import io
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from server.database import Database,seed_demo
from server.maintenance import snapshot_database,validate_database
from server.storage import check_storage,StorageFull
from deploy.amvera.budget import estimate
from deploy.amvera.runtime import initialize,validate_origin
from deploy.amvera.release import validate_target
from deploy.amvera.independent_backup import encrypt


class Amvera(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=self.root/'ksu.sqlite3'
        self.password=secrets.token_urlsafe(24)
        seed_demo(Database(self.db),self.password,secrets.token_urlsafe(24))

    def tearDown(self):self.temp.cleanup()

    def test_budget(self):
        self.assertEqual(str(estimate()),'489.60')
        for kwargs in [{'hourly_rate':'0.70'},{'extras':'35'},{'replicas':2},{'hours':721},{'hourly_rate':'NaN'}]:
            with self.assertRaises(ValueError):estimate(**kwargs)

    def test_strict_origin_and_release_target(self):
        validate_origin('https://ksu.example.org')
        for origin in ['http://ksu.example.org','https://host/path','https://user@host','']:
            with self.assertRaises(ValueError):validate_origin(origin)
        validate_target('https://git.amvera.ru/account/project.git')
        for target in ['https://evil.example/a/b','https://user@git.amvera.ru/a/b','https://git.amvera.ru/a/b?token=bad','https://git.amvera.ru/a/../../b']:
            with self.assertRaises(ValueError):validate_target(target)

    def test_no_silent_reset_and_explicit_bootstrap(self):
        fresh=self.root/'new'/'ksu.sqlite3';fresh.parent.mkdir()
        with self.assertRaises(ValueError):initialize(fresh,False)
        initialize(fresh,True);validate_database(fresh)
        contents=(fresh.parent/'demo-accounts.txt').read_bytes()
        initialize(fresh,True)
        self.assertEqual((fresh.parent/'demo-accounts.txt').read_bytes(),contents)
        fresh.unlink()
        with self.assertRaises(ValueError):initialize(fresh,True)

    def test_seed_failure_does_not_leave_partial_accounts(self):
        fresh=self.root/'atomic.sqlite3';db=Database(fresh)
        with patch('server.database.password_hash',side_effect=[secrets.token_urlsafe(30),ValueError('Synthetic interruption')]):
            with self.assertRaises(ValueError):seed_demo(db,secrets.token_urlsafe(24),secrets.token_urlsafe(24))
        with db.connect() as con:
            self.assertEqual(con.execute('select count(*) from users').fetchone()[0],0)

    def test_disk_guard_and_sqlite_size_limit(self):
        with patch.dict(os.environ,{'KSU_DATA_MAX_BYTES':str(self.db.stat().st_size+1)}):
            with self.assertRaises(StorageFull):check_storage(self.db)
            client=TestClient(create_app(self.db,'http://testserver',False))
            self.assertEqual(client.post('/api/login',headers={'Origin':'http://testserver'},json={'username':'manager','password':self.password}).status_code,507)
            client.close()
        with patch.dict(os.environ,{'KSU_DB_MAX_BYTES':str(1024*1024)}):
            db=Database(self.db)
            with self.assertRaises(sqlite3.OperationalError):
                with db.connect() as con:con.execute('insert into media values(?,?,?,?,?,?)',('large','s1','manager',bytes(2*1024*1024),b'', 'demo'))
        with patch.dict(os.environ,{'KSU_DB_MAX_BYTES':'4096'}):
            with self.assertRaises(ValueError):Database(self.db)

    def test_backup_rights_and_encrypted_restore(self):
        origin='http://testserver'
        app=create_app(self.db,origin,False)
        with TestClient(app,headers={'Origin':origin}) as manager:
            self.assertEqual(manager.post('/api/admin/backup').status_code,401)
            response=manager.post('/api/login',json={'username':'manager','password':self.password})
            csrf=response.json()['csrf']
            self.assertEqual(manager.post('/api/admin/backup',headers={'X-CSRF-Token':'bad'}).status_code,403)
            response=manager.post('/api/admin/backup',headers={'X-CSRF-Token':csrf})
            self.assertEqual(response.status_code,200)
            copy=self.root/'copy.sqlite3';copy.write_bytes(response.content)
            validate_database(copy)
            with sqlite3.connect(copy) as con:self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0],0)
            # A manager's backup contains hashes/records; encrypt before any off-host publication.
            key=self.root/'private.pem';cert=self.root/'public.pem';cipher=self.root/'copy.cms';restored=self.root/'restored.sqlite3'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=SyntheticBackupTest','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            encrypt(copy,cert,cipher)
            self.assertNotIn(b'SQLite format',cipher.read_bytes())
            subprocess.run(['openssl','cms','-decrypt','-binary','-inform','DER','-in',str(cipher),'-inkey',str(key),'-recip',str(cert),'-out',str(restored)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            validate_database(restored)
            self.assertEqual(copy.read_bytes(),restored.read_bytes())
            self.assertEqual(cipher.stat().st_mode&0o777,0o600)

    def test_inspector_cannot_export_whole_database(self):
        password=secrets.token_urlsafe(24)
        other=self.root/'other.sqlite3';seed_demo(Database(other),secrets.token_urlsafe(24),password)
        with TestClient(create_app(other,'http://testserver',False),headers={'Origin':'http://testserver'}) as client:
            response=client.post('/api/login',json={'username':'inspector','password':password})
            self.assertEqual(client.post('/api/admin/backup',headers={'X-CSRF-Token':response.json()['csrf']}).status_code,403)

    def test_readonly_missing_snapshot_never_creates_empty_source(self):
        source=self.root/'missing.sqlite3';destination=self.root/'out.sqlite3'
        with self.assertRaises(FileNotFoundError):snapshot_database(source,destination)
        self.assertFalse(source.exists());self.assertFalse(destination.exists())
