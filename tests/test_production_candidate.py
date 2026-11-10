"""Operational release regression; synthetic data only, no browser acceptance replay."""
from collections import namedtuple
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from deploy.release_candidate import bundle
from server import local_backup
from server.backup_job import verify
from server.database import Database,seed_demo
from server.pg_admin import fingerprints
from server.release_schema import checksum
from server.release_backup import snapshot_frozen
from server.storage import StorageFull,check_storage

NOW=datetime(2026,11,10,12,tzinfo=timezone.utc)


class LocalDailyBackup(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ksu-synthetic-daily-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'live/ksu.sqlite3';self.source.parent.mkdir()
        self.db=Database(self.source);seed_demo(self.db,'synthetic owner password','synthetic inspector password')
        with self.db.connect() as con:
            con.execute('insert into media values(?,?,?,?,?,?)',('synthetic-photo','s1','inspector',b'synthetic photo',b'thumbnail','2026-11-10'))
            self.before=fingerprints(con)
        self.gate=threading.BoundedSemaphore(1)
        self.job=local_backup.LocalBackups(self.source,self.root,self.root/'backup-v4',self.gate)

    def test_three_verified_runs_keep_two_and_preserve_source_and_foreign_files(self):
        foreign=self.job.directory/'owner-private.sqlite3';foreign.write_bytes(b'synthetic old owner backup')
        results=[self.job.run_once(NOW+timedelta(days=n)) for n in range(3)]
        self.assertFalse((self.job.directory/results[0]['file']).exists())
        self.assertEqual({p.name for p in self.job.owned()},{r['file'] for r in results[1:]})
        for result in results[1:]:
            target=self.job.directory/result['file']
            self.assertEqual(checksum(target),result['sha256']);self.assertTrue(verify(target,self.root)['restored'])
            with sqlite3.connect(target) as con:self.assertEqual(fingerprints(con),self.before)
        self.assertEqual(foreign.read_bytes(),b'synthetic old owner backup')
        with self.db.connect() as con:self.assertEqual(fingerprints(con),self.before)
        self.assertTrue(self.job.summary(NOW+timedelta(days=2))['healthy'])
        self.assertFalse(self.job.summary(NOW+timedelta(days=3,hours=3))['healthy'])

    def test_failed_restore_preserves_previous_success_and_never_certifies_new_file(self):
        first=self.job.run_once(NOW);old=self.job.directory/first['file']
        with patch('server.local_backup.verify',side_effect=ValueError('synthetic restore failure')):
            with self.assertRaises(ValueError):self.job.run_once(NOW+timedelta(days=1))
        self.assertEqual(self.job.state()['lastSuccess'],first);self.assertEqual(checksum(old),first['sha256'])
        self.assertEqual(len(self.job.owned()),1);self.assertFalse(self.job.summary(NOW+timedelta(days=1))['healthy'])
        self.assertFalse(list(self.job.directory.glob('.local-work-*')))
        self.assertFalse(self.job.tick(NOW+timedelta(days=1,minutes=14)))
        self.assertTrue(self.job.tick(NOW+timedelta(days=1,minutes=16)))

    def test_disk_and_volume_cap_fail_without_pruning(self):
        first=self.job.run_once(NOW)
        Disk=namedtuple('Disk','total used free')
        with patch('server.local_backup.shutil.disk_usage',return_value=Disk(1000,999,1)):
            with self.assertRaises(StorageFull):self.job.run_once(NOW+timedelta(days=1))
        with patch.dict(os.environ,{'KSU_DATA_MAX_BYTES':'100'}):
            with self.assertRaises(StorageFull):self.job.run_once(NOW+timedelta(days=1))
        self.assertEqual(self.job.state()['lastSuccess'],first);self.assertTrue((self.job.directory/first['file']).is_file())
        # Business writes count backups/legacy data outside the new DB subdirectory.
        (self.root/'legacy-data').write_bytes(b'x'*1048576)
        with patch.dict(os.environ,{'KSU_DATA_MAX_BYTES':'1048576','KSU_BACKUP_VOLUME_ROOT':str(self.root)}):
            with self.assertRaises(StorageFull):check_storage(self.source,reserve=1)
            with self.assertRaises(StorageFull):check_storage(self.root.parent/'elsewhere.sqlite3',reserve=1)

    def test_catchup_after_restart_and_shared_export_gate(self):
        self.gate.acquire()
        try:self.assertIsNone(self.job.run_once(NOW))
        finally:self.gate.release()
        self.assertTrue(self.job.tick(NOW));self.assertFalse(self.job.tick(NOW+timedelta(hours=23)))
        restarted=local_backup.LocalBackups(self.source,self.root,self.job.directory)
        self.assertFalse(restarted.tick(NOW+timedelta(hours=23)))
        self.assertTrue(restarted.tick(NOW+timedelta(days=2)))
        self.assertEqual(len(restarted.owned()),2)

    def test_opt_in_and_foreign_namespace_symlinks_refused(self):
        with patch.dict(os.environ,{'KSU_LOCAL_BACKUP_MODE':'off'}):self.assertIsNone(local_backup.LocalBackups.environment(self.db,self.root,self.gate))
        for mode in ('invalid','daily '):
            with patch.dict(os.environ,{'KSU_LOCAL_BACKUP_MODE':mode}),self.assertRaises(ValueError):local_backup.LocalBackups.environment(self.db,self.root,self.gate)
        with self.assertRaises(ValueError):local_backup.LocalBackups(self.source,self.root,self.root)
        with self.assertRaises(ValueError):local_backup.LocalBackups(self.source,self.root,self.root.parent/'foreign')
        foreign=self.root/'foreign';foreign.mkdir();(foreign/'keep').write_text('synthetic')
        with self.assertRaises(ValueError):local_backup.LocalBackups(self.source,self.root,foreign)
        link=self.root/'link';link.symlink_to(self.job.directory,target_is_directory=True)
        with self.assertRaises(ValueError):local_backup.LocalBackups(self.source,self.root,link/'nested')
        source_link=self.root/'db-link';source_link.symlink_to(self.source)
        with self.assertRaises(ValueError):local_backup.LocalBackups(source_link,self.root,self.root/'another')
        (self.job.directory/'namespace.json').write_text('{}')
        with self.assertRaises(ValueError):local_backup.LocalBackups(self.source,self.root,self.job.directory)

    def test_frozen_backup_selects_current_subdirectory_instead_of_old_database(self):
        # After cutover the old root DB intentionally remains. Never back it up by mistake.
        frozen=self.root/'frozen';(frozen/'live-v4').mkdir(parents=True)
        from server.maintenance import snapshot_database
        snapshot_database(self.source,frozen/'live-v4/ksu.sqlite3')
        (frozen/'ksu.sqlite3').write_bytes(b'old database sentinel, not current')
        original=checksum(frozen/'live-v4/ksu.sqlite3')
        report=snapshot_frozen(frozen,self.root/'selected.sqlite3',confirmed=True,database_relative='live-v4/ksu.sqlite3')
        self.assertEqual(report['database_relative'],'live-v4/ksu.sqlite3')
        self.assertEqual(checksum(frozen/'live-v4/ksu.sqlite3'),original)
        with sqlite3.connect(self.root/'selected.sqlite3') as con:self.assertEqual(fingerprints(con),self.before)
        for path in ('../live/ksu.sqlite3','/live/ksu.sqlite3','live-v4/../ksu.sqlite3','live-v4\\ksu.sqlite3'):
            with self.assertRaises(ValueError):snapshot_frozen(frozen,self.root/'forbidden.sqlite3',confirmed=True,database_relative=path)


class Delivery(unittest.TestCase):
    def test_committed_source_bundle_identity_no_private_files_no_overwrite_and_tamper_detection(self):
        with tempfile.TemporaryDirectory(prefix='ksu-synthetic-bundle-') as directory:
            root=Path(directory)/'delivery'
            result=bundle.export(bundle.ACCEPTED,root)
            self.assertFalse(result['deployed']);self.assertEqual(bundle.verify(root),bundle.ACCEPTED)
            self.assertIn('ARG KSU_BUILD_SHA='+bundle.ACCEPTED,(root/'Dockerfile.offline-release').read_text())
            for path in ('server/seed.json','.git','tests','var','Dockerfile','deploy/amvera'):
                self.assertFalse((root/path).exists(),path)
            with self.assertRaises(FileExistsError):bundle.export(bundle.ACCEPTED,root)
            (root/'requirements.txt').write_text('tampered synthetic file')
            with self.assertRaises(ValueError):bundle.verify(root)
            (root/'corporate.sqlite3').write_bytes(b'synthetic forbidden file')
            with self.assertRaises(ValueError):bundle.verify(root)

    def test_accepted_assets_and_acl_implementations_unchanged(self):
        # Core ACL/offline protocol remains unchanged. The new checklist editor and
        # production seed guard are intentionally tested by focused new checks.
        paths=['offline','assets','server/service.py',
            'server/viewer.py','server/viewer_schema.py','server/offline_release.py','server/reports.py']
        tracked=subprocess.run(['git','ls-tree','-r','--name-only',bundle.ACCEPTED,*paths],check=True,capture_output=True,text=True).stdout.splitlines()
        for name in tracked:
            expected=subprocess.run(['git','show',bundle.ACCEPTED+':'+name],check=True,capture_output=True).stdout
            self.assertEqual(Path(name).read_bytes(),expected,name)

    def test_no_automatic_delivery_or_github_corporate_backup(self):
        for name in ('ksu-amvera-backup.yml','ksu-amvera-release.yml'):
            text=Path('.github/workflows',name).read_text()
            self.assertIn('workflow_dispatch:',text)
            for forbidden in ('schedule:','workflow_run:','push:','secrets.','upload-artifact','deploy.amvera'):
                self.assertNotIn(forbidden,text)
        # Every primary dependency is already in the accepted commit, no patch replay.
        snapshot=json.loads(Path('docs/release/OPEN_PR_SNAPSHOT.json').read_text())
        for pr in snapshot['pull_requests']:
            result=subprocess.run(['git','merge-base','--is-ancestor',pr['head_sha'],bundle.ACCEPTED])
            self.assertEqual(result.returncode,1 if pr['number'] in (5,8,32) else 0,pr['number'])
        # The side branding PR's official asset was integrated by copy in PR34.
        brand=subprocess.run(['git','show','df35747a4a8c3da27718343574fe9bbeb12ddb12:assets/instrakt-logo.svg'],check=True,capture_output=True).stdout
        self.assertEqual(Path('assets/instrakt-logo.svg').read_bytes(),brand)
