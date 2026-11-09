"""Existing SQLite from accepted PR #28: additive, repeatable stage-1 upgrade."""
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

from server.database import ROOT,Database
from server.pg_admin import fingerprints,V2_TABLES

BASE='95952d36dc2098e0f033c8a7abe08551387ae671'


class ExistingSQLite(unittest.TestCase):
    def test_actual_accepted_code_fixture_preserved_and_upgrade_repeatable(self):
        with tempfile.TemporaryDirectory(prefix='ksu-stage1-upgrade-') as directory:
            root=Path(directory);old=root/'old';old.mkdir()
            archive=subprocess.run(['git','archive',BASE],cwd=ROOT,check=True,capture_output=True).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(old,filter='data')
            fixture=r'''
from pathlib import Path
from tests.test_integration import Integration
from server.maintenance import snapshot_database
case=Integration();case.setUp()
try:
 assert case.change({'siteId':'s1','date':'2026-11-09','goal':'ДЕМО · Прежний выезд'}).status_code==200
 job=case.upload();case.preview(job);assert case.commit(job)['status']=='done'
 snapshot_database(case.app.state.db.path,Path('../previous.sqlite3'))
finally:case.tearDown()
'''
            process=subprocess.run([sys.executable,'-c',fixture],cwd=old,capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr[-2000:])
            source=root/'previous.sqlite3'
            with closing(sqlite3.connect(source)) as con:before=fingerprints(con,tables=V2_TABLES)
            from server.release_schema import prepare_sqlite
            prepare_sqlite(source,root/'upgraded',closed_backup=True)
            target=root/'upgraded/ksu.sqlite3';Database(target);Database(target)
            with closing(sqlite3.connect(target)) as con:
                self.assertEqual(fingerprints(con,tables=V2_TABLES),before)
                self.assertEqual(con.execute('select count(*) from operation_receipts').fetchone()[0],0)
                self.assertEqual(con.execute('pragma integrity_check').fetchone()[0],'ok')
                self.assertFalse(con.execute('pragma foreign_key_check').fetchall())
