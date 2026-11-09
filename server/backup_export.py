"""Opt-in large SQLite export. One caller gate lives in app; work is on persistent disk."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
from .storage import directory_bytes,StorageFull

MAXIMUM=256*1024*1024


def directory(source):
    source=Path(source).resolve();root=Path(os.getenv('KSU_BACKUP_VOLUME_ROOT',str(source.parent))).resolve()
    folder=Path(os.environ['KSU_BACKUP_EXPORT_DIR'])
    if folder.is_symlink() or not folder.resolve().is_relative_to(root) or folder.resolve()==root:
        raise ValueError('A dedicated export directory on the data volume is required')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as con:
        size=con.execute('pragma page_count').fetchone()[0]*con.execute('pragma page_size').fetchone()[0]
    if size>MAXIMUM:raise StorageFull('SQLite превышает установленный предел; лимит автоматически не увеличивается')
    reserve=2*size+128*1024*1024
    cap=int(os.getenv('KSU_DATA_MAX_BYTES','0'))
    if shutil.disk_usage(root).free<reserve or cap and directory_bytes(root)+reserve>cap:
        raise StorageFull('Недостаточно резерва для резервной копии; существующие файлы не удаляются')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    return folder
