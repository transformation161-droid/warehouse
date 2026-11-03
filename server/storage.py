"""Conservative disk guards for a billed persistent volume; no schema changes."""
import os
import shutil
from pathlib import Path


class StorageFull(Exception):
    pass


def directory_bytes(path):
    total=0
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    raise StorageFull('Unexpected symbolic link in the data directory')
                if entry.is_dir(follow_symlinks=False):total+=directory_bytes(entry.path)
                elif entry.is_file(follow_symlinks=False):total+=entry.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                # SQLite may remove WAL/SHM files, and import workers may remove staging
                # files, between scandir and stat. A vanished entry occupies no space.
                # Other IO errors and unexpected symlinks must still fail closed.
                continue
    return total


def check_storage(database, reserve=8*1024*1024):
    limit=int(os.getenv('KSU_DATA_MAX_BYTES','0'))
    if not limit:return
    directory=Path(database).parent
    if directory_bytes(directory)+reserve > limit:
        raise StorageFull('Лимит данных пилота достигнут. Нужна выгрузка копии и проверка объёма администратором.')
    if shutil.disk_usage(directory).free < 256*1024*1024:
        raise StorageFull('На постоянном диске недостаточно свободного места.')
