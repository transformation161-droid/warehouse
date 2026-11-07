"""Standalone import must not change the live SQLite snapshot contract."""
from contextlib import closing
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest

from server.database import Database,seed_demo
from server.maintenance import snapshot_database
from server.pg_admin import migrate_sqlite


class StandaloneSQLite(unittest.TestCase):
    def test_live_snapshot_includes_committed_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'live.sqlite3';target=Path(directory)/'snapshot.sqlite3'
            seed_demo(Database(source),secrets.token_urlsafe(24),secrets.token_urlsafe(24))
            with closing(sqlite3.connect(source)) as live:
                live.execute('pragma journal_mode=wal');live.execute('pragma wal_autocheckpoint=0')
                live.execute("update users set name='DEMO uncheckpointed WAL' where username='manager'");live.commit()
                self.assertGreater(Path(str(source)+'-wal').stat().st_size,0)
                snapshot_database(source,target)
                with closing(sqlite3.connect(target)) as copied:
                    self.assertEqual(copied.execute("select name from users where username='manager'").fetchone()[0],'DEMO uncheckpointed WAL')

    def test_standalone_rejects_sidecars_without_opening_postgres(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'copy.sqlite3';source.write_bytes(b'test')
            for suffix in ('-wal','-shm','-journal'):
                sidecar=Path(str(source)+suffix);sidecar.touch()
                with self.subTest(suffix=suffix),self.assertRaisesRegex(ValueError,'sidecars'):
                    migrate_sqlite(source,'host=127.0.0.1 dbname=unused user=unused',standalone=True)
                sidecar.unlink()
