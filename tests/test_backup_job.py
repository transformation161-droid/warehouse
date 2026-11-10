"""Bounded export and owner-side restore failures do not certify or overwrite backups."""
from contextlib import closing
from collections import namedtuple
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from server import backup_job,backup_export
from server.maintenance import snapshot_database
from server.release_schema import checksum
from server.storage import StorageFull
from tests import test_offline_release as release


class Backup(unittest.TestCase):
    client=release.Release.client
    def setUp(self):release.Release.setUp(self)
    def tearDown(self):release.Release.tearDown(self)
    def test_opt_in_export_over_16m_stream_hash_restore_and_cleanup(self):
        db=self.app.state.db;root=Path(self.temp.name);folder=root/'export'
        with db.connect() as con:
            for n in range(10):con.execute('insert into media values(?,?,?,?,?,?)',(str(n),'s1','inspector',bytes([n])*2097152,b'thumbnail','2026-11-09'))
        before=release.Release.facts(self)
        with patch.dict(os.environ,{'KSU_BACKUP_EXPORT_DIR':str(folder)}):
            response=self.manager.post('/api/admin/backup');self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
        self.assertGreater(len(response.content),16*1024**2)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),response.headers['X-Backup-SHA256'])
        self.assertEqual(list(folder.iterdir()),[])
        downloaded=root/'downloaded.sqlite3';downloaded.write_bytes(response.content)
        self.assertTrue(backup_job.verify(downloaded,root)['restored']);self.assertEqual(before,release.Release.facts(self))
    def test_disk_reserve_and_timeout_refuse_without_changing_source_or_previous_backup(self):
        root=Path(self.temp.name);source=self.app.state.db.path;old=root/'old.sqlite3';snapshot_database(source,old);before=checksum(old)
        Disk=namedtuple('Disk','total used free')
        with patch.dict(os.environ,{'KSU_BACKUP_EXPORT_DIR':str(root/'work')}),patch('server.backup_export.shutil.disk_usage',return_value=Disk(1000,999,1)):
            with self.assertRaises(StorageFull):backup_export.directory(source)
        with patch('server.maintenance.time.monotonic',side_effect=[0,100]):
            with self.assertRaises(TimeoutError):snapshot_database(source,root/'failed.sqlite3',timeout=1)
        self.assertFalse((root/'failed.sqlite3').exists());self.assertEqual(checksum(old),before)
    def test_restore_and_health_fail_on_corruption_or_stale_failed_result(self):
        root=Path(self.temp.name);valid=root/'valid.sqlite3';snapshot_database(self.app.state.db.path,valid)
        result=backup_job.verify(valid,root);result.update(file=valid.name,verifiedAt=datetime.now(timezone.utc).isoformat())
        manifest=root/'protected.verification.json';backup_job.atomic_json(manifest,result,exclusive=True)
        with self.assertRaises(FileExistsError):backup_job.atomic_json(manifest,{'bad':True},exclusive=True)
        self.assertEqual(json.loads(manifest.read_text()),result)
        backup_job.atomic_json(root/'status.json',{'lastAttempt':'success','lastSuccess':result});self.assertTrue(backup_job.health(root))
        backup_job.atomic_json(root/'status.json',{'lastAttempt':'failed','lastSuccess':result});self.assertFalse(backup_job.health(root))
        result['verifiedAt']=(datetime.now(timezone.utc)-timedelta(hours=27)).isoformat()
        backup_job.atomic_json(root/'status.json',{'lastAttempt':'success','lastSuccess':result});self.assertFalse(backup_job.health(root))
        corrupt=root/'corrupt.sqlite3';corrupt.write_bytes(b'CORRUPT SYNTHETIC BACKUP')
        with self.assertRaises((sqlite3.DatabaseError,ValueError)):backup_job.verify(corrupt,root)
        self.assertTrue(valid.exists());self.assertEqual(checksum(valid),result['sha256'])
