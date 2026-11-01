import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from deploy.render_config import render
from deploy.rotate_backups import rotate
from server.app import create_app
from server.database import Database, seed_demo
from server.maintenance import snapshot_database, validate_database
import secrets


class Deployment(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.db=self.folder/'data'/'pilot.sqlite3'
        seed_demo(Database(self.db),secrets.token_urlsafe(24),secrets.token_urlsafe(24))

    def tearDown(self):self.temp.cleanup()

    def test_production_settings_and_injection_rejection(self):
        configuration=render('ksu.192-0-2-1.sslip.io')
        self.assertIn('KSU_SECURE_COOKIES=1',configuration['ksu.env'])
        self.assertIn('KSU_REQUIRE_INITIALIZED=1',configuration['ksu.env'])
        self.assertIn('KSU_TRUST_LOCAL_PROXY=1',configuration['ksu.env'])
        for invalid in ['https://example.org','example.org:443','example.org\nKSU_SECURE_COOKIES=0','*.example.org','example.org/../../data','127.0.0.1','-bad.example.org']:
            with self.assertRaises(ValueError):render(invalid)

    def test_missing_or_empty_production_database_fails_closed(self):
        with patch.dict(os.environ,{'KSU_REQUIRE_INITIALIZED':'1'}):
            missing=self.folder/'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):create_app(missing)
            self.assertFalse(missing.exists())
            empty=self.folder/'empty.sqlite3';Database(empty)
            with self.assertRaises(ValueError):create_app(empty)
            create_app(self.db)

    def test_invalid_backup_does_not_create_source_or_publish_target(self):
        destination=self.folder/'backup.sqlite3'
        source=self.folder/'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):snapshot_database(source,destination)
        self.assertFalse(source.exists());self.assertFalse(destination.exists())
        source.write_bytes(b'not-a-database')
        with self.assertRaises(sqlite3.DatabaseError):snapshot_database(source,destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.folder.glob('*.partial')))

    def test_no_overwrite_and_private_snapshot_modes(self):
        destination=self.folder/'backup.sqlite3'
        snapshot_database(self.db,destination);validate_database(destination)
        self.assertEqual(destination.stat().st_mode & 0o777,0o600)
        original=destination.read_bytes()
        with self.assertRaises(FileExistsError):snapshot_database(self.db,destination)
        self.assertEqual(destination.read_bytes(),original)
        symlink=self.folder/'symlink.sqlite3';symlink.symlink_to(destination)
        with self.assertRaises(FileExistsError):snapshot_database(self.db,symlink)

    def test_rotation_only_after_success_and_preserves_other_backups(self):
        directory=self.folder/'backups';directory.mkdir()
        manual=directory/'manual.sqlite3';manual.write_bytes(b'operator-owned')
        for _ in range(4):rotate(self.db,directory,2)
        snapshots=list(directory.glob('daily-*.sqlite3'))
        self.assertEqual(len(snapshots),2)
        for snapshot in snapshots:validate_database(snapshot)
        with self.assertRaises(FileNotFoundError):rotate(self.folder/'notfound',directory,2)
        self.assertEqual(set(directory.glob('daily-*.sqlite3')),set(snapshots))
        self.assertEqual(manual.read_bytes(),b'operator-owned')
