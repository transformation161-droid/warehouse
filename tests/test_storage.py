"""Disk admission must tolerate transient SQLite files without hiding other failures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.storage import directory_bytes, StorageFull


class Storage(unittest.TestCase):
    def test_sqlite_file_disappearing_during_scan_is_ignored(self):
        entry=MagicMock()
        entry.is_symlink.return_value=False
        entry.is_dir.return_value=False
        entry.is_file.return_value=True
        entry.stat.side_effect=FileNotFoundError('synthetic.sqlite3-shm')
        scan=MagicMock()
        scan.__enter__.return_value=[entry]
        with patch('server.storage.os.scandir',return_value=scan):
            self.assertEqual(directory_bytes('/synthetic'),0)
        entry.stat.side_effect=PermissionError('synthetic access failure')
        with patch('server.storage.os.scandir',return_value=scan):
            with self.assertRaises(PermissionError):directory_bytes('/synthetic')

    def test_actual_files_counted_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory(prefix='ksu-storage-') as folder:
            root=Path(folder)
            (root/'DEMO').mkdir()
            (root/'DEMO'/'data').write_bytes(b'12345')
            self.assertEqual(directory_bytes(root),5)
            (root/'link').symlink_to(root/'DEMO'/'data')
            with self.assertRaises(StorageFull):directory_bytes(root)
