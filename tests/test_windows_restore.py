"""Recovery safety and end-to-end runtime checks using synthetic data only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from portable.recovery import RecoveryError, file_hash, prepare, save_summary
from portable.selftest import fixture, run

ROOT = Path(__file__).resolve().parent.parent


class WindowsRecovery(unittest.TestCase):
    def test_runtime_accounts_images_reports_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'result.json'
            run(output, ROOT / 'deploy/windows/assets/DejaVuSans.ttf')
            result = json.loads(output.read_text())
            self.assertTrue(result['original_unchanged'])
            self.assertTrue(result['both_logins'])
            self.assertTrue(result['json_xlsx_pdf'])

    def test_new_folder_every_time_and_sessions_revoked_only_in_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            source = parent / 'исходная копия.sqlite3'
            fixture(source)
            with closing(sqlite3.connect(source)) as con, con:
                con.execute("insert into sessions values('hash','manager','csrf',9999999999)")
            before = file_hash(source)
            first, second = prepare(source, parent), prepare(source, parent)
            self.assertNotEqual(first['folder'], second['folder'])
            with closing(sqlite3.connect(first['database'])) as con:
                self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0], 0)
            with closing(sqlite3.connect(source)) as con, con:
                self.assertEqual(con.execute('select count(*) from sessions').fetchone()[0], 1)
            self.assertEqual(file_hash(source), before)
            report, complete = save_summary(first, {}, {'manual': False}, True)
            self.assertFalse(complete)
            self.assertIn('ПРОВЕРКА НЕ ЗАВЕРШЕНА', report.read_text(encoding='utf-8-sig'))
            with source.open('ab') as handle:
                handle.write(b'changed elsewhere')
            report, complete = save_summary(first, {'manager': True, 'inspector': True}, {'manual': True}, True)
            self.assertFalse(complete)
            self.assertIn('ИЗМЕНИЛАСЬ', report.read_text(encoding='utf-8-sig'))

    def test_rejects_live_or_corrupt_files_without_changing_them(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            source = parent / 'bad.sqlite3'
            source.write_bytes(b'not a database')
            before = file_hash(source)
            with self.assertRaises(sqlite3.DatabaseError):
                prepare(source, parent)
            self.assertEqual(file_hash(source), before)
            Path(str(source) + '-wal').touch()
            with self.assertRaises(RecoveryError):
                prepare(source, parent)

    def test_corrupt_image_rejected_before_server_start(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            source = parent / 'bad-image.sqlite3'
            fixture(source)
            with closing(sqlite3.connect(source)) as con, con:
                con.execute("update media set thumbnail=?", (b'not an image',))
            before = file_hash(source)
            with self.assertRaises(Exception):
                prepare(source, parent)
            self.assertEqual(file_hash(source), before)
